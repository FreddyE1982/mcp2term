"""Xonsh integration for the remote MCP terminal client."""

from __future__ import annotations

import json
import shlex
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .file_command import (
    FileCommandHelp,
    FileCommandParseError,
    ManageFileCommand,
    parse_manage_file_command,
)
from .input import InputReader, TerminalInputReader
from .intro import IntroContext, render_intro_message
from .session import FileOperationResponse, RemoteMcpSession
from .state import RemoteShellState

EXEC_FUNCTION_NAME = "__mcp_remote_execute__"
_TRANSFORM_PREFIX = f"{EXEC_FUNCTION_NAME}("


@dataclass(slots=True)
class RemoteCommandProcessor:
    """Coordinates local shell semantics with the remote MCP session."""

    session: RemoteMcpSession
    state: RemoteShellState
    status_callback: Callable[[int], None] = field(default=lambda _status: None)
    output_writer: Callable[[str], None] = print
    error_writer: Callable[[str], None] = lambda message: print(message, file=sys.stderr)
    input_reader_factory: Callable[[], InputReader] | None = None
    current_command_id: str | None = field(default=None, init=False, repr=False)

    def execute(self, raw_command: str) -> int:
        command = raw_command.rstrip("\n")
        if not command.strip():
            self.status_callback(0)
            return 0

        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as exc:
            self.error_writer(f"parse error: {exc}")
            self.status_callback(1)
            return 1

        assignments, remainder = self._split_assignments(tokens)
        if not remainder:
            if assignments:
                self.state.update_environment(assignments)
            self.status_callback(0)
            return 0

        command_name = remainder[0]
        if command_name in {"exit", "quit"}:
            code = int(remainder[1]) if len(remainder) > 1 else 0
            self.status_callback(code)
            raise SystemExit(code)

        builtin_handlers: dict[str, Callable[[list[str], dict[str, str]], int]] = {
            "cd": self._handle_cd,
            "export": self._handle_export,
            "unset": self._handle_unset,
        }

        if handler := builtin_handlers.get(command_name):
            self.state.update_environment(assignments)
            return handler(remainder[1:], assignments)

        if command_name == "filetool":
            return self._handle_manage_file_command(remainder[1:], assignments)

        return self._execute_remote_command(remainder, assignments)

    def _execute_remote_command(self, tokens: list[str], assignments: dict[str, str]) -> int:
        command_text = " ".join(shlex.quote(token) for token in tokens)
        allocate_pty = self._should_allocate_pty(tokens)
        command_id, future = self.session.run_command_async(
            command_text,
            working_directory=self.state.cwd,
            environment=self.state.environment,
            ephemeral_environment=assignments,
            allocate_pty=allocate_pty,
        )
        self.current_command_id = command_id
        reader = self._create_input_reader()
        try:
            while not future.done():
                try:
                    forwarded = self._forward_interactive_input(command_id, reader)
                except KeyboardInterrupt:
                    self._handle_interrupt(command_id)
                    continue
                except Exception as exc:
                    self.error_writer(str(exc))
                    self.status_callback(1)
                    return 1
                if not forwarded:
                    time.sleep(0.05)
            response = future.result()
        except Exception as exc:
            self.error_writer(str(exc))
            self.status_callback(1)
            return 1
        finally:
            reader.close()
            self.current_command_id = None

        assert response is not None
        self.state.cwd = response.working_directory or self.state.cwd
        self.status_callback(response.return_code)
        if response.timed_out:
            self.error_writer(f"Command timed out after {response.duration:.2f}s")
        return response.return_code

    def _handle_cd(self, arguments: list[str], _: dict[str, str]) -> int:
        target = arguments[0] if arguments else "~"
        try:
            new_cwd = self.session.resolve_working_directory(target)
        except Exception as exc:
            self.error_writer(f"cd: {exc}")
            self.status_callback(1)
            return 1
        self.state.cwd = new_cwd
        self.status_callback(0)
        return 0

    def _handle_export(self, arguments: list[str], assignments: dict[str, str]) -> int:
        updates = dict(assignments)
        for entry in arguments:
            if "=" not in entry:
                self.error_writer(f"export: invalid assignment '{entry}'")
                self.status_callback(1)
                return 1
            key, value = entry.split("=", 1)
            updates[key] = value
        if updates:
            self.state.update_environment(updates)
        self.status_callback(0)
        return 0

    def _handle_unset(self, arguments: list[str], _: dict[str, str]) -> int:
        if arguments:
            self.state.remove_environment_keys(arguments)
        self.status_callback(0)
        return 0

    def _handle_manage_file_command(
        self,
        arguments: list[str],
        assignments: dict[str, str],
    ) -> int:
        self.state.update_environment(assignments)
        try:
            command: ManageFileCommand = parse_manage_file_command(arguments)
        except FileCommandHelp as help_exc:
            help_text = help_exc.help_text.rstrip("\n")
            for line in help_text.split("\n"):
                self.output_writer(line)
            self.status_callback(0)
            return 0
        except FileCommandParseError as parse_exc:
            usage = (parse_exc.usage or "").strip()
            if usage:
                for line in usage.split("\n"):
                    if line:
                        self.error_writer(line)
            self.error_writer(f"filetool: {parse_exc}")
            self.status_callback(2)
            return 2
        except Exception as exc:
            self.error_writer(f"filetool: {exc}")
            self.status_callback(1)
            return 1

        try:
            response = self.session.manage_file(
                command.path,
                operation=command.operation,
                content=command.content,
                pattern=command.pattern,
                line=command.line,
                start_line=command.start_line,
                end_line=command.end_line,
                encoding=command.encoding,
                create_parents=command.create_parents,
                overwrite=command.overwrite,
                create_if_missing=command.create_if_missing,
                escape_profile=command.escape_profile,
                follow_symlinks=command.follow_symlinks,
                use_regex=command.use_regex,
                ignore_case=command.ignore_case,
                max_replacements=command.max_replacements,
                anchor_text=command.anchor_text,
                anchor_use_regex=command.anchor_use_regex,
                anchor_ignore_case=command.anchor_ignore_case,
                anchor_after=command.anchor_after,
                anchor_occurrence=command.anchor_occurrence,
            )
        except Exception as exc:
            self.error_writer(f"filetool: {exc}")
            self.status_callback(1)
            return 1

        self._render_manage_file_response(response, output_format=command.output_format)
        status = 0 if response.success else 1
        self.status_callback(status)
        return status

    def _render_manage_file_response(
        self,
        response: FileOperationResponse,
        *,
        output_format: str = "human",
    ) -> None:
        format_choice = (output_format or "human").strip().lower()
        if format_choice not in {"human", "json"}:
            format_choice = "human"
        message = response.message
        if not message:
            if response.success:
                message = f"{response.operation} succeeded for {response.path}".strip()
            else:
                message = f"{response.operation} failed for {response.path}".strip()
        writer = self.output_writer if response.success else self.error_writer
        if message:
            writer(message)

        if response.metadata:
            if format_choice == "json":
                serialized = json.dumps(response.metadata, indent=2, sort_keys=True)
                for line in serialized.splitlines():
                    writer(line)
            else:
                width = max(len(str(key)) for key in response.metadata.keys())
                for key in sorted(response.metadata.keys()):
                    value = response.metadata[key]
                    writer(f"{key:>{width}} : {value}")

        if response.lines:
            width = max(len(str(line.number)) for line in response.lines)
            for line in response.lines:
                self.output_writer(f"{line.number:>{width}} | {line.text}")
        elif response.operation == "print" and response.content and not response.line_numbers:
            for line in response.content.splitlines():
                self.output_writer(line)

        if response.line_numbers:
            label = "Line number" if len(response.line_numbers) == 1 else "Line numbers"
            formatted = ", ".join(str(number) for number in response.line_numbers)
            self.output_writer(f"{label}: {formatted}")

    @staticmethod
    def _split_assignments(tokens: list[str]) -> tuple[dict[str, str], list[str]]:
        assignments: dict[str, str] = {}
        remainder: list[str] = []
        iterator = iter(tokens)
        for token in iterator:
            if "=" in token and not token.startswith("=") and remainder == []:
                name, value = token.split("=", 1)
                if name:
                    assignments[name] = value
                    continue
            remainder.append(token)
            remainder.extend(iterator)
            break
        return assignments, remainder

    def _create_input_reader(self) -> InputReader:
        if self.input_reader_factory is not None:
            return self.input_reader_factory()
        return TerminalInputReader()

    def _should_allocate_pty(self, tokens: list[str]) -> bool:
        return self.input_reader_factory is not None

    def _forward_interactive_input(self, command_id: str, reader: InputReader) -> bool:
        forwarded = False
        for chunk in reader.read(0.1):
            forwarded = True
            if chunk.data:
                if not self.session.send_stdin(command_id, chunk.data):
                    self.error_writer("Remote command is no longer accepting input.")
                    return True
            if chunk.eof:
                if not self.session.send_stdin(command_id, "", eof=True):
                    self.error_writer("Failed to signal EOF to remote command.")
                return True
        return forwarded

    def _handle_interrupt(self, command_id: str) -> None:
        try:
            cancel_response = self.session.cancel_command(command_id)
        except Exception as cancel_exc:
            self.error_writer(f"cancel failed: {cancel_exc}")
            return
        if cancel_response.delivered:
            signal_label = cancel_response.signal_name or str(cancel_response.signal)
            self.error_writer(f"Sent {signal_label} to remote command {command_id}.")
        else:
            self.error_writer("No running remote command to interrupt.")


class XonshShellRunner:
    """Bootstraps xonsh and installs the remote command executor."""

    def __init__(self, processor: RemoteCommandProcessor, *, url: str) -> None:
        self._processor = processor
        self._url = url

    def run(self) -> None:
        from xonsh.built_ins import XSH
        from xonsh.events import events
        from xonsh.main import setup

        setup(shell_type="best")
        XSH.env["XONSH_INTERACTIVE"] = True

        def set_status(code: int) -> None:
            history = XSH.history
            if history is not None:
                history.last_cmd_rtn = code
            XSH.env["?"] = code

        self._processor.status_callback = set_status

        def executor(command: str) -> int:
            return self._processor.execute(command)

        XSH.ctx[EXEC_FUNCTION_NAME] = executor
        XSH.env["PROMPT"] = lambda: f"[remote:{self._processor.state.cwd}] $ "

        def transform(cmd: str) -> str:
            stripped = cmd.strip()
            if not stripped:
                return cmd
            if stripped.startswith(_TRANSFORM_PREFIX):
                return cmd
            return f"{EXEC_FUNCTION_NAME}({cmd!r})"

        unsubscribe = _subscribe_event(events.on_transform_command, transform)

        print(f"Connected to {self._url} (cwd: {self._processor.state.cwd})")

        intro_message = render_intro_message(
            IntroContext(
                url=self._url,
                cwd=self._processor.state.cwd,
                default_timeout=self._processor.session.default_timeout,
                session=self._processor.session,
                processor=self._processor,
                state=self._processor.state,
            )
        )
        print(intro_message, end="")

        try:
            XSH.shell.shell.cmdloop()
        finally:
            unsubscribe()


def _subscribe_event(event: Any, handler: Callable[..., Any]) -> Callable[[], None]:
    """Subscribe to a xonsh event, returning an unsubscriber callback."""

    if hasattr(event, "connect") and callable(getattr(event, "connect")):
        event.connect(handler)

        def unsubscribe() -> None:
            event.disconnect(handler)

        return unsubscribe

    if callable(event):
        registered = event(handler)

        def unsubscribe() -> None:
            target = registered or handler
            if hasattr(event, "discard") and callable(getattr(event, "discard")):
                event.discard(target)
            elif hasattr(event, "remove") and callable(getattr(event, "remove")):
                event.remove(target)
            else:  # pragma: no cover - defensive fallback
                raise TypeError("Event does not support removal operations")

        return unsubscribe

    raise TypeError(
        "Unsupported event object: expected connect/disconnect or add/discard interface"
    )
