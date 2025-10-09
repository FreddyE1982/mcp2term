"""Configuration helpers for the MCP terminal server."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, MutableMapping

from .ngrok import NgrokSettings


@dataclass(slots=True)
class ServerConfig:
    """Runtime configuration for the MCP terminal server."""

    shell_path: str = "/bin/bash"
    working_directory: Path = field(default_factory=lambda: Path.cwd())
    inherit_environment: bool = True
    additional_environment: MutableMapping[str, str] = field(default_factory=dict)
    plugin_modules: tuple[str, ...] = ()
    command_timeout: float | None = None
    console_echo: bool = True
    stream_chunk_size: int = 65536
    ngrok: NgrokSettings = field(default_factory=NgrokSettings)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ServerConfig":
        """Create configuration from environment variables."""

        env = dict(os.environ if environ is None else environ)
        defaults = cls()

        shell_path = env.get("MCP2TERM_SHELL", defaults.shell_path)
        working_directory = Path(env.get("MCP2TERM_WORKDIR", defaults.working_directory)).expanduser().resolve()
        inherit_raw = env.get("MCP2TERM_INHERIT_ENV")
        inherit_environment = (
            defaults.inherit_environment
            if inherit_raw is None
            else inherit_raw.lower() in {"1", "true", "yes", "on"}
        )
        additional_environment: MutableMapping[str, str] = dict(defaults.additional_environment)
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
        plugins_raw = env.get("MCP2TERM_PLUGINS")
        if plugins_raw is None:
            plugin_modules = defaults.plugin_modules
        else:
            plugin_modules = tuple(filter(None, (module.strip() for module in plugins_raw.split(","))))
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
            timeout_value = defaults.command_timeout
        console_echo_raw = env.get("MCP2TERM_CONSOLE_ECHO")
        if console_echo_raw is None:
            console_echo_enabled = defaults.console_echo
        else:
            normalized_console = console_echo_raw.lower()
            if normalized_console in {"1", "true", "yes", "on"}:
                console_echo_enabled = True
            elif normalized_console in {"0", "false", "no", "off"}:
                console_echo_enabled = False
            else:
                raise ValueError("MCP2TERM_CONSOLE_ECHO must be a boolean value")
        chunk_size_raw = env.get("MCP2TERM_STREAM_CHUNK_SIZE")
        if chunk_size_raw is None:
            stream_chunk_size = defaults.stream_chunk_size
        else:
            try:
                stream_chunk_size = int(chunk_size_raw)
            except ValueError as exc:
                raise ValueError("MCP2TERM_STREAM_CHUNK_SIZE must be an integer") from exc
            if stream_chunk_size <= 0:
                raise ValueError("MCP2TERM_STREAM_CHUNK_SIZE must be a positive integer")
        ngrok_settings = NgrokSettings()
        ngrok_enable_raw = env.get("MCP2TERM_NGROK_ENABLE")
        if ngrok_enable_raw is not None:
            ngrok_settings.enabled = ngrok_enable_raw.lower() in {"1", "true", "yes", "on"}
        ngrok_binary = env.get("MCP2TERM_NGROK_BIN")
        if ngrok_binary:
            ngrok_settings.binary = ngrok_binary
        ngrok_api = env.get("MCP2TERM_NGROK_API_URL")
        if ngrok_api:
            ngrok_settings.api_base_url = ngrok_api
        region = env.get("MCP2TERM_NGROK_REGION")
        if region:
            ngrok_settings.region = region
        config_path_raw = env.get("MCP2TERM_NGROK_CONFIG")
        if config_path_raw:
            ngrok_settings.config_path = Path(config_path_raw).expanduser().resolve()
        hostname = env.get("MCP2TERM_NGROK_HOSTNAME")
        if hostname:
            ngrok_settings.hostname = hostname
        domain = env.get("MCP2TERM_NGROK_DOMAIN")
        if domain:
            ngrok_settings.domain = domain
        edge = env.get("MCP2TERM_NGROK_EDGE")
        if edge:
            ngrok_settings.edge = edge
        log_level_raw = env.get("MCP2TERM_NGROK_LOG_LEVEL")
        if log_level_raw:
            normalized_log_level = log_level_raw.lower()
            if normalized_log_level not in {"debug", "info", "warn", "error"}:
                raise ValueError("Invalid MCP2TERM_NGROK_LOG_LEVEL value")
            ngrok_settings.log_level = normalized_log_level
        transports_raw = env.get("MCP2TERM_NGROK_TRANSPORTS")
        if transports_raw:
            transports = tuple(filter(None, (item.strip() for item in transports_raw.split(","))))
            valid_transports = {"stdio", "sse", "streamable-http"}
            if not transports:
                raise ValueError("MCP2TERM_NGROK_TRANSPORTS must list at least one transport")
            if any(entry not in valid_transports for entry in transports):
                raise ValueError("MCP2TERM_NGROK_TRANSPORTS contains unsupported value")
            ngrok_settings.transports = transports
        extra_args_raw = env.get("MCP2TERM_NGROK_EXTRA_ARGS")
        if extra_args_raw:
            try:
                parsed_args = json.loads(extra_args_raw)
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid JSON for MCP2TERM_NGROK_EXTRA_ARGS") from exc
            if not isinstance(parsed_args, list):
                raise ValueError("MCP2TERM_NGROK_EXTRA_ARGS must be a JSON list")
            ngrok_settings.extra_args = tuple(str(item) for item in parsed_args)
        env_override_raw = env.get("MCP2TERM_NGROK_ENV")
        if env_override_raw:
            try:
                parsed_env = json.loads(env_override_raw)
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid JSON for MCP2TERM_NGROK_ENV") from exc
            if not isinstance(parsed_env, dict):
                raise ValueError("MCP2TERM_NGROK_ENV must decode to a JSON object")
            ngrok_settings.environment = {str(key): str(value) for key, value in parsed_env.items()}
        start_timeout_raw = env.get("MCP2TERM_NGROK_START_TIMEOUT")
        if start_timeout_raw:
            try:
                start_timeout = float(start_timeout_raw)
            except ValueError as exc:
                raise ValueError("Invalid MCP2TERM_NGROK_START_TIMEOUT value") from exc
            if start_timeout <= 0:
                raise ValueError("MCP2TERM_NGROK_START_TIMEOUT must be positive")
            ngrok_settings.start_timeout = start_timeout
        poll_interval_raw = env.get("MCP2TERM_NGROK_POLL_INTERVAL")
        if poll_interval_raw:
            try:
                poll_interval = float(poll_interval_raw)
            except ValueError as exc:
                raise ValueError("Invalid MCP2TERM_NGROK_POLL_INTERVAL value") from exc
            if poll_interval <= 0:
                raise ValueError("MCP2TERM_NGROK_POLL_INTERVAL must be positive")
            ngrok_settings.poll_interval = poll_interval
        request_timeout_raw = env.get("MCP2TERM_NGROK_REQUEST_TIMEOUT")
        if request_timeout_raw:
            try:
                request_timeout = float(request_timeout_raw)
            except ValueError as exc:
                raise ValueError("Invalid MCP2TERM_NGROK_REQUEST_TIMEOUT value") from exc
            if request_timeout <= 0:
                raise ValueError("MCP2TERM_NGROK_REQUEST_TIMEOUT must be positive")
            ngrok_settings.request_timeout = request_timeout
        shutdown_timeout_raw = env.get("MCP2TERM_NGROK_SHUTDOWN_TIMEOUT")
        if shutdown_timeout_raw:
            try:
                shutdown_timeout = float(shutdown_timeout_raw)
            except ValueError as exc:
                raise ValueError("Invalid MCP2TERM_NGROK_SHUTDOWN_TIMEOUT value") from exc
            if shutdown_timeout <= 0:
                raise ValueError("MCP2TERM_NGROK_SHUTDOWN_TIMEOUT must be positive")
            ngrok_settings.shutdown_timeout = shutdown_timeout
        return cls(
            shell_path=shell_path,
            working_directory=working_directory,
            inherit_environment=inherit_environment,
            additional_environment=additional_environment,
            plugin_modules=plugin_modules,
            command_timeout=timeout_value,
            console_echo=console_echo_enabled,
            stream_chunk_size=stream_chunk_size,
            ngrok=ngrok_settings,
        )

    def build_environment(self) -> dict[str, str]:
        """Construct the environment mapping for command execution."""

        env = dict(os.environ) if self.inherit_environment else {}
        env.update(self.additional_environment)
        return env
