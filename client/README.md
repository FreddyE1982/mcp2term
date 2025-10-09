# mcp2term-client

`mcp2term-client` is an interactive [xonsh](https://xon.sh/) shell that connects to a remote
[mcp2term](../README.md) MCP server over the Streamable HTTP transport. The client mirrors a
traditional terminal session so that commands typed locally execute on the remote server while
stdout/stderr stream back in real time.

## Features

- Automatic installation of xonsh on first launch.
- Live streaming of stdout and stderr via MCP logging notifications.
- Persistent working directory and environment management using the server's tool arguments.
- Support for shell built-ins such as `cd`, inline environment assignments, `export`, and `unset`.
- Forward `Ctrl+C` interrupts to the remote server, delivering configurable signals via the MCP `cancel_command` tool.
- Friendly prompt that shows the remote working directory.
- Backpressure detection that reports when the client buffers output or pending commands so you know when to wait for catch-up.

## Usage

```bash
python -m mcp2term_client --url https://your-ngrok-url.example/mcp
```

or, when installed via `pip`:

```bash
mcp2term-client --url https://your-ngrok-url.example/mcp
```

Pass `--timeout` to set a default command timeout in seconds. The client requires an ngrok tunnel
or another publicly reachable Streamable HTTP endpoint exposed by the `mcp2term` server. Supplying the base URL (for example `https://your-ngrok-url.example`) also works—the client automatically appends `/mcp` when no path is provided.
