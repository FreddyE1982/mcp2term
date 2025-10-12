"""Parsing utilities for the client-side ``filetool`` command."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, TextIO

__all__ = [
    "FileCommandError",
    "FileCommandHelp",
    "FileCommandParseError",
    "ManageFileCommand",
    "EscapeProfileContext",
    "list_escape_profiles",
    "parse_manage_file_command",
    "register_escape_profile",
    "render_manage_file_help",
]


_ALLOWED_OPERATIONS = (
    "append",
    "create",
    "delete",
    "insert",
    "locate",
    "patch",
    "print",
    "stat",
    "replace",
    "write",
)

_INLINE_CONTENT_OPERATIONS = frozenset({
    "append",
    "create",
    "insert",
    "patch",
    "replace",
    "write",
})

_ESCAPE_SEQUENCE_PATTERN = re.compile(r"\\([nrt0])")


def _decode_inline_escape_sequences(text: str) -> str:
    """Return ``text`` with common escape sequences expanded.

    The decoding intentionally focuses on the sequences most frequently used when
    authors need to express multi-line payloads from single-line shells. Only the
    escapes recognised by :class:`_ESCAPE_SEQUENCE_PATTERN` are expanded so that
    other backslash-prefixed values remain untouched (for example the ``\\`` that
    prefixes ``"\\ No newline at end of file"`` lines in unified diffs).
    """

    def _replace(match: re.Match[str]) -> str:
        mapping = {
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "0": "\0",
        }
        return mapping.get(match.group(1), match.group(0))

    return _ESCAPE_SEQUENCE_PATTERN.sub(_replace, text)


def _should_decode_inline_escape_sequences(text: str) -> bool:
    """Return ``True`` when ``text`` should be interpreted for inline escapes."""

    if "\n" in text or "\r" in text:
        # Real newlines are already present so the caller was able to supply
        # structured content directly; avoid rewriting the payload.
        return False
    return _ESCAPE_SEQUENCE_PATTERN.search(text) is not None


@dataclass(slots=True)
class EscapeProfileContext:
    """Context describing when an escape profile is being applied."""

    operation: str
    namespace: argparse.Namespace


EscapeProfileHandler = Callable[[str, EscapeProfileContext], str]


_ESCAPE_PROFILE_HANDLERS: dict[str, EscapeProfileHandler] = {}


def register_escape_profile(
    name: str,
    handler: EscapeProfileHandler,
    *,
    replace: bool = False,
) -> None:
    """Register a new inline escape decoding profile.

    Parameters
    ----------
    name:
        Human readable identifier referenced by the ``--escape-profile`` flag.
    handler:
        Callable responsible for transforming inline content based on the
        provided :class:`EscapeProfileContext`.
    replace:
        When ``True`` an existing profile with the same name is overwritten.

    Raises
    ------
    ValueError
        Raised when ``name`` is empty/whitespace only, ``handler`` is not
        callable, or the profile already exists without ``replace`` enabled.
    """

    if not callable(handler):  # pragma: no cover - defensive guard
        raise ValueError("Escape profile handler must be callable")
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("Escape profile name must not be empty")
    if not replace and normalized in _ESCAPE_PROFILE_HANDLERS:
        raise ValueError(f"Escape profile '{normalized}' is already registered")
    _ESCAPE_PROFILE_HANDLERS[normalized] = handler


def list_escape_profiles() -> tuple[str, ...]:
    """Return the registered escape profile names in alphabetical order."""

    return tuple(sorted(_ESCAPE_PROFILE_HANDLERS))


def _normalize_escape_profile(name: str, *, usage: str | None = None) -> str:
    normalized = name.strip().lower()
    if not normalized:
        raise FileCommandParseError(
            "--escape-profile requires a non-empty value",
            usage=usage,
        )
    if normalized not in _ESCAPE_PROFILE_HANDLERS:
        available = ", ".join(list_escape_profiles()) or "<none>"
        raise FileCommandParseError(
            f"Unknown escape profile '{name}'. Available profiles: {available}",
            usage=usage,
        )
    return normalized


def _auto_escape_profile_handler(text: str, context: EscapeProfileContext) -> str:
    if context.operation in _INLINE_CONTENT_OPERATIONS and _should_decode_inline_escape_sequences(text):
        return _decode_inline_escape_sequences(text)
    return text


def _literal_escape_profile_handler(text: str, _context: EscapeProfileContext) -> str:
    return text


def _ensure_builtin_profiles_registered() -> None:
    if "auto" not in _ESCAPE_PROFILE_HANDLERS:
        register_escape_profile("auto", _auto_escape_profile_handler)
    if "none" not in _ESCAPE_PROFILE_HANDLERS:
        register_escape_profile("none", _literal_escape_profile_handler)


_ensure_builtin_profiles_registered()


class FileCommandError(ValueError):
    """Base exception raised when parsing `filetool` arguments."""


class FileCommandHelp(FileCommandError):
    """Raised when help text should be displayed instead of executing the command."""

    def __init__(self, help_text: str) -> None:
        super().__init__("help requested")
        self.help_text = help_text


class FileCommandParseError(FileCommandError):
    """Raised when command arguments are invalid."""

    def __init__(self, message: str, usage: str | None = None) -> None:
        super().__init__(message)
        self.usage = usage


@dataclass(slots=True)
class ManageFileCommand:
    """Represents a fully parsed ``filetool`` invocation."""

    operation: str
    path: str
    content: str | None
    line: int | None
    start_line: int | None
    end_line: int | None
    encoding: str
    create_parents: bool
    overwrite: bool
    create_if_missing: bool
    escape_profile: str
    follow_symlinks: bool
    output_format: str


class _ArgumentParser(argparse.ArgumentParser):
    """Custom parser that raises exceptions instead of exiting."""

    def error(self, message: str) -> None:  # pragma: no cover - handled by caller
        raise FileCommandParseError(message, usage=self.format_usage())


def _create_parser() -> _ArgumentParser:
    description = (
        "Call the server's manage_file tool to create, modify, inspect, or inventory remote files."
    )
    epilog = """Operations:
  create   Create or overwrite a file with optional content.
  write    Replace the entire file contents.
  append   Append text to the end of a file, optionally creating it.
  insert   Insert new lines before the specified line number.
  replace  Replace a range of lines with new content.
  delete   Remove a range of lines entirely.
  print    Display the requested line range with numbering.
  locate   Report line numbers containing the provided search text.
  patch    Apply a unified diff patch to an existing file.
  stat     Display filesystem metadata for the target path.
"""
    parser = _ArgumentParser(
        prog="filetool",
        description=description,
        epilog=epilog,
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ensure_builtin_profiles_registered()
    available_profiles = ", ".join(list_escape_profiles())
    parser.add_argument("operation", choices=_ALLOWED_OPERATIONS)
    parser.add_argument("path", help="Path to the remote file (relative to the server root).")
    parser.add_argument(
        "--content",
        "-c",
        dest="content",
        help=(
            "Inline text content for operations that require it. "
            "When the payload contains escape sequences such as \\n or \\t and no "
            "literal newlines, those escapes are expanded automatically so single-line shells can "
            "submit multi-line data."
        ),
    )
    parser.add_argument(
        "--content-from-file",
        dest="content_file",
        help="Load content from a local file using the specified encoding.",
    )
    parser.add_argument(
        "--stdin",
        dest="read_stdin",
        action="store_true",
        help="Read content from standard input until EOF.",
    )
    parser.add_argument(
        "--escape-profile",
        default="auto",
        help=(
            "Selects the inline escape decoding strategy. Available profiles: "
            f"{available_profiles or 'auto, none'}."
        ),
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="Text encoding used when reading or writing files.",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("human", "json"),
        default="human",
        help=(
            "Select the local rendering style for responses. "
            "The 'human' format prints descriptive text while 'json' emits machine-readable metadata."
        ),
    )
    parser.add_argument(
        "--line",
        type=int,
        help="Line number used by the insert operation.",
    )
    parser.add_argument(
        "--start-line",
        type=int,
        help="Start line for replace, delete, or print operations.",
    )
    parser.add_argument(
        "--end-line",
        type=int,
        help="End line for replace, delete, or print operations.",
    )
    parser.add_argument(
        "--create-parents",
        action="store_true",
        help="Create parent directories automatically when writing files.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow the create operation to overwrite an existing file.",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_false",
        dest="overwrite",
        help="Prevent create from overwriting existing files (default).",
    )
    parser.add_argument(
        "--create-if-missing",
        action="store_true",
        dest="create_if_missing",
        default=True,
        help="Allow append to create files that do not yet exist (default).",
    )
    parser.add_argument(
        "--no-create-if-missing",
        action="store_false",
        dest="create_if_missing",
        help="Require append to target an existing file.",
    )
    parser.add_argument(
        "--follow-symlinks",
        dest="follow_symlinks",
        action="store_true",
        default=True,
        help="Follow symbolic links when inspecting metadata (default).",
    )
    parser.add_argument(
        "--no-follow-symlinks",
        dest="follow_symlinks",
        action="store_false",
        help="Inspect symbolic link metadata without resolving the target.",
    )
    return parser


def render_manage_file_help() -> str:
    """Return the formatted help text for the ``filetool`` command."""

    parser = _create_parser()
    help_text = parser.format_help().rstrip()
    if help_text:
        help_text += "\n"
    help_text += "Use `filetool --help` to display this message."
    return help_text


def parse_manage_file_command(
    arguments: Sequence[str],
    *,
    stdin: TextIO | None = None,
) -> ManageFileCommand:
    """Parse raw argument tokens into a :class:`ManageFileCommand`."""

    parser = _create_parser()
    if any(token in {"-h", "--help"} for token in arguments):
        raise FileCommandHelp(render_manage_file_help())
    try:
        namespace = parser.parse_args(arguments)
    except FileCommandParseError:
        raise
    except SystemExit as exc:  # pragma: no cover - argparse fallback safeguard
        raise FileCommandParseError(
            f"argument parsing failed with exit status {exc.code}",
            usage=parser.format_usage(),
        ) from exc

    operation = namespace.operation
    stdin_stream = stdin if stdin is not None else sys.stdin
    usage = parser.format_usage()
    escape_profile = _normalize_escape_profile(namespace.escape_profile, usage=usage)
    namespace.escape_profile = escape_profile
    content = _resolve_content(
        namespace,
        stdin_stream,
        operation=operation,
        escape_profile=escape_profile,
    )
    path = namespace.path
    encoding = namespace.encoding
    line = namespace.line
    start_line = namespace.start_line
    end_line = namespace.end_line

    _validate_arguments(operation, line, start_line, end_line, content)

    if operation == "stat":
        content = None

    return ManageFileCommand(
        operation=operation,
        path=path,
        content=content,
        line=line,
        start_line=start_line,
        end_line=end_line,
        encoding=encoding,
        create_parents=bool(namespace.create_parents),
        overwrite=bool(namespace.overwrite),
        create_if_missing=bool(namespace.create_if_missing),
        escape_profile=escape_profile,
        follow_symlinks=bool(namespace.follow_symlinks),
        output_format=str(namespace.output_format),
    )


def _resolve_content(
    namespace: argparse.Namespace,
    stdin_stream: TextIO,
    *,
    operation: str,
    escape_profile: str,
) -> str | None:
    """Determine the content payload from the parsed namespace."""

    sources = [
        name
        for name, enabled in {
            "content": namespace.content is not None,
            "content_file": namespace.content_file is not None,
            "stdin": bool(namespace.read_stdin),
        }.items()
        if enabled
    ]
    if len(sources) > 1:
        raise FileCommandParseError(
            "Only one of --content, --content-from-file, or --stdin may be provided.",
            usage=_create_parser().format_usage(),
        )
    if not sources:
        return None

    if namespace.content is not None:
        content = namespace.content
        handler = _ESCAPE_PROFILE_HANDLERS[escape_profile]
        context = EscapeProfileContext(operation=operation, namespace=namespace)
        return handler(content, context)
    if namespace.content_file is not None:
        path = Path(namespace.content_file).expanduser()
        try:
            return path.read_text(encoding=namespace.encoding)
        except FileNotFoundError as exc:
            raise FileCommandParseError(f"Content file not found: {path}") from exc
        except OSError as exc:
            raise FileCommandParseError(f"Failed to read content file: {path}: {exc}") from exc
    try:
        return stdin_stream.read()
    except OSError as exc:  # pragma: no cover - extremely rare
        raise FileCommandParseError(f"Failed to read stdin: {exc}") from exc


def _validate_arguments(
    operation: str,
    line: int | None,
    start_line: int | None,
    end_line: int | None,
    content: str | None,
) -> None:
    """Validate operation-specific requirements."""

    if operation == "stat":
        # Metadata inspections accept dedicated filters that do not overlap with the
        # traditional line-focused options. Skip the remaining validation logic so
        # forward-compatible filters can be introduced without tightening checks
        # here prematurely.
        return

    if line is not None:
        if line <= 0:
            raise FileCommandParseError("--line must be a positive integer")
        if operation != "insert":
            raise FileCommandParseError("--line is only valid for the insert operation")

    if start_line is not None:
        if start_line <= 0:
            raise FileCommandParseError("--start-line must be a positive integer")
        if operation not in {"replace", "delete", "print"}:
            raise FileCommandParseError(
                "--start-line is only valid for replace, delete, or print operations",
            )
    if end_line is not None:
        if end_line <= 0:
            raise FileCommandParseError("--end-line must be a positive integer")
        if start_line is None:
            raise FileCommandParseError("--end-line requires --start-line to be set")
        if end_line < start_line:
            raise FileCommandParseError("--end-line cannot be less than --start-line")

    if operation in {"write", "append", "insert"} and content is None:
        raise FileCommandParseError(
            f"The {operation} operation requires content via --content, --content-from-file, or --stdin.",
        )

    if operation == "patch" and content is None:
        raise FileCommandParseError(
            "The patch operation requires a unified diff via --content, --content-from-file, or --stdin.",
        )

    if operation == "locate":
        if content is None or content.strip() == "":
            raise FileCommandParseError("locate requires non-empty content text")

    if operation == "replace" and content is None:
        # Empty string is valid and should replace the range with nothing.
        return

    if operation == "create" and content is None:
        return

    if operation in {"delete", "print"} and content is not None:
        raise FileCommandParseError(
            f"The {operation} operation does not accept content arguments.",
        )

