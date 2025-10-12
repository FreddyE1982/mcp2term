"""Introductory banner generation for the mcp2term client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence, TYPE_CHECKING, List
import textwrap

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from .session import RemoteMcpSession
    from .shell import RemoteCommandProcessor
    from .state import RemoteShellState


@dataclass(frozen=True)
class IntroContext:
    """Contextual data available to intro message providers."""

    url: str
    cwd: str
    default_timeout: float | None
    session: "RemoteMcpSession"
    processor: "RemoteCommandProcessor"
    state: "RemoteShellState"


@dataclass(frozen=True)
class IntroSection:
    """Represents a titled section in the intro banner."""

    title: str
    items: Sequence[str]
    description: str | None = None


class IntroSectionProvider(Protocol):
    """Callable that contributes sections to the intro banner."""

    def __call__(self, context: IntroContext) -> Iterable[IntroSection]:
        ...


_intro_section_providers: list[IntroSectionProvider] = []


def register_intro_section_provider(provider: IntroSectionProvider) -> None:
    """Register a new provider used when building the intro banner."""

    _intro_section_providers.append(provider)


def iter_intro_sections(context: IntroContext) -> Iterable[IntroSection]:
    """Yield sections produced by all registered providers."""

    providers: Sequence[IntroSectionProvider]
    if _intro_section_providers:
        providers = tuple(_intro_section_providers)
    else:
        providers = (_default_intro_section_provider,)
    for provider in providers:
        yield from provider(context)


def render_intro_message(context: IntroContext, *, width: int = 100) -> str:
    """Render the full intro banner for display to the terminal."""

    header = (
        "Welcome to the mcp2term remote shell! Here's a refresher on what this client can do."
    )
    lines: List[str] = [""]
    lines.extend(
        textwrap.wrap(
            header,
            width=width,
            initial_indent="",
            subsequent_indent="",
        )
    )
    lines.append("")

    for section in iter_intro_sections(context):
        lines.append(f"{section.title}:")
        if section.description:
            lines.extend(
                textwrap.wrap(
                    section.description,
                    width=width,
                    initial_indent="  ",
                    subsequent_indent="  ",
                )
            )
        for item in section.items:
            lines.extend(
                textwrap.wrap(
                    item,
                    width=width,
                    initial_indent="  • ",
                    subsequent_indent="    ",
                )
            )
        lines.append("")

    message = "\n".join(lines).rstrip()
    if not message.endswith("\n"):
        message += "\n"
    return message


def _default_intro_section_provider(context: IntroContext) -> Iterable[IntroSection]:
    """Return the default intro content describing core capabilities."""

    if context.default_timeout is not None:
        timeout_line = f"Default command timeout is set to {context.default_timeout:.0f}s."
    else:
        timeout_line = "No default command timeout is configured."

    capabilities: list[str] = [
        "Execute remote shell commands with live stdout/stderr streaming and mirrored console output.",
        "Persist your working directory automatically after each successful command.",
        "Use inline assignments (e.g. `KEY=value make build`) to inject per-command environment overrides.",
        timeout_line,
        "Interactive programs receive your keystrokes in real time; EOF (Ctrl+D) is forwarded as expected.",
        "Ctrl+C sends the configured interrupt signal to the remote process via the MCP cancellation API.",
        "Backpressure notices warn you whenever output buffering or pending requests build up, so you know when to pause.",
        "Startup diagnostics explain connectivity issues if the Streamable HTTP endpoint cannot be reached.",
        "Inline escape sequences such as `\\n` and `\\t` decode automatically by default, and `--escape-profile` lets you opt into literal or custom decoding strategies without touching server code.",
    ]

    special_commands: list[str] = [
        "`cd <path>` resolves against the remote host and updates the prompt once the directory change succeeds.",
        "`export NAME=value` and inline `NAME=value` assignments update the persistent remote environment.",
        "`unset NAME [NAME ...]` removes variables from the persistent environment.",
        "`exit [code]` / `quit [code]` cleanly terminate the session with an optional exit status.",
        "The prompt displays the remote working directory so you always know where commands will execute.",
    ]

    file_operations: list[str] = [
        "Use `filetool <operation> <path> [options]` to invoke the server's manage_file tool without leaving the shell.",
        "`create` makes a brand new file; combine with `--create-parents` or `--overwrite` as needed.",
        "`write` replaces the entire file contents, and `append` safely extends the end of a file.",
        "`prepend` places text at the very start of a file and honours `--create-if-missing` for brand new documents.",
        "`insert --line N` adds new text before the specified line, while `replace`/`delete` operate over line ranges via `--start-line` and `--end-line`.",
        "`print` streams selected line ranges back to the terminal, perfect for quick inspections without an editor.",
        "`locate --content \"needle\"` returns the remote line numbers that match your search text for faster navigation.",
        "`patch --stdin` or `--content-from-file` applies unified diffs just like the automation-friendly `apply_patch` helper.",
        "Inline patches (including literal `\\ No newline at end of file` markers) round-trip cleanly through the parser and remote workflow thanks to expanded unit and integration coverage.",
        "`substitute --pattern PATTERN --content TEXT` performs literal or regex replacements with `--regex`, `--ignore-case`, and `--max-replacements` controls while streaming replacement metrics back to your terminal.",
        "All file edits emit structured audit events through the plugin registry so operators can observe and extend behaviour.",
        "`--escape-profile none` bypasses inline decoding entirely so binary-safe payloads and templating DSLs survive transport unchanged, while custom profiles can be registered by plugins.",
        "`stat` reports detailed filesystem metadata, and `--format json` switches to machine-friendly output while flags such as `--no-follow-symlinks` expose symlink details without dereferencing targets.",
    ]

    description = (
        "The client mirrors a traditional terminal while layering in managed file editing and rich telemetry."
    )

    yield IntroSection(title="Core capabilities", items=tuple(capabilities), description=description)
    yield IntroSection(title="Handy built-ins", items=tuple(special_commands))
    yield IntroSection(
        title="File editing toolbox",
        items=tuple(file_operations),
        description=(
            "File commands map directly to the server's `manage_file` MCP tool. Combine options such as `--encoding`, "
            "`--create-if-missing`, `--escape-profile`, and `--eof` flags to match your workflow while plugins can "
            "introduce additional decoding policies on demand."
        ),
    )


register_intro_section_provider(_default_intro_section_provider)


__all__ = [
    "IntroContext",
    "IntroSection",
    "IntroSectionProvider",
    "iter_intro_sections",
    "register_intro_section_provider",
    "render_intro_message",
]
