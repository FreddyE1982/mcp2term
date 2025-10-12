"""Interactive terminal console used for broadcasting user messages."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from datetime import datetime
from multiprocessing.connection import Client
from typing import Iterable

from .chat_bridge import (
    ChatBridgeEnvelope,
    ENVELOPE_KIND_APPEND,
    ENVELOPE_KIND_MESSAGE,
    ENVELOPE_KIND_STOP,
)

LOGGER = logging.getLogger("mcp2term.chat_terminal")


def _configure_logging(verbose: bool) -> None:
    """Configure the module level logging pipeline.

    Parameters
    ----------
    verbose:
        When ``True`` the log level is elevated to ``DEBUG`` so that the
        auxiliary console reports detailed diagnostic messages while the
        operator experiments with terminal integrations.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="[%(levelname)s] %(message)s")


def _format_timestamp() -> str:
    """Return the current local time formatted for human readable logs."""
    return datetime.now().strftime("%H:%M:%S")


def _listener_thread(connection: Client, *, history_header: str) -> None:
    """Listen for messages originating from the primary server process.

    The listener stays responsive while the foreground thread waits for
    operator input. Whenever the server echoes a message back into the
    console the listener prints the payload alongside a timestamp so the
    session retains an easily scannable audit trail.
    """
    try:
        while True:
            try:
                envelope = connection.recv()
            except (EOFError, OSError):
                print("\n[chat bridge] Connection closed by server.")
                break
            if not isinstance(envelope, ChatBridgeEnvelope):
                LOGGER.debug("Ignoring unexpected payload from server: %r", envelope)
                continue
            if envelope.kind == ENVELOPE_KIND_STOP:
                break
            if envelope.kind == ENVELOPE_KIND_APPEND and envelope.payload:
                print(f"\n{history_header} {_format_timestamp()}  {envelope.payload}")
                print("chat> ", end="", flush=True)
    finally:
        try:
            connection.close()
        except Exception:  # pragma: no cover - defensive close
            LOGGER.debug("Connection already closed while stopping listener thread")


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    """Parse command line arguments for the auxiliary chat console."""
    parser = argparse.ArgumentParser(
        description=(
            "Start an auxiliary terminal session for broadcasting messages to "
            "all connected MCP clients."
        )
    )
    parser.add_argument("--address", required=True, help="Hostname or IP address exposed by the server listener.")
    parser.add_argument("--port", required=True, type=int, help="TCP port exposed by the server listener.")
    parser.add_argument("--auth-key", required=True, help="Hex encoded authentication key for the listener.")
    parser.add_argument(
        "--history-header",
        default="[delivered]",
        help="Label printed before entries appended to the terminal history.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging for diagnostics.",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    """Entry point for the interactive chat console module."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    _configure_logging(args.verbose)

    try:
        auth_key = bytes.fromhex(args.auth_key)
    except ValueError:
        print("Authentication key must be valid hexadecimal", file=sys.stderr)
        return 2

    address = (args.address, int(args.port))

    try:
        connection = Client(address, authkey=auth_key)
    except Exception as exc:  # pragma: no cover - network environment dependent
        print(f"Unable to connect to chat bridge at {address}: {exc}", file=sys.stderr)
        return 1

    listener = threading.Thread(
        target=_listener_thread,
        args=(connection,),
        kwargs={"history_header": args.history_header},
        daemon=True,
    )
    listener.start()

    print("mcp2term user chat console")
    print("Type a message and press Enter to broadcast it to all clients.")
    print("Type '/exit' or '/quit' to close this console.")

    try:
        while True:
            try:
                user_input = input("chat> ").strip()
            except EOFError:
                print("\nInput stream closed. Exiting chat console.")
                break
            if not user_input:
                continue
            if user_input in {"/exit", "/quit"}:
                break
            envelope = ChatBridgeEnvelope(kind=ENVELOPE_KIND_MESSAGE, payload=user_input)
            try:
                connection.send(envelope)
            except (EOFError, OSError):
                print("\nConnection to server was lost while sending message.")
                break
            print(f"[{_format_timestamp()}] queued: {user_input}")
    finally:
        try:
            connection.send(ChatBridgeEnvelope(kind=ENVELOPE_KIND_STOP))
        except Exception:  # pragma: no cover - defensive send during shutdown
            LOGGER.debug("Unable to notify server about chat console shutdown")
        try:
            connection.close()
        except Exception:  # pragma: no cover - defensive close
            LOGGER.debug("Connection already closed on shutdown")
        listener.join(timeout=1)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual execution only
    raise SystemExit(main())
