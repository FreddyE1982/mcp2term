"""Input reader utilities for interactive remote sessions."""

from __future__ import annotations

import os
import queue
import selectors
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Protocol


@dataclass(slots=True)
class InputChunk:
    """Represents a unit of user input to forward to the remote command."""

    data: str
    eof: bool = False


class InputReader(Protocol):
    """Protocol describing interactive input sources."""

    def read(self, timeout: float) -> Iterable[InputChunk]:
        """Return any available input chunks within ``timeout`` seconds."""

    def close(self) -> None:
        """Release resources held by the reader."""


class TerminalInputReader:
    """Read interactive input from ``sys.stdin`` using platform-appropriate APIs."""

    def __init__(self) -> None:
        self._isatty = sys.stdin.isatty()
        self._selector: selectors.BaseSelector | None = None
        if self._isatty and os.name != "nt":
            self._selector = selectors.DefaultSelector()
            self._selector.register(sys.stdin, selectors.EVENT_READ)

    def read(self, timeout: float) -> Iterable[InputChunk]:
        if not self._isatty:
            time.sleep(max(0.0, timeout))
            return []
        if os.name == "nt":
            return list(self._read_windows(timeout))
        return list(self._read_posix(timeout))

    def close(self) -> None:
        if self._selector is not None:
            try:
                self._selector.unregister(sys.stdin)
            except Exception:
                pass
            self._selector.close()
            self._selector = None

    def _read_posix(self, timeout: float) -> Iterable[InputChunk]:
        assert self._selector is not None
        events = self._selector.select(timeout)
        if not events:
            return []
        line = sys.stdin.readline()
        if line == "":
            return [InputChunk(data="", eof=True)]
        return [InputChunk(data=line, eof=False)]

    def _read_windows(self, timeout: float) -> Iterable[InputChunk]:
        try:
            import msvcrt  # type: ignore[import-not-found]
        except ModuleNotFoundError:  # pragma: no cover - defensive fallback
            time.sleep(max(0.0, timeout))
            return []

        deadline = time.monotonic() + max(0.0, timeout)
        buffer: list[str] = []
        while time.monotonic() < deadline:
            if msvcrt.kbhit():  # pragma: no cover - platform specific
                ch = msvcrt.getwche()
                if ch in {"\r", "\n"}:
                    buffer.append("\n")
                    break
                if ch == "\x1a":  # Ctrl+Z EOF
                    return [InputChunk(data="", eof=True)]
                buffer.append(ch)
            else:
                time.sleep(0.01)
        if not buffer:
            return []
        return [InputChunk(data="".join(buffer), eof=False)]


class QueueInputReader:
    """Input reader backed by a ``queue.Queue`` of :class:`InputChunk` objects."""

    def __init__(self, source: "queue.Queue[InputChunk]") -> None:
        self._source = source

    def read(self, timeout: float) -> Iterable[InputChunk]:
        deadline = time.monotonic() + max(0.0, timeout)
        chunks: list[InputChunk] = []
        remaining = deadline - time.monotonic()
        try:
            chunk = self._source.get(timeout=max(0.0, remaining))
        except queue.Empty:
            return []
        chunks.append(chunk)
        while True:
            try:
                chunks.append(self._source.get_nowait())
            except queue.Empty:
                break
        return chunks

    def close(self) -> None:
        return


__all__ = [
    "InputChunk",
    "InputReader",
    "QueueInputReader",
    "TerminalInputReader",
]
