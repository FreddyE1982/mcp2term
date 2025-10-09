"""Utilities for ensuring xonsh is available before launching the client."""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
import threading
from typing import Final

_INSTALL_LOCK: Final = threading.Lock()


def _is_xonsh_available() -> bool:
    """Return True when the xonsh module can be imported."""

    return importlib.util.find_spec("xonsh") is not None


def ensure_xonsh_installed(python_executable: str | None = None) -> None:
    """Install xonsh via pip if it is not already present."""

    if _is_xonsh_available():
        return

    with _INSTALL_LOCK:
        if _is_xonsh_available():
            return

        executable = python_executable or sys.executable
        command = [executable, "-m", "pip", "install", "xonsh"]
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                "Failed to install xonsh automatically. "
                "Install it manually with 'pip install xonsh'."
            )

        importlib.invalidate_caches()
        importlib.import_module("xonsh")
