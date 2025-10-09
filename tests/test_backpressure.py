import time

import pytest

from mcp2term_client.backpressure import BackpressureMonitor
from mcp2term_client.session import LogMessage, LogStreamer


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_backpressure_monitor_emits_notices(use_real_dependencies: bool) -> None:
    messages: list[str] = []
    monitor = BackpressureMonitor(
        name="test-monitor",
        threshold=2,
        recovery_threshold=0,
        notice_writer=messages.append,
        enter_message_factory=lambda count: f"enter-{count}",
        exit_message_factory=lambda: "exit",
    )

    monitor.increment()
    assert messages == []

    monitor.increment()
    assert messages == ["enter-2"]
    assert monitor.in_backpressure is True

    monitor.decrement()
    assert monitor.in_backpressure is True

    monitor.decrement()
    assert monitor.in_backpressure is False
    assert messages == ["enter-2", "exit"]
    assert monitor.pending == 0


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_log_streamer_reports_buffering(use_real_dependencies: bool, capsys) -> None:
    notices: list[str] = []
    monitor = BackpressureMonitor(
        name="stream-monitor",
        threshold=2,
        recovery_threshold=0,
        notice_writer=notices.append,
        enter_message_factory=lambda count: f"enter-{count}",
        exit_message_factory=lambda: "exit",
    )
    streamer = LogStreamer(monitor=monitor, notice_writer=notices.append)

    try:
        streamer.submit(LogMessage(level="INFO", text="first"))
        streamer.submit(LogMessage(level="INFO", text="second"))
        time.sleep(0.1)
    finally:
        streamer.stop()

    captured = capsys.readouterr()
    assert "first" in captured.out
    assert "second" in captured.out
    assert "enter-2" in notices
    assert "exit" in notices
