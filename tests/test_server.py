"""Tests for the MCP server factory and plugin exposure."""

import asyncio
import io
import os
import threading
from pathlib import Path

import pytest

from mcp2term.config import ServerConfig
from mcp2term.files import FileOperationResult
from mcp2term.plugin import (
    FileOperationEvent,
    PluginManager,
    PluginRegistry,
    ServerWarningEvent,
)
from mcp2term.server import ConsoleStreamProxy, UserChatBridge, create_server


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_server_registers_run_command_tool(use_real_dependencies: bool) -> None:
    server = create_server(config=ServerConfig())
    tools = asyncio.run(server.list_tools())
    tool_names = {tool.name for tool in tools}
    assert "run_command" in tool_names
    assert "cancel_command" in tool_names
    assert "manage_file" in tool_names


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_plugin_manager_exports_include_shell_executor(use_real_dependencies: bool) -> None:
    manager = PluginManager()
    manager.refresh_exports()
    exported_names = set(manager.exports)
    assert any(name.endswith("ShellCommandExecutor") for name in exported_names)
    assert any(name.endswith("BackpressureMonitor") for name in exported_names)


class WarningRecorder:
    def __init__(self) -> None:
        self.events: list[ServerWarningEvent] = []

    async def on_server_warning(self, event: ServerWarningEvent) -> None:
        self.events.append(event)


class FileOperationRecorder:
    def __init__(self) -> None:
        self.events: list[FileOperationEvent] = []

    async def on_file_operation(self, event: FileOperationEvent) -> None:
        self.events.append(event)


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_console_stream_proxy_buffers_and_flushes(use_real_dependencies: bool) -> None:
    stream = io.StringIO()
    lock = threading.RLock()
    proxy = ConsoleStreamProxy(name="stdout", underlying=stream, lock=lock)

    assert proxy.name == "stdout"
    proxy.write("alpha")
    assert stream.getvalue() == "alpha"

    proxy.set_paused(True)
    proxy.write("beta")
    proxy.writelines(["-", "gamma", "\n"])

    assert proxy.is_paused()
    assert stream.getvalue() == "alpha"

    proxy.flush()
    assert stream.getvalue() == "alpha"

    proxy.set_paused(False)
    assert not proxy.is_paused()
    assert stream.getvalue() == "alphabetagamma\n"

    proxy.write("delta")
    proxy.flush()
    assert stream.getvalue().endswith("gamma\ndelta")


def test_plugin_manager_emits_warning_event() -> None:
    manager = PluginManager()
    recorder = WarningRecorder()
    PluginRegistry(manager).register_warning_listener(recorder)

    asyncio.run(
        manager.emit_server_warning(
            ServerWarningEvent(
                tool_name="run_command",
                message="failure",
                details={"command": "echo"},
                exception=None,
            )
        )
    )

    assert recorder.events
    assert recorder.events[0].tool_name == "run_command"
    assert recorder.events[0].message == "failure"


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_plugin_manager_emits_file_operation_event(tmp_path: Path, use_real_dependencies: bool) -> None:
    manager = PluginManager()
    recorder = FileOperationRecorder()
    PluginRegistry(manager).register_file_operation_listener(recorder)

    result = FileOperationResult(
        path=tmp_path / "demo.txt",
        operation="create",
        success=True,
        changed=True,
        encoding="utf-8",
        message="created",
        content="hello",
    )
    event = FileOperationEvent(
        raw_path="demo.txt",
        operation="create",
        arguments={"path": "demo.txt", "encoding": "utf-8"},
        result=result,
    )

    asyncio.run(manager.emit_file_operation(event))

    assert len(recorder.events) == 1
    recorded = recorder.events[0]
    assert recorded.result is result
    assert recorded.result.encoding == "utf-8"
    assert recorded.raw_path == "demo.txt"
    assert recorded.arguments["path"] == "demo.txt"


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_chat_bridge_inactive_when_terminal_disabled(use_real_dependencies: bool) -> None:
    manager = PluginManager()
    original = os.environ.get("MCP2TERM_CHAT_TERMINAL")
    os.environ["MCP2TERM_CHAT_TERMINAL"] = "disabled"

    async def _exercise_bridge() -> None:
        async with UserChatBridge(plugin_manager=manager, console_echo=False) as bridge:
            assert not bridge.is_active

    try:
        asyncio.run(_exercise_bridge())
    finally:
        if original is None:
            os.environ.pop("MCP2TERM_CHAT_TERMINAL", None)
        else:
            os.environ["MCP2TERM_CHAT_TERMINAL"] = original


