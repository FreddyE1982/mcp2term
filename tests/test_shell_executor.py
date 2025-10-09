"""Tests for the shell command executor."""

import asyncio

import pytest

from mcp2term.config import ServerConfig
from mcp2term.plugin import PluginManager, PluginRegistry
from mcp2term.shell import CommandResult, CommandTimeoutError, ShellCommandExecutor
from mcp2term.streaming import InMemoryStreamRecorder


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_shell_executor_streams_output(use_real_dependencies: bool) -> None:
    config = ServerConfig()
    manager = PluginManager()
    manager.refresh_exports()
    recorder = InMemoryStreamRecorder()
    PluginRegistry(manager).register_command_listener(recorder)
    executor = ShellCommandExecutor(config, manager)

    result = asyncio.run(executor.run("printf 'hello' && printf ' world\\n'"))

    assert "hello world" in result.stdout
    assert result.request.command_id
    assert recorder.start_event is not None
    assert recorder.complete_event is not None
    stdout_chunks = [chunk for chunk in recorder.chunks if chunk.stream == "stdout"]
    assert stdout_chunks, "Expected stdout chunks to be recorded"


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_shell_executor_timeout_records_completion(use_real_dependencies: bool) -> None:
    config = ServerConfig()
    manager = PluginManager()
    manager.refresh_exports()
    recorder = InMemoryStreamRecorder()
    PluginRegistry(manager).register_command_listener(recorder)
    executor = ShellCommandExecutor(config, manager)

    async def invoke_timeout() -> None:
        await executor.run("python -c 'import time; time.sleep(1)'", timeout=0.1)

    with pytest.raises(CommandTimeoutError) as excinfo:
        asyncio.run(invoke_timeout())

    timeout_error = excinfo.value
    assert timeout_error.event is not None
    assert timeout_error.event.return_code != 0
    assert recorder.complete_event is not None
    assert recorder.complete_event.return_code == timeout_error.event.return_code


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_console_echo_mirrors_stdout(use_real_dependencies: bool, capsys) -> None:
    config = ServerConfig()
    manager = PluginManager()
    manager.refresh_exports()
    executor = ShellCommandExecutor(config, manager)

    asyncio.run(executor.run("printf 'hello world\\n'"))

    captured = capsys.readouterr()
    assert "▶ printf 'hello world\\n'" in captured.out
    assert "hello world" in captured.out
    assert "✔ exit code" in captured.out


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_console_echo_mirrors_stderr(use_real_dependencies: bool, capsys) -> None:
    config = ServerConfig()
    manager = PluginManager()
    manager.refresh_exports()
    executor = ShellCommandExecutor(config, manager)

    asyncio.run(
        executor.run("python -c \"import sys; sys.stderr.write('boom\\n')\"")
    )

    captured = capsys.readouterr()
    assert "boom" in captured.err


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_console_echo_can_be_disabled(use_real_dependencies: bool, capsys) -> None:
    config = ServerConfig()
    manager = PluginManager()
    manager.refresh_exports()
    manager.set_console_echo_enabled(False)
    executor = ShellCommandExecutor(config, manager)

    asyncio.run(executor.run("printf 'quiet run\\n'"))


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_shell_executor_interrupts_running_command(use_real_dependencies: bool) -> None:
    config = ServerConfig()
    manager = PluginManager()
    manager.refresh_exports()
    recorder = InMemoryStreamRecorder()
    PluginRegistry(manager).register_command_listener(recorder)
    executor = ShellCommandExecutor(config, manager)

    async def run_and_interrupt() -> CommandResult:
        command_id = "test-interrupt"
        task = asyncio.create_task(
            executor.run(
                "python -c 'import time; time.sleep(5)'",
                command_id=command_id,
            )
        )
        while recorder.start_event is None:
            await asyncio.sleep(0.05)
        # Wait briefly to ensure the process is actively sleeping.
        await asyncio.sleep(0.1)
        delivered = await executor.interrupt(command_id)
        assert delivered, "Expected interrupt signal to be delivered"
        result = await task
        return result

    result = asyncio.run(run_and_interrupt())
    assert result.return_code != 0


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_shell_executor_handles_large_output(use_real_dependencies: bool) -> None:
    config = ServerConfig(stream_chunk_size=4096)
    manager = PluginManager()
    manager.refresh_exports()
    recorder = InMemoryStreamRecorder()
    PluginRegistry(manager).register_command_listener(recorder)
    executor = ShellCommandExecutor(config, manager)

    command = "python -c \"import sys; sys.stdout.write('x'*131072)\""
    result = asyncio.run(executor.run(command))

    assert len(result.stdout) == 131072
    assert any(chunk.stream == "stdout" for chunk in recorder.chunks)
    assert result.return_code == 0
