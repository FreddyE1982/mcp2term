import asyncio
import json
import queue
import sys
import threading
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client

_DEFAULT_STREAMABLE_HTTP_PATH = "/mcp"


def _normalize_streamable_http_url(url: str) -> str:
    """Ensure the URL points at the Streamable HTTP endpoint."""

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Streamable HTTP URL must include a scheme and host")
    path = parsed.path or ""
    if path.rstrip("/") == "":
        normalized_path = _DEFAULT_STREAMABLE_HTTP_PATH
    else:
        stripped = path.rstrip("/")
        if not stripped.startswith("/"):
            stripped = "/" + stripped
        if stripped.endswith(_DEFAULT_STREAMABLE_HTTP_PATH):
            normalized_path = stripped
        else:
            normalized_path = path
    return urlunparse(parsed._replace(path=normalized_path))


@dataclass(slots=True)
class CommandResponse:
    """Structured response returned from the server's ``run_command`` tool."""

    command: str
    working_directory: str
    return_code: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    duration: float
    timed_out: bool

    @classmethod
    def from_call_tool_result(cls, result: types.CallToolResult) -> "CommandResponse":
        """Create a response object from a tool result payload."""

        payload: dict[str, Any] = {}
        if result.structuredContent:
            payload.update(result.structuredContent)
        else:
            for block in result.content:
                if isinstance(block, types.TextContent):
                    try:
                        data = json.loads(block.text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict):
                        payload.update(data)
        return cls(
            command=str(payload.get("command", "")),
            working_directory=str(payload.get("working_directory", "")),
            return_code=int(payload.get("return_code", 0)),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
            started_at=str(payload.get("started_at", "")),
            finished_at=str(payload.get("finished_at", "")),
            duration=float(payload.get("duration", 0.0)),
            timed_out=bool(payload.get("timed_out", False)),
        )


@dataclass(slots=True)
class LogMessage:
    """Represents a streaming log message from the MCP session."""

    level: str
    text: str


class LogStreamer:
    """Background printer for streaming log messages."""

    def __init__(self) -> None:
        self._queue: "queue.SimpleQueue[LogMessage | None]" = queue.SimpleQueue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="mcp2term-client-logs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._thread:
            return
        self._queue.put(None)
        self._thread.join()
        self._thread = None

    def submit(self, message: LogMessage) -> None:
        if not self._thread or not self._thread.is_alive():
            self.start()
        self._queue.put(message)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            stream = sys.stderr if item.level.upper() in {"ERROR", "WARN", "WARNING"} else sys.stdout
            stream.write(item.text)
            if not item.text.endswith("\n"):
                stream.write("\n")
            stream.flush()


@dataclass(slots=True)
class _Request:
    action: str
    payload: dict[str, Any]
    future: Future[Any]


class RemoteMcpSession:
    """Facade around ``ClientSession`` backed by a dedicated asyncio event loop."""

    def __init__(self, url: str, *, default_timeout: float | None = None) -> None:
        self._raw_url = url
        self._url = _normalize_streamable_http_url(url)
        self._default_timeout = default_timeout
        self._transport_cm: Any = None
        self._transport: Any = None
        self._session: ClientSession | None = None
        self._log_streamer = LogStreamer()
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._loop_lock = threading.Lock()
        self._request_queue: asyncio.Queue[_Request] | None = None
        self._worker_future: Future[Any] | None = None
        self._startup_ready = threading.Event()
        self._startup_error: BaseException | None = None

    @property
    def default_timeout(self) -> float | None:
        return self._default_timeout

    @property
    def endpoint_url(self) -> str:
        """Normalized Streamable HTTP endpoint used for the MCP session."""

        return self._url

    @property
    def raw_url(self) -> str:
        """Original URL provided by the user before normalization."""

        return self._raw_url

    def start(self) -> None:
        if self._started:
            return
        self._ensure_loop_started()
        if self._loop is None:
            raise RuntimeError("Event loop failed to start")
        self._startup_ready.clear()
        self._startup_error = None
        worker = asyncio.run_coroutine_threadsafe(self._session_worker(), self._loop)
        self._worker_future = worker
        if not self._startup_ready.wait(timeout=30.0):
            worker.cancel()
            self._shutdown_loop()
            raise TimeoutError("Remote MCP session did not initialize in time")
        if self._startup_error is not None:
            worker.cancel()
            self._shutdown_loop()
            raise self._startup_error
        self._log_streamer.start()
        self._started = True

    def close(self) -> None:
        if not self._started:
            return
        self._log_streamer.stop()
        try:
            self._submit_request("stop")
            if self._worker_future is not None:
                with suppress(Exception):
                    self._worker_future.result()
        finally:
            self._shutdown_loop()
            self._started = False

    def run_command(
        self,
        command: str,
        *,
        working_directory: str | None,
        environment: dict[str, str] | None = None,
        ephemeral_environment: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResponse:
        env: dict[str, str] = {}
        if environment:
            env.update(environment)
        if ephemeral_environment:
            env.update(ephemeral_environment)
        effective_timeout = timeout if timeout is not None else self._default_timeout
        return self._submit_request(
            "call_tool",
            command=command,
            working_directory=working_directory,
            environment=env,
            timeout=effective_timeout,
        )

    def resolve_working_directory(self, working_directory: str | None = None) -> str:
        response = self.run_command(
            "pwd",
            working_directory=working_directory,
            environment=None,
            ephemeral_environment=None,
        )
        if response.return_code != 0:
            message = response.stderr.strip() or response.stdout.strip() or "unable to resolve directory"
            raise RuntimeError(message)
        return response.working_directory or response.stdout.strip()

    def _ensure_loop_started(self) -> None:
        if self._loop and self._loop_thread and self._loop_thread.is_alive():
            return
        with self._loop_lock:
            if self._loop and self._loop_thread and self._loop_thread.is_alive():
                return
            self._loop_ready.clear()
            thread = threading.Thread(
                target=self._loop_runner,
                name="mcp2term-client-loop",
                daemon=True,
            )
            thread.start()
            self._loop_thread = thread
            if not self._loop_ready.wait(timeout=10.0):
                raise RuntimeError("Event loop failed to start")

    def _shutdown_loop(self) -> None:
        loop = self._loop
        thread = self._loop_thread
        if loop is None or thread is None:
            self._request_queue = None
            self._worker_future = None
            return

        def _stop_loop() -> None:
            loop.stop()

        loop.call_soon_threadsafe(_stop_loop)
        thread.join(timeout=10.0)
        if thread.is_alive():
            raise RuntimeError("Event loop thread did not shut down")
        self._loop = None
        self._loop_thread = None
        self._request_queue = None
        self._worker_future = None

    def _loop_runner(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                gathered = asyncio.gather(*tasks, return_exceptions=True)
                with suppress(Exception):
                    loop.run_until_complete(gathered)
            with suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _session_worker(self) -> None:
        queue_: asyncio.Queue[_Request] = asyncio.Queue()
        self._request_queue = queue_
        try:
            self._transport_cm = streamablehttp_client(self._url)
            self._transport = await self._transport_cm.__aenter__()
            read_stream, write_stream, _ = self._transport
            self._session = ClientSession(read_stream, write_stream, logging_callback=self._handle_log_message)
            await self._session.__aenter__()
            await self._session.initialize()
            self._startup_ready.set()

            while True:
                request = await queue_.get()
                if request.action == "stop":
                    request.future.set_result(None)
                    break
                try:
                    if request.action == "call_tool":
                        result = await self._async_call_tool(
                            request.payload["command"],
                            request.payload.get("working_directory"),
                            request.payload.get("environment", {}),
                            request.payload.get("timeout"),
                        )
                        request.future.set_result(result)
                    else:
                        raise RuntimeError(f"Unknown request action: {request.action}")
                except Exception as exc:
                    request.future.set_exception(exc)
        except Exception as exc:
            self._startup_error = exc
            self._startup_ready.set()
            raise
        finally:
            try:
                if self._session is not None:
                    await self._session.__aexit__(None, None, None)
            finally:
                self._session = None
            try:
                if self._transport_cm is not None:
                    await self._transport_cm.__aexit__(None, None, None)
            finally:
                self._transport_cm = None
                self._transport = None
            self._request_queue = None

    def _submit_request(self, action: str, **payload: Any) -> Any:
        if self._loop is None or self._request_queue is None:
            raise RuntimeError("RemoteMcpSession used before start()")
        future: Future[Any] = Future()
        request = _Request(action=action, payload=payload, future=future)

        def _enqueue() -> None:
            if self._request_queue is None:
                future.set_exception(RuntimeError("RemoteMcpSession is shutting down"))
                return
            self._request_queue.put_nowait(request)

        self._loop.call_soon_threadsafe(_enqueue)
        return future.result()

    async def _async_call_tool(
        self,
        command: str,
        working_directory: str | None,
        environment: dict[str, str],
        timeout: float | None,
    ) -> CommandResponse:
        if self._session is None:
            raise RuntimeError("RemoteMcpSession used before start()")
        arguments: dict[str, Any] = {"command": command}
        if working_directory:
            arguments["working_directory"] = working_directory
        if environment:
            arguments["environment"] = environment
        if timeout is not None:
            arguments["timeout"] = timeout
        result = await self._session.call_tool("run_command", arguments)
        return CommandResponse.from_call_tool_result(result)

    async def _handle_log_message(self, params: types.LoggingMessageNotificationParams) -> None:
        data = params.data
        if not isinstance(data, str):
            data = json.dumps(data, ensure_ascii=False)
        self._log_streamer.submit(LogMessage(level=str(params.level), text=data))
