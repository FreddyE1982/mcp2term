"""Xonsh integration for the remote MCP terminal client."""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, field
from typing import Callable

from .session import RemoteMcpSession
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

        return self._execute_remote_command(remainder, assignments)

    def _execute_remote_command(self, tokens: list[str], assignments: dict[str, str]) -> int:
        command_text = " ".join(shlex.quote(token) for token in tokens)
        try:
            response = self.session.run_command(
                command_text,
                working_directory=self.state.cwd,
                environment=self.state.environment,
                ephemeral_environment=assignments,
            )
        except Exception as exc:
            self.error_writer(str(exc))
            self.status_callback(1)
            return 1

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

        events.on_transform_command.connect(transform)

        print(f"Connected to {self._url} (cwd: {self._processor.state.cwd})")

        try:
            XSH.shell.shell.cmdloop()
        finally:
            events.on_transform_command.disconnect(transform)
