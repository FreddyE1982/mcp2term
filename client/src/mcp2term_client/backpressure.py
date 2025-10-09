"""Utilities for detecting and reporting backpressure conditions."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Callable


NoticeWriter = Callable[[str], None]


@dataclass(slots=True)
class BackpressureMonitor:
    """Track outstanding work items and emit notices when buffering occurs."""

    name: str
    threshold: int
    notice_writer: NoticeWriter
    recovery_threshold: int | None = None
    enter_message_factory: Callable[[int], str] | None = None
    exit_message_factory: Callable[[], str] | None = None
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _pending: int = field(default=0, init=False, repr=False)
    _in_backpressure: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.threshold <= 0:
            raise ValueError("threshold must be positive")
        if self.recovery_threshold is None:
            self.recovery_threshold = max(0, self.threshold // 2)
        if self.recovery_threshold < 0:
            raise ValueError("recovery_threshold must be non-negative")
        if self.exit_message_factory is None:
            self.exit_message_factory = lambda: f"[{self.name}] backlog cleared; operations resumed in real time."
        if self.enter_message_factory is None:
            self.enter_message_factory = (
                lambda count: (
                    f"[{self.name}] buffering {count} pending item(s); processing will resume shortly."
                )
            )

    def increment(self, amount: int = 1) -> int:
        """Increase the tracked count and emit a notice if threshold is crossed."""

        if amount <= 0:
            raise ValueError("amount must be positive")
        message: str | None = None
        with self._lock:
            self._pending += amount
            count = self._pending
            if not self._in_backpressure and count >= self.threshold:
                self._in_backpressure = True
                message = self.enter_message_factory(count)
        if message:
            self.notice_writer(message)
        return count

    def decrement(self, amount: int = 1) -> int:
        """Decrease the tracked count and emit a notice when backpressure clears."""

        if amount <= 0:
            raise ValueError("amount must be positive")
        message: str | None = None
        with self._lock:
            self._pending = max(0, self._pending - amount)
            count = self._pending
            if self._in_backpressure and count <= self.recovery_threshold:
                self._in_backpressure = False
                message = self.exit_message_factory()
        if message:
            self.notice_writer(message)
        return self._pending

    def reset(self) -> None:
        """Reset the monitor to an idle state, emitting notices if necessary."""

        message: str | None = None
        with self._lock:
            self._pending = 0
            if self._in_backpressure:
                self._in_backpressure = False
                message = self.exit_message_factory()
        if message:
            self.notice_writer(message)

    @property
    def pending(self) -> int:
        """Return the number of outstanding items currently tracked."""

        with self._lock:
            return self._pending

    @property
    def in_backpressure(self) -> bool:
        """Return whether the monitor is currently signalling backpressure."""

        with self._lock:
            return self._in_backpressure


__all__ = ["BackpressureMonitor", "NoticeWriter"]
