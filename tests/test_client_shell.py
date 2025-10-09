"""Tests for the mcp2term client shell integrations."""

from __future__ import annotations

from typing import Callable

import pytest

from mcp2term_client.shell import _subscribe_event


class _LegacyEvent:
    """Legacy xonsh event style exposing connect/disconnect methods."""

    def __init__(self) -> None:
        self.connected: list[Callable[..., object]] = []

    def connect(self, handler):
        self.connected.append(handler)
        return handler

    def disconnect(self, handler):
        self.connected.remove(handler)

    def fire(self, **kwargs):
        for handler in list(self.connected):
            handler(**kwargs)


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_subscribe_event_supports_legacy_and_modern_events(
    use_real_dependencies: bool,
) -> None:
    calls: list[str] = []

    def handler(*, cmd: str, **_: object) -> str:
        calls.append(cmd)
        return "transformed"

    if use_real_dependencies:
        from xonsh.built_ins import XSH
        from xonsh.events import events
        from xonsh.main import setup
        from xonsh.shell import transform_command

        setup(shell_type="none")
        event = events.on_transform_command
        unsubscribe = _subscribe_event(event, handler)

        try:
            result = transform_command("echo hello")
            assert result == "transformed"
            assert calls[0] == "echo hello"
            assert calls[-1] == "transformed"
        finally:
            unsubscribe()
            assert handler not in event
            XSH.unload()
    else:
        event = _LegacyEvent()
        unsubscribe = _subscribe_event(event, handler)

        try:
            event.fire(cmd="legacy")
            assert calls == ["legacy"]
        finally:
            unsubscribe()
            assert handler not in event.connected
