"""Streaming primitives for command execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


@dataclass(slots=True)
class CommandRequest:
    """Represents a command execution request."""

    command: str
    working_directory: str
    environment: dict[str, str]
    timeout: float | None


@dataclass(slots=True)
class CommandStartEvent:
    """Event emitted when a command begins execution."""

    request: CommandRequest
    started_at: datetime


@dataclass(slots=True)
class CommandOutputChunk:
    """Chunk of output emitted by a running command."""

    request: CommandRequest
    timestamp: datetime
    stream: Literal["stdout", "stderr"]
    data: str


@dataclass(slots=True)
class CommandCompleteEvent:
    """Event emitted when a command completes."""

    request: CommandRequest
    started_at: datetime
    finished_at: datetime
    return_code: int
    stdout: str
    stderr: str
    duration: float


class InMemoryStreamRecorder:
    """Utility sink capturing streamed output for introspection and testing."""

    def __init__(self) -> None:
        self.chunks: list[CommandOutputChunk] = []
        self.start_event: CommandStartEvent | None = None
        self.complete_event: CommandCompleteEvent | None = None
        self._lock = asyncio.Lock()

    async def on_command_start(self, event: CommandStartEvent) -> None:
        async with self._lock:
            self.start_event = event

    async def on_command_stdout(self, event: CommandOutputChunk) -> None:
        async with self._lock:
            self.chunks.append(event)

    async def on_command_stderr(self, event: CommandOutputChunk) -> None:
        async with self._lock:
            self.chunks.append(event)

    async def on_command_complete(self, event: CommandCompleteEvent) -> None:
        async with self._lock:
            self.complete_event = event


def utcnow() -> datetime:
    """Return timezone-aware UTC now."""

    return datetime.now(timezone.utc)
