"""Command line interface for running the MCP terminal server."""

from __future__ import annotations

import argparse
import logging

from .config import ServerConfig
from .server import create_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the mcp2term MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport mechanism to expose. Defaults to stdio.",
    )
    parser.add_argument(
        "--mount-path",
        default=None,
        help="Custom mount path for HTTP transports.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    config = ServerConfig.from_env()
    server = create_server(config=config)
    server.run(transport=args.transport, mount_path=args.mount_path)


if __name__ == "__main__":
    main()
