"""Factory for the MCP terminal server."""

from __future__ import annotations

import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

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


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplicationState:
    """Objects shared across MCP requests."""

    config: ServerConfig
    plugin_manager: PluginManager
    executor: ShellCommandExecutor


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
    }


def _serialize_event(event: CommandCompleteEvent, *, timed_out: bool) -> dict[str, Any]:
    result = CommandResult(
        request=event.request,
        stdout=event.stdout,
        stderr=event.stderr,
        return_code=event.return_code,
        started_at=event.started_at,
        finished_at=event.finished_at,
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
        state = ApplicationState(config=resolved_config, plugin_manager=manager, executor=executor)
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
        ctx: Context[ServerSession, ApplicationState],
    ) -> dict[str, Any]:
        target_path = Path(working_directory).expanduser().resolve() if working_directory else None
        state = ctx.request_context.lifespan_context
        details = {
            "command": command,
            "command_id": command_id or "",
            "working_directory": str(target_path) if target_path else "",
        }
        try:
            result = await ctx.request_context.lifespan_context.executor.run(
                command,
                ctx=ctx,
                working_directory=target_path,
                environment=environment,
                timeout=timeout,
                command_id=command_id,
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
