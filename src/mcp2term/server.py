"""Factory for the MCP terminal server."""

from __future__ import annotations

import logging
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import weakref
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from multiprocessing.connection import Connection, Listener
from types import MappingProxyType
from typing import Any

import anyio
from anyio.abc import TaskGroup as AnyIOTaskGroup

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from .chat_bridge import (
    ChatBridgeEnvelope,
    ENVELOPE_KIND_APPEND,
    ENVELOPE_KIND_MESSAGE,
    ENVELOPE_KIND_STOP,
)
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


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TerminalLaunchPlan:
    """Describe how to start the auxiliary terminal chat interface."""

    command: list[str]
    track_process: bool = True


class SystemTerminalLauncher:
    """Launch a new terminal window for the interactive chat console.

    The launcher inspects the current operating system and environment to
    select an appropriate terminal emulator. Operators can override the
    automatic detection by setting ``MCP2TERM_CHAT_TERMINAL`` to an explicit
    command. Setting the variable to ``disable`` (or similar synonyms) skips
    the auxiliary console entirely which mirrors the behaviour of the old
    PyQt bridge when no display server was available.
    """

    _DISABLE_VALUES = {"disable", "disabled", "off", "none", "false", "0"}

    def __init__(self, *, environment: Mapping[str, str] | None = None) -> None:
        env = dict(os.environ if environment is None else environment)
        self._environment = MappingProxyType(env)

    def prepare_plan(self, script_invocation: Sequence[str], *, title: str) -> TerminalLaunchPlan | None:
        """Return a launch plan or ``None`` when a terminal cannot be located."""

        override = self._environment.get("MCP2TERM_CHAT_TERMINAL")
        if override:
            normalized = override.strip()
            if normalized.lower() in self._DISABLE_VALUES:
                return None
            return TerminalLaunchPlan(command=shlex.split(override) + list(script_invocation))

        if sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
            return self._prepare_linux_plan(script_invocation, title)
        if sys.platform == "darwin":
            return self._prepare_macos_plan(script_invocation, title)
        if os.name == "nt":
            return self._prepare_windows_plan(script_invocation, title)
        return None

    def launch(
        self,
        plan: TerminalLaunchPlan,
        *,
        extra_environment: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[str] | None:
        """Execute ``plan`` and return the spawned process when trackable."""

        env = dict(os.environ)
        if extra_environment is not None:
            env.update(dict(extra_environment))

        popen_kwargs: dict[str, Any] = {"env": env, "close_fds": True}
        if os.name == "nt":  # pragma: no cover - platform specific branch
            creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            popen_kwargs["creationflags"] = creation_flags
        else:
            popen_kwargs["start_new_session"] = True

        try:
            process = subprocess.Popen(plan.command, **popen_kwargs)
        except FileNotFoundError:
            logger.exception("Unable to start chat terminal; command missing: %s", plan.command)
            return None
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Unexpected error while starting chat terminal: %s", plan.command)
            return None

        return process if plan.track_process else None

    def terminate(self, process: subprocess.Popen[str] | None) -> None:
        """Attempt to terminate the terminal process if still active."""

        if process is None:
            return
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except Exception:  # pragma: no cover - terminal already closed
            logger.debug("Terminal process already terminated during shutdown")

    def _prepare_linux_plan(
        self,
        script_invocation: Sequence[str],
        title: str,
    ) -> TerminalLaunchPlan | None:
        invocation = list(script_invocation)
        candidates = [
            ("x-terminal-emulator", ["-T", title, "-e"]),
            ("gnome-terminal", ["--title", title, "--"]),
            ("konsole", ["--new-tab", "-p", f"tabtitle={title}", "-e"]),
            ("kitty", ["--title", title]),
            ("alacritty", ["-t", title, "-e"]),
            ("wezterm", ["start", "--title", title, "--"]),
            ("xterm", ["-T", title, "-e"]),
        ]
        for executable, args in candidates:
            if shutil.which(executable):
                if args and args[-1] == "-e":
                    command = [executable, *args, *invocation]
                else:
                    command = [executable, *args, *invocation]
                return TerminalLaunchPlan(command=command)
        return None

    def _prepare_macos_plan(
        self,
        script_invocation: Sequence[str],
        title: str,
    ) -> TerminalLaunchPlan | None:
        command = " ".join(shlex.quote(part) for part in script_invocation)
        osa_lines = [
            "tell application \"Terminal\" to activate",
            f"tell application \"Terminal\" to do script \"{command}\"",
            f"tell application \"Terminal\" to set custom title of front window to \"{title}\"",
        ]
        return TerminalLaunchPlan(
            command=["osascript", *[item for line in osa_lines for item in ("-e", line)]],
            track_process=False,
        )

    def _prepare_windows_plan(
        self,
        script_invocation: Sequence[str],
        title: str,
    ) -> TerminalLaunchPlan | None:
        command = ["cmd.exe", "/c", "start", f"\"{title}\"", *script_invocation]
        return TerminalLaunchPlan(command=command, track_process=False)


@dataclass(slots=True)
class ApplicationState:
    """Objects shared across MCP requests."""

    config: ServerConfig
    plugin_manager: PluginManager
    executor: ShellCommandExecutor
    chat_bridge: "UserChatBridge"


class UserChatBridge:
    """Coordinate the auxiliary terminal console for server-side messaging.

    The bridge spawns a new terminal window hosting :mod:`mcp2term.chat_terminal`
    so administrators can broadcast messages to every connected MCP client. The
    helper process communicates with the main server via a
    :class:`multiprocessing.connection.Listener` socket, ensuring that messages
    travel through the same ordering guarantees as command output without
    resorting to GUI toolkits.
    """

    _MESSAGE_PREFIX = "[MESSAGE FROM USER. DO NOT IGNORE:]"
    _HISTORY_HEADER = "[delivered]"

    def __init__(
        self,
        *,
        plugin_manager: PluginManager,
        console_echo: bool,
        terminal_launcher: SystemTerminalLauncher | None = None,
    ) -> None:
        self._plugin_manager = plugin_manager
        self._console_echo = console_echo
        self._terminal_launcher = terminal_launcher or SystemTerminalLauncher()
        self._sessions: weakref.WeakSet[ServerSession] = weakref.WeakSet()
        self._task_group: AnyIOTaskGroup | None = None
        self._active = False
        self._pump_cancel_scope: anyio.CancelScope | None = None
        self._listener: Listener | None = None
        self._auth_key: bytes | None = None
        self._connection: Connection | None = None
        self._terminal_process: subprocess.Popen[str] | None = None

    @property
    def is_active(self) -> bool:
        """Return ``True`` when the chat bridge successfully started."""

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
        """Start background tasks and spawn the terminal console when possible."""

        try:
            plan, address = self._prepare_launch_plan()
        except Exception as exc:  # pragma: no cover - defensive startup logging
            logger.warning("User chat bridge inactive: %s", exc)
            self._cleanup_listener()
            self._plugin_manager.register_export("mcp2term.user_chat.bridge", self)
            return self

        if plan is None:
            logger.info("User chat bridge inactive: no compatible terminal command detected.")
            self._cleanup_listener()
            self._plugin_manager.register_export("mcp2term.user_chat.bridge", self)
            return self

        self._task_group = await anyio.create_task_group().__aenter__()
        try:
            self._task_group.start_soon(self._run_listener)
            self._terminal_process = self._terminal_launcher.launch(plan)
        except Exception:
            await self._shutdown_tasks()
            raise
        else:
            self._active = True
            self._plugin_manager.register_export("mcp2term.user_chat.bridge", self)
            logger.info(
                "User chat bridge initialised with terminal console bound to %s:%s.",
                address[0],
                address[1],
            )
        return self

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        """Shut down the terminal console and background listener."""

        await self._shutdown_tasks()

    async def _shutdown_tasks(self) -> None:
        """Terminate all background resources owned by the bridge."""

        if self._pump_cancel_scope is not None:
            self._pump_cancel_scope.cancel()

        await self._send_control_envelope(ChatBridgeEnvelope(kind=ENVELOPE_KIND_STOP))

        if self._task_group is not None:
            await self._task_group.__aexit__(None, None, None)
            self._task_group = None

        if self._terminal_process is not None:
            self._terminal_launcher.terminate(self._terminal_process)
            self._terminal_process = None

        self._cleanup_listener()

        self._plugin_manager.register_export("mcp2term.user_chat.bridge", None)
        self._active = False
        self._sessions = weakref.WeakSet()
        self._pump_cancel_scope = None
        self._connection = None
        self._auth_key = None

    def _cleanup_listener(self) -> None:
        """Close any open listener socket associated with the bridge."""

        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:  # pragma: no cover - listener already closed
                logger.debug("Chat bridge listener already closed during cleanup")
            self._listener = None

    def _prepare_launch_plan(self) -> tuple[TerminalLaunchPlan | None, tuple[str, int]]:
        """Create the listener and compute the terminal launch plan."""

        self._auth_key = secrets.token_bytes(32)
        self._listener = Listener(("127.0.0.1", 0), authkey=self._auth_key)
        address = self._listener.address
        if not isinstance(address, tuple) or len(address) != 2:
            raise RuntimeError("Listener provided an unexpected address format")
        host = str(address[0])
        port = int(address[1])
        script_invocation = [
            sys.executable,
            "-m",
            "mcp2term.chat_terminal",
            "--address",
            host,
            "--port",
            str(port),
            "--auth-key",
            self._auth_key.hex(),
            "--history-header",
            self._HISTORY_HEADER,
        ]
        plan = self._terminal_launcher.prepare_plan(script_invocation, title="mcp2term Chat Console")
        return plan, (host, port)

    async def _run_listener(self) -> None:
        """Accept connections from the auxiliary terminal and relay messages."""

        listener = self._listener
        if listener is None:
            return
        with anyio.CancelScope() as scope:
            self._pump_cancel_scope = scope
            try:
                connection = await anyio.to_thread.run_sync(listener.accept)
            except Exception:
                logger.exception("User chat bridge failed to accept terminal connection")
                return
            finally:
                try:
                    listener.close()
                except Exception:  # pragma: no cover - listener already closed
                    logger.debug("Chat bridge listener already closed after accept")
                self._listener = None

            self._connection = connection
            await self._send_control_envelope(
                ChatBridgeEnvelope(kind=ENVELOPE_KIND_APPEND, payload="Chat console connected."),
            )

            while True:
                try:
                    envelope = await anyio.to_thread.run_sync(connection.recv)
                except (EOFError, OSError):
                    logger.info("Chat console disconnected; stopping listener loop")
                    break
                if not isinstance(envelope, ChatBridgeEnvelope):
                    logger.debug("Ignoring unexpected payload from chat console: %r", envelope)
                    continue
                if envelope.kind == ENVELOPE_KIND_STOP:
                    break
                if envelope.kind == ENVELOPE_KIND_MESSAGE and envelope.payload:
                    await self._broadcast_message(envelope.payload)

            try:
                connection.close()
            except Exception:  # pragma: no cover - connection already closed
                logger.debug("Chat console connection already closed during shutdown")

            self._connection = None
            self._active = False
        self._pump_cancel_scope = None

    async def _send_control_envelope(self, envelope: ChatBridgeEnvelope) -> None:
        """Forward ``envelope`` to the terminal console if connected."""

        connection = self._connection
        if connection is None:
            return
        try:
            await anyio.to_thread.run_sync(connection.send, envelope)
        except (EOFError, OSError):
            logger.debug("Unable to deliver control envelope to chat console")

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
        if self._console_echo:
            print(formatted, flush=True)
        await self._send_control_envelope(
            ChatBridgeEnvelope(kind=ENVELOPE_KIND_APPEND, payload=formatted),
        )



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
        line: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        encoding: str = "utf-8",
        create_parents: bool = False,
        overwrite: bool = False,
        create_if_missing: bool = True,
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
        }
        if content is not None:
            request_arguments["content"] = content
        if line is not None:
            request_arguments["line"] = line
        if start_line is not None:
            request_arguments["start_line"] = start_line
        if end_line is not None:
            request_arguments["end_line"] = end_line

        def _failure(message: str) -> FileOperationResult:
            return FileOperationResult(
                path=resolved_path,
                operation=operation,
                success=False,
                changed=False,
                encoding=encoding,
                message=message,
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
                    )
                case "write":
                    result = editor.write_file(
                        path,
                        text=content or "",
                        create_parents=create_parents,
                        encoding=encoding,
                    )
                case "append":
                    if content is None:
                        raise FileOperationError("Append operation requires content text")
                    result = editor.append_text(
                        path,
                        text=content,
                        encoding=encoding,
                        create_if_missing=create_if_missing,
                    )
                case "insert":
                    if line is None:
                        raise FileOperationError("Insert operation requires a line number")
                    if content is None:
                        raise FileOperationError("Insert operation requires content text")
                    result = editor.insert_lines(
                        path,
                        line=line,
                        text=content,
                        encoding=encoding,
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
                    )
                case "print":
                    result = editor.read_lines(
                        path,
                        start_line=start_line,
                        end_line=end_line,
                        encoding=encoding,
                    )
                case "locate":
                    if content is None:
                        raise FileOperationError("Locate operation requires content text")
                    result = editor.find_line_numbers(
                        path,
                        text=content,
                        encoding=encoding,
                    )
                case "patch":
                    if content is None:
                        raise FileOperationError("Patch operation requires diff content")
                    result = editor.apply_patch(
                        path,
                        patch_text=content,
                        encoding=encoding,
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
