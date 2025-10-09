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

### Connecting via ngrok

1. Ensure the ngrok agent is installed and authenticated on the machine that runs the server (`ngrok config add-authtoken <token>`).
2. Start the server with an HTTP transport that supports tunnelling:

   ```bash
   mcp2term --transport streamable-http
   ```

   Watch for the log entry `ngrok public URL: https://<random>.ngrok.app/mcp`.

3. Point the client at that HTTPS URL from any machine that can reach the Internet:

   ```bash
   mcp2term-client --url https://<random>.ngrok.app/mcp
   ```

   You can combine `--url` with `--timeout`, `--env`, or any other client flags; the tunnel behaves like a regular HTTPS endpoint.

### Local server pairing

During development you can run everything on the same machine by launching the server with Streamable HTTP and disabling ngrok:

```bash
mcp2term --transport streamable-http --disable-ngrok
mcp2term-client --url http://127.0.0.1:8000/mcp
```

See the project-wide [Local Usage Guide](../docs/local_usage.md) for a complete setup checklist, optional configuration, and troubleshooting suggestions.
