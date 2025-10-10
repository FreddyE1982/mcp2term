from __future__ import annotations

import io
from pathlib import Path

import pytest

from mcp2term_client.file_command import (
    FileCommandHelp,
    FileCommandParseError,
    ManageFileCommand,
    parse_manage_file_command,
)


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_parse_manage_file_inline_content(tmp_path: Path, use_real_dependencies: bool) -> None:
    command: ManageFileCommand = parse_manage_file_command(
        [
            "create",
            "notes.txt",
            "--content",
            "hello world",
            "--create-parents",
        ]
    )
    assert command.operation == "create"
    assert command.path == "notes.txt"
    assert command.content == "hello world"
    assert command.create_parents is True
    assert command.encoding == "utf-8"


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_parse_manage_file_from_stdin(tmp_path: Path, use_real_dependencies: bool) -> None:
    stdin_stream = io.StringIO("line-one\nline-two\n")
    command = parse_manage_file_command(
        ["write", "story.txt", "--stdin", "--encoding", "utf-8"],
        stdin=stdin_stream,
    )
    assert command.operation == "write"
    assert command.content == "line-one\nline-two\n"
    assert command.encoding == "utf-8"


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_parse_manage_file_from_file(tmp_path: Path, use_real_dependencies: bool) -> None:
    content_file = tmp_path / "payload.txt"
    content_file.write_text("payload", encoding="utf-8")
    command = parse_manage_file_command(
        [
            "append",
            "story.txt",
            "--content-from-file",
            str(content_file),
            "--no-create-if-missing",
        ]
    )
    assert command.operation == "append"
    assert command.content == "payload"
    assert command.create_if_missing is False


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_parse_manage_file_patch_from_file(tmp_path: Path, use_real_dependencies: bool) -> None:
    diff_file = tmp_path / "delta.diff"
    diff_file.write_text(
        """--- a/sample.txt\n+++ b/sample.txt\n@@ -1 +1 @@\n-old\n+new\n""",
        encoding="utf-8",
    )
    command = parse_manage_file_command(
        [
            "patch",
            "sample.txt",
            "--content-from-file",
            str(diff_file),
        ]
    )
    assert command.operation == "patch"
    assert "@@ -1 +1 @@" in (command.content or "")


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_parse_manage_file_patch_requires_content(
    tmp_path: Path, use_real_dependencies: bool
) -> None:
    with pytest.raises(FileCommandParseError):
        parse_manage_file_command([
            "patch",
            "sample.txt",
        ])


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_parse_manage_file_rejects_conflicting_sources(
    tmp_path: Path, use_real_dependencies: bool
) -> None:
    with pytest.raises(FileCommandParseError):
        parse_manage_file_command(
            [
                "write",
                "story.txt",
                "--content",
                "text",
                "--stdin",
            ]
        )


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_parse_manage_file_help(tmp_path: Path, use_real_dependencies: bool) -> None:
    with pytest.raises(FileCommandHelp):
        parse_manage_file_command(["--help"])


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_parse_manage_file_decodes_inline_patch_escape_sequences(
    tmp_path: Path, use_real_dependencies: bool
) -> None:
    inline_patch = (
        f"--- a/sample.txt\\n"
        f"+++ b/sample.txt\\n"
        "@@ -1 +1 @@\\n"
        "-old\\n"
        "+new\\n"
    )
    command = parse_manage_file_command(
        [
            "patch",
            "sample.txt",
            "--content",
            inline_patch,
        ]
    )
    assert command.content is not None
    assert command.content.splitlines()[0] == f"--- a/sample.txt"
    assert command.content.splitlines()[-1] == "+new"


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_parse_manage_file_preserves_backslash_leading_lines(
    tmp_path: Path, use_real_dependencies: bool
) -> None:
    inline_patch = (
        f"--- a/sample.txt\\n"
        f"+++ b/sample.txt\\n"
        "@@ -1,2 +1,2 @@\\n"
        " alpha\\n"
        "-beta\\n"
        "\\ No newline at end of file\\n"
        "+beta-updated\\n"
        "\\ No newline at end of file\\n"
    )
    command = parse_manage_file_command(
        [
            "patch",
            "sample.txt",
            "--content",
            inline_patch,
        ]
    )
    assert command.content is not None
    assert "\\ No newline at end of file" in command.content
