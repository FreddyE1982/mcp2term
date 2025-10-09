# mcp2term

An implementation of a Model Context Protocol (MCP) server that grants safe, auditable access to a system shell. The server streams stdout and stderr in real time while capturing rich metadata for plugins and downstream consumers.

## Features

- **Full command execution** with configurable shell, working directory, environment variables, and timeouts.
- **Live streaming** of stdout and stderr via MCP log notifications so clients observe progress as it happens.
- **Robust chunked streaming** that handles large stdout/stderr volumes without blocking or truncation.
- **Plugin architecture** that exposes every function, class, and variable defined in the package, enabling extensions to observe command lifecycles or inject custom behaviour.
- **Automatic ngrok tunneling** so HTTP transports are reachable without additional manual setup.
- **Typed lifespan context** shared with MCP tools for dependency access and lifecycle management.
- **Structured tool responses** including timing information to make results easy for agents to consume.
- **Console mirroring** so operators always see the command stream, stdout, and stderr on the hosting terminal by default.

## Installation

```bash
pip install -e .
```

The project targets Python 3.12 or newer.

## Configuration

`ServerConfig` reads settings from environment variables:

| Variable | Description | Default |
| --- | --- | --- |
| `MCP2TERM_SHELL` | Shell executable used for commands. | `/bin/bash` |
| `MCP2TERM_WORKDIR` | Working directory for commands. | Current directory |
| `MCP2TERM_INHERIT_ENV` | When `true`, inherit the parent environment. | `true` |
| `MCP2TERM_EXTRA_ENV` | JSON object merged into the command environment. | `{}` |
| `MCP2TERM_PLUGINS` | Comma-separated dotted module paths to load as plugins. | *(none)* |
| `MCP2TERM_COMMAND_TIMEOUT` | Default timeout in seconds for commands. | unlimited |
| `MCP2TERM_STREAM_CHUNK_SIZE` | Bytes read from stdout/stderr per chunk while streaming. | `65536` |
| `MCP2TERM_CONSOLE_ECHO` | Mirror commands and output to the server console (`true`/`false`). | `true` |

## Running the server

```bash
mcp2term --transport stdio
```

Change `--transport` to `sse` or `streamable-http` to use the corresponding MCP transports. `--log-level` controls verbosity and `--mount-path` overrides the HTTP mount location when relevant.

While the server is running it mirrors every executed command, stdout chunk, and stderr chunk to the hosting console. Set `MCP2TERM_CONSOLE_ECHO=false` to suppress the mirroring when embedding the server into log-sensitive environments.

When running with the `streamable-http` transport the MCP endpoint is served from the `/mcp` path (or `--mount-path` plus `/mcp` when a custom mount is provided). The CLI prints the fully qualified URL, including the `/mcp` suffix, to make tunnelling targets such as ngrok easy to copy.

## MCP tools

The server exposes two tools for remote command management:

`run_command(command: str, working_directory: Optional[str], environment: Optional[dict[str, str]], timeout: Optional[float]], command_id: Optional[str])`

The tool returns structured JSON containing:

- `command_id`: unique identifier assigned to the invocation
- `command`: executed command string
- `working_directory`: resolved working directory
- `return_code`: process exit code (non-zero for failure)
- `stdout` / `stderr`: aggregated output
- `started_at` / `finished_at`: ISO 8601 timestamps
- `duration`: execution duration in seconds
- `timed_out`: boolean flag indicating whether a timeout occurred

While a command runs the server emits stdout and stderr chunks as MCP log messages, preserving ordering through asynchronous streaming. Clients can reuse `command_id` values when making follow-up requests.

`cancel_command(command_id: str, signal_value: Optional[str | int])`

Sending `cancel_command` forwards a signal (defaulting to `SIGINT`) to the running process identified by `command_id`. The response includes the numeric `signal`, its symbolic `signal_name`, and a `delivered` flag confirming whether the process was still active when the signal was sent.

`send_stdin(command_id: str, data: Optional[str], eof: bool = False)`

Use `send_stdin` to stream additional input to an interactive command. The tool accepts optional text payloads and an `eof` flag
that closes the stdin pipe once all required data has been delivered. The response reports whether the input was accepted so
clients can retry or surface helpful diagnostics.

## Plugins

Plugins implement the `PluginProtocol` (via a module-level `PLUGIN` object) and can register `CommandStreamListener` instances to observe command lifecycle events. When the server starts it loads modules listed in `MCP2TERM_PLUGINS`, exposing the entire `mcp2term` namespace through the plugin registry for inspection or extension.

A minimal plugin skeleton:

```python
from dataclasses import dataclass

from mcp2term.plugin import CommandStreamListener, PluginProtocol, PluginRegistry

@dataclass
class EchoListener(CommandStreamListener):
    async def on_command_stdout(self, event):
        print(event.data, end="")

    async def on_command_start(self, event):
        print(f"Starting: {event.request.command}")

    async def on_command_stderr(self, event):
        print(f"[stderr] {event.data}", end="")

    async def on_command_complete(self, event):
        print(f"Finished with {event.return_code}")


class ShellEchoPlugin(PluginProtocol):
    name = "shell-echo"
    version = "1.0.0"

    def activate(self, registry: PluginRegistry):
        registry.register_command_listener(EchoListener())


PLUGIN = ShellEchoPlugin()
```

## Development

Run the test suite with:

```bash
pytest
```

Tests are parameterised to run with or without dependency stubbing, ensuring full execution paths remain verified.

## Ngrok integration

By default `mcp2term` opens an ngrok tunnel whenever you run the server with the `sse` or `streamable-http` transports. The tunnel exposes the local HTTP endpoint using the ngrok agent that must already be authenticated (for example via `ngrok config add-authtoken`). Unless overridden, the server now requests the reserved domain `alpaca-model-easily.ngrok-free.app` so clients always receive a predictable hostname.

Control the integration with the following environment variables:

| Variable | Description | Default |
| --- | --- | --- |
| `MCP2TERM_NGROK_ENABLE` | Enable or disable automatic tunnel creation. | `true` |
| `MCP2TERM_NGROK_TRANSPORTS` | Comma-separated transports that should be tunnelled (`stdio`, `sse`, `streamable-http`). | `sse,streamable-http` |
| `MCP2TERM_NGROK_BIN` | Path to the `ngrok` executable. | `ngrok` |
| `MCP2TERM_NGROK_API_URL` | Base URL for the local ngrok API. | `http://127.0.0.1:4040` |
| `MCP2TERM_NGROK_REGION` | Optional ngrok region to target. | *(none)* |
| `MCP2TERM_NGROK_LOG_LEVEL` | ngrok log level (`debug`, `info`, `warn`, `error`). | `info` |
| `MCP2TERM_NGROK_EXTRA_ARGS` | JSON array of additional CLI arguments passed to ngrok. | `[]` |
| `MCP2TERM_NGROK_ENV` | JSON object merged into the ngrok process environment. | `{}` |
| `MCP2TERM_NGROK_START_TIMEOUT` | Seconds to wait for tunnel provisioning. | `15` |
| `MCP2TERM_NGROK_POLL_INTERVAL` | Seconds between tunnel status checks. | `0.5` |
| `MCP2TERM_NGROK_REQUEST_TIMEOUT` | HTTP timeout for API calls. | `5` |
| `MCP2TERM_NGROK_SHUTDOWN_TIMEOUT` | Seconds to wait for ngrok to terminate gracefully. | `5` |
| `MCP2TERM_NGROK_CONFIG` | Optional path to an ngrok configuration file. | *(none)* |
| `MCP2TERM_NGROK_HOSTNAME` / `MCP2TERM_NGROK_DOMAIN` / `MCP2TERM_NGROK_EDGE` | Custom host bindings to request from ngrok. | `alpaca-model-easily.ngrok-free.app` for domain |

Use the `--disable-ngrok` flag when running `mcp2term` to opt out of tunneling for a single invocation.
