from __future__ import annotations

from pathlib import Path

import pytest

from mcp2term.files import FileEditor, FileOperationError


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_file_editor_create_print_and_locate(tmp_path: Path, use_real_dependencies: bool) -> None:
    base_dir = tmp_path if use_real_dependencies else tmp_path
    editor = FileEditor(base_dir)

    create_result = editor.create_file(
        "notes.txt",
        text="first line\nsecond line\nthird line\n",
        overwrite=False,
        create_parents=False,
        encoding="utf-8",
    )
    assert create_result.success is True
    assert create_result.changed is True
    assert create_result.encoding == "utf-8"
    create_payload = create_result.to_payload()
    assert create_payload["encoding"] == "utf-8"
    assert (base_dir / "notes.txt").read_text(encoding="utf-8").splitlines() == [
        "first line",
        "second line",
        "third line",
    ]

    print_result = editor.read_lines(
        "notes.txt",
        start_line=2,
        end_line=3,
        encoding="utf-8",
    )
    assert print_result.success is True
    assert [line.number for line in print_result.lines] == [2, 3]
    assert [line.text for line in print_result.lines] == ["second line", "third line"]

    locate_result = editor.find_line_numbers(
        "notes.txt",
        text="second line",
        encoding="utf-8",
    )
    assert locate_result.line_numbers == (2,)
    assert locate_result.encoding == "utf-8"
    locate_payload = locate_result.to_payload()
    assert locate_payload["line_numbers"] == [2]


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_file_editor_insert_replace_and_delete(tmp_path: Path, use_real_dependencies: bool) -> None:
    base_dir = tmp_path if use_real_dependencies else tmp_path
    editor = FileEditor(base_dir)

    editor.create_file(
        "story.txt",
        text="alpha\nbeta\ngamma\n",
        overwrite=False,
        create_parents=False,
        encoding="utf-8",
    )

    insert_result = editor.insert_lines(
        "story.txt",
        line=2,
        text="inserted-one\ninserted-two\n",
        encoding="utf-8",
    )
    assert insert_result.message == "Inserted 2 line(s) at 2"
    assert "inserted-one" in insert_result.content

    replace_result = editor.replace_range(
        "story.txt",
        start_line=3,
        end_line=4,
        text="updated-middle\n",
        encoding="utf-8",
    )
    assert replace_result.message == "Replaced lines 3-4"
    assert "updated-middle" in replace_result.content

    delete_result = editor.delete_range(
        "story.txt",
        start_line=4,
        end_line=4,
        encoding="utf-8",
    )
    assert delete_result.message == "Deleted lines 4-4"
    final_lines = (base_dir / "story.txt").read_text(encoding="utf-8").splitlines()
    assert final_lines == ["alpha", "inserted-one", "updated-middle"]


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_file_editor_rejects_out_of_range_insert(tmp_path: Path, use_real_dependencies: bool) -> None:
    base_dir = tmp_path if use_real_dependencies else tmp_path
    editor = FileEditor(base_dir)

    editor.create_file(
        "bounds.txt",
        text="only\n",
        overwrite=False,
        create_parents=False,
        encoding="utf-8",
    )

    with pytest.raises(FileOperationError):
        editor.insert_lines(
            "bounds.txt",
            line=5,
            text="out-of-range\n",
            encoding="utf-8",
        )
