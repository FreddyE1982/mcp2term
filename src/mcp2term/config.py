"""Configuration helpers for the MCP terminal server."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, MutableMapping


@dataclass(slots=True)
class ServerConfig:
    """Runtime configuration for the MCP terminal server."""

    shell_path: str = "/bin/bash"
    working_directory: Path = field(default_factory=lambda: Path.cwd())
    inherit_environment: bool = True
    additional_environment: MutableMapping[str, str] = field(default_factory=dict)
    plugin_modules: tuple[str, ...] = ()
    command_timeout: float | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ServerConfig":
        """Create configuration from environment variables."""

        env = dict(os.environ if environ is None else environ)
        shell_path = env.get("MCP2TERM_SHELL", cls.shell_path)
        working_directory = Path(env.get("MCP2TERM_WORKDIR", os.getcwd())).expanduser().resolve()
        inherit_environment = env.get("MCP2TERM_INHERIT_ENV", "true").lower() in {"1", "true", "yes", "on"}
        additional_environment: MutableMapping[str, str] = {}
        extra_env_raw = env.get("MCP2TERM_EXTRA_ENV")
        if extra_env_raw:
            try:
                loaded = json.loads(extra_env_raw)
                if not isinstance(loaded, dict):
                    raise ValueError("MCP2TERM_EXTRA_ENV must decode to a JSON object")
                for key, value in loaded.items():
                    additional_environment[str(key)] = str(value)
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid JSON for MCP2TERM_EXTRA_ENV") from exc
        plugin_modules = tuple(filter(None, (module.strip() for module in env.get("MCP2TERM_PLUGINS", "").split(","))))
        timeout_raw = env.get("MCP2TERM_COMMAND_TIMEOUT")
        timeout_value: float | None
        if timeout_raw:
            try:
                timeout_value = float(timeout_raw)
                if timeout_value <= 0:
                    raise ValueError("MCP2TERM_COMMAND_TIMEOUT must be positive if provided")
            except ValueError as exc:
                raise ValueError("Invalid MCP2TERM_COMMAND_TIMEOUT value") from exc
        else:
            timeout_value = None
        return cls(
            shell_path=shell_path,
            working_directory=working_directory,
            inherit_environment=inherit_environment,
            additional_environment=additional_environment,
            plugin_modules=plugin_modules,
            command_timeout=timeout_value,
        )

    def build_environment(self) -> dict[str, str]:
        """Construct the environment mapping for command execution."""

        env = dict(os.environ) if self.inherit_environment else {}
        env.update(self.additional_environment)
        return env
