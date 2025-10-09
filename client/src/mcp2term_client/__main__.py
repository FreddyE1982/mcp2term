"""Command line entry point for the mcp2term xonsh client."""

from __future__ import annotations

import argparse
import sys
from contextlib import suppress

from .install import ensure_xonsh_installed
from .session import RemoteMcpSession
from .shell import RemoteCommandProcessor, XonshShellRunner
from .state import RemoteShellState


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Connect to an mcp2term server with an interactive xonsh shell")
    parser.add_argument("--url", required=True, help="Ngrok or Streamable HTTP URL of the remote MCP server")
    parser.add_argument("--timeout", type=float, default=None, help="Default command timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    ensure_xonsh_installed()

    session = RemoteMcpSession(args.url, default_timeout=args.timeout)
    session.start()
    try:
        initial_cwd = session.resolve_working_directory()
    except Exception as exc:  # pragma: no cover - defensive startup
        session.close()
        raise SystemExit(f"Failed to initialize remote session: {exc}") from exc

    state = RemoteShellState(cwd=initial_cwd)
    processor = RemoteCommandProcessor(session=session, state=state)
    runner = XonshShellRunner(processor, url=session.endpoint_url)

    try:
        runner.run()
    finally:
        with suppress(Exception):
            session.close()


if __name__ == "__main__":
    main(sys.argv[1:])
