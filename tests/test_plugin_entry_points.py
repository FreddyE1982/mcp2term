"""Tests for plugin discovery via Python entry points."""

from __future__ import annotations

import asyncio
import importlib
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from mcp2term.plugin import PluginManager


def _write_entry_point_distribution(base_dir: Path, package_name: str) -> str:
    """Create a temporary plugin package and distribution metadata."""

    package_dir = base_dir / package_name
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text(
        "# Package marker for entry point plugin tests\n",
        encoding="utf-8",
    )

    module_name = f"{package_name}.entry_point"
    module_path = package_dir / "entry_point.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            from dataclasses import dataclass

            from mcp2term.plugin import (
                CommandStreamListener,
                PluginProtocol,
                PluginRegistry,
            )
            from mcp2term.streaming import (
                CommandCompleteEvent,
                CommandOutputChunk,
                CommandStartEvent,
            )


            activation_log: list[str] = []


            @dataclass(slots=True)
            class RecordingListener(CommandStreamListener):
                log: list[str]

                async def on_command_start(self, event: CommandStartEvent) -> None:
                    self.log.append(f"start:{event.request.command}")

                async def on_command_stdout(self, event: CommandOutputChunk) -> None:
                    self.log.append(f"stdout:{len(event.data)}")

                async def on_command_stderr(self, event: CommandOutputChunk) -> None:
                    self.log.append(f"stderr:{len(event.data)}")

                async def on_command_complete(self, event: CommandCompleteEvent) -> None:
                    self.log.append(f"complete:{event.return_code}")


            class EntryPointPlugin(PluginProtocol):
                name = "entry-point-test"
                version = "1.0.0"

                def activate(self, registry: PluginRegistry) -> None:
                    activation_log.append("activated")
                    registry.register_export(f"{__name__}.activation_log", activation_log)
                    registry.register_command_listener(RecordingListener(activation_log))


            PLUGIN = EntryPointPlugin()
            """
        ),
        encoding="utf-8",
    )

    distribution_name = f"{package_name}-0.0.0.dist-info"
    dist_info = base_dir / distribution_name
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        textwrap.dedent(
            f"""
            Metadata-Version: 2.1
            Name: {package_name}
            Version: 0.0.0
            """
        ),
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        textwrap.dedent(
            """
            [mcp2term.plugins]
            entry-point-test = {module_name}:PLUGIN
            """
        ).format(module_name=module_name),
        encoding="utf-8",
    )

    return module_name


def _cleanup_modules(module_name: str) -> None:
    parts = module_name.split(".")
    for index in range(len(parts), 0, -1):
        sys.modules.pop(".".join(parts[:index]), None)


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_entry_point_plugin_activation(tmp_path: Path, use_real_dependencies: bool) -> None:
    package_name = f"ep_plugin_{uuid.uuid4().hex}"
    module_name = _write_entry_point_distribution(tmp_path, package_name)

    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    try:
        manager = PluginManager()
        manager.refresh_exports()
        asyncio.run(manager.load_plugins_async(()))

        plugin_module = importlib.import_module(module_name)
        export_key = f"{module_name}.activation_log"
        assert export_key in manager.exports
        activation_log = manager.exports[export_key]
        assert activation_log == plugin_module.activation_log
        assert activation_log.count("activated") == 1
        assert any(
            listener.__class__.__module__ == module_name
            for listener in manager.command_listeners
        )
    finally:
        sys.path.remove(str(tmp_path))
        _cleanup_modules(module_name)


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_entry_point_plugin_not_reactivated(tmp_path: Path, use_real_dependencies: bool) -> None:
    package_name = f"ep_plugin_{uuid.uuid4().hex}"
    module_name = _write_entry_point_distribution(tmp_path, package_name)

    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    try:
        manager = PluginManager()
        manager.refresh_exports()
        asyncio.run(manager.load_plugins_async(()))
        asyncio.run(manager.load_plugins_async(()))

        export_key = f"{module_name}.activation_log"
        assert export_key in manager.exports
        activation_log = manager.exports[export_key]
        assert activation_log.count("activated") == 1
    finally:
        sys.path.remove(str(tmp_path))
        _cleanup_modules(module_name)
