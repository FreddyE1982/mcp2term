"""Shared primitives for coordinating interactive user chat bridges."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ChatBridgeEnvelope:
    """Container for inter-process chat bridge messages.

    The :class:`~multiprocessing.connection.Connection` API transparently
    serialises dataclasses, so the envelope provides a convenient, typed
    structure that both the server and the auxiliary terminal console can rely
    upon when exchanging control and message payloads.
    """

    kind: str
    payload: str | None = None


ENVELOPE_KIND_MESSAGE = "message"
ENVELOPE_KIND_STOP = "stop"
ENVELOPE_KIND_SHUTDOWN = "shutdown"
ENVELOPE_KIND_APPEND = "append"
