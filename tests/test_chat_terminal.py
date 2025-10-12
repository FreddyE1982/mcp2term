"""Validation suite for the retired chat terminal compatibility shim."""

from __future__ import annotations

import logging

import pytest

from mcp2term import chat_terminal


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_chat_terminal_main_reports_retirement(caplog, use_real_dependencies: bool) -> None:
    """Ensure running the legacy entry point reports a graceful no-op."""

    with caplog.at_level(logging.INFO, logger="mcp2term.chat_terminal"):
        exit_code = chat_terminal.main([])

    assert exit_code == 0
    assert any("decommissioned" in record.message for record in caplog.records)
