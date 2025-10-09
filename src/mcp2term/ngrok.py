"""Utilities for managing ngrok tunnels used by the MCP terminal server."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence
from urllib import error, parse, request

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        return _utcnow()
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        logger.debug("Failed to parse ngrok timestamp %s", value, exc_info=True)
        return _utcnow()


def _canonical_addresses(host: str, port: int) -> set[str]:
    base_variants = {
        f"{host}:{port}",
        f"http://{host}:{port}",
        f"https://{host}:{port}",
        f"localhost:{port}",
        f"http://localhost:{port}",
        f"https://localhost:{port}",
        f"127.0.0.1:{port}",
        f"http://127.0.0.1:{port}",
        f"https://127.0.0.1:{port}",
        f"0.0.0.0:{port}",
        f"http://0.0.0.0:{port}",
        f"https://0.0.0.0:{port}",
    }
    return base_variants


@dataclass(slots=True)
class NgrokTunnel:
    """Describes an active ngrok tunnel."""

    name: str
    public_url: str
    proto: str
    host: str
    port: int
    started_at: datetime
    inspect_url: str
    raw: Mapping[str, Any]

    @property
    def is_https(self) -> bool:
        return self.public_url.startswith("https://")


@dataclass(slots=True)
class NgrokSettings:
    """Configuration for the ngrok integration."""

    enabled: bool = True
    binary: str = "ngrok"
    api_base_url: str = "http://127.0.0.1:4040"
    region: str | None = None
    config_path: Path | None = None
    hostname: str | None = None
    domain: str | None = "alpaca-model-easily.ngrok-free.app"
    edge: str | None = None
    log_level: str = "info"
    transports: tuple[str, ...] = ("sse", "streamable-http")
    start_timeout: float = 15.0
    poll_interval: float = 0.5
    request_timeout: float = 5.0
    shutdown_timeout: float = 5.0
    extra_args: tuple[str, ...] = ()
    environment: MutableMapping[str, str] = field(default_factory=dict)

    def api_url(self, path: str) -> str:
        base = self.api_base_url.rstrip("/")
        normalized = path if path.startswith("/") else f"/{path}"
        return f"{base}{normalized}"

    def is_enabled_for(self, transport: str) -> bool:
        return self.enabled and transport in self.transports


class NgrokController:
    """Lifecycle manager for an ngrok tunnel."""

    def __init__(self, settings: NgrokSettings) -> None:
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._tunnel: NgrokTunnel | None = None

    @property
    def tunnel(self) -> NgrokTunnel | None:
        return self._tunnel

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, *, host: str, port: int, labels: Sequence[str] | None = None) -> NgrokTunnel:
        if not self.settings.enabled:
            raise RuntimeError("NgrokController.start called while ngrok integration disabled")
        if self.is_running():
            raise RuntimeError("ngrok tunnel already active")

        command = self._build_command(host, port, labels)
        logger.debug("Starting ngrok with command: %s", command)
        environment = os.environ.copy()
        environment.update(self.settings.environment)

        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
            )
        except FileNotFoundError as exc:  # pragma: no cover - depends on system
            raise RuntimeError(
                f"Unable to launch ngrok executable '{self.settings.binary}'. Ensure it is installed and on PATH."
            ) from exc

        self._stop_event.clear()
        if self._process.stdout:
            self._stdout_thread = threading.Thread(
                target=self._consume_stdout,
                args=(self._process.stdout,),
                name="ngrok-stdout-listener",
                daemon=True,
            )
            self._stdout_thread.start()

        try:
            tunnel = self._wait_for_tunnel(host, port)
        except Exception:
            self.stop(force=True)
            raise

        self._tunnel = tunnel
        logger.info("ngrok tunnel established at %s", tunnel.public_url)
        return tunnel

    def stop(self, *, force: bool = False) -> None:
        if self._tunnel and not force:
            try:
                self._delete_tunnel(self._tunnel)
            except Exception:  # pragma: no cover - defensive logging
                logger.warning("Failed to delete ngrok tunnel via API", exc_info=True)
        self._tunnel = None

        if self._process is None:
            return

        if self.is_running():
            self._process.terminate()
            try:
                self._process.wait(timeout=self.settings.shutdown_timeout)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                logger.debug("ngrok process did not exit in time; killing")
                self._process.kill()
                self._process.wait()

        self._stop_event.set()
        if self._stdout_thread and self._stdout_thread.is_alive():
            self._stdout_thread.join(timeout=1)
        if self._process.stdout:
            try:
                self._process.stdout.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.debug("Error closing ngrok stdout", exc_info=True)
        self._process = None
        self._stdout_thread = None

    @contextmanager
    def connect(self, *, host: str, port: int, labels: Sequence[str] | None = None) -> Iterator[NgrokTunnel]:
        tunnel = self.start(host=host, port=port, labels=labels)
        try:
            yield tunnel
        finally:
            self.stop()

    def _build_command(self, host: str, port: int, labels: Sequence[str] | None) -> list[str]:
        cmd = [self.settings.binary, "http", f"{host}:{port}"]
        if self.settings.region:
            cmd.append(f"--region={self.settings.region}")
        if self.settings.config_path:
            cmd.append(f"--config={self.settings.config_path}")
        if self.settings.hostname:
            cmd.append(f"--hostname={self.settings.hostname}")
        if self.settings.domain:
            cmd.append(f"--domain={self.settings.domain}")
        if self.settings.edge:
            cmd.append(f"--edge={self.settings.edge}")
        cmd.append(f"--log-level={self.settings.log_level}")
        cmd.append("--log=stdout")
        cmd.append("--log-format=json")
        for label in labels or ():
            cmd.append(f"--metadata={label}")
        cmd.extend(self.settings.extra_args)
        return cmd

    def _consume_stdout(self, stream: Iterator[str]) -> None:
        try:
            for line in stream:
                if self._stop_event.is_set():
                    break
                logger.debug("ngrok: %s", line.rstrip())
        except Exception:  # pragma: no cover - defensive logging
            logger.debug("Error consuming ngrok output", exc_info=True)

    def _wait_for_tunnel(self, host: str, port: int) -> NgrokTunnel:
        deadline = time.monotonic() + self.settings.start_timeout
        target_addresses = _canonical_addresses(host, port)

        while time.monotonic() < deadline:
            if self._process and self._process.poll() is not None:
                raise RuntimeError(
                    f"ngrok process exited unexpectedly with code {self._process.returncode}"
                )
            try:
                tunnels = self._list_tunnels()
            except ConnectionError:
                tunnels = []
            candidate = self._select_tunnel(tunnels, target_addresses)
            if candidate:
                return self._create_tunnel(candidate, host, port)
            time.sleep(self.settings.poll_interval)

        raise TimeoutError(
            "Timed out waiting for ngrok tunnel to become available. "
            "Check ngrok logs for details."
        )

    def _select_tunnel(
        self, tunnels: Iterable[Mapping[str, Any]], target_addresses: set[str]
    ) -> Mapping[str, Any] | None:
        https_candidate: Mapping[str, Any] | None = None
        for tunnel in tunnels:
            if not isinstance(tunnel, Mapping):
                continue
            addr = str(tunnel.get("config", {}).get("addr", ""))
            forwards_to = str(tunnel.get("forwards_to", ""))
            if addr in target_addresses or forwards_to in target_addresses:
                if str(tunnel.get("proto")) == "https":
                    return tunnel
                https_candidate = tunnel
        return https_candidate

    def _create_tunnel(self, data: Mapping[str, Any], host: str, port: int) -> NgrokTunnel:
        public_url = str(data.get("public_url", ""))
        if not public_url:
            raise RuntimeError("ngrok API returned tunnel without public_url")
        name = str(data.get("name", "")) or public_url
        inspect_url = str(data.get("inspect_url", ""))
        started_at = _parse_timestamp(data.get("created_at"))
        return NgrokTunnel(
            name=name,
            public_url=public_url,
            proto=str(data.get("proto", "")),
            host=host,
            port=port,
            started_at=started_at,
            inspect_url=inspect_url,
            raw=data,
        )

    def _list_tunnels(self) -> list[Mapping[str, Any]]:
        try:
            payload = self._request_json("GET", "/api/tunnels")
        except error.URLError as exc:  # pragma: no cover - network dependent
            raise ConnectionError("Unable to reach ngrok API") from exc
        tunnels = payload.get("tunnels", []) if isinstance(payload, Mapping) else []
        return [t for t in tunnels if isinstance(t, Mapping)]

    def _delete_tunnel(self, tunnel: NgrokTunnel) -> None:
        encoded_name = parse.quote(tunnel.name, safe="")
        try:
            self._request_json("DELETE", f"/api/tunnels/{encoded_name}")
        except error.URLError:
            logger.debug("Failed to delete ngrok tunnel via API", exc_info=True)

    def _request_json(self, method: str, path: str, data: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        url = self.settings.api_url(path)
        body: bytes | None = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
        req = request.Request(url, data=body, method=method.upper())
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with request.urlopen(req, timeout=self.settings.request_timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            payload = resp.read()
            text = payload.decode(charset)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                logger.debug("Unexpected response from ngrok API: %s", text)
                return {}


__all__ = ["NgrokController", "NgrokSettings", "NgrokTunnel"]
