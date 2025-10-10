"""mcp2term package exposing MCP terminal server components."""

from .config import ServerConfig
from .files import FileEditor, FileLine, FileOperationError, FileOperationResult
from .ngrok import NgrokController, NgrokSettings, NgrokTunnel
from .plugin import (
    FileOperationEvent,
    FileOperationListener,
    GlobalPluginManager,
    PluginManager,
    PluginRegistry,
    ServerWarningEvent,
)
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
    "FileEditor",
    "FileLine",
    "FileOperationError",
    "FileOperationResult",
    "create_server",
    "CommandRequest",
    "CommandStartEvent",
    "CommandOutputChunk",
    "CommandCompleteEvent",
    "PluginManager",
    "PluginRegistry",
    "GlobalPluginManager",
    "FileOperationEvent",
    "FileOperationListener",
    "ServerWarningEvent",
    "NgrokController",
    "NgrokSettings",
    "NgrokTunnel",
]
