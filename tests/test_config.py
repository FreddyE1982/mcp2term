"""Tests for configuration parsing, including ngrok settings."""

import os
from pathlib import Path

import pytest

from mcp2term.config import ServerConfig


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_ngrok_settings_defaults(use_real_dependencies: bool) -> None:
    config = ServerConfig.from_env({})
    assert config.ngrok.enabled is True
    assert config.ngrok.binary == "ngrok"
    assert config.ngrok.transports == ("sse", "streamable-http")
    assert config.ngrok.start_timeout > 0
    assert config.ngrok.poll_interval > 0
    assert config.ngrok.domain == "alpaca-model-easily.ngrok-free.app"


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_ngrok_settings_parsing(use_real_dependencies: bool, tmp_path: Path) -> None:
    config_path = tmp_path / "ngrok.yml"
    config_path.write_text("version: 3")
    env = {
        "MCP2TERM_NGROK_ENABLE": "0",
        "MCP2TERM_NGROK_BIN": "/usr/local/bin/ngrok",
        "MCP2TERM_NGROK_API_URL": "http://localhost:5000/api",
        "MCP2TERM_NGROK_REGION": "eu",
        "MCP2TERM_NGROK_CONFIG": str(config_path),
        "MCP2TERM_NGROK_HOSTNAME": "demo.example.com",
        "MCP2TERM_NGROK_DOMAIN": "example.com",
        "MCP2TERM_NGROK_EDGE": "edge-id",
        "MCP2TERM_NGROK_LOG_LEVEL": "debug",
        "MCP2TERM_NGROK_TRANSPORTS": "sse,streamable-http",
        "MCP2TERM_NGROK_EXTRA_ARGS": "[\"--inspect\", \"false\"]",
        "MCP2TERM_NGROK_ENV": "{\"MY_FLAG\": \"1\"}",
        "MCP2TERM_NGROK_START_TIMEOUT": "30",
        "MCP2TERM_NGROK_POLL_INTERVAL": "1.5",
        "MCP2TERM_NGROK_REQUEST_TIMEOUT": "10",
        "MCP2TERM_NGROK_SHUTDOWN_TIMEOUT": "12",
    }
    config = ServerConfig.from_env(env)
    assert config.ngrok.enabled is False
    assert config.ngrok.binary == "/usr/local/bin/ngrok"
    assert config.ngrok.api_base_url == "http://localhost:5000/api"
    assert config.ngrok.region == "eu"
    assert config.ngrok.config_path == config_path.resolve()
    assert config.ngrok.hostname == "demo.example.com"
    assert config.ngrok.domain == "example.com"
    assert config.ngrok.edge == "edge-id"
    assert config.ngrok.log_level == "debug"
    assert config.ngrok.extra_args == ("--inspect", "false")
    assert config.ngrok.environment == {"MY_FLAG": "1"}
    assert config.ngrok.start_timeout == pytest.approx(30.0)
    assert config.ngrok.poll_interval == pytest.approx(1.5)
    assert config.ngrok.request_timeout == pytest.approx(10.0)
    assert config.ngrok.shutdown_timeout == pytest.approx(12.0)


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_invalid_ngrok_transport_raises(use_real_dependencies: bool) -> None:
    with pytest.raises(ValueError):
        ServerConfig.from_env({"MCP2TERM_NGROK_TRANSPORTS": "sse,invalid"})


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_console_echo_toggle(use_real_dependencies: bool) -> None:
    disabled = ServerConfig.from_env({"MCP2TERM_CONSOLE_ECHO": "0"})
    assert disabled.console_echo is False
    enabled = ServerConfig.from_env({"MCP2TERM_CONSOLE_ECHO": "true"})
    assert enabled.console_echo is True
    with pytest.raises(ValueError):
        ServerConfig.from_env({"MCP2TERM_CONSOLE_ECHO": "maybe"})


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_build_environment_exports_launch_directory(use_real_dependencies: bool) -> None:
    config = ServerConfig(inherit_environment=False)
    env = config.build_environment()
    assert env["PYTHONPATH"] == str(config.launch_directory)


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_build_environment_preserves_existing_pythonpath(use_real_dependencies: bool) -> None:
    config = ServerConfig(inherit_environment=False)
    original_pythonpath = os.pathsep.join([
        "/tmp/demo-one",
        str(config.launch_directory),
        "/tmp/demo-two",
    ])
    config.additional_environment["PYTHONPATH"] = original_pythonpath
    env = config.build_environment()
    expected_entries = [str(config.launch_directory), "/tmp/demo-one", "/tmp/demo-two"]
    assert env["PYTHONPATH"].split(os.pathsep) == expected_entries
    assert config.additional_environment["PYTHONPATH"] == original_pythonpath
