"""Plugin management for the MCP terminal server."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import pkgutil
import sys
from dataclasses import dataclass, field
from importlib import metadata
from types import ModuleType
from typing import Any, Awaitable, Iterable, Mapping, MutableMapping, Protocol, TextIO, runtime_checkable

from .streaming import (
    CommandCompleteEvent,
    CommandOutputChunk,
    CommandStartEvent,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class CommandStreamListener(Protocol):
    """Protocol for plugins interested in command execution events."""

    async def on_command_start(self, event: CommandStartEvent) -> None: ...

    async def on_command_stdout(self, event: CommandOutputChunk) -> None: ...

    async def on_command_stderr(self, event: CommandOutputChunk) -> None: ...

    async def on_command_complete(self, event: CommandCompleteEvent) -> None: ...


@runtime_checkable
class PluginProtocol(Protocol):
    """Protocol implemented by plugin modules."""

    name: str
    version: str

    def activate(self, registry: "PluginRegistry") -> Awaitable[None] | None: ...


class ConsoleEchoListener(CommandStreamListener):
    """Listener that mirrors command activity to the local console."""

    def __init__(
        self,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        prefix: str = "[mcp2term]",
    ) -> None:
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr
        self._prefix = prefix

    def _write_line(self, stream: TextIO, message: str) -> None:
        stream.write(f"{self._prefix} {message}\n")
        stream.flush()

    async def on_command_start(self, event: CommandStartEvent) -> None:
        self._write_line(self._stdout, f"▶ {event.request.command}")
        self._write_line(
            self._stdout,
            f"  cwd: {event.request.working_directory}",
        )

    async def on_command_stdout(self, event: CommandOutputChunk) -> None:
        if not event.data:
            return
        self._stdout.write(event.data)
        self._stdout.flush()

    async def on_command_stderr(self, event: CommandOutputChunk) -> None:
        if not event.data:
            return
        self._stderr.write(event.data)
        self._stderr.flush()

    async def on_command_complete(self, event: CommandCompleteEvent) -> None:
        self._write_line(
            self._stdout,
            f"✔ exit code {event.return_code} ({event.duration:.3f}s)",
        )


@dataclass(slots=True)
class PluginRegistry:
    """Registry exposed to plugins for integration hooks."""

    _manager: "PluginManager"

    def register_command_listener(self, listener: CommandStreamListener) -> None:
        if not isinstance(listener, CommandStreamListener):  # type: ignore[arg-type]
            raise TypeError("Listener must implement CommandStreamListener protocol")
        logger.debug("Registering command listener %s", listener)
        self._manager.command_listeners.append(listener)

    def get_export(self, qualified_name: str) -> Any:
        try:
            return self._manager.exports[qualified_name]
        except KeyError as exc:
            raise KeyError(f"Unknown export: {qualified_name}") from exc

    def list_exports(self) -> Mapping[str, Any]:
        return dict(self._manager.exports)

    def register_export(self, qualified_name: str, value: Any) -> None:
        logger.debug("Registering additional export %s", qualified_name)
        self._manager.exports[qualified_name] = value


@dataclass(slots=True)
class PluginManager:
    """Central plugin manager keeping track of loaded plugins and exports."""

    exports: MutableMapping[str, Any] = field(default_factory=dict)
    command_listeners: list[CommandStreamListener] = field(default_factory=list)
    loaded_plugins: dict[str, PluginProtocol] = field(default_factory=dict)
    entry_point_group: str = "mcp2term.plugins"
    _console_echo_listener: ConsoleEchoListener | None = field(
        default=None, init=False, repr=False
    )
    _export_packages: set[str] = field(default_factory=set, init=False, repr=False)
    _normalized_module_cache: dict[tuple[str, ...], tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._initialize_export_packages()
        self._ensure_console_echo_listener()

    def _ensure_console_echo_listener(self) -> None:
        if self._console_echo_listener is not None:
            return
        listener = ConsoleEchoListener()
        self.command_listeners.append(listener)
        self._console_echo_listener = listener

    def _initialize_export_packages(self) -> None:
        """Seed export packages with known namespaces from this repository."""

        for package_name in ("mcp2term", "mcp2term_client"):
            self.register_export_package(package_name, missing_ok=True)

    def register_export_package(self, package_name: str, *, missing_ok: bool = False) -> None:
        """Register an importable package whose members should be exposed to plugins."""

        try:
            importlib.import_module(package_name)
        except ModuleNotFoundError:
            if missing_ok:
                return
            raise
        self._export_packages.add(package_name)

    def list_export_packages(self) -> tuple[str, ...]:
        """Return the tuple of package names whose exports are tracked."""

        return tuple(sorted(self._export_packages))

    def set_console_echo_enabled(self, enabled: bool) -> None:
        """Enable or disable mirroring command activity to the local console."""

        if enabled:
            if self._console_echo_listener is None:
                listener = ConsoleEchoListener()
                self.command_listeners.append(listener)
                self._console_echo_listener = listener
            return

        if self._console_echo_listener is None:
            return
        try:
            self.command_listeners.remove(self._console_echo_listener)
        except ValueError:  # pragma: no cover - defensive guard
            pass
        self._console_echo_listener = None

    def refresh_exports(self) -> None:
        """Refresh exported symbols from loaded mcp2term modules."""

        logger.debug("Refreshing plugin exports")
        module_names = set(self._gather_known_module_names())
        for module_name in sorted(module_names):
            try:
                module = importlib.import_module(module_name)
            except Exception:  # pragma: no cover - defensive guard for optional deps
                logger.exception("Failed to import %s while collecting plugin exports", module_name)
                continue
            self.register_module_exports(module)

    def register_module_exports(self, module: ModuleType) -> None:
        """Register exports from a specific module."""

        for attr_name, value in vars(module).items():
            if attr_name.startswith("_"):
                continue
            qualified_name = f"{module.__name__}.{attr_name}"
            self.exports[qualified_name] = value

    def _gather_known_module_names(self) -> Iterable[str]:
        """Collect module names from registered packages and the current interpreter state."""

        discovered: set[str] = set()
        package_prefixes = tuple(sorted(self._export_packages))
        for package_name in package_prefixes:
            discovered.update(self._discover_package_modules(package_name))
        for module_name, module in list(sys.modules.items()):
            if package_prefixes and not module_name.startswith(package_prefixes):
                continue
            if not isinstance(module, ModuleType):
                continue
            discovered.add(module_name)
        return discovered

    def _discover_package_modules(self, package_name: str) -> set[str]:
        """Return module names found within ``package_name`` without importing them eagerly."""

        names: set[str] = set()
        try:
            package = importlib.import_module(package_name)
        except ModuleNotFoundError:
            return names
        names.add(package.__name__)
        package_path = getattr(package, "__path__", None)
        if package_path is None:
            return names
        for module_info in pkgutil.walk_packages(package_path, f"{package.__name__}."):
            names.add(module_info.name)
        return names

    async def load_plugins_async(self, module_names: tuple[str, ...]) -> None:
        """Load plugin modules and entry point plugins."""

        normalized_modules = self._normalize_module_names(module_names)
        skip_module_names: set[str] = set()

        for entry_point in self._iter_entry_points():
            key = f"entry_point:{entry_point.name}"
            if key in self.loaded_plugins:
                logger.debug("Entry point plugin %s already loaded", entry_point.name)
                continue
            plugin_info = self._load_entry_point(entry_point)
            if plugin_info is None:
                continue
            plugin_key, plugin, module_name = plugin_info
            if module_name:
                skip_module_names.add(module_name)
            await self._activate_plugin(plugin_key, plugin)

        for module_name in normalized_modules:
            if module_name in skip_module_names:
                logger.debug(
                    "Skipping configured module %s because it was provided by an entry point",
                    module_name,
                )
                continue
            await self._load_plugin_module(module_name)

    def _normalize_module_names(self, module_names: Iterable[str]) -> tuple[str, ...]:
        key = tuple(module_names)
        if key in self._normalized_module_cache:
            return self._normalized_module_cache[key]
        seen: set[str] = set()
        normalized: list[str] = []
        for module_name in module_names:
            name = module_name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            normalized.append(name)
        result = tuple(normalized)
        self._normalized_module_cache[key] = result
        return result

    async def _activate_plugin(self, key: str, plugin: PluginProtocol) -> None:
        logger.info("Activating plugin %s", key)
        registry = PluginRegistry(self)
        activation_result = plugin.activate(registry)
        if inspect.isawaitable(activation_result):
            await activation_result
        self.loaded_plugins[key] = plugin
        self.refresh_exports()

    async def _load_plugin_module(self, module_name: str) -> None:
        if module_name in self.loaded_plugins:
            logger.debug("Plugin %s already loaded", module_name)
            return
        logger.info("Loading plugin module %s", module_name)
        module = importlib.import_module(module_name)
        plugin = self._locate_plugin(module)
        await self._activate_plugin(module_name, plugin)

    def _iter_entry_points(self) -> Iterable[metadata.EntryPoint]:
        try:
            entry_points = metadata.entry_points()
        except Exception:  # pragma: no cover - defensive guard
            logger.exception("Failed to enumerate entry points for plugin discovery")
            return ()
        if hasattr(entry_points, "select"):
            selected = entry_points.select(group=self.entry_point_group)
        else:  # pragma: no cover - Python < 3.10 compatibility path
            selected = entry_points.get(self.entry_point_group, ())  # type: ignore[assignment]
        return tuple(selected)

    def _load_entry_point(
        self, entry_point: metadata.EntryPoint
    ) -> tuple[str, PluginProtocol, str | None] | None:
        try:
            loaded = entry_point.load()
        except Exception:
            logger.exception("Failed to load entry point %s", entry_point.name)
            return None

        try:
            plugin_object = self._coerce_plugin_object(loaded, entry_point=entry_point)
        except Exception:  # pragma: no cover - defensive logging for unexpected errors
            logger.exception(
                "Unhandled error while preparing plugin from entry point %s",
                entry_point.name,
            )
            return None
        if plugin_object is None:
            logger.warning(
                "Entry point %s did not resolve to a PluginProtocol instance", entry_point.name
            )
            return None

        module_name = getattr(plugin_object, "__module__", None)
        plugin_key = f"entry_point:{entry_point.name}"
        return plugin_key, plugin_object, module_name

    def _coerce_plugin_object(
        self,
        candidate: Any,
        *,
        entry_point: metadata.EntryPoint | None = None,
    ) -> PluginProtocol | None:
        if isinstance(candidate, PluginProtocol):  # type: ignore[arg-type]
            return candidate
        if isinstance(candidate, ModuleType):
            return self._locate_plugin(candidate)
        if isinstance(candidate, str):
            module = importlib.import_module(candidate)
            return self._locate_plugin(module)
        if inspect.isclass(candidate):
            try:
                instance = candidate()
            except Exception:
                logger.exception(
                    "Failed to instantiate plugin class %s from entry point %s",
                    candidate,
                    entry_point.name if entry_point else "<unknown>",
                )
                return None
            return self._coerce_plugin_object(instance, entry_point=entry_point)
        return None

    def register_export(self, qualified_name: str, value: Any) -> None:
        """Register or update an export available to plugins."""

        logger.debug("Registering export %s", qualified_name)
        self.exports[qualified_name] = value

    def load_plugins(self, module_names: tuple[str, ...]) -> None:
        """Synchronously load plugins, running the event loop if needed."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.load_plugins_async(module_names))
        else:
            raise RuntimeError(
                "An event loop is already running; call load_plugins_async instead of load_plugins"
            )

    def _locate_plugin(self, module: ModuleType) -> PluginProtocol:
        candidates = [
            getattr(module, attr)
            for attr in ("PLUGIN", "plugin", "PLUGIN_INSTANCE")
            if hasattr(module, attr)
        ]
        for candidate in candidates:
            if isinstance(candidate, PluginProtocol):  # type: ignore[arg-type]
                return candidate
        for value in vars(module).values():
            if isinstance(value, PluginProtocol):  # type: ignore[arg-type]
                return value
        raise ValueError(f"Module {module.__name__} does not define a plugin instance")

    async def emit_command_start(self, event: CommandStartEvent) -> None:
        await self._broadcast("on_command_start", event)

    async def emit_stdout(self, event: CommandOutputChunk) -> None:
        await self._broadcast("on_command_stdout", event)

    async def emit_stderr(self, event: CommandOutputChunk) -> None:
        await self._broadcast("on_command_stderr", event)

    async def emit_command_complete(self, event: CommandCompleteEvent) -> None:
        await self._broadcast("on_command_complete", event)

    async def _broadcast(self, method: str, event: Any) -> None:
        if not self.command_listeners:
            return
        awaitables: list[Awaitable[None]] = []
        for listener in list(self.command_listeners):
            handler = getattr(listener, method, None)
            if handler is None:
                continue
            try:
                result = handler(event)
            except Exception:  # pragma: no cover - defensive logging
                logger.exception("Command listener %s failed invoking %s", listener, method)
                continue
            if inspect.isawaitable(result):
                awaitables.append(result)  # type: ignore[arg-type]
        if awaitables:
            await asyncio.gather(*awaitables, return_exceptions=False)


GlobalPluginManager = PluginManager()
