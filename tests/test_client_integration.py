"""Integration tests for the xonsh-based client."""

from __future__ import annotations

import asyncio
import os
import queue
import shlex
import subprocess
import sys
import time
import uuid
from concurrent.futures import CancelledError, Future
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from mcp2term_client.input import InputChunk, QueueInputReader
from mcp2term_client.session import (
    CommandResponse,
    RemoteMcpSession,
    RemoteMcpSessionError,
)
from mcp2term_client.shell import RemoteCommandProcessor
from mcp2term_client.state import RemoteShellState


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SERVER_HOST = "127.0.0.1"


@contextmanager
def running_server(*, extra_env: dict[str, str] | None = None) -> str:
    import socket

    env = os.environ.copy()
    env["PYTHONPATH"] = str(os.path.join(ROOT, "src"))
    if extra_env:
        env.update(extra_env)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((SERVER_HOST, 0))
        port = sock.getsockname()[1]
    script = (
        "from mcp2term.config import ServerConfig\n"
        "from mcp2term.server import create_server\n"
        "config = ServerConfig.from_env()\n"
        "server = create_server(config=config)\n"
        f"server.settings.host = '{SERVER_HOST}'\n"
        f"server.settings.port = {port}\n"
        "server.run(transport='streamable-http', mount_path=None)\n"
    )
    command = [sys.executable, "-c", script]
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        server_url = f"http://{SERVER_HOST}:{port}/mcp"
        _wait_for_server(server_url, process)
        yield server_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _wait_for_server(url: str, process: subprocess.Popen[str], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                "Server process exited prematurely:\n"
                f"STDOUT: {stdout.decode().strip()}\n"
                f"STDERR: {stderr.decode().strip()}"
            )
        try:
            if _probe_server(url):
                return
        except Exception:
            pass
        time.sleep(0.2)
    process.terminate()
    stdout, stderr = process.communicate()
    raise RuntimeError(
        "Server did not become ready in time:\n"
        f"STDOUT: {stdout.decode().strip()}\n"
        f"STDERR: {stderr.decode().strip()}"
    )


def _probe_server(url: str) -> bool:
    """Attempt to establish and initialize an MCP session."""

    async def attempt() -> bool:
        try:
            async with streamablehttp_client(url, timeout=5.0, sse_read_timeout=5.0) as (
                read_stream,
                write_stream,
                _,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
            return True
        except Exception:
            return False

    return anyio.run(attempt)


@contextmanager
def not_found_server() -> str:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"missing")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((SERVER_HOST, 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://{SERVER_HOST}:{server.server_address[1]}"
        yield url
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_remote_session_executes_command(use_real_dependencies: bool) -> None:
    with running_server() as url:
        session = RemoteMcpSession(url)
        session.start()
        try:
            cwd = session.resolve_working_directory()
            state = RemoteShellState(cwd=cwd)
            response = session.run_command(
                "echo integration",
                working_directory=state.cwd,
                environment=state.environment,
            )
        finally:
            session.close()
    assert "integration" in response.stdout
    assert response.return_code == 0
    assert response.command_id


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_remote_processor_manages_state(use_real_dependencies: bool) -> None:
    with running_server() as url:
        session = RemoteMcpSession(url)
        session.start()
        try:
            state = RemoteShellState(cwd=session.resolve_working_directory())
            statuses: list[int] = []
            processor = RemoteCommandProcessor(session=session, state=state, status_callback=statuses.append)

            assert processor.execute("pwd") == 0
            assert statuses[-1] == 0

            assert processor.execute("export TEST_VAR=friend") == 0
            assert state.environment["TEST_VAR"] == "friend"

            assert processor.execute("TEST_VAR=world echo $TEST_VAR") == 0
            assert statuses[-1] == 0
            assert state.environment["TEST_VAR"] == "friend"

            assert processor.execute("unset TEST_VAR") == 0
            assert "TEST_VAR" not in state.environment

            assert processor.execute("cd /") == 0
            assert state.cwd == "/"
        finally:
            session.close()


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_remote_session_normalizes_root_url(use_real_dependencies: bool) -> None:
    with running_server() as full_url:
        base_url = full_url.rsplit("/mcp", 1)[0]
        session = RemoteMcpSession(base_url)
        session.start()
        try:
            assert session.endpoint_url.endswith("/mcp")
            cwd = session.resolve_working_directory()
            response = session.run_command(
                "echo normalized",
                working_directory=cwd,
                environment=None,
            )
        finally:
            session.close()
    assert "normalized" in response.stdout
    assert response.return_code == 0
    assert response.command_id


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_remote_session_can_cancel_command(use_real_dependencies: bool) -> None:
    with running_server() as url:
        session = RemoteMcpSession(url)
        session.start()
        try:
            cwd = session.resolve_working_directory()
            command_id, future = session.run_command_async(
                "python -c 'import time; time.sleep(5)'",
                working_directory=cwd,
                environment=None,
            )
            # Allow the command to start before sending the cancellation.
            time.sleep(0.5)
            cancel_response = session.cancel_command(command_id)
            assert cancel_response.command_id == command_id
            assert cancel_response.delivered
            response = future.result(timeout=10.0)
        finally:
            session.close()

    assert response.return_code != 0
    assert response.command_id == command_id


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_remote_session_reports_diagnostics_for_not_found(use_real_dependencies: bool) -> None:
    with not_found_server() as url:
        session = RemoteMcpSession(url)
        with pytest.raises(RemoteMcpSessionError) as excinfo:
            session.start()
        message = str(excinfo.value)
        assert "HTTP 404" in message
        assert url in message
        session.close()


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_remote_session_streams_stdin(use_real_dependencies: bool) -> None:
    with running_server() as url:
        session = RemoteMcpSession(url)
        session.start()
        try:
            cwd = session.resolve_working_directory()
            command_id, future = session.run_command_async(
                "python -c \"import sys; data = sys.stdin.read(); print(data.strip())\"",
                working_directory=cwd,
                environment=None,
            )
            delivered = False
            for _ in range(50):
                delivered = session.send_stdin(command_id, "hello remote\n")
                if delivered:
                    break
                time.sleep(0.1)
            assert delivered, "Failed to deliver stdin to remote command"
            eof_delivered = False
            for _ in range(50):
                eof_delivered = session.send_stdin(command_id, "", eof=True)
                if eof_delivered:
                    break
                time.sleep(0.1)
            assert eof_delivered, "Failed to deliver EOF to remote command"
            response = future.result(timeout=10.0)
        finally:
            session.close()
    assert "hello remote" in response.stdout
    assert response.return_code == 0


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_remote_processor_handles_interactive_cli(use_real_dependencies: bool) -> None:
    with running_server() as url:
        session = RemoteMcpSession(url)
        session.start()
        try:
            state = RemoteShellState(cwd=session.resolve_working_directory())
            statuses: list[int] = []
            errors: list[str] = []
            input_queue: "queue.Queue[InputChunk]" = queue.Queue()
            exit_code = -1
            verify_response = None

            def reader_factory() -> QueueInputReader:
                return QueueInputReader(input_queue)

            processor = RemoteCommandProcessor(
                session=session,
                state=state,
                status_callback=statuses.append,
                output_writer=lambda message: None,
                error_writer=errors.append,
                input_reader_factory=reader_factory,
            )

            def feed() -> None:
                time.sleep(0.5)
                input_queue.put(InputChunk(data="from pathlib import Path\n"))
                time.sleep(0.2)
                input_queue.put(
                    InputChunk(data="Path('interactive_flag.txt').write_text('client!')\n")
                )
                time.sleep(0.2)
                input_queue.put(InputChunk(data="exit()\n"))

            feeder = Thread(target=feed, daemon=True)
            feeder.start()
            exit_code = processor.execute("python")
            feeder.join(timeout=5)

            verify_response = session.run_command(
                "python -c \"import pathlib; print(pathlib.Path('interactive_flag.txt').read_text())\"",
                working_directory=state.cwd,
                environment=None,
            )
            session.run_command(
                "python -c \"import pathlib; pathlib.Path('interactive_flag.txt').unlink(missing_ok=True)\"",
                working_directory=state.cwd,
                environment=None,
            )
        finally:
            session.close()

    assert exit_code == 0
    assert statuses and statuses[-1] == 0
    assert not errors
    assert verify_response is not None
    assert "client!" in verify_response.stdout


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_remote_session_survives_server_warnings(use_real_dependencies: bool) -> None:
    warning_messages: list[str] = []
    response: CommandResponse | None = None
    follow_up = None
    with running_server(extra_env={"MCP2TERM_SHELL": "/nonexistent-mcp-shell"}) as url:
        session = RemoteMcpSession(url, notice_writer=warning_messages.append)
        session.start()
        try:
            response = session.run_command(
                "echo failure",
                working_directory="/",
                environment=None,
            )
            follow_up = session.cancel_command("missing-command")
        finally:
            session.close()

    assert response is not None
    assert response.return_code != 0
    assert response.warnings
    assert follow_up is not None
    assert follow_up.warnings
    assert any("WARNING" in message for message in warning_messages)


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_remote_session_close_cancels_active_requests(use_real_dependencies: bool) -> None:
    with running_server() as url:
        session = RemoteMcpSession(url)
        session.start()
        future: Future[CommandResponse] | None = None
        try:
            cwd = session.resolve_working_directory()
            _, future = session.run_command_async(
                "python -c 'import time; time.sleep(10)'",
                working_directory=cwd,
                environment=None,
            )
            time.sleep(0.5)
        finally:
            session.close()

    assert future is not None
    assert future.cancelled()
    with pytest.raises(CancelledError):
        future.result(timeout=0.1)


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_manage_file_command_executes_remote_operations(
    use_real_dependencies: bool,
) -> None:
    with running_server() as url:
        session = RemoteMcpSession(url)
        session.start()
        state: RemoteShellState | None = None
        target_dir = "mcp-file-tests"
        try:
            cwd = session.resolve_working_directory()
            state = RemoteShellState(cwd=cwd)
            statuses: list[int] = []
            outputs: list[str] = []
            errors: list[str] = []
            processor = RemoteCommandProcessor(
                session=session,
                state=state,
                status_callback=statuses.append,
                output_writer=lambda message: outputs.append(message),
                error_writer=lambda message: errors.append(message),
            )

            unique_name = f"mcp-file-{uuid.uuid4().hex}.txt"
            relative_path = f"{target_dir}/{unique_name}"

            outputs.clear()
            errors.clear()
            create_status = processor.execute(
                f"filetool create {relative_path} --content alpha --create-parents --overwrite"
            )
            assert create_status == 0
            assert statuses and statuses[-1] == 0
            assert not errors
            assert outputs

            outputs.clear()
            errors.clear()
            print_status = processor.execute(f"filetool print {relative_path}")
            assert print_status == 0
            assert statuses and statuses[-1] == 0
            assert not errors
            assert any("| alpha" in line for line in outputs)

            outputs.clear()
            errors.clear()
            locate_status = processor.execute(
                f"filetool locate {relative_path} --content alpha"
            )
            assert locate_status == 0
            assert statuses and statuses[-1] == 0
            assert not errors
            assert any("Line" in line for line in outputs)
        finally:
            cleanup_state = state
            try:
                if cleanup_state is not None:
                    session.run_command(
                        f"rm -rf {shlex.quote(target_dir)}",
                        working_directory=cleanup_state.cwd,
                        environment=cleanup_state.environment,
                    )
            finally:
                session.close()
