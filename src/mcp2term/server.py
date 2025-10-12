"""Factory for the MCP terminal server."""

from __future__ import annotations

import io
import logging
import os
import select
import signal
import sys
import threading
import time
import weakref
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, TextIO

import anyio
from anyio import BrokenResourceError, ClosedResourceError, EndOfStream, get_cancelled_exc_class
from anyio.abc import ObjectReceiveStream, ObjectSendStream, TaskGroup as AnyIOTaskGroup

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from .config import ServerConfig
from .files import FileEditor, FileOperationError, FileOperationResult
from .plugin import (
    FileOperationEvent,
    GlobalPluginManager,
    PluginManager,
    ServerWarningEvent,
)
from .shell import CommandExecutionError, CommandResult, CommandTimeoutError, ShellCommandExecutor
from .streaming import CommandCompleteEvent, utcnow

try:  # pragma: no cover - Windows environments do not provide termios/tty
    import termios
    import tty
except ImportError:  # pragma: no cover - handled at runtime for Windows
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplicationState:
    """Objects shared across MCP requests."""

    config: ServerConfig
    plugin_manager: PluginManager
    executor: ShellCommandExecutor
    chat_bridge: "UserChatBridge"


class ConsoleStreamProxy(io.TextIOBase):
    """Thread-safe stream wrapper buffering output while console echoing is paused."""

    def __init__(
        self,
        *,
        name: str,
        underlying: TextIO,
        lock: threading.RLock,
    ) -> None:
        self._name = name
        self._underlying = underlying
        self._lock = lock
        self._buffer: list[str] = []
        self._paused = False
        self._encoding = getattr(underlying, "encoding", "utf-8")
        self._errors = getattr(underlying, "errors", "strict")

    @property
    def name(self) -> str:
        """Return the diagnostic label used for logging."""

        return self._name

    @property
    def encoding(self) -> str:
        """Return the encoding reported to downstream writers."""

        return self._encoding

    @property
    def errors(self) -> str:
        """Return the error handling mode reported to downstream writers."""

        return self._errors

    def writable(self) -> bool:  # pragma: no cover - trivial delegation
        return True

    def readable(self) -> bool:  # pragma: no cover - trivial delegation
        return False

    def seekable(self) -> bool:  # pragma: no cover - trivial delegation
        return False

    def isatty(self) -> bool:
        """Return ``True`` when the underlying stream is a TTY."""

        return bool(getattr(self._underlying, "isatty", lambda: False)())

    def fileno(self) -> int:
        """Return the file descriptor of the underlying stream."""

        fileno_func = getattr(self._underlying, "fileno", None)
        if fileno_func is None:
            raise io.UnsupportedOperation("Underlying stream does not expose fileno()")
        return int(fileno_func())

    def flush(self) -> None:
        """Flush any buffered output when not paused."""

        with self._lock:
            if self._paused:
                return
            self._underlying.flush()

    def close(self) -> None:  # pragma: no cover - underlying streams stay open
        """Flush buffered output without closing the underlying stream."""

        with self._lock:
            if self._buffer:
                self._underlying.write("".join(self._buffer))
                self._buffer.clear()
            self._underlying.flush()

    def detach(self) -> None:  # pragma: no cover - TextIOBase contract
        raise io.UnsupportedOperation("ConsoleStreamProxy does not support detach()")

    def write(self, data: str) -> int:
        """Write ``data`` or buffer it when console echoing is paused."""

        if not data:
            return 0
        if not isinstance(data, str):
            data = str(data)
        with self._lock:
            if self._paused:
                self._buffer.append(data)
                return len(data)
            written = self._underlying.write(data)
            if written is None:
                written = len(data)
            self._underlying.flush()
            return written

    def writelines(self, lines: Iterable[str]) -> None:
        """Write multiple ``lines`` while respecting the pause state."""

        for line in lines:
            self.write(line)

    def set_paused(self, paused: bool) -> None:
        """Toggle buffering and flush queued output upon resuming."""

        with self._lock:
            if self._paused == paused:
                return
            self._paused = paused
            if not paused and self._buffer:
                buffered = "".join(self._buffer)
                self._buffer.clear()
                self._underlying.write(buffered)
                self._underlying.flush()

    def is_paused(self) -> bool:
        """Return ``True`` when the proxy currently buffers writes."""

        return self._paused

    def __getattr__(self, item: str) -> Any:  # pragma: no cover - delegation helper
        return getattr(self._underlying, item)

class UserChatBridge:
    """Coordinate interactive console messaging without auxiliary terminals.

    When active the bridge listens to the hosting console for the activation
    key ``/``. Once triggered it temporarily pauses console echoing so that
    command output does not interleave with the operator's message. The typed
    text is echoed locally and, after pressing :kbd:`Enter`, broadcast to every
    connected MCP client as an informational log entry. Pressing :kbd:`Esc`
    cancels the interaction without sending anything to clients.
    """

    _MESSAGE_PREFIX = "[MESSAGE FROM USER. DO NOT IGNORE!]"
    _ACTIVATION_KEY = "/"
    _DISABLE_VALUES = {"disable", "disabled", "off", "none", "false", "0"}
    _PROMPT = "[Operator ➜ Clients] Enter message (Esc to cancel): "
    _CANCEL_NOTICE = "[Operator message cancelled]"
    _EMPTY_NOTICE = "[Operator message ignored: no text entered]"
    _READ_TIMEOUT = 0.05
    _MAX_PENDING_MESSAGES = 64

    def __init__(self, *, plugin_manager: PluginManager, console_echo: bool) -> None:
        """Initialise the bridge with references to the plugin manager."""

        self._plugin_manager = plugin_manager
        self._console_echo = console_echo
        self._sessions: weakref.WeakSet[ServerSession] = weakref.WeakSet()
        self._task_group: AnyIOTaskGroup | None = None
        self._active = False
        self._message_sender: ObjectSendStream[str] | None = None
        self._message_receiver: ObjectReceiveStream[str] | None = None
        self._stop_event = threading.Event()
        self._stdin = sys.stdin
        self._stdout_base: TextIO = sys.stdout
        self._stderr_base: TextIO = sys.stderr
        self._stdout_proxy: ConsoleStreamProxy | None = None
        self._stderr_proxy: ConsoleStreamProxy | None = None
        self._stdin_fd = self._determine_stdin_fd()
        self._terminal_settings: list[Any] | None = None
        self._interactive = False
        self._console_lock = threading.RLock()
        self._input_lock = threading.Lock()
        self._console_paused = False

    @property
    def is_active(self) -> bool:
        """Return ``True`` when console-based messaging is operational."""

        return self._active

    def register_context(self, ctx: Context[ServerSession, ApplicationState]) -> None:
        """Record the session associated with ``ctx`` for subsequent broadcasts."""

        if not self._active:
            return
        try:
            session = ctx.request_context.session
        except ValueError:
            return
        if session is None:
            return
        self._sessions.add(session)

    async def __aenter__(self) -> "UserChatBridge":
        """Activate the console listener when interactive input is available."""

        if self._should_disable_from_environment():
            logger.info("User chat bridge inactive: disabled via MCP2TERM_CHAT_TERMINAL.")
            self._plugin_manager.register_export("mcp2term.user_chat.bridge", self)
            return self

        if not self._stdin_supports_interaction():
            logger.info("User chat bridge inactive: standard input is not a TTY.")
            self._plugin_manager.register_export("mcp2term.user_chat.bridge", self)
            return self

        try:
            self._initialise_console_mode()
        except Exception as exc:  # pragma: no cover - environment specific
            logger.warning(
                "User chat bridge inactive: unable to configure console for interactive messaging: %s",
                exc,
            )
            self._restore_console_mode()
            self._plugin_manager.register_export("mcp2term.user_chat.bridge", self)
            return self

        self._task_group = await anyio.create_task_group().__aenter__()
        send_stream, receive_stream = anyio.create_memory_object_stream[str](self._MAX_PENDING_MESSAGES)
        self._message_sender = send_stream
        self._message_receiver = receive_stream
        self._interactive = True
        self._install_console_stream_proxies()
        try:
            self._task_group.start_soon(self._consume_messages)
            self._task_group.start_soon(self._monitor_console)
        except Exception:
            await self._shutdown_tasks()
            raise

        self._active = True
        self._plugin_manager.register_export("mcp2term.user_chat.bridge", self)
        logger.info(
            "User chat bridge initialised: press '%s' then type a message to reach all clients.",
            self._ACTIVATION_KEY,
        )
        return self

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        """Shut down the console listener and restore terminal state."""

        await self._shutdown_tasks()

    async def _shutdown_tasks(self) -> None:
        """Terminate background activities and clear registered exports."""

        self._stop_event.set()

        if self._message_sender is not None:
            with suppress(Exception):
                self._message_sender.close()
        if self._message_receiver is not None:
            with suppress(Exception):
                self._message_receiver.close()

        if self._task_group is not None:
            await self._task_group.__aexit__(None, None, None)
            self._task_group = None

        self._restore_console_mode()
        self._remove_console_stream_proxies()
        self._plugin_manager.register_export("mcp2term.user_chat.bridge", None)
        self._sessions = weakref.WeakSet()
        self._message_sender = None
        self._message_receiver = None
        self._active = False
        self._interactive = False
        self._console_paused = False
        self._stop_event = threading.Event()

    def _determine_stdin_fd(self) -> int | None:
        """Return the file descriptor for ``stdin`` when available."""

        try:
            return self._stdin.fileno()
        except (AttributeError, io.UnsupportedOperation):
            return None

    def _should_disable_from_environment(self) -> bool:
        """Return ``True`` if the environment requests feature deactivation."""

        override = os.environ.get("MCP2TERM_CHAT_TERMINAL")
        if not override:
            return False
        return override.strip().lower() in self._DISABLE_VALUES

    def _stdin_supports_interaction(self) -> bool:
        """Return ``True`` when ``stdin`` appears suitable for key capture."""

        if self._stdin_fd is None:
            return False
        try:
            return bool(self._stdin.isatty())
        except Exception:  # pragma: no cover - defensive guard
            return False

    def _initialise_console_mode(self) -> None:
        """Enable character-at-a-time reading on POSIX terminals."""

        if os.name == "nt":
            return  # Windows uses ``msvcrt`` functions which do not require setup
        if termios is None or tty is None:
            raise RuntimeError("termios support not available; cannot enable interactive input")
        if self._stdin_fd is None:
            raise RuntimeError("stdin file descriptor unavailable")
        self._terminal_settings = termios.tcgetattr(self._stdin_fd)
        tty.setcbreak(self._stdin_fd)

    def _restore_console_mode(self) -> None:
        """Restore any console state modified during activation."""

        if os.name == "nt":
            return
        if self._stdin_fd is None:
            return
        if self._terminal_settings is None:
            return
        try:
            termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._terminal_settings)
        except Exception:  # pragma: no cover - defensive guard
            logger.debug("Failed to restore terminal settings; continuing shutdown")
        finally:
            self._terminal_settings = None

    def _install_console_stream_proxies(self) -> None:
        """Replace ``sys.stdout`` and ``sys.stderr`` with pause-aware proxies."""

        if self._stdout_proxy is not None or self._stderr_proxy is not None:
            return
        self._stdout_proxy = ConsoleStreamProxy(
            name="stdout",
            underlying=self._stdout_base,
            lock=self._console_lock,
        )
        self._stderr_proxy = ConsoleStreamProxy(
            name="stderr",
            underlying=self._stderr_base,
            lock=self._console_lock,
        )
        sys.stdout = self._stdout_proxy
        sys.stderr = self._stderr_proxy
        self._plugin_manager.update_console_echo_streams(
            stdout=self._stdout_proxy,
            stderr=self._stderr_proxy,
        )

    def _remove_console_stream_proxies(self) -> None:
        """Restore the original console streams if proxies were installed."""

        if self._stdout_proxy is not None and sys.stdout is self._stdout_proxy:
            sys.stdout = self._stdout_base
        if self._stderr_proxy is not None and sys.stderr is self._stderr_proxy:
            sys.stderr = self._stderr_base
        self._plugin_manager.update_console_echo_streams(
            stdout=self._stdout_base,
            stderr=self._stderr_base,
        )
        self._stdout_proxy = None
        self._stderr_proxy = None

    async def _consume_messages(self) -> None:
        """Relay queued messages to all connected sessions."""

        receiver = self._message_receiver
        if receiver is None:
            return
        try:
            async with receiver:
                async for message in receiver:
                    await self._broadcast_message(message)
        except EndOfStream:
            return

    async def _monitor_console(self) -> None:
        """Capture messages typed via the activation key and queue them."""

        send_stream = self._message_sender
        if send_stream is None:
            return

        while not self._stop_event.is_set():
            try:
                message = await anyio.to_thread.run_sync(
                    self._wait_for_console_message,
                    cancellable=True,
                )
            except get_cancelled_exc_class():
                break

            if message is None:
                continue

            try:
                await send_stream.send(message)
            except (BrokenResourceError, ClosedResourceError):
                break

        self._stop_event.set()

    def _wait_for_console_message(self) -> str | None:
        """Block until the activation key is pressed and a message is entered."""

        while not self._stop_event.is_set():
            key = self._read_key(timeout=self._READ_TIMEOUT)
            if key is None:
                continue
            if key == self._ACTIVATION_KEY:
                return self._capture_message()
        return None

    def _capture_message(self) -> str | None:
        """Capture a full line of input from the operator."""

        if not self._interactive:
            return None
        if not self._input_lock.acquire(blocking=False):
            return None

        try:
            self._pause_console_echo()
            with self._console_lock:
                self._stdout_base.write("\n")
                self._stdout_base.write(self._PROMPT)
                self._stdout_base.flush()

            buffer: list[str] = []
            while not self._stop_event.is_set():
                key = self._read_key(timeout=None)
                if key is None:
                    continue
                if key in {"\r", "\n"}:
                    with self._console_lock:
                        self._stdout_base.write("\n")
                        self._stdout_base.flush()
                    message = "".join(buffer).strip()
                    if not message:
                        self._announce_to_console(self._EMPTY_NOTICE)
                        return None
                    self._announce_to_console("[Operator message queued for delivery]")
                    return message
                if key == "\x1b":
                    self._announce_to_console(self._CANCEL_NOTICE)
                    return None
                if key in {"\x7f", "\b"}:
                    if buffer:
                        buffer.pop()
                        with self._console_lock:
                            self._stdout_base.write("\b \b")
                            self._stdout_base.flush()
                    continue
                if key == "\x03":
                    raise KeyboardInterrupt
                if self._is_printable_character(key):
                    buffer.append(key)
                    with self._console_lock:
                        self._stdout_base.write(key)
                        self._stdout_base.flush()
            return None
        finally:
            self._resume_console_echo()
            self._input_lock.release()

    def _is_printable_character(self, value: str) -> bool:
        """Return ``True`` when ``value`` should be appended to the buffer."""

        if value == "\t":
            return False
        return value.isprintable()

    def _read_key(self, *, timeout: float | None) -> str | None:
        """Return a single character from the console respecting ``timeout``."""

        effective_timeout = self._READ_TIMEOUT if timeout is None else timeout
        if os.name == "nt":
            return self._read_key_windows(effective_timeout)
        return self._read_key_posix(effective_timeout)

    def _read_key_posix(self, timeout: float | None) -> str | None:
        """Read one character from ``stdin`` using POSIX primitives."""

        if self._stdin_fd is None:
            return None
        while not self._stop_event.is_set():
            try:
                ready, _, _ = select.select([self._stdin_fd], [], [], timeout)
            except InterruptedError:
                continue
            if not ready:
                return None
            try:
                data = os.read(self._stdin_fd, 1)
            except OSError:
                return None
            if not data:
                return None
            try:
                return data.decode("utf-8", errors="ignore")
            except Exception:
                return None
        return None

    def _read_key_windows(self, timeout: float) -> str | None:  # pragma: no cover - Windows specific
        """Read a character using the Windows console APIs."""

        import msvcrt

        sleep_interval = min(timeout, self._READ_TIMEOUT)
        deadline = time.monotonic() + timeout
        while not self._stop_event.is_set():
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                return char
            if time.monotonic() >= deadline:
                return None
            time.sleep(sleep_interval)
        return None

    def _pause_console_echo(self) -> None:
        """Disable console echoing so streamed output does not interleave."""

        if self._console_paused:
            return
        if self._console_echo:
            self._plugin_manager.set_console_echo_enabled(False)
        if self._stdout_proxy is not None:
            self._stdout_proxy.set_paused(True)
        if self._stderr_proxy is not None:
            self._stderr_proxy.set_paused(True)
        self._console_paused = True

    def _resume_console_echo(self) -> None:
        """Re-enable console echoing if it was paused for message entry."""

        if not self._console_paused:
            return
        if self._stdout_proxy is not None:
            self._stdout_proxy.set_paused(False)
        if self._stderr_proxy is not None:
            self._stderr_proxy.set_paused(False)
        if self._console_echo:
            self._plugin_manager.set_console_echo_enabled(True)
        self._console_paused = False

    def _announce_to_console(self, message: str) -> None:
        """Write ``message`` to ``stdout`` with serialised locking."""

        with self._console_lock:
            self._stdout_base.write(message + "\n")
            self._stdout_base.flush()

    async def _broadcast_message(self, message: str) -> None:
        """Send ``message`` to every connected session as a log entry."""

        formatted = f"{self._MESSAGE_PREFIX} {message}"
        active_sessions = [session for session in list(self._sessions) if session is not None]
        failures = 0
        for session in active_sessions:
            try:
                await session.send_log_message("info", formatted, logger="user-chat")
            except Exception:  # pragma: no cover - network/runtime variability
                failures += 1
                logger.exception("Failed to deliver user chat message to a session")
                self._sessions.discard(session)
        logger.info(
            "User chat message dispatched to %d sessions (%d failures): %s",
            len(active_sessions),
            failures,
            message,
        )
        self._announce_to_console(formatted)



def _serialize_result(result: CommandResult, *, timed_out: bool) -> dict[str, Any]:
    return {
        "command_id": result.request.command_id,
        "command": result.request.command,
        "working_directory": result.request.working_directory,
        "return_code": result.return_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "duration": result.duration,
        "timed_out": timed_out,
        "pty_allocated": result.pty_allocated,
    }


def _serialize_timeout(event: CommandTimeoutError) -> dict[str, Any]:
    assert event.event is not None
    return {
        "command_id": event.event.request.command_id,
        "command": event.event.request.command,
        "working_directory": event.event.request.working_directory,
        "return_code": event.event.return_code,
        "stdout": event.event.stdout,
        "stderr": event.event.stderr,
        "started_at": event.event.started_at.isoformat(),
        "finished_at": event.event.finished_at.isoformat(),
        "duration": event.event.duration,
        "timed_out": True,
        "pty_allocated": event.event.request.allocate_pty,
    }


def _serialize_event(event: CommandCompleteEvent, *, timed_out: bool) -> dict[str, Any]:
    result = CommandResult(
        request=event.request,
        stdout=event.stdout,
        stderr=event.stderr,
        return_code=event.return_code,
        started_at=event.started_at,
        finished_at=event.finished_at,
        pty_allocated=event.request.allocate_pty,
    )
    return _serialize_result(result, timed_out=timed_out)


def _resolve_signal(value: str | int | None) -> int:
    if value is None:
        return signal.SIGINT
    if isinstance(value, int):
        return value
    normalized = value.upper()
    if not normalized.startswith("SIG"):
        normalized = f"SIG{normalized}"
    try:
        return getattr(signal, normalized)
    except AttributeError as exc:  # pragma: no cover - defensive branch
        raise ValueError(f"Unknown signal: {value}") from exc


async def _emit_server_warning(
    ctx: Context[ServerSession, ApplicationState],
    state: ApplicationState,
    *,
    tool_name: str,
    base_message: str,
    exception: BaseException | None = None,
    details: dict[str, Any] | None = None,
) -> str:
    details = details or {}
    if exception is not None and base_message:
        message = f"{base_message}: {exception}"
    elif exception is not None:
        message = str(exception)
    else:
        message = base_message

    logger.warning("%s warning: %s", tool_name, message, exc_info=exception)
    try:
        await ctx.warning(message)
    except Exception:  # pragma: no cover - defensive logging
        logger.exception("Failed to deliver warning to client for %s", tool_name)

    await state.plugin_manager.emit_server_warning(
        ServerWarningEvent(
            tool_name=tool_name,
            message=message,
            details=details,
            exception=exception,
        )
    )
    return message


def _append_warning(payload: dict[str, Any], warning: str) -> dict[str, Any]:
    existing = payload.get("warnings")
    if isinstance(existing, (list, tuple, set)):
        warnings = [str(item) for item in existing if item]
    elif existing:
        warnings = [str(existing)]
    else:
        warnings = []
    warnings.append(warning)
    payload["warnings"] = warnings
    return payload


def _basic_command_failure(
    command: str,
    working_directory: Path | None,
    command_id: str | None,
    message: str,
) -> dict[str, Any]:
    finished = utcnow()
    cwd = str(working_directory) if working_directory is not None else ""
    return {
        "command_id": command_id or "",
        "command": command,
        "working_directory": cwd,
        "return_code": -1,
        "stdout": "",
        "stderr": message,
        "started_at": finished.isoformat(),
        "finished_at": finished.isoformat(),
        "duration": 0.0,
        "timed_out": False,
        "pty_allocated": False,
    }


def create_server(
    *,
    config: ServerConfig | None = None,
    plugin_manager: PluginManager | None = None,
) -> FastMCP:
    """Create a configured FastMCP server instance."""

    resolved_config = config or ServerConfig.from_env()
    manager = plugin_manager or GlobalPluginManager
    manager.refresh_exports()
    manager.set_console_echo_enabled(resolved_config.console_echo)

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[ApplicationState]:
        await manager.load_plugins_async(resolved_config.plugin_modules)
        executor = ShellCommandExecutor(resolved_config, manager)
        chat_bridge = UserChatBridge(
            plugin_manager=manager,
            console_echo=resolved_config.console_echo,
        )
        async with chat_bridge:
            state = ApplicationState(
                config=resolved_config,
                plugin_manager=manager,
                executor=executor,
                chat_bridge=chat_bridge,
            )
            try:
                yield state
            finally:
                pass

    server = FastMCP(
        name="mcp2term",
        instructions=(
            "Execute shell commands with streaming output for stdout and stderr. "
            "Use responsibly and prefer safe commands."
        ),
        lifespan=lifespan,
    )

    @server.tool(name="run_command", description="Execute a shell command with live stdout/stderr streaming.")
    async def run_command(  # type: ignore[no-redef]
        command: str,
        *,
        working_directory: str | None = None,
        environment: dict[str, str] | None = None,
        timeout: float | None = None,
        command_id: str | None = None,
        allocate_pty: bool | None = None,
        ctx: Context[ServerSession, ApplicationState],
    ) -> dict[str, Any]:
        target_path = Path(working_directory).expanduser().resolve() if working_directory else None
        state = ctx.request_context.lifespan_context
        state.chat_bridge.register_context(ctx)
        details = {
            "command": command,
            "command_id": command_id or "",
            "working_directory": str(target_path) if target_path else "",
            "allocate_pty": bool(allocate_pty),
        }
        try:
            result = await ctx.request_context.lifespan_context.executor.run(
                command,
                ctx=ctx,
                working_directory=target_path,
                environment=environment,
                timeout=timeout,
                command_id=command_id,
                allocate_pty=bool(allocate_pty),
            )
            return _serialize_result(result, timed_out=False)
        except CommandTimeoutError as exc:
            if exc.event is None:
                warning = await _emit_server_warning(
                    ctx,
                    state,
                    tool_name="run_command",
                    base_message=f"Command '{command}' timed out",
                    exception=exc,
                    details=details,
                )
                return _append_warning(
                    _basic_command_failure(command, target_path, command_id, str(exc)),
                    warning,
                )
            warning = await _emit_server_warning(
                ctx,
                state,
                tool_name="run_command",
                base_message=f"Command '{command}' timed out",
                exception=exc,
                details=details,
            )
            return _append_warning(_serialize_timeout(exc), warning)
        except CommandExecutionError as exc:
            warning = await _emit_server_warning(
                ctx,
                state,
                tool_name="run_command",
                base_message="Command execution failed",
                exception=exc,
                details=details,
            )
            if exc.event is not None:
                return _append_warning(_serialize_event(exc.event, timed_out=False), warning)
            return _append_warning(
                _basic_command_failure(command, target_path, command_id, str(exc)),
                warning,
            )
        except Exception as exc:
            warning = await _emit_server_warning(
                ctx,
                state,
                tool_name="run_command",
                base_message="Unexpected error while executing command",
                exception=exc,
                details=details,
            )
            return _append_warning(
                _basic_command_failure(command, target_path, command_id, str(exc)),
                warning,
            )

    @server.tool(name="cancel_command", description="Send a signal to a running command.")
    async def cancel_command(  # type: ignore[no-redef]
        command_id: str,
        *,
        signal_value: str | int | None = None,
        ctx: Context[ServerSession, ApplicationState],
    ) -> dict[str, Any]:
        state = ctx.request_context.lifespan_context
        state.chat_bridge.register_context(ctx)
        details = {"command_id": command_id, "signal_value": signal_value}
        try:
            resolved_signal = _resolve_signal(signal_value)
        except ValueError as exc:
            warning = await _emit_server_warning(
                ctx,
                state,
                tool_name="cancel_command",
                base_message="Invalid signal value",
                exception=exc,
                details=details,
            )
            return {
                "command_id": command_id,
                "signal": None,
                "signal_name": None,
                "delivered": False,
                "warnings": [warning],
            }

        try:
            delivered = await ctx.request_context.lifespan_context.executor.send_signal(
                command_id,
                resolved_signal,
            )
            try:
                signal_name = signal.Signals(resolved_signal).name
            except ValueError:  # pragma: no cover - non-standard signal
                signal_name = str(resolved_signal)
            response = {
                "command_id": command_id,
                "signal": resolved_signal,
                "signal_name": signal_name,
                "delivered": delivered,
            }
        except Exception as exc:
            warning = await _emit_server_warning(
                ctx,
                state,
                tool_name="cancel_command",
                base_message="Failed to send signal to command",
                exception=exc,
                details=details,
            )
            response = {
                "command_id": command_id,
                "signal": resolved_signal,
                "signal_name": None,
                "delivered": False,
                "warnings": [warning],
            }
        else:
            if not delivered:
                warning = await _emit_server_warning(
                    ctx,
                    state,
                    tool_name="cancel_command",
                    base_message=f"No running command matches {command_id}; signal not delivered",
                    exception=None,
                    details=details,
                )
                response = _append_warning(response, warning)
        return response

    @server.tool(name="send_stdin", description="Forward input to a running command's stdin pipe.")
    async def send_stdin(  # type: ignore[no-redef]
        command_id: str,
        data: str | None = None,
        *,
        eof: bool = False,
        ctx: Context[ServerSession, ApplicationState],
    ) -> dict[str, Any]:
        payload = data or ""
        state = ctx.request_context.lifespan_context
        state.chat_bridge.register_context(ctx)
        details = {"command_id": command_id, "data_length": len(payload), "eof": eof}
        try:
            accepted = await state.executor.send_stdin(
                command_id,
                payload,
                eof=eof,
            )
        except Exception as exc:
            warning = await _emit_server_warning(
                ctx,
                state,
                tool_name="send_stdin",
                base_message="Failed to forward stdin to command",
                exception=exc,
                details=details,
            )
            return {
                "command_id": command_id,
                "accepted": False,
                "eof": eof,
                "warnings": [warning],
            }
        response = {
            "command_id": command_id,
            "accepted": accepted,
            "eof": eof,
        }
        if not accepted:
            warning = await _emit_server_warning(
                ctx,
                state,
                tool_name="send_stdin",
                base_message="Command is not accepting additional stdin",
                exception=None,
                details=details,
            )
            response = _append_warning(response, warning)
        return response

    @server.tool(
        name="manage_file",
        description=(
            "Create, edit, and inspect files on the remote host including line-based operations."
        ),
    )
    async def manage_file(  # type: ignore[no-redef]
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
        anchor: str | None = None,
        anchor_use_regex: bool = False,
        anchor_ignore_case: bool = False,
        anchor_after: bool = False,
        anchor_occurrence: int | None = None,
        ctx: Context[ServerSession, ApplicationState],
    ) -> dict[str, Any]:
        state = ctx.request_context.lifespan_context
        state.chat_bridge.register_context(ctx)
        editor = FileEditor(state.config.working_directory)
        resolved_path = editor.resolve_path(path)

        request_arguments: dict[str, Any] = {
            "path": path,
            "encoding": encoding,
            "create_parents": create_parents,
            "overwrite": overwrite,
            "create_if_missing": create_if_missing,
            "resolved_path": str(resolved_path),
            "escape_profile": escape_profile,
            "follow_symlinks": follow_symlinks,
            "use_regex": use_regex,
            "ignore_case": ignore_case,
        }
        if content is not None:
            request_arguments["content"] = content
        if pattern is not None:
            request_arguments["pattern"] = pattern
        if line is not None:
            request_arguments["line"] = line
        if start_line is not None:
            request_arguments["start_line"] = start_line
        if end_line is not None:
            request_arguments["end_line"] = end_line
        if max_replacements is not None:
            request_arguments["max_replacements"] = max_replacements
        if anchor is not None:
            request_arguments["anchor"] = anchor
        if anchor_use_regex:
            request_arguments["anchor_use_regex"] = anchor_use_regex
        if anchor_ignore_case:
            request_arguments["anchor_ignore_case"] = anchor_ignore_case
        if anchor_after:
            request_arguments["anchor_after"] = anchor_after
        if anchor_occurrence is not None:
            request_arguments["anchor_occurrence"] = anchor_occurrence

        def _failure(message: str) -> FileOperationResult:
            return FileOperationResult(
                path=resolved_path,
                operation=operation,
                success=False,
                changed=False,
                encoding=encoding,
                message=message,
                escape_profile=escape_profile,
            )

        async def _notify_plugins(
            result: FileOperationResult,
            *,
            warning: str | None = None,
        ) -> None:
            await state.plugin_manager.emit_file_operation(
                FileOperationEvent(
                    raw_path=path,
                    operation=operation,
                    arguments=MappingProxyType(dict(request_arguments)),
                    result=result,
                    warning=warning,
                )
            )

        try:
            match operation:
                case "create":
                    result = editor.create_file(
                        path,
                        text=content,
                        overwrite=overwrite,
                        create_parents=create_parents,
                        encoding=encoding,
                        escape_profile=escape_profile,
                    )
                case "write":
                    result = editor.write_file(
                        path,
                        text=content or "",
                        create_parents=create_parents,
                        encoding=encoding,
                        escape_profile=escape_profile,
                    )
                case "append":
                    if content is None:
                        raise FileOperationError("Append operation requires content text")
                    result = editor.append_text(
                        path,
                        text=content,
                        encoding=encoding,
                        create_if_missing=create_if_missing,
                        escape_profile=escape_profile,
                    )
                case "prepend":
                    if content is None:
                        raise FileOperationError("Prepend operation requires content text")
                    result = editor.prepend_text(
                        path,
                        text=content,
                        encoding=encoding,
                        create_if_missing=create_if_missing,
                        escape_profile=escape_profile,
                    )
                case "insert":
                    if content is None:
                        raise FileOperationError("Insert operation requires content text")
                    if anchor is not None:
                        occurrence_value = anchor_occurrence or 1
                        result = editor.insert_lines_at_anchor(
                            path,
                            anchor_text=anchor,
                            text=content,
                            encoding=encoding,
                            after=anchor_after,
                            occurrence=occurrence_value,
                            use_regex=anchor_use_regex,
                            ignore_case=anchor_ignore_case,
                            escape_profile=escape_profile,
                        )
                    else:
                        if any(
                            [
                                anchor_use_regex,
                                anchor_ignore_case,
                                anchor_after,
                                anchor_occurrence is not None,
                            ]
                        ):
                            raise FileOperationError(
                                "Anchor modifiers cannot be used without anchor text",
                            )
                        if line is None:
                            raise FileOperationError("Insert operation requires a line number")
                        result = editor.insert_lines(
                            path,
                            line=line,
                            text=content,
                            encoding=encoding,
                            escape_profile=escape_profile,
                        )
                case "replace":
                    if start_line is None:
                        raise FileOperationError("Replace operation requires start_line")
                    effective_end = end_line or start_line
                    result = editor.replace_range(
                        path,
                        start_line=start_line,
                        end_line=effective_end,
                        text=content or "",
                        encoding=encoding,
                        escape_profile=escape_profile,
                    )
                case "delete":
                    if start_line is None:
                        raise FileOperationError("Delete operation requires start_line")
                    effective_end = end_line or start_line
                    result = editor.delete_range(
                        path,
                        start_line=start_line,
                        end_line=effective_end,
                        encoding=encoding,
                        escape_profile=escape_profile,
                    )
                case "print":
                    result = editor.read_lines(
                        path,
                        start_line=start_line,
                        end_line=end_line,
                        encoding=encoding,
                        escape_profile=escape_profile,
                    )
                case "locate":
                    if content is None:
                        raise FileOperationError("Locate operation requires content text")
                    result = editor.find_line_numbers(
                        path,
                        text=content,
                        encoding=encoding,
                        escape_profile=escape_profile,
                    )
                case "patch":
                    if content is None:
                        raise FileOperationError("Patch operation requires diff content")
                    result = editor.apply_patch(
                        path,
                        patch_text=content,
                        encoding=encoding,
                        escape_profile=escape_profile,
                    )
                case "stat":
                    result = editor.stat_file(
                        path,
                        encoding=encoding,
                        follow_symlinks=follow_symlinks,
                        escape_profile=escape_profile,
                    )
                case "substitute":
                    if pattern is None:
                        raise FileOperationError("Substitute operation requires a pattern")
                    if content is None:
                        raise FileOperationError("Substitute operation requires replacement text")
                    result = editor.substitute_text(
                        path,
                        pattern=pattern,
                        replacement=content,
                        encoding=encoding,
                        use_regex=use_regex,
                        ignore_case=ignore_case,
                        max_replacements=max_replacements,
                        escape_profile=escape_profile,
                    )
                case _:
                    raise FileOperationError(f"Unsupported file operation: {operation}")
        except FileOperationError as exc:
            warning = await _emit_server_warning(
                ctx,
                state,
                tool_name="manage_file",
                base_message=str(exc),
                exception=None,
                details={
                    "path": str(resolved_path),
                    "operation": operation,
                },
            )
            failure = _failure(str(exc))
            await _notify_plugins(failure, warning=warning)
            return _append_warning(failure.to_payload(), warning)
        except Exception as exc:  # pragma: no cover - defensive
            warning = await _emit_server_warning(
                ctx,
                state,
                tool_name="manage_file",
                base_message="Unexpected failure during file operation",
                exception=exc,
                details={
                    "path": str(resolved_path),
                    "operation": operation,
                },
            )
            failure = _failure(warning)
            await _notify_plugins(failure, warning=warning)
            return _append_warning(failure.to_payload(), warning)

        await _notify_plugins(result)
        return result.to_payload()

    return server
