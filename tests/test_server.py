"""Tests for the MCP server factory and plugin exposure."""

import asyncio

import pytest

from mcp2term.config import ServerConfig
from mcp2term.plugin import PluginManager, PluginRegistry, ServerWarningEvent
from mcp2term.server import create_server


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_server_registers_run_command_tool(use_real_dependencies: bool) -> None:
    server = create_server(config=ServerConfig())
    tools = asyncio.run(server.list_tools())
    tool_names = {tool.name for tool in tools}
    assert "run_command" in tool_names
    assert "cancel_command" in tool_names


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
