"""Shell command execution with streaming for MCP."""

from __future__ import annotations

import asyncio
from asyncio.subprocess import Process
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Mapping

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


@dataclass(slots=True)
class CommandResult:
    """Represents the result of executing a command."""

    request: CommandRequest
    stdout: str
    stderr: str
    return_code: int
    started_at: datetime
    finished_at: datetime

    @property
    def duration(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


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


class ShellCommandExecutor:
    """Execute shell commands with streaming output reporting."""

    def __init__(self, config: ServerConfig, plugin_manager: PluginManager) -> None:
        self.config = config
        self.plugin_manager = plugin_manager

    async def run(
        self,
        command: str,
        *,
        ctx: Context | None = None,
        working_directory: Path | None = None,
        environment: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        env = self.config.build_environment()
        if environment:
            env.update({str(key): str(value) for key, value in environment.items()})
        cwd = str((working_directory or self.config.working_directory).expanduser().resolve())
        timeout_value = timeout if timeout is not None else self.config.command_timeout
        request = CommandRequest(
            command=command,
            working_directory=cwd,
            environment=dict(env),
            timeout=timeout_value,
        )
        started_at = utcnow()
        await self.plugin_manager.emit_command_start(CommandStartEvent(request=request, started_at=started_at))
        emitter = _ContextEmitter(ctx, self.plugin_manager, request)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                executable=self.config.shell_path,
            )
        except FileNotFoundError as exc:  # pragma: no cover - depends on environment
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

        stdout_task = asyncio.create_task(self._consume_stream(process.stdout, emitter.emit_stdout))
        stderr_task = asyncio.create_task(self._consume_stream(process.stderr, emitter.emit_stderr))

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

        stdout_text, stderr_text = await asyncio.gather(stdout_task, stderr_task)
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
        )

    async def _consume_stream(
        self,
        stream: asyncio.StreamReader | None,
        emitter: Callable[[str], Awaitable[None]],
    ) -> str:
        if stream is None:
            return ""
        chunks: list[str] = []
        try:
            while True:
                data = await stream.readline()
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                chunks.append(text)
                await emitter(text)
        except asyncio.CancelledError:
            raise
        return "".join(chunks)
