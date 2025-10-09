"""Plugin management for the MCP terminal server."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Awaitable, Mapping, MutableMapping, Protocol, TextIO, runtime_checkable

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
    _console_echo_listener: ConsoleEchoListener | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._ensure_console_echo_listener()

    def _ensure_console_echo_listener(self) -> None:
        if self._console_echo_listener is not None:
            return
        listener = ConsoleEchoListener()
        self.command_listeners.append(listener)
        self._console_echo_listener = listener

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
        for module_name, module in list(sys.modules.items()):
            if not module_name.startswith("mcp2term"):
                continue
            if not isinstance(module, ModuleType):
                continue
            for attr_name, value in vars(module).items():
                if attr_name.startswith("_"):
                    continue
                qualified_name = f"{module_name}.{attr_name}"
                self.exports[qualified_name] = value

    def register_module_exports(self, module: ModuleType) -> None:
        """Register exports from a specific module."""

        for attr_name, value in vars(module).items():
            if attr_name.startswith("_"):
                continue
            qualified_name = f"{module.__name__}.{attr_name}"
            self.exports[qualified_name] = value

    async def load_plugins_async(self, module_names: tuple[str, ...]) -> None:
        """Load plugin modules by dotted names."""

        for module_name in module_names:
            if module_name in self.loaded_plugins:
                logger.debug("Plugin %s already loaded", module_name)
                continue
            logger.info("Loading plugin module %s", module_name)
            module = importlib.import_module(module_name)
            plugin = self._locate_plugin(module)
            registry = PluginRegistry(self)
            activation_result = plugin.activate(registry)
            if inspect.isawaitable(activation_result):
                await activation_result
            self.loaded_plugins[module_name] = plugin
            self.refresh_exports()

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
