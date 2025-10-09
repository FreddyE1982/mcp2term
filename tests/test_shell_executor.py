"""Tests for the shell command executor."""

import pytest

from mcp2term.config import ServerConfig
from mcp2term.plugin import PluginManager, PluginRegistry
from mcp2term.shell import CommandTimeoutError, ShellCommandExecutor
from mcp2term.streaming import InMemoryStreamRecorder


@pytest.mark.asyncio
@pytest.mark.parametrize("use_real_dependencies", [False, True])
async def test_shell_executor_streams_output(use_real_dependencies: bool) -> None:
    config = ServerConfig()
    manager = PluginManager()
    manager.refresh_exports()
    recorder = InMemoryStreamRecorder()
    PluginRegistry(manager).register_command_listener(recorder)
    executor = ShellCommandExecutor(config, manager)

    result = await executor.run("printf 'hello' && printf ' world\\n'")

    assert "hello world" in result.stdout
    assert recorder.start_event is not None
    assert recorder.complete_event is not None
    stdout_chunks = [chunk for chunk in recorder.chunks if chunk.stream == "stdout"]
    assert stdout_chunks, "Expected stdout chunks to be recorded"


@pytest.mark.asyncio
@pytest.mark.parametrize("use_real_dependencies", [False, True])
async def test_shell_executor_timeout_records_completion(use_real_dependencies: bool) -> None:
    config = ServerConfig()
    manager = PluginManager()
    manager.refresh_exports()
    recorder = InMemoryStreamRecorder()
    PluginRegistry(manager).register_command_listener(recorder)
    executor = ShellCommandExecutor(config, manager)

    with pytest.raises(CommandTimeoutError) as excinfo:
        await executor.run("python -c 'import time; time.sleep(1)'", timeout=0.1)

    timeout_error = excinfo.value
    assert timeout_error.event is not None
    assert timeout_error.event.return_code != 0
    assert recorder.complete_event is not None
    assert recorder.complete_event.return_code == timeout_error.event.return_code
