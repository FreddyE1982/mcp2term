# Xonsh Installation Notes

- Source: https://xon.sh/
- Installation via pip: `pip install xonsh` installs the latest release from PyPI (currently 0.19.9).
- After installation, launch the interactive shell with the `xonsh` command. To make it the login shell run `chsh -s $(which xonsh)` (requires user permissions).
- Optional enhancements include installing prompt_toolkit support with `xpip install -U 'xonsh[full]'` as suggested by the welcome banner.
- Configuration lives in `~/.xonshrc`; create it manually or via `xonfig web`.
