import asyncio
import json
import logging
import queue
import sys
import threading
import textwrap
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError

from .backpressure import BackpressureMonitor, NoticeWriter

logger = logging.getLogger(__name__)

_DEFAULT_STREAMABLE_HTTP_PATH = "/mcp"
_DIAGNOSTIC_TIMEOUT = 5.0


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


def _default_notice_writer(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")
    sys.stderr.flush()


def _extract_warnings(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Normalize warning data from structured responses."""

    collected: list[str] = []

    def _record(value: Any) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text:
            collected.append(text)

    data = payload.get("warnings")
    if isinstance(data, Mapping):
        for item in data.values():
            _record(item)
    elif isinstance(data, (list, tuple, set)):
        for item in data:
            _record(item)
    elif data is not None:
        _record(data)

    if "warning" in payload:
        _record(payload["warning"])

    ordered: list[str] = []
    seen: set[str] = set()
    for item in collected:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return tuple(ordered)


def _tool_result_to_payload(result: types.CallToolResult) -> dict[str, Any]:
    """Convert a tool result into a dictionary payload."""

    payload: dict[str, Any] = {}
    if result.structuredContent and isinstance(result.structuredContent, Mapping):
        payload.update(result.structuredContent)
    if not payload:
        for block in result.content:
            if isinstance(block, types.TextContent):
                try:
                    data = json.loads(block.text)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, Mapping):
                    payload.update(data)
    return payload


def _iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class CommandResponse:
    """Structured response returned from the server's ``run_command`` tool."""

    command_id: str
    command: str
    working_directory: str
    return_code: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    duration: float
    timed_out: bool
    pty_allocated: bool
    warnings: tuple[str, ...] = tuple()

    @classmethod
    def from_call_tool_result(cls, result: types.CallToolResult) -> "CommandResponse":
        """Create a response object from a tool result payload."""

        payload = _tool_result_to_payload(result)
        return cls(
            command_id=str(payload.get("command_id", "")),
            command=str(payload.get("command", "")),
            working_directory=str(payload.get("working_directory", "")),
            return_code=int(payload.get("return_code", 0)),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
            started_at=str(payload.get("started_at", "")),
            finished_at=str(payload.get("finished_at", "")),
            duration=float(payload.get("duration", 0.0)),
            timed_out=bool(payload.get("timed_out", False)),
            pty_allocated=bool(payload.get("pty_allocated", False)),
            warnings=_extract_warnings(payload),
        )


@dataclass(slots=True)
class CancelCommandResponse:
    """Response returned from the server's ``cancel_command`` tool."""

    command_id: str
    signal: int | None
    signal_name: str | None
    delivered: bool
    warnings: tuple[str, ...] = tuple()

    @classmethod
    def from_call_tool_result(cls, result: types.CallToolResult) -> "CancelCommandResponse":
        payload = _tool_result_to_payload(result)
        signal_value = payload.get("signal")
        return cls(
            command_id=str(payload.get("command_id", "")),
            signal=int(signal_value) if signal_value is not None else None,
            signal_name=str(payload.get("signal_name")) if payload.get("signal_name") is not None else None,
            delivered=bool(payload.get("delivered", False)),
            warnings=_extract_warnings(payload),
        )


@dataclass(slots=True)
class SendInputResponse:
    """Response confirming delivery of stdin data to a running command."""

    command_id: str
    accepted: bool
    eof: bool
    warnings: tuple[str, ...] = tuple()

    @classmethod
    def from_call_tool_result(cls, result: types.CallToolResult) -> "SendInputResponse":
        payload = _tool_result_to_payload(result)
        return cls(
            command_id=str(payload.get("command_id", "")),
            accepted=bool(payload.get("accepted", False)),
            eof=bool(payload.get("eof", False)),
            warnings=_extract_warnings(payload),
        )


@dataclass(slots=True)
class FileOperationLine:
    """Represents an individual line returned from a file operation."""

    number: int
    text: str


@dataclass(slots=True)
class FileOperationResponse:
    """Structured response returned from the server's ``manage_file`` tool."""

    path: str
    operation: str
    success: bool
    changed: bool
    encoding: str
    message: str | None
    content: str | None
    lines: tuple[FileOperationLine, ...]
    line_numbers: tuple[int, ...]
    metadata: Mapping[str, Any] | None = None
    warnings: tuple[str, ...] = tuple()

    @classmethod
    def from_call_tool_result(cls, result: types.CallToolResult) -> "FileOperationResponse":
        payload = _tool_result_to_payload(result)
        lines_payload = payload.get("lines")
        parsed_lines: list[FileOperationLine] = []
        if isinstance(lines_payload, (list, tuple)):
            for entry in lines_payload:
                if not isinstance(entry, Mapping):
                    continue
                number_raw = entry.get("number")
                text_raw = entry.get("text", "")
                try:
                    number = int(number_raw)
                except (TypeError, ValueError):
                    continue
                parsed_lines.append(FileOperationLine(number=number, text=str(text_raw)))
        line_numbers_payload = payload.get("line_numbers")
        parsed_numbers: list[int] = []
        if isinstance(line_numbers_payload, (list, tuple, set)):
            for entry in line_numbers_payload:
                try:
                    parsed_numbers.append(int(entry))
                except (TypeError, ValueError):
                    continue
        metadata_payload = payload.get("metadata")
        parsed_metadata: dict[str, Any] | None = None
        if isinstance(metadata_payload, Mapping):
            parsed_metadata = {str(key): value for key, value in metadata_payload.items()}

        return cls(
            path=str(payload.get("path", "")),
            operation=str(payload.get("operation", "")),
            success=bool(payload.get("success", False)),
            changed=bool(payload.get("changed", False)),
            encoding=str(payload.get("encoding", "")),
            message=str(payload.get("message")) if payload.get("message") is not None else None,
            content=str(payload.get("content")) if payload.get("content") is not None else None,
            lines=tuple(parsed_lines),
            line_numbers=tuple(parsed_numbers),
            metadata=parsed_metadata,
            warnings=_extract_warnings(payload),
        )


@dataclass(slots=True)
class LogMessage:
    """Represents a streaming log message from the MCP session."""

    level: str
    text: str


class LogStreamer:
    """Background printer for streaming log messages."""

    def __init__(
        self,
        *,
        monitor: BackpressureMonitor | None = None,
        buffer_notice_threshold: int = 256,
        notice_writer: NoticeWriter | None = None,
    ) -> None:
        self._queue: "queue.SimpleQueue[LogMessage | None]" = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._notice_writer = notice_writer or _default_notice_writer
        if monitor is None:
            threshold = max(1, buffer_notice_threshold)
            monitor = BackpressureMonitor(
                name="mcp2term-client",
                threshold=threshold,
                recovery_threshold=max(0, threshold // 2),
                notice_writer=self._notice_writer,
                enter_message_factory=lambda count: (
                    f"[mcp2term-client] Buffering {count} output message(s) from the server; display may lag."
                ),
                exit_message_factory=lambda: "[mcp2term-client] Output buffer drained; resuming live streaming.",
            )
        self._monitor = monitor

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
        self._monitor.reset()

    def submit(self, message: LogMessage) -> None:
        if not self._thread or not self._thread.is_alive():
            self.start()
        self._queue.put(message)
        self._monitor.increment()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            stream = sys.stderr if item.level.upper() in {"ERROR", "WARN", "WARNING"} else sys.stdout
            try:
                stream.write(item.text)
                if not item.text.endswith("\n"):
                    stream.write("\n")
                stream.flush()
            finally:
                self._monitor.decrement()


@dataclass(slots=True)
class _Request:
    action: str
    payload: dict[str, Any]
    future: Future[Any]
    track_backpressure: bool


@dataclass(slots=True)
class EndpointProbeResult:
    """Represents the outcome of probing an HTTP endpoint."""

    method: str
    url: str
    status_code: int | None
    success: bool
    detail: str | None

    def summary(self) -> str:
        if self.status_code is not None:
            status_part = f"HTTP {self.status_code}"
        else:
            status_part = "connection failed"
        if self.detail:
            return f"{status_part} – {self.detail}"
        return status_part


class RemoteMcpSessionError(RuntimeError):
    """Raised when the remote MCP session fails to start."""


class RemoteMcpSession:
    """Facade around ``ClientSession`` backed by a dedicated asyncio event loop."""

    def __init__(
        self,
        url: str,
        *,
        default_timeout: float | None = None,
        notice_writer: NoticeWriter | None = None,
        output_buffer_threshold: int = 512,
        input_buffer_threshold: int = 32,
    ) -> None:
        self._raw_url = url
        self._url = _normalize_streamable_http_url(url)
        self._default_timeout = default_timeout
        self._notice_writer: NoticeWriter = notice_writer or _default_notice_writer
        output_threshold = max(1, int(output_buffer_threshold))
        input_threshold = max(1, int(input_buffer_threshold))
        self._output_monitor = BackpressureMonitor(
            name="mcp2term-client output",
            threshold=output_threshold,
            recovery_threshold=max(1, output_threshold // 2),
            notice_writer=self._notice_writer,
            enter_message_factory=lambda count: (
                f"[mcp2term-client] Buffering {count} output message(s) from the server; display may lag."
            ),
            exit_message_factory=lambda: "[mcp2term-client] Output buffer drained; resuming live streaming.",
        )
        self._input_monitor = BackpressureMonitor(
            name="mcp2term-client input",
            threshold=input_threshold,
            recovery_threshold=max(0, input_threshold // 2),
            notice_writer=self._notice_writer,
            enter_message_factory=lambda count: (
                f"[mcp2term-client] Buffering {count} pending request(s) to the server; "
                "commands will run once the connection catches up."
            ),
            exit_message_factory=lambda: (
                "[mcp2term-client] Command submission queue has drained; requests are being delivered immediately."
            ),
        )
        self._transport_cm: Any = None
        self._transport: Any = None
        self._session: ClientSession | None = None
        self._log_streamer = LogStreamer(monitor=self._output_monitor, notice_writer=self._notice_writer)
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._loop_lock = threading.Lock()
        self._request_queue: asyncio.Queue[_Request] | None = None
        self._worker_future: Future[Any] | None = None
        self._startup_ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._active_tasks: set[asyncio.Task[Any]] = set()

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

    @staticmethod
    def generate_command_id() -> str:
        """Create a unique command identifier suitable for cancellation requests."""

        return uuid4().hex

    def _emit_warning(self, message: str, *, exception: BaseException | None = None) -> None:
        text = message.strip()
        if exception is not None and text and str(exception) not in text:
            text = f"{text}: {exception}"
        elif exception is not None and not text:
            text = str(exception)
        if not text:
            return
        prefixed = text if text.startswith("[mcp2term-client]") else f"[mcp2term-client] WARNING: {text}"
        if exception is not None:
            logger.warning(text, exc_info=exception)
        else:
            logger.warning(text)
        try:
            self._notice_writer(prefixed)
        except Exception as writer_error:  # pragma: no cover - defensive logging
            logger.error("Failed to deliver warning notice", exc_info=writer_error)

    def _emit_warnings(self, warnings: Iterable[str]) -> None:
        for warning in warnings:
            self._emit_warning(warning)

    def start(self) -> None:
        if self._started:
            return
        self._ensure_loop_started()
        if self._loop is None:
            raise RuntimeError("Event loop failed to start")
        self._input_monitor.reset()
        self._output_monitor.reset()
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
            self._submit_request("stop", track_backpressure=False)
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
        command_id: str | None = None,
        allocate_pty: bool = False,
    ) -> CommandResponse:
        command_id, future = self.run_command_async(
            command,
            working_directory=working_directory,
            environment=environment,
            ephemeral_environment=ephemeral_environment,
            timeout=timeout,
            command_id=command_id,
            allocate_pty=allocate_pty,
        )
        try:
            return future.result()
        except Exception as exc:
            self._emit_warning(f"run_command request for '{command}' failed", exception=exc)
            raise

    def run_command_async(
        self,
        command: str,
        *,
        working_directory: str | None,
        environment: dict[str, str] | None = None,
        ephemeral_environment: dict[str, str] | None = None,
        timeout: float | None = None,
        command_id: str | None = None,
        allocate_pty: bool = False,
    ) -> tuple[str, Future[CommandResponse]]:
        env: dict[str, str] = {}
        if environment:
            env.update(environment)
        if ephemeral_environment:
            env.update(ephemeral_environment)
        effective_timeout = timeout if timeout is not None else self._default_timeout
        actual_command_id = command_id or self.generate_command_id()
        future = self._submit_request(
            "call_tool",
            command=command,
            working_directory=working_directory,
            environment=env,
            timeout=effective_timeout,
            command_id=actual_command_id,
            allocate_pty=allocate_pty,
            wait=False,
        )
        assert isinstance(future, Future)
        return actual_command_id, future

    def cancel_command(
        self,
        command_id: str,
        *,
        signal: str | int | None = None,
    ) -> CancelCommandResponse:
        try:
            response = self._submit_request(
                "cancel_command",
                command_id=command_id,
                signal_value=signal,
            )
        except Exception as exc:
            self._emit_warning(f"cancel_command request for {command_id} failed", exception=exc)
            raise
        assert isinstance(response, CancelCommandResponse)
        return response

    def send_stdin(
        self,
        command_id: str,
        data: str,
        *,
        eof: bool = False,
    ) -> bool:
        try:
            response = self._submit_request(
                "send_stdin",
                command_id=command_id,
                data=data,
                eof=eof,
            )
        except Exception as exc:
            self._emit_warning(f"send_stdin request for {command_id} failed", exception=exc)
            raise
        assert isinstance(response, SendInputResponse)
        return response.accepted

    def manage_file(
        self,
        path: str,
        *,
        operation: str,
        content: str | None = None,
        pattern: str | None = None,
        line: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        encoding: str = "utf-8",
        create_parents: bool = False,
        overwrite: bool = False,
        create_if_missing: bool = True,
        escape_profile: str = "auto",
        follow_symlinks: bool = True,
        use_regex: bool = False,
        ignore_case: bool = False,
        max_replacements: int | None = None,
        anchor_text: str | None = None,
        anchor_use_regex: bool = False,
        anchor_ignore_case: bool = False,
        anchor_after: bool = False,
        anchor_occurrence: int | None = None,
    ) -> FileOperationResponse:
        try:
            response = self._submit_request(
                "manage_file",
                path=path,
                operation=operation,
                content=content,
                pattern=pattern,
                line=line,
                start_line=start_line,
                end_line=end_line,
                encoding=encoding,
                create_parents=create_parents,
                overwrite=overwrite,
                create_if_missing=create_if_missing,
                escape_profile=escape_profile,
                follow_symlinks=follow_symlinks,
                use_regex=use_regex,
                ignore_case=ignore_case,
                max_replacements=max_replacements,
                anchor_text=anchor_text,
                anchor_use_regex=anchor_use_regex,
                anchor_ignore_case=anchor_ignore_case,
                anchor_after=anchor_after,
                anchor_occurrence=anchor_occurrence,
            )
        except Exception as exc:
            self._emit_warning(
                f"manage_file request for {operation} {path} failed",
                exception=exc,
            )
            raise
        assert isinstance(response, FileOperationResponse)
        return response

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

    def _handle_request_failure(self, request: _Request, error: Exception) -> bool:
        if request.future.cancelled():
            return True

        def _finalize(response: object) -> bool:
            warnings: Iterable[str] = getattr(response, "warnings", tuple())  # type: ignore[attr-defined]
            if not request.future.done():
                request.future.set_result(response)
            self._emit_warnings(warnings)
            return True

        action = request.action
        if action == "call_tool":
            payload = request.payload
            command = str(payload.get("command", "")) if payload.get("command") is not None else ""
            working_dir_raw = payload.get("working_directory")
            working_directory = str(working_dir_raw) if working_dir_raw is not None else ""
            command_id_raw = payload.get("command_id")
            command_id = str(command_id_raw) if command_id_raw is not None else ""
            timestamp = _iso_timestamp()
            if command:
                warning_message = f"Failed to execute '{command}' on remote host"
            else:
                warning_message = "Remote command execution failed"
            warning_message = f"{warning_message}: {error}"
            response = CommandResponse(
                command_id=command_id,
                command=command,
                working_directory=working_directory,
                return_code=-1,
                stdout="",
                stderr=str(error),
                started_at=timestamp,
                finished_at=timestamp,
                duration=0.0,
                timed_out=False,
                pty_allocated=bool(payload.get("allocate_pty", False)),
                warnings=(warning_message,),
            )
            return _finalize(response)
        if action == "cancel_command":
            command_id = str(request.payload.get("command_id", ""))
            warning_message = f"Failed to cancel command {command_id}: {error}"
            response = CancelCommandResponse(
                command_id=command_id,
                signal=None,
                signal_name=None,
                delivered=False,
                warnings=(warning_message,),
            )
            return _finalize(response)
        if action == "send_stdin":
            command_id = str(request.payload.get("command_id", ""))
            warning_message = f"Failed to send input to command {command_id}: {error}"
            response = SendInputResponse(
                command_id=command_id,
                accepted=False,
                eof=bool(request.payload.get("eof", False)),
                warnings=(warning_message,),
            )
            return _finalize(response)
        if action == "manage_file":
            raw_path = request.payload.get("path", "")
            path = str(raw_path) if raw_path is not None else ""
            operation_raw = request.payload.get("operation", "")
            operation = str(operation_raw) if operation_raw is not None else "manage_file"
            encoding_raw = request.payload.get("encoding", "utf-8")
            encoding = str(encoding_raw) if encoding_raw is not None else "utf-8"
            message = f"Failed to execute filetool {operation} on {path or 'target'}: {error}"
            response = FileOperationResponse(
                path=path,
                operation=operation,
                success=False,
                changed=False,
                encoding=encoding,
                message=message,
                content=None,
                lines=tuple(),
                line_numbers=tuple(),
                warnings=(message,),
            )
            return _finalize(response)
        return False

    async def _session_worker(self) -> None:
        queue_: asyncio.Queue[_Request] = asyncio.Queue()
        self._request_queue = queue_
        try:
            self._transport_cm = streamablehttp_client(self._url)
            self._transport = await self._transport_cm.__aenter__()
            read_stream, write_stream, _ = self._transport
            self._session = ClientSession(read_stream, write_stream, logging_callback=self._handle_log_message)
            await self._session.__aenter__()
            try:
                await self._session.initialize()
            except Exception as exc:
                processed = await self._augment_startup_exception(exc)
                self._startup_error = processed
                self._startup_ready.set()
                raise processed
            else:
                self._startup_ready.set()

            while True:
                request = await queue_.get()
                if request.track_backpressure:
                    self._input_monitor.decrement()
                if request.action == "stop":
                    request.future.set_result(None)
                    break
                try:
                    if request.action == "call_tool":
                        task = asyncio.create_task(
                            self._async_call_tool(
                                request.payload["command"],
                                request.payload.get("working_directory"),
                                request.payload.get("environment", {}),
                                request.payload.get("timeout"),
                                request.payload.get("command_id"),
                                bool(request.payload.get("allocate_pty", False)),
                            )
                        )
                        self._active_tasks.add(task)

                        def _complete(
                            finished: asyncio.Task[Any],
                            *,
                            future: Future[Any],
                            request_ref: _Request,
                        ) -> None:
                            self._active_tasks.discard(finished)
                            if finished.cancelled():
                                if not future.cancelled():
                                    future.cancel()
                                return
                            try:
                                result = finished.result()
                            except asyncio.CancelledError as cancel_error:
                                if not future.cancelled():
                                    future.set_exception(cancel_error)
                            except Exception as error:
                                handled = self._handle_request_failure(request_ref, error)
                                if not handled and not future.done():
                                    future.set_exception(error)
                            except BaseException:
                                raise
                            else:
                                future.set_result(result)
                                if isinstance(result, CommandResponse):
                                    self._emit_warnings(result.warnings)

                        task.add_done_callback(
                            lambda finished, future=request.future, request_ref=request: _complete(
                                finished,
                                future=future,
                                request_ref=request_ref,
                            )
                        )
                    elif request.action == "cancel_command":
                        result = await self._async_cancel_command(
                            request.payload["command_id"],
                            request.payload.get("signal_value"),
                        )
                        request.future.set_result(result)
                        self._emit_warnings(result.warnings)
                    elif request.action == "send_stdin":
                        result = await self._async_send_stdin(
                            request.payload["command_id"],
                            request.payload.get("data", ""),
                            request.payload.get("eof", False),
                        )
                        request.future.set_result(result)
                        self._emit_warnings(result.warnings)
                    elif request.action == "manage_file":
                        result = await self._async_manage_file(
                            request.payload["path"],
                            request.payload["operation"],
                            request.payload.get("content"),
                            request.payload.get("pattern"),
                            request.payload.get("line"),
                            request.payload.get("start_line"),
                            request.payload.get("end_line"),
                            request.payload.get("encoding", "utf-8"),
                            bool(request.payload.get("create_parents", False)),
                            bool(request.payload.get("overwrite", False)),
                            bool(request.payload.get("create_if_missing", True)),
                            str(request.payload.get("escape_profile", "auto")),
                            bool(request.payload.get("follow_symlinks", True)),
                            bool(request.payload.get("use_regex", False)),
                            bool(request.payload.get("ignore_case", False)),
                            request.payload.get("max_replacements"),
                            request.payload.get("anchor_text"),
                            bool(request.payload.get("anchor_use_regex", False)),
                            bool(request.payload.get("anchor_ignore_case", False)),
                            bool(request.payload.get("anchor_after", False)),
                            request.payload.get("anchor_occurrence"),
                        )
                        request.future.set_result(result)
                        self._emit_warnings(result.warnings)
                    else:
                        raise RuntimeError(f"Unknown request action: {request.action}")
                except Exception as exc:
                    handled = self._handle_request_failure(request, exc)
                    if not handled and not request.future.done():
                        request.future.set_exception(exc)
        except Exception as exc:
            if not self._startup_ready.is_set():
                processed = await self._augment_startup_exception(exc)
                self._startup_error = processed
                self._startup_ready.set()
                raise processed
            self._startup_error = exc
            self._startup_ready.set()
            raise
        finally:
            if self._active_tasks:
                pending = list(self._active_tasks)
                self._active_tasks.clear()
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
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
            self._input_monitor.reset()

    def _submit_request(
        self,
        action: str,
        wait: bool = True,
        *,
        track_backpressure: bool = True,
        **payload: Any,
    ) -> Any | Future[Any]:
        if self._loop is None or self._request_queue is None:
            raise RuntimeError("RemoteMcpSession used before start()")
        future: Future[Any] = Future()
        request = _Request(
            action=action,
            payload=payload,
            future=future,
            track_backpressure=track_backpressure,
        )

        def _enqueue() -> None:
            if self._request_queue is None:
                future.set_exception(RuntimeError("RemoteMcpSession is shutting down"))
                return
            self._request_queue.put_nowait(request)
            if track_backpressure:
                self._input_monitor.increment()

        self._loop.call_soon_threadsafe(_enqueue)
        if wait:
            return future.result()
        return future

    async def _async_call_tool(
        self,
        command: str,
        working_directory: str | None,
        environment: dict[str, str],
        timeout: float | None,
        command_id: str | None,
        allocate_pty: bool,
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
        if command_id is not None:
            arguments["command_id"] = command_id
        if allocate_pty:
            arguments["allocate_pty"] = True
        result = await self._session.call_tool("run_command", arguments)
        return CommandResponse.from_call_tool_result(result)

    async def _async_cancel_command(
        self,
        command_id: str,
        signal_value: str | int | None,
    ) -> CancelCommandResponse:
        if self._session is None:
            raise RuntimeError("RemoteMcpSession used before start()")
        arguments: dict[str, Any] = {"command_id": command_id}
        if signal_value is not None:
            arguments["signal_value"] = signal_value
        result = await self._session.call_tool("cancel_command", arguments)
        return CancelCommandResponse.from_call_tool_result(result)

    async def _async_send_stdin(
        self,
        command_id: str,
        data: str,
        eof: bool,
    ) -> SendInputResponse:
        if self._session is None:
            raise RuntimeError("RemoteMcpSession used before start()")
        arguments: dict[str, Any] = {"command_id": command_id, "eof": eof}
        if data:
            arguments["data"] = data
        result = await self._session.call_tool("send_stdin", arguments)
        return SendInputResponse.from_call_tool_result(result)

    async def _async_manage_file(
        self,
        path: str,
        operation: str,
        content: str | None,
        pattern: str | None,
        line: int | None,
        start_line: int | None,
        end_line: int | None,
        encoding: str,
        create_parents: bool,
        overwrite: bool,
        create_if_missing: bool,
        escape_profile: str,
        follow_symlinks: bool,
        use_regex: bool,
        ignore_case: bool,
        max_replacements: int | None,
        anchor_text: str | None,
        anchor_use_regex: bool,
        anchor_ignore_case: bool,
        anchor_after: bool,
        anchor_occurrence: int | None,
    ) -> FileOperationResponse:
        if self._session is None:
            raise RuntimeError("RemoteMcpSession used before start()")
        arguments: dict[str, Any] = {
            "path": path,
            "operation": operation,
            "encoding": encoding,
            "create_parents": create_parents,
            "overwrite": overwrite,
            "create_if_missing": create_if_missing,
            "escape_profile": escape_profile,
            "follow_symlinks": follow_symlinks,
            "use_regex": use_regex,
            "ignore_case": ignore_case,
        }
        if content is not None:
            arguments["content"] = content
        if pattern is not None:
            arguments["pattern"] = pattern
        if line is not None:
            arguments["line"] = line
        if start_line is not None:
            arguments["start_line"] = start_line
        if end_line is not None:
            arguments["end_line"] = end_line
        if max_replacements is not None:
            arguments["max_replacements"] = max_replacements
        if anchor_text is not None:
            arguments["anchor"] = anchor_text
            arguments["anchor_use_regex"] = anchor_use_regex
            arguments["anchor_ignore_case"] = anchor_ignore_case
            arguments["anchor_after"] = anchor_after
            if anchor_occurrence is not None:
                arguments["anchor_occurrence"] = anchor_occurrence
        elif anchor_after or anchor_use_regex or anchor_ignore_case or anchor_occurrence is not None:
            raise ValueError(
                "Anchor modifiers cannot be supplied without anchor text."
            )
        result = await self._session.call_tool("manage_file", arguments)
        return FileOperationResponse.from_call_tool_result(result)

    async def _handle_log_message(self, params: types.LoggingMessageNotificationParams) -> None:
        data = params.data
        if not isinstance(data, str):
            data = json.dumps(data, ensure_ascii=False)
        self._log_streamer.submit(LogMessage(level=str(params.level), text=data))

    async def _augment_startup_exception(self, exc: BaseException) -> BaseException:
        if isinstance(exc, RemoteMcpSessionError):
            return exc
        should_probe = isinstance(exc, (httpx.HTTPError, McpError))
        if isinstance(exc, McpError) and exc.error.message != "Session terminated":
            should_probe = False
        if not should_probe:
            return exc
        diagnostics: list[EndpointProbeResult] = []
        try:
            diagnostics = await self._collect_connection_diagnostics()
        except Exception as probe_error:  # pragma: no cover - defensive
            diagnostics = [
                EndpointProbeResult(
                    method="probe",
                    url=self._url,
                    status_code=None,
                    success=False,
                    detail=f"diagnostics failed: {probe_error}",
                )
            ]
        message_lines = [
            f"Unable to initialize MCP session against {self._url}",
            f"Root cause: {exc}",
        ]
        if diagnostics:
            message_lines.append("HTTP diagnostics:")
            for result in diagnostics:
                prefix = "  - "
                status = result.summary()
                message_lines.append(f"{prefix}{result.method} {result.url} -> {status}")
        if any(result.status_code == 404 for result in diagnostics):
            message_lines.append(
                "Suggestion: confirm that the server is running and that the URL includes the correct Streamable HTTP mount "
                "path (usually '/mcp')."
            )
        elif all(not result.success for result in diagnostics):
            message_lines.append(
                "Suggestion: verify network connectivity to the host and ensure any tunnels (such as ngrok) are active."
            )
        return RemoteMcpSessionError("\n".join(message_lines))

    async def _collect_connection_diagnostics(self) -> list[EndpointProbeResult]:
        candidates = list(self._candidate_probe_urls())
        results: list[EndpointProbeResult] = []
        for url in candidates:
            result = await self._probe_endpoint(url)
            results.append(result)
        return results

    def _candidate_probe_urls(self) -> Iterable[str]:
        parsed = urlparse(self._url)
        base = parsed._replace(path="", params="", query="", fragment="")
        candidates: list[str] = [self._url]
        if not self._url.endswith("/"):
            candidates.append(self._url + "/")
        candidates.append(urlunparse(base))
        base_with_slash = urlunparse(base._replace(path="/"))
        candidates.append(base_with_slash)
        seen: set[str] = set()
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            yield url

    async def _probe_endpoint(self, url: str) -> EndpointProbeResult:
        async with httpx.AsyncClient(timeout=_DIAGNOSTIC_TIMEOUT, follow_redirects=True) as client:
            try:
                response = await client.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": "mcp2term-client-diagnostic",
                        "method": "mcp2term/diagnostic",
                        "params": {},
                    },
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
            except httpx.RequestError as error:
                return EndpointProbeResult(
                    method="POST",
                    url=url,
                    status_code=None,
                    success=False,
                    detail=str(error),
                )
        detail = self._summarize_response(response)
        success = 200 <= response.status_code < 400
        return EndpointProbeResult(
            method="POST",
            url=url,
            status_code=response.status_code,
            success=success,
            detail=detail,
        )

    @staticmethod
    def _summarize_response(response: httpx.Response) -> str | None:
        content_type = response.headers.get("Content-Type", "").lower()
        snippet: str | None = None
        try:
            if "application/json" in content_type:
                data = response.json()
                snippet = json.dumps(data, ensure_ascii=False)
            else:
                snippet = response.text
        except Exception:  # pragma: no cover - defensive fallback
            snippet = response.text
        if snippet is None:
            return None
        return textwrap.shorten(snippet.replace("\n", " ").strip(), width=160, placeholder="…")
