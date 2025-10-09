# Local Usage Guide

This guide walks through running the `mcp2term` server on your workstation and connecting with the companion `mcp2term-client` shell. It assumes Python 3.12 or newer is available.

## 1. Install dependencies

1. Create and activate a virtual environment (recommended):

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Install the server and client packages in editable mode so code changes are immediately reflected:

   ```bash
   pip install -e .
   pip install -e ./client
   ```

   Installing the client ensures the `mcp2term-client` console script is available. The first time you run the client it will bootstrap `xonsh` automatically if it is not already installed.

## 2. Launch the server

Run the server locally with the Streamable HTTP transport so the client can connect over HTTP:

```bash
mcp2term --transport streamable-http --disable-ngrok
```

Key defaults to know:

- **Host:** `127.0.0.1`
- **Port:** `8000`
- **Mount path:** `/mcp`

The `--disable-ngrok` flag keeps the server bound to localhost without trying to open a public tunnel. Leave the terminal open so the process keeps running.

### Optional configuration

- Override the host/port with environment variables supported by `FastMCP` (for example `MCP_SETTINGS_HOST` and `MCP_SETTINGS_PORT`).
- Use `MCP2TERM_SHELL="/bin/zsh"` to change the command interpreter invoked on the server.
- Export `MCP2TERM_COMMAND_TIMEOUT=60` to enforce a default 60‑second timeout for all commands.
- Provide additional plugins via `MCP2TERM_PLUGINS="your_package.plugin"`; all plugin exports registered in `mcp2term.plugin.GlobalPluginManager` become visible to extensions.

Refer to [`src/mcp2term/config.py`](../src/mcp2term/config.py) for the complete list of configuration knobs.

## 3. Expose the server over ngrok (optional)

`mcp2term` can publish its HTTP transports through ngrok automatically so teammates or remote devices can connect without poking firewall holes.

1. Install the ngrok agent from [ngrok.com/download](https://ngrok.com/download) and authenticate it once:

   ```bash
   ngrok config add-authtoken <your-token-here>
   ```

2. Launch the server with an HTTP-friendly transport (Streamable HTTP or SSE). Leave `--disable-ngrok` off so tunnelling remains enabled:

   ```bash
   mcp2term --transport streamable-http
   ```

   The startup log shows a line similar to `ngrok public URL: https://<random>.ngrok.app/mcp`. ngrok chooses the domain unless you request a specific hostname through environment variables such as `MCP2TERM_NGROK_HOSTNAME`, `MCP2TERM_NGROK_DOMAIN`, or `MCP2TERM_NGROK_EDGE` (features depend on your ngrok plan).

3. Share the HTTPS forwarding URL with collaborators. The tunnel stays alive for as long as the server process runs. When you interrupt the server (`Ctrl+C`), the tunnel closes automatically.

4. Optional tweaks:

   - Export `MCP2TERM_NGROK_REGION=eu` (or another supported region code) to keep the tunnel geographically close to the client.
   - Point to a custom ngrok binary with `MCP2TERM_NGROK_BIN=/opt/ngrok/ngrok`.
   - Supply additional CLI flags via `MCP2TERM_NGROK_EXTRA_ARGS='["--inspect=false"]'` to disable the inspection interface when you do not need it.
   - Combine with the preset system documented in [`futures.md`](../futures.md#local-session-presets) when you start curating repeatable setups.

If you prefer to launch ngrok yourself, start the tunnel manually (`ngrok http 8000`) and run the server with `--disable-ngrok`. Provide the forwarding URL to the client as described in the next section.

## 4. Connect with the client

With the server running, start the interactive shell from a new terminal session:

```bash
mcp2term-client --url http://127.0.0.1:8000/mcp
```

The client performs the following on startup:

1. Ensures `xonsh` is installed (installing it automatically if necessary).
2. Creates an MCP session targeting the Streamable HTTP endpoint you supplied.
3. Fetches the remote working directory so the prompt mirrors the server state.

You can now run shell commands as if you were on the remote machine. Output streams arrive in real time, and the prompt updates based on `cd` and environment changes you execute within the client.

### Passing a default timeout

Use `--timeout 120` to apply a default command timeout (in seconds) to every execution. Individual commands can still override the timeout by exporting the `MCP2TERM_COMMAND_TIMEOUT` environment variable on the server side or by building higher-level tools that wrap `run_command`.

## 5. Verifying the round trip

To confirm everything is wired up correctly:

1. Start the server in one terminal with Streamable HTTP as described above.
2. Run the client in another terminal and connect to `http://127.0.0.1:8000/mcp`. If you enabled ngrok, repeat the same step from a different machine using the forwarded HTTPS URL to verify remote connectivity and latency.
3. Execute a simple command such as `uname -a` and observe the output streaming in.
4. Try a failing command (e.g., `ls /nonexistent`) to verify stderr is surfaced immediately along with the non-zero exit status.

When you are finished, exit the client with `Ctrl+D` or `exit`, then stop the server process with `Ctrl+C`.

## 6. Troubleshooting tips

- **Connection refused:** Ensure the server is still running, listening on `127.0.0.1:8000`, and that you did not change the mount path. The client expects the `/mcp` suffix.
- **xonsh install prompts for sudo:** Install within a user-writable virtual environment so no elevated permissions are required.
- **Timeout errors:** Increase the timeout via `--timeout` on the client or set `MCP2TERM_COMMAND_TIMEOUT` before launching the server.
- **Plugin visibility:** Inspect loaded plugins by querying `mcp2term.plugin.GlobalPluginManager.exports` from a Python REPL. Every function, class, and variable exported by the server package is exposed here for plugin authors.

For deeper insight into MCP transports and server capabilities, consult the resources collected in the [`research/`](../research) directory.
