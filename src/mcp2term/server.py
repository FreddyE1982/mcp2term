"""Factory for the MCP terminal server."""

from __future__ import annotations

import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from .config import ServerConfig
from .plugin import GlobalPluginManager, PluginManager
from .shell import CommandExecutionError, CommandResult, CommandTimeoutError, ShellCommandExecutor


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
                raise
            return _serialize_timeout(exc)
        except CommandExecutionError as exc:
            raise RuntimeError(str(exc)) from exc

    @server.tool(name="cancel_command", description="Send a signal to a running command.")
    async def cancel_command(  # type: ignore[no-redef]
        command_id: str,
        *,
        signal_value: str | int | None = None,
        ctx: Context[ServerSession, ApplicationState],
    ) -> dict[str, Any]:
        try:
            resolved_signal = _resolve_signal(signal_value)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

        delivered = await ctx.request_context.lifespan_context.executor.send_signal(
            command_id,
            resolved_signal,
        )
        try:
            signal_name = signal.Signals(resolved_signal).name
        except ValueError:  # pragma: no cover - non-standard signal
            signal_name = str(resolved_signal)
        return {
            "command_id": command_id,
            "signal": resolved_signal,
            "signal_name": signal_name,
            "delivered": delivered,
        }

    @server.tool(name="send_stdin", description="Forward input to a running command's stdin pipe.")
    async def send_stdin(  # type: ignore[no-redef]
        command_id: str,
        data: str | None = None,
        *,
        eof: bool = False,
        ctx: Context[ServerSession, ApplicationState],
    ) -> dict[str, Any]:
        payload = data or ""
        accepted = await ctx.request_context.lifespan_context.executor.send_stdin(
            command_id,
            payload,
            eof=eof,
        )
        return {
            "command_id": command_id,
            "accepted": accepted,
            "eof": eof,
        }

    return server
