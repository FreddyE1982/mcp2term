"""Plugin management for the MCP terminal server."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import traceback
import pkgutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import (
    Any,
    Awaitable,
    Iterable,
    Mapping,
    MutableMapping,
    Protocol,
    TextIO,
    runtime_checkable,
)

from .files import FileOperationResult
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


@dataclass(slots=True)
class ServerWarningEvent:
    """Warning raised by the server runtime that should be surfaced to plugins."""

    tool_name: str
    message: str
    details: Mapping[str, Any]
    exception: BaseException | None = None

    def formatted_exception(self) -> str | None:
        if self.exception is None:
            return None
        return "".join(traceback.format_exception_only(type(self.exception), self.exception)).strip()


@runtime_checkable
class ServerWarningListener(Protocol):
    """Protocol for plugins interested in server warning notifications."""

    async def on_server_warning(self, event: ServerWarningEvent) -> None: ...


@dataclass(slots=True)
class FileOperationEvent:
    """Event describing the outcome of a file management operation."""

    raw_path: str
    operation: str
    arguments: Mapping[str, Any]
    result: FileOperationResult
    warning: str | None = None

    @property
    def path(self) -> Path:
        """Return the resolved file path associated with the operation."""

        return self.result.path


@runtime_checkable
class FileOperationListener(Protocol):
    """Protocol for plugins observing file management events."""

    async def on_file_operation(self, event: FileOperationEvent) -> None: ...


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

    @property
    def prefix(self) -> str:
        """Return the prefix prepended to every mirrored log line."""

        return self._prefix

    def update_streams(self, *, stdout: TextIO | None = None, stderr: TextIO | None = None) -> None:
        """Rebind the underlying output streams used for console mirroring."""

        if stdout is not None:
            self._stdout = stdout
        if stderr is not None:
            self._stderr = stderr

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

    def register_warning_listener(self, listener: ServerWarningListener) -> None:
        if not isinstance(listener, ServerWarningListener):  # type: ignore[arg-type]
            raise TypeError("Listener must implement ServerWarningListener protocol")
        logger.debug("Registering server warning listener %s", listener)
        self._manager.warning_listeners.append(listener)

    def register_file_operation_listener(self, listener: FileOperationListener) -> None:
        if not isinstance(listener, FileOperationListener):  # type: ignore[arg-type]
            raise TypeError("Listener must implement FileOperationListener protocol")
        logger.debug("Registering file operation listener %s", listener)
        self._manager.file_operation_listeners.append(listener)


@dataclass(slots=True)
class PluginManager:
    """Central plugin manager keeping track of loaded plugins and exports."""

    exports: MutableMapping[str, Any] = field(default_factory=dict)
    command_listeners: list[CommandStreamListener] = field(default_factory=list)
    warning_listeners: list[ServerWarningListener] = field(default_factory=list)
    file_operation_listeners: list[FileOperationListener] = field(default_factory=list)
    loaded_plugins: dict[str, PluginProtocol] = field(default_factory=dict)
    _console_echo_listener: ConsoleEchoListener | None = field(
        default=None, init=False, repr=False
    )
    _export_packages: set[str] = field(default_factory=set, init=False, repr=False)
    _console_stdout: TextIO = field(init=False, repr=False)
    _console_stderr: TextIO = field(init=False, repr=False)
    _console_prefix: str = field(default="[mcp2term]", init=False, repr=False)
    _console_echo_enabled: bool = field(default=True, init=False, repr=False)

    def __post_init__(self) -> None:
        self._console_stdout = sys.stdout
        self._console_stderr = sys.stderr
        self._initialize_export_packages()
        self._ensure_console_echo_listener()

    def _ensure_console_echo_listener(self) -> None:
        if self._console_echo_listener is not None:
            return
        if not self._console_echo_enabled:
            return
        listener = ConsoleEchoListener(
            stdout=self._console_stdout,
            stderr=self._console_stderr,
            prefix=self._console_prefix,
        )
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
            self._console_echo_enabled = True
            if self._console_echo_listener is None:
                listener = ConsoleEchoListener(
                    stdout=self._console_stdout,
                    stderr=self._console_stderr,
                    prefix=self._console_prefix,
                )
                self.command_listeners.append(listener)
                self._console_echo_listener = listener
            return

        if self._console_echo_listener is None:
            self._console_echo_enabled = False
            return
        try:
            self.command_listeners.remove(self._console_echo_listener)
        except ValueError:  # pragma: no cover - defensive guard
            pass
        self._console_echo_listener = None
        self._console_echo_enabled = False

    def update_console_echo_streams(self, *, stdout: TextIO, stderr: TextIO) -> None:
        """Update the streams used for console mirroring without toggling state."""

        self._console_stdout = stdout
        self._console_stderr = stderr
        listener = self._console_echo_listener
        if listener is not None:
            listener.update_streams(stdout=stdout, stderr=stderr)
        elif self._console_echo_enabled:
            listener = ConsoleEchoListener(
                stdout=stdout,
                stderr=stderr,
                prefix=self._console_prefix,
            )
            self.command_listeners.append(listener)
            self._console_echo_listener = listener

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
        await self._broadcast(self.command_listeners, "on_command_start", event)

    async def emit_stdout(self, event: CommandOutputChunk) -> None:
        await self._broadcast(self.command_listeners, "on_command_stdout", event)

    async def emit_stderr(self, event: CommandOutputChunk) -> None:
        await self._broadcast(self.command_listeners, "on_command_stderr", event)

    async def emit_command_complete(self, event: CommandCompleteEvent) -> None:
        await self._broadcast(self.command_listeners, "on_command_complete", event)

    async def emit_server_warning(self, event: ServerWarningEvent) -> None:
        await self._broadcast(self.warning_listeners, "on_server_warning", event)

    async def emit_file_operation(self, event: FileOperationEvent) -> None:
        await self._broadcast(self.file_operation_listeners, "on_file_operation", event)

    async def _broadcast(
        self,
        listeners: Iterable[Any],
        method: str,
        event: Any,
    ) -> None:
        listeners = list(listeners)
        if not listeners:
            return
        awaitables: list[Awaitable[None]] = []
        for listener in listeners:
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
