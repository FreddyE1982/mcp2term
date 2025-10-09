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
- Friendly prompt that shows the remote working directory.

## Usage

```bash
python -m mcp2term_client --url https://your-ngrok-url.example/mcp
```

or, when installed via `pip`:

```bash
mcp2term-client --url https://your-ngrok-url.example/mcp
```

Pass `--timeout` to set a default command timeout in seconds. The client requires an ngrok tunnel
or another publicly reachable Streamable HTTP endpoint exposed by the `mcp2term` server.
