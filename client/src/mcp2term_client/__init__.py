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
from .session import (
    CancelCommandResponse,
    CommandResponse,
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
]
