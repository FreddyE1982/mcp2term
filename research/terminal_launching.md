# Terminal Launching Strategies for Auxiliary Consoles

- **Linux / FreeBSD:**
  - Preferred generic entry point is `x-terminal-emulator` which respects distro alternatives.
  - Common dedicated emulators support `-e` or `--` for command forwarding: `gnome-terminal --`, `konsole -e`, `kitty --title`, `alacritty -e`, `wezterm start --`, `xterm -e`.
  - Use explicit title flags (`-T`, `--title`, `-t`) so operators can identify the helper window quickly.
- **macOS:**
  - `osascript` can drive the built-in Terminal app: `osascript -e 'tell application "Terminal" to activate' -e 'tell application "Terminal" to do script "<command>"'`.
  - Setting the custom title requires a follow-up AppleScript statement targeting the front window.
- **Windows:**
  - `cmd.exe /c start "<title>" <command...>` opens a new console window, automatically detaching from the parent process.
  - Use `CREATE_NEW_CONSOLE` flag when invoking commands directly with `subprocess` to guarantee a fresh terminal.
- **Operator overrides:**
  - Allow an environment variable such as `MCP2TERM_CHAT_TERMINAL` to provide an explicit command sequence (`python -m ...`, `wezterm start -- ...`).
  - Treat override values `disabled`, `off`, `none`, and `false` as signals to skip launching any helper console.
- **General guidelines:**
  - Always detach helper windows (`start_new_session=True` on POSIX, `CREATE_NEW_CONSOLE` on Windows) so the MCP server can shut down independently.
  - Provide friendly logging when no terminal is available to aid headless deployments.
