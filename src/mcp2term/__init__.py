"""mcp2term package exposing MCP terminal server components."""

from .config import ServerConfig
from .plugin import GlobalPluginManager, PluginManager, PluginRegistry
from .server import create_server
from .shell import ShellCommandExecutor
from .streaming import (
    CommandCompleteEvent,
    CommandOutputChunk,
    CommandRequest,
    CommandStartEvent,
)

__all__ = [
    "ServerConfig",
    "ShellCommandExecutor",
    "create_server",
    "CommandRequest",
    "CommandStartEvent",
    "CommandOutputChunk",
    "CommandCompleteEvent",
    "PluginManager",
    "PluginRegistry",
    "GlobalPluginManager",
]
