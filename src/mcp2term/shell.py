"""Shell command execution with streaming for MCP."""

from __future__ import annotations

import asyncio
import codecs
import errno
import os
import pty
import logging
import signal
from contextlib import suppress
from asyncio.subprocess import Process
from dataclasses import dataclass, field
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from uuid import uuid4

from mcp.server.fastmcp import Context

from .config import ServerConfig
from .plugin import PluginManager
from .streaming import (
    CommandCompleteEvent,
    CommandOutputChunk,
    CommandRequest,
    CommandStartEvent,
    utcnow,
)


logger = logging.getLogger(__name__)

_LONG_RUNNING_NOTICE = "COMANND STILL PROCESSING. PLEASE WAIT"


@dataclass(slots=True)
class CommandResult:
    """Represents the result of executing a command."""

    request: CommandRequest
    stdout: str
    stderr: str
    return_code: int
    started_at: datetime
    finished_at: datetime
    pty_allocated: bool

    @property
    def duration(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(slots=True)
class _ManagedProcess:
    """Track runtime metadata for a running subprocess."""

    process: Process
    stdin: asyncio.StreamWriter | None
    allocate_pty: bool
    pty_master: int | None = None
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def send(self, command_id: str, data: str, *, eof: bool) -> bool:
        if self.stdin is not None and not self.stdin.is_closing():
            try:
                if data:
                    self.stdin.write(data.encode("utf-8"))
                    await self.stdin.drain()
                if eof:
                    try:
                        self.stdin.write_eof()
                    except (AttributeError, RuntimeError, ValueError):
                        self.stdin.close()
                return True
            except (BrokenPipeError, ConnectionResetError, RuntimeError, ValueError) as exc:
                logger.warning("Failed to deliver stdin to command %s: %s", command_id, exc)
                return False

        if self.pty_master is None:
            return False

        async with self._write_lock:
            loop = asyncio.get_running_loop()
            try:
                if data:
                    await loop.run_in_executor(None, os.write, self.pty_master, data.encode("utf-8"))
                if eof:
                    await loop.run_in_executor(None, os.write, self.pty_master, b"\x04")
                return True
            except OSError as exc:  # pragma: no cover - platform specific
                logger.warning("Failed to deliver stdin to PTY command %s: %s", command_id, exc)
                return False

    def close(self) -> None:
        if self.stdin is not None and not self.stdin.is_closing():
            self.stdin.close()
        if self.pty_master is not None:
            try:
                os.close(self.pty_master)
            except OSError:
                pass
            self.pty_master = None


class CommandExecutionError(RuntimeError):
    """Raised when a command fails to execute correctly."""

    def __init__(self, message: str, event: CommandCompleteEvent | None = None) -> None:
        super().__init__(message)
        self.event = event


class CommandTimeoutError(CommandExecutionError):
    """Raised when a command exceeds the configured timeout."""


class _ContextEmitter:
    def __init__(self, ctx: Context | None, plugin_manager: PluginManager, request: CommandRequest) -> None:
        self._ctx = ctx
        self._plugin_manager = plugin_manager
        self._request = request

    @property
    def request(self) -> CommandRequest:
        """Return the command request associated with this emitter."""

        return self._request

    async def emit_stdout(self, text: str) -> None:
        chunk = CommandOutputChunk(request=self._request, timestamp=utcnow(), stream="stdout", data=text)
        if self._ctx is not None:
            await self._ctx.info(text)
        await self._plugin_manager.emit_stdout(chunk)

    async def emit_stderr(self, text: str) -> None:
        chunk = CommandOutputChunk(request=self._request, timestamp=utcnow(), stream="stderr", data=text)
        if self._ctx is not None:
            await self._ctx.error(text)
        await self._plugin_manager.emit_stderr(chunk)

    async def emit_progress_notice(self, text: str) -> None:
        """Surface progress notices through the MCP context and server logs."""

        if self._ctx is not None:
            try:
                await self._ctx.info(text)
            except Exception:  # pragma: no cover - defensive logging for unexpected failures
                logger.exception(
                    "Failed to forward long-running command notice to context for %s", self._request.command_id
                )
        logger.info("Command %s still running: %s", self._request.command_id, text)


class ShellCommandExecutor:
    """Execute shell commands with streaming output reporting."""

    def __init__(self, config: ServerConfig, plugin_manager: PluginManager) -> None:
        self.config = config
        self.plugin_manager = plugin_manager
        self._running_commands: dict[str, _ManagedProcess] = {}
        self._running_lock = asyncio.Lock()
        self.plugin_manager.register_export("mcp2term.shell.executor", self)

    async def run(
        self,
        command: str,
        *,
        ctx: Context | None = None,
        working_directory: Path | None = None,
        environment: Mapping[str, str] | None = None,
        timeout: float | None = None,
        command_id: str | None = None,
        allocate_pty: bool = False,
    ) -> CommandResult:
        env = self.config.build_environment()
        if environment:
            env.update({str(key): str(value) for key, value in environment.items()})
        cwd = str((working_directory or self.config.working_directory).expanduser().resolve())
        timeout_value = timeout if timeout is not None else self.config.command_timeout
        request = CommandRequest(
            command_id=command_id or uuid4().hex,
            command=command,
            working_directory=cwd,
            environment=dict(env),
            timeout=timeout_value,
            allocate_pty=allocate_pty,
        )
        started_at = utcnow()
        await self.plugin_manager.emit_command_start(CommandStartEvent(request=request, started_at=started_at))
        emitter = _ContextEmitter(ctx, self.plugin_manager, request)

        process: Process | None = None
        managed: _ManagedProcess | None = None
        stdout_task: asyncio.Task[str] | None = None
        stderr_task: asyncio.Task[str] | None = None
        master_fd: int | None = None
        slave_fd: int | None = None
        command_completed = asyncio.Event()
        progress_task: asyncio.Task[None] | None = None
        try:
            try:
                if allocate_pty:
                    master_fd, slave_fd = pty.openpty()
                    os.set_inheritable(master_fd, False)
                    os.set_inheritable(slave_fd, False)
                    process = await asyncio.create_subprocess_shell(
                        command,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        stdin=slave_fd,
                        cwd=cwd,
                        env=env,
                        executable=self.config.shell_path,
                    )
                    os.close(slave_fd)
                    slave_fd = None
                    managed = _ManagedProcess(
                        process=process,
                        stdin=None,
                        allocate_pty=True,
                        pty_master=master_fd,
                    )
                    stdout_task = asyncio.create_task(
                        self._consume_pty_stream(master_fd, emitter.emit_stdout)
                    )
                else:
                    process = await asyncio.create_subprocess_shell(
                        command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        stdin=asyncio.subprocess.PIPE,
                        cwd=cwd,
                        env=env,
                        executable=self.config.shell_path,
                    )
                    managed = _ManagedProcess(
                        process=process,
                        stdin=process.stdin,
                        allocate_pty=False,
                    )
                    stdout_task = asyncio.create_task(
                        self._consume_stream(process.stdout, emitter.emit_stdout)
                    )
                    stderr_task = asyncio.create_task(
                        self._consume_stream(process.stderr, emitter.emit_stderr)
                    )
            except FileNotFoundError as exc:  # pragma: no cover - depends on environment
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                if slave_fd is not None:
                    try:
                        os.close(slave_fd)
                    except OSError:
                        pass
                finished_at = utcnow()
                event = CommandCompleteEvent(
                    request=request,
                    started_at=started_at,
                    finished_at=finished_at,
                    return_code=-1,
                    stdout="",
                    stderr=str(exc),
                    duration=(finished_at - started_at).total_seconds(),
                )
                await self.plugin_manager.emit_command_complete(event)
                raise CommandExecutionError(f"Failed to execute command: {exc}", event=event) from exc

            async with self._running_lock:
                assert managed is not None
                self._running_commands[request.command_id] = managed

            progress_task = asyncio.create_task(
                self._monitor_long_running_command(
                    emitter,
                    command_completed,
                    delay=self.config.long_command_notice_delay,
                    interval=self.config.long_command_notice_interval,
                )
            )

            async def wait_process(proc: Process) -> int:
                if timeout_value is None:
                    return await proc.wait()
                return await asyncio.wait_for(proc.wait(), timeout=timeout_value)

            timed_out = False
            try:
                return_code = await wait_process(process)
            except asyncio.TimeoutError:
                timed_out = True
                process.kill()
                await process.wait()
                return_code = process.returncode or -1
            except Exception:
                process.kill()
                await process.wait()
                raise

            if stdout_task is None:
                stdout_text = ""
            else:
                stdout_text = await stdout_task
            if stderr_task is None:
                stderr_text = ""
            else:
                stderr_text = await stderr_task
            finished_at = utcnow()
            event = CommandCompleteEvent(
                request=request,
                started_at=started_at,
                finished_at=finished_at,
                return_code=return_code,
                stdout=stdout_text,
                stderr=stderr_text,
                duration=(finished_at - started_at).total_seconds(),
            )
            await self.plugin_manager.emit_command_complete(event)

            if timed_out:
                raise CommandTimeoutError(
                    f"Command '{command}' timed out after {timeout_value} seconds", event=event
                )

            return CommandResult(
                request=request,
                stdout=stdout_text,
                stderr=stderr_text,
                return_code=return_code,
                started_at=started_at,
                finished_at=finished_at,
                pty_allocated=allocate_pty,
            )
        finally:
            command_completed.set()
            if progress_task is not None:
                progress_task.cancel()
                with suppress(asyncio.CancelledError):
                    await progress_task
            if process is not None:
                async with self._running_lock:
                    entry = self._running_commands.pop(request.command_id, None)
                if entry is not None:
                    entry.close()

    async def _consume_stream(
        self,
        stream: asyncio.StreamReader | None,
        emitter: Callable[[str], Awaitable[None]],
    ) -> str:
        if stream is None:
            return ""

        chunk_size = max(1, int(self.config.stream_chunk_size))
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = StringIO()
        try:
            while True:
                try:
                    data = await stream.read(chunk_size)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                text = decoder.decode(data)
                if text:
                    buffer.write(text)
                    await emitter(text)
        except asyncio.CancelledError:
            raise

        remaining = decoder.decode(b"", final=True)
        if remaining:
            buffer.write(remaining)
            await emitter(remaining)
        return buffer.getvalue()

    async def _consume_pty_stream(
        self,
        master_fd: int,
        emitter: Callable[[str], Awaitable[None]],
    ) -> str:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        read_fd = os.dup(master_fd)
        read_file = os.fdopen(read_fd, "rb", buffering=0)
        transport = None
        try:
            transport, _ = await loop.connect_read_pipe(lambda: protocol, read_file)
            return await self._consume_stream(reader, emitter)
        finally:
            if transport is not None:
                transport.close()
            try:
                read_file.close()
            except OSError:
                pass

    async def _monitor_long_running_command(
        self,
        emitter: _ContextEmitter,
        completion_event: asyncio.Event,
        *,
        delay: float,
        interval: float,
    ) -> None:
        """Emit periodic notices for commands exceeding the configured duration."""

        normalized_delay = max(0.0, float(delay))
        normalized_interval = max(0.0, float(interval))
        try:
            if normalized_delay > 0:
                try:
                    await asyncio.wait_for(completion_event.wait(), timeout=normalized_delay)
                    return
                except asyncio.TimeoutError:
                    pass
            elif completion_event.is_set():
                return

            while not completion_event.is_set():
                await emitter.emit_progress_notice(_LONG_RUNNING_NOTICE)
                if completion_event.is_set():
                    break
                if normalized_interval <= 0:
                    await asyncio.sleep(0)
                    continue
                try:
                    await asyncio.wait_for(completion_event.wait(), timeout=normalized_interval)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Error while emitting long-running command notices for %s", emitter.request.command_id
            )

    async def send_signal(self, command_id: str, sig: int = signal.SIGINT) -> bool:
        """Send ``sig`` to the running command identified by ``command_id``."""

        async with self._running_lock:
            entry = self._running_commands.get(command_id)

        if entry is None:
            return False

        process = entry.process
        try:
            process.send_signal(sig)
            return True
        except ProcessLookupError:
            return False
        except AttributeError:  # pragma: no cover - platform fallback
            try:
                process.terminate()
            except ProcessLookupError:
                return False
            return True

    async def interrupt(self, command_id: str) -> bool:
        """Send ``SIGINT`` to the running command when present."""

        return await self.send_signal(command_id, signal.SIGINT)

    async def send_stdin(self, command_id: str, data: str, *, eof: bool = False) -> bool:
        """Deliver ``data`` to the stdin pipe for the running command.

        Parameters
        ----------
        command_id:
            Identifier of the running command.
        data:
            Text to forward to the process stdin. An empty string is ignored
            unless ``eof`` is True.
        eof:
            When True, closes the stdin stream after writing ``data``.
        """

        async with self._running_lock:
            entry = self._running_commands.get(command_id)

        if entry is None:
            return False

        return await entry.send(command_id, data, eof=eof)
