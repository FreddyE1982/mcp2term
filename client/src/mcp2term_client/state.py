"""State tracking for the remote MCP shell session."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class RemoteShellState:
    """Represents mutable session state for the remote shell."""

    cwd: str
    environment: Dict[str, str] = field(default_factory=dict)

    def build_environment(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        """Return the environment dictionary for a command execution."""

        merged = dict(self.environment)
        if overrides:
            merged.update(overrides)
        return merged

    def update_environment(self, updates: dict[str, str]) -> None:
        """Persistently update the session environment."""

        self.environment.update(updates)

    def remove_environment_keys(self, keys: list[str]) -> None:
        """Remove environment keys when present."""

        for key in keys:
            self.environment.pop(key, None)
