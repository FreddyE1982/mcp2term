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
    assert insert_result.metadata is not None
    assert insert_result.metadata.get("resolved_insertion_line") == 2

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
def test_file_editor_insert_with_anchor(tmp_path: Path, use_real_dependencies: bool) -> None:
    base_dir = tmp_path if use_real_dependencies else tmp_path
    editor = FileEditor(base_dir)

    editor.create_file(
        "anchor.txt",
        text="alpha\nbeta\ngamma\nbeta\n",
        overwrite=False,
        create_parents=False,
        encoding="utf-8",
    )

    anchored_result = editor.insert_lines_at_anchor(
        "anchor.txt",
        anchor_text="beta",
        text="delta\n",
        encoding="utf-8",
        after=True,
        occurrence=2,
        use_regex=False,
        ignore_case=False,
    )

    assert anchored_result.success is True
    assert "delta" in (anchored_result.content or "")
    assert anchored_result.metadata is not None
    assert anchored_result.metadata.get("anchor_occurrence") == 2
    assert anchored_result.metadata.get("anchor_after") is True
    assert anchored_result.metadata.get("anchor_start_line") == 4
    assert anchored_result.metadata.get("resolved_insertion_line") == 5

    regex_result = editor.insert_lines_at_anchor(
        "anchor.txt",
        anchor_text=r"^alp.*$",
        text="preface\n",
        encoding="utf-8",
        after=False,
        occurrence=1,
        use_regex=True,
        ignore_case=True,
    )

    assert regex_result.message.startswith("Inserted 1 line(s)")
    assert regex_result.metadata is not None
    assert regex_result.metadata.get("anchor_use_regex") is True
    file_lines = (base_dir / "anchor.txt").read_text(encoding="utf-8").splitlines()
    assert file_lines[0] == "preface"
    assert "delta" in file_lines


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_file_editor_insert_with_anchor_failures(
    tmp_path: Path, use_real_dependencies: bool
) -> None:
    base_dir = tmp_path if use_real_dependencies else tmp_path
    editor = FileEditor(base_dir)

    editor.create_file(
        "failure.txt",
        text="one\ntwo\nthree\n",
        overwrite=False,
        create_parents=False,
        encoding="utf-8",
    )

    with pytest.raises(FileOperationError):
        editor.insert_lines_at_anchor(
            "failure.txt",
            anchor_text="missing",
            text="noop\n",
            encoding="utf-8",
            after=False,
            occurrence=1,
            use_regex=False,
            ignore_case=False,
        )

    with pytest.raises(FileOperationError):
        editor.insert_lines_at_anchor(
            "failure.txt",
            anchor_text=r"(two)",
            text="noop\n",
            encoding="utf-8",
            after=False,
            occurrence=2,
            use_regex=True,
            ignore_case=False,
        )


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


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_file_editor_apply_patch(tmp_path: Path, use_real_dependencies: bool) -> None:
    base_dir = tmp_path if use_real_dependencies else tmp_path
    editor = FileEditor(base_dir)

    editor.create_file(
        "sample.txt",
        text="alpha\nbeta\ngamma\n",
        overwrite=False,
        create_parents=False,
        encoding="utf-8",
    )

    patch_text = """--- a/sample.txt\n+++ b/sample.txt\n@@ -1,3 +1,4 @@\n alpha\n-beta\n+beta-updated\n gamma\n+delta\n"""
    result = editor.apply_patch(
        "sample.txt",
        patch_text=patch_text,
        encoding="utf-8",
    )

    assert result.success is True
    assert result.changed is True
    assert "Applied patch" in (result.message or "")
    updated = (base_dir / "sample.txt").read_text(encoding="utf-8")
    assert updated.splitlines() == ["alpha", "beta-updated", "gamma", "delta"]


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_file_editor_apply_patch_rejects_mismatch(
    tmp_path: Path, use_real_dependencies: bool
) -> None:
    base_dir = tmp_path if use_real_dependencies else tmp_path
    editor = FileEditor(base_dir)

    editor.create_file(
        "mismatch.txt",
        text="one\ntwo\n",
        overwrite=False,
        create_parents=False,
        encoding="utf-8",
    )

    patch_text = """--- a/mismatch.txt\n+++ b/mismatch.txt\n@@ -1,2 +1,2 @@\n-one\n+uno\n-two\n"""

    with pytest.raises(FileOperationError):
        editor.apply_patch(
            "mismatch.txt",
            patch_text=patch_text,
            encoding="utf-8",
        )


@pytest.mark.parametrize("use_real_dependencies", [False, True])
def test_file_editor_apply_patch_preserves_missing_newline(
    tmp_path: Path, use_real_dependencies: bool
) -> None:
    base_dir = tmp_path if use_real_dependencies else tmp_path
    editor = FileEditor(base_dir)

    editor.create_file(
        "nonl.txt",
        text="alpha\nbeta",
        overwrite=False,
        create_parents=False,
        encoding="utf-8",
    )

    patch_text = (
        "--- a/nonl.txt\n"
        "+++ b/nonl.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " alpha\n"
        "-beta\n"
        "\\ No newline at end of file\n"
        "+beta-updated\n"
        "\\ No newline at end of file\n"
    )

    result = editor.apply_patch(
        "nonl.txt",
        patch_text=patch_text,
        encoding="utf-8",
    )

    assert result.success is True
    assert result.changed is True
    content = (base_dir / "nonl.txt").read_bytes()
    assert content.endswith(b"beta-updated")
