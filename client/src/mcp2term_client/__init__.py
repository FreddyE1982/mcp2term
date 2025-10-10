"""Xonsh-based client for the mcp2term MCP server."""

from .backpressure import BackpressureMonitor
from .input import InputChunk, InputReader, QueueInputReader, TerminalInputReader
from .intro import (
    IntroContext,
    IntroSection,
    IntroSectionProvider,
    iter_intro_sections,
    register_intro_section_provider,
    render_intro_message,
)
from .file_command import (
    FileCommandError,
    FileCommandHelp,
    FileCommandParseError,
    ManageFileCommand,
    parse_manage_file_command,
    render_manage_file_help,
)
from .session import (
    CancelCommandResponse,
    CommandResponse,
    FileOperationLine,
    FileOperationResponse,
    SendInputResponse,
    RemoteMcpSession,
    RemoteMcpSessionError,
)
from .shell import RemoteCommandProcessor, XonshShellRunner
from .state import RemoteShellState

__all__ = [
    "BackpressureMonitor",
    "CancelCommandResponse",
    "CommandResponse",
    "FileOperationLine",
    "FileOperationResponse",
    "SendInputResponse",
    "InputChunk",
    "InputReader",
    "IntroContext",
    "IntroSection",
    "IntroSectionProvider",
    "QueueInputReader",
    "register_intro_section_provider",
    "render_intro_message",
    "iter_intro_sections",
    "TerminalInputReader",
    "RemoteCommandProcessor",
    "RemoteMcpSession",
    "RemoteMcpSessionError",
    "RemoteShellState",
    "XonshShellRunner",
    "FileCommandError",
    "FileCommandHelp",
    "FileCommandParseError",
    "ManageFileCommand",
    "parse_manage_file_command",
    "render_manage_file_help",
]
