"""Command line interface for running the MCP terminal server."""

from __future__ import annotations

import argparse
import logging

from .config import ServerConfig
from .plugin import GlobalPluginManager
from .server import create_server

logger = logging.getLogger(__name__)


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
    parser.add_argument(
        "--disable-ngrok",
        action="store_true",
        help="Disable automatic ngrok tunneling even when transports support it.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    config = ServerConfig.from_env()
    if args.disable_ngrok:
        config.ngrok.enabled = False
    server = create_server(config=config)

    GlobalPluginManager.register_export("mcp2term.ngrok.controller", None)
    GlobalPluginManager.register_export("mcp2term.ngrok.tunnel", None)

    ngrok_controller = None
    if config.ngrok.is_enabled_for(args.transport):
        from .ngrok import NgrokController

        ngrok_controller = NgrokController(config.ngrok)
        GlobalPluginManager.register_export("mcp2term.ngrok.controller", ngrok_controller)
        labels = [f"transport={args.transport}"]
        if args.mount_path:
            labels.append(f"mount={args.mount_path}")
        try:
            tunnel = ngrok_controller.start(
                host=server.settings.host,
                port=server.settings.port,
                labels=labels,
            )
        except Exception:
            GlobalPluginManager.register_export("mcp2term.ngrok.controller", None)
            GlobalPluginManager.register_export("mcp2term.ngrok.tunnel", None)
            raise
        else:
            GlobalPluginManager.register_export("mcp2term.ngrok.tunnel", tunnel)
            logger.info("ngrok public URL: %s", tunnel.public_url)
    else:
        if not config.ngrok.enabled:
            logger.info("ngrok tunneling disabled via configuration")
        else:
            logger.debug("ngrok tunneling skipped for transport %s", args.transport)

    try:
        server.run(transport=args.transport, mount_path=args.mount_path)
    finally:
        if ngrok_controller:
            ngrok_controller.stop()
            GlobalPluginManager.register_export("mcp2term.ngrok.tunnel", ngrok_controller.tunnel)


if __name__ == "__main__":
    main()
