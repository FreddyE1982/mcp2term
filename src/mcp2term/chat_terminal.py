"""Retired auxiliary chat terminal interface.

This module intentionally performs no action. Historical deployments relied on
an out-of-process terminal window to broadcast operator messages, but the
modern in-band :class:`~mcp2term.server.UserChatBridge` now handles the
functionality directly. The module remains importable so that legacy entry
points keep functioning without raising ``ImportError``.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

LOGGER = logging.getLogger("mcp2term.chat_terminal")


@dataclass(slots=True)
class ChatTerminalResult:
    """Describe the outcome of executing the retired chat terminal entry point.

    Attributes
    ----------
    exit_code:
        Numeric process exit code to propagate back to the host shell.
    message:
        Human-readable explanation clarifying that the terminal has been
        decommissioned and that the server continues operating normally.
    """

    exit_code: int = 0
    message: str = (
        "The standalone chat terminal has been decommissioned. "
        "Use the console-integrated messaging workflow instead."
    )


def _configure_logging(verbose: bool) -> None:
    """Initialise the logging pipeline for backward compatibility."""

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="[%(levelname)s] %(message)s")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command line arguments accepted by the legacy entry point.

    The parser intentionally mirrors the historic CLI so that external
    automation invoking ``python -m mcp2term.chat_terminal`` keeps functioning
    without modification. The parameters are optional because the data is no
    longer required, but they remain recognised to avoid ``SystemExit`` errors
    when supplied by legacy scripts.
    """

    parser = argparse.ArgumentParser(
        description=(
            "This helper is a compatibility shim. The dedicated chat terminal "
            "is no longer required because messaging happens in the primary "
            "console."
        )
    )
    parser.add_argument("--address", help="Legacy argument retained for compatibility.")
    parser.add_argument("--port", type=int, help="Legacy argument retained for compatibility.")
    parser.add_argument("--auth-key", help="Legacy argument retained for compatibility.")
    parser.add_argument(
        "--history-header",
        default="[delivered]",
        help="Legacy argument retained for compatibility.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging for diagnostic investigations.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress informational messages emitted by this compatibility shim.",
    )
    return parser.parse_args(list(argv))


def _announce_retirement(result: ChatTerminalResult, *, quiet: bool) -> None:
    """Emit a single informational message explaining the decommissioning."""

    if quiet:
        LOGGER.debug("Chat terminal retirement notice suppressed by --quiet flag.")
        return
    LOGGER.info(result.message)


def main(argv: Iterable[str] | None = None) -> int:
    """Primary entry point retained for compatibility with old launch scripts."""

    parsed = _parse_args(tuple(argv) if argv is not None else tuple())
    _configure_logging(parsed.verbose)
    result = ChatTerminalResult()
    _announce_retirement(result, quiet=parsed.quiet)
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - manual execution only
    raise SystemExit(main())
