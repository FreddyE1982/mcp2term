"""High level file editing helpers for the MCP terminal server."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import stat as stat_module
import re
from pathlib import Path
from typing import Any, Mapping


class FileOperationError(RuntimeError):
    """Raised when a file operation cannot be completed."""


@dataclass(slots=True)
class FileLine:
    """Represents a single line of text in a file."""

    number: int
    text: str


@dataclass(slots=True)
class FileOperationResult:
    """Structured outcome of a file operation."""

    path: Path
    operation: str
    success: bool
    changed: bool
    encoding: str
    message: str | None = None
    content: str | None = None
    lines: tuple[FileLine, ...] = tuple()
    line_numbers: tuple[int, ...] = tuple()
    escape_profile: str | None = None
    metadata: Mapping[str, Any] | None = None

    def to_payload(self) -> dict[str, object]:
        """Serialize the result into a JSON compatible mapping."""

        payload: dict[str, object] = {
            "path": str(self.path),
            "operation": self.operation,
            "success": self.success,
            "changed": self.changed,
            "encoding": self.encoding,
        }
        if self.message is not None:
            payload["message"] = self.message
        if self.content is not None:
            payload["content"] = self.content
        if self.lines:
            payload["lines"] = [
                {"number": line.number, "text": line.text} for line in self.lines
            ]
        if self.line_numbers:
            payload["line_numbers"] = list(self.line_numbers)
        if self.escape_profile is not None:
            payload["escape_profile"] = self.escape_profile
        if self.metadata is not None:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(slots=True)
class UnifiedDiffLine:
    """Represents a single line inside a unified diff hunk."""

    tag: str
    text: str


@dataclass(slots=True)
class UnifiedDiffHunk:
    """A single hunk within a unified diff file patch."""

    old_start: int
    old_length: int
    new_start: int
    new_length: int
    lines: tuple[UnifiedDiffLine, ...]


@dataclass(slots=True)
class UnifiedDiffFilePatch:
    """Unified diff patch data for a single file."""

    old_path: str | None
    new_path: str | None
    hunks: tuple[UnifiedDiffHunk, ...]


class FileEditor:
    """Performs high level editing operations against a working directory."""

    def __init__(self, base_directory: Path) -> None:
        self._base_directory = base_directory

    def resolve_path(self, raw_path: str) -> Path:
        """Resolve ``raw_path`` relative to the working directory."""

        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (self._base_directory / path).resolve()
        else:
            path = path.resolve()
        return path

    def create_file(
        self,
        raw_path: str,
        *,
        text: str | None,
        overwrite: bool,
        create_parents: bool,
        encoding: str,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        target = self.resolve_path(raw_path)
        existed = target.exists()
        if existed and not overwrite:
            raise FileOperationError(f"File already exists: {target}")
        self._ensure_parent(target, create_parents=create_parents)
        content = text or ""
        target.write_text(content, encoding=encoding)
        verb = "Overwrote" if existed else "Created"
        return FileOperationResult(
            path=target,
            operation="create",
            success=True,
            changed=True,
            encoding=encoding,
            message=f"{verb} {target}",
            content=content,
            escape_profile=escape_profile,
        )

    def write_file(
        self,
        raw_path: str,
        *,
        text: str,
        create_parents: bool,
        encoding: str,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        target = self.resolve_path(raw_path)
        self._ensure_parent(target, create_parents=create_parents)
        target.write_text(text, encoding=encoding)
        return FileOperationResult(
            path=target,
            operation="write",
            success=True,
            changed=True,
            encoding=encoding,
            message=f"Wrote {target}",
            content=text,
            escape_profile=escape_profile,
        )

    def append_text(
        self,
        raw_path: str,
        *,
        text: str,
        encoding: str,
        create_if_missing: bool,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        target = self.resolve_path(raw_path)
        if not target.exists():
            if not create_if_missing:
                raise FileOperationError(f"File does not exist: {target}")
            self._ensure_parent(target, create_parents=True)
            existing_text = ""
        else:
            existing_text = target.read_text(encoding=encoding)
        new_text = existing_text + (text or "")
        target.write_text(new_text, encoding=encoding)
        return FileOperationResult(
            path=target,
            operation="append",
            success=True,
            changed=True,
            encoding=encoding,
            message=f"Appended to {target}",
            content=new_text,
            escape_profile=escape_profile,
        )

    def prepend_text(
        self,
        raw_path: str,
        *,
        text: str,
        encoding: str,
        create_if_missing: bool,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        """Prepend ``text`` to ``raw_path`` while respecting creation flags."""

        target = self.resolve_path(raw_path)
        existed = target.exists()
        if not existed:
            if not create_if_missing:
                raise FileOperationError(f"File does not exist: {target}")
            self._ensure_parent(target, create_parents=True)
            existing_text = ""
        else:
            existing_text = target.read_text(encoding=encoding)

        prepend_text = text or ""
        if not existed and not prepend_text:
            # Creating a brand new empty file still counts as a change.
            target.write_text("", encoding=encoding)
            message = f"Created empty file at {target}"
            resulting_text = ""
            changed = True
        elif prepend_text:
            resulting_text = prepend_text + existing_text
            target.write_text(resulting_text, encoding=encoding)
            message = (
                f"Prepended to {target}" if existed else f"Created {target} with prepended content"
            )
            changed = True
        else:
            resulting_text = existing_text
            message = f"No changes applied to {target}" if existed else f"Created {target}"
            changed = not existed
            if not existed:
                target.write_text(resulting_text, encoding=encoding)

        return FileOperationResult(
            path=target,
            operation="prepend",
            success=True,
            changed=changed,
            encoding=encoding,
            message=message,
            content=resulting_text,
            escape_profile=escape_profile,
        )

    def apply_patch(
        self,
        raw_path: str,
        *,
        patch_text: str,
        encoding: str,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        """Apply a unified diff patch to an existing file."""

        if not patch_text or patch_text.strip() == "":
            raise FileOperationError("Patch content must not be empty")

        target = self.resolve_path(raw_path)
        if not target.exists():
            raise FileOperationError(f"File does not exist: {target}")

        original_text = target.read_text(encoding=encoding)
        parsed_patch = _parse_unified_diff(patch_text)

        patch_paths = {
            entry
            for entry in (
                _normalise_patch_path(
                    parsed_patch.old_path, base_directory=self._base_directory
                ),
                _normalise_patch_path(
                    parsed_patch.new_path, base_directory=self._base_directory
                ),
            )
            if entry is not None
        }

        if patch_paths and target not in patch_paths:
            raise FileOperationError(
                "Patch targets a different file than requested operation",
            )

        updated_text, applied_hunks = _apply_unified_patch(
            original_text,
            parsed_patch,
        )

        target.write_text(updated_text, encoding=encoding)

        message = (
            "Patch applied with no changes"
            if updated_text == original_text
            else f"Applied patch with {applied_hunks} hunk(s)"
        )

        return FileOperationResult(
            path=target,
            operation="patch",
            success=True,
            changed=updated_text != original_text,
            encoding=encoding,
            message=message,
            content=updated_text,
            escape_profile=escape_profile,
        )

    def insert_lines(
        self,
        raw_path: str,
        *,
        line: int,
        text: str,
        encoding: str,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        lines, trailing_newline = self._load_lines(raw_path, encoding=encoding)
        index = self._validate_insertion_index(line, len(lines))
        insert_lines, insert_trailing = self._split_lines(text)
        lines[index:index] = insert_lines
        self._write_lines(
            raw_path,
            lines,
            encoding=encoding,
            trailing_newline=trailing_newline or insert_trailing,
        )
        return FileOperationResult(
            path=self.resolve_path(raw_path),
            operation="insert",
            success=True,
            changed=True,
            encoding=encoding,
            message=f"Inserted {len(insert_lines)} line(s) at {line}",
            content="\n".join(lines),
            escape_profile=escape_profile,
        )

    def replace_range(
        self,
        raw_path: str,
        *,
        start_line: int,
        end_line: int,
        text: str,
        encoding: str,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        lines, trailing_newline = self._load_lines(raw_path, encoding=encoding)
        start_index, end_index, actual_end = self._validate_range(
            start_line, end_line, len(lines)
        )
        replacement_lines, replacement_trailing = self._split_lines(text)
        lines[start_index:end_index] = replacement_lines
        self._write_lines(
            raw_path,
            lines,
            encoding=encoding,
            trailing_newline=trailing_newline or replacement_trailing,
        )
        return FileOperationResult(
            path=self.resolve_path(raw_path),
            operation="replace",
            success=True,
            changed=True,
            encoding=encoding,
            message=f"Replaced lines {start_line}-{actual_end}",
            content="\n".join(lines),
            escape_profile=escape_profile,
        )

    def delete_range(
        self,
        raw_path: str,
        *,
        start_line: int,
        end_line: int,
        encoding: str,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        lines, trailing_newline = self._load_lines(raw_path, encoding=encoding)
        start_index, end_index, actual_end = self._validate_range(
            start_line, end_line, len(lines)
        )
        del lines[start_index:end_index]
        self._write_lines(
            raw_path,
            lines,
            encoding=encoding,
            trailing_newline=trailing_newline,
        )
        return FileOperationResult(
            path=self.resolve_path(raw_path),
            operation="delete",
            success=True,
            changed=True,
            encoding=encoding,
            message=f"Deleted lines {start_line}-{actual_end}",
            content="\n".join(lines),
            escape_profile=escape_profile,
        )

    def read_lines(
        self,
        raw_path: str,
        *,
        start_line: int | None,
        end_line: int | None,
        encoding: str,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        lines, _ = self._load_lines(raw_path, encoding=encoding)
        total_lines = len(lines)
        if total_lines == 0:
            subset: list[str] = []
            start = 1
            actual_end = 0
        else:
            start = start_line or 1
            requested_end = end_line or total_lines
            start_index, end_index, actual_end = self._validate_range(
                start, requested_end, total_lines
            )
            subset = lines[start_index:end_index]
        numbered = tuple(
            FileLine(number=start + offset, text=line) for offset, line in enumerate(subset)
        )
        content = "\n".join(line.text for line in numbered)
        if subset:
            message = f"Showing lines {start}-{actual_end}"
        elif total_lines == 0:
            message = "File is empty"
        else:
            message = "No lines in requested range"
        return FileOperationResult(
            path=self.resolve_path(raw_path),
            operation="print",
            success=True,
            changed=False,
            encoding=encoding,
            message=message,
            content=content,
            lines=numbered,
            escape_profile=escape_profile,
        )

    def find_line_numbers(
        self,
        raw_path: str,
        *,
        text: str,
        encoding: str,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        if not text:
            raise FileOperationError("Search text must not be empty")
        lines, _ = self._load_lines(raw_path, encoding=encoding)
        matches = [index + 1 for index, line in enumerate(lines) if line == text]
        message = (
            f"Found {len(matches)} matching line(s)" if matches else "No matching line found"
        )
        return FileOperationResult(
            path=self.resolve_path(raw_path),
            operation="locate",
            success=True,
            changed=False,
            encoding=encoding,
            message=message,
            line_numbers=tuple(matches),
            escape_profile=escape_profile,
        )

    def stat_file(
        self,
        raw_path: str,
        *,
        encoding: str,
        follow_symlinks: bool,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        """Collect rich filesystem metadata for ``raw_path``.

        The resulting :class:`FileOperationResult` includes a ``metadata`` mapping
        containing resolved paths, timestamp information, and POSIX-style
        permission data so both human operators and programmatic clients can act
        on the inspection results without additional lookups.
        """

        target = self.resolve_path(raw_path)
        canonical_path = target.resolve(strict=False)
        metadata: dict[str, Any] = {
            "raw_path": raw_path,
            "resolved_path": str(target),
            "canonical_path": str(canonical_path),
            "follow_symlinks": follow_symlinks,
        }

        def _record_link_target() -> None:
            if not target.is_symlink():
                return
            try:
                metadata["link_target"] = os.readlink(target)
            except OSError as exc:
                metadata["link_target_error"] = str(exc)

        try:
            stat_result = target.stat(follow_symlinks=follow_symlinks)
        except FileNotFoundError as exc:
            _record_link_target()
            metadata.update(
                {
                    "exists": False,
                    "is_symlink": target.is_symlink(),
                    "stat_error": str(exc),
                }
            )
            return FileOperationResult(
                path=target,
                operation="stat",
                success=False,
                changed=False,
                encoding=encoding,
                message=f"File not found: {target}",
                escape_profile=escape_profile,
                metadata=metadata,
            )

        file_type = self._describe_file_type(stat_result.st_mode)

        metadata.update(
            {
                "exists": True,
                "file_type": file_type,
                "is_symlink": target.is_symlink(),
                "size_bytes": stat_result.st_size,
                "mode": stat_result.st_mode,
                "permissions": stat_module.filemode(stat_result.st_mode),
                "owner": stat_result.st_uid,
                "group": stat_result.st_gid,
                "device": stat_result.st_dev,
                "inode": stat_result.st_ino,
                "hard_links": stat_result.st_nlink,
                "accessed_timestamp": stat_result.st_atime,
                "modified_timestamp": stat_result.st_mtime,
                "created_timestamp": stat_result.st_ctime,
                "accessed_at": self._format_timestamp(stat_result.st_atime),
                "modified_at": self._format_timestamp(stat_result.st_mtime),
                "created_at": self._format_timestamp(stat_result.st_ctime),
            }
        )
        _record_link_target()

        return FileOperationResult(
            path=target,
            operation="stat",
            success=True,
            changed=False,
            encoding=encoding,
            message=f"Metadata for {target}",
            escape_profile=escape_profile,
            metadata=metadata,
        )

    def substitute_text(
        self,
        raw_path: str,
        *,
        pattern: str,
        replacement: str,
        encoding: str,
        use_regex: bool,
        ignore_case: bool,
        max_replacements: int | None,
        escape_profile: str | None = None,
    ) -> FileOperationResult:
        """Perform pattern-based substitution within ``raw_path``."""

        if not pattern:
            raise FileOperationError("Substitute pattern must not be empty")

        target = self.resolve_path(raw_path)
        if not target.exists():
            raise FileOperationError(f"File does not exist: {target}")

        original_text = target.read_text(encoding=encoding)
        replacements = 0
        resulting_text = original_text

        if use_regex or ignore_case or max_replacements is not None:
            flags = re.MULTILINE
            if ignore_case:
                flags |= re.IGNORECASE
            compiled_pattern_text = pattern if use_regex else re.escape(pattern)
            try:
                compiled = re.compile(compiled_pattern_text, flags)
            except re.error as exc:
                raise FileOperationError(f"Invalid regular expression: {exc}") from exc
            count = 0 if max_replacements is None else max_replacements

            if use_regex:
                resulting_text, replacements = compiled.subn(replacement, original_text, count=count)
            else:
                def _literal_sub(_match: re.Match[str]) -> str:
                    return replacement

                resulting_text, replacements = compiled.subn(_literal_sub, original_text, count=count)
        else:
            if pattern in original_text:
                replacements = original_text.count(pattern)
                resulting_text = original_text.replace(pattern, replacement)

        changed = replacements > 0
        if changed:
            target.write_text(resulting_text, encoding=encoding)

        metadata: dict[str, Any] = {
            "pattern": pattern,
            "regex": use_regex,
            "ignore_case": ignore_case,
            "replacements": replacements,
        }
        if max_replacements is not None:
            metadata["max_replacements"] = max_replacements

        message = (
            f"Substituted {replacements} occurrence(s)"
            if replacements
            else "No matches found for substitute pattern"
        )

        return FileOperationResult(
            path=target,
            operation="substitute",
            success=True,
            changed=changed,
            encoding=encoding,
            message=message,
            content=resulting_text,
            escape_profile=escape_profile,
            metadata=metadata,
        )

    def _ensure_parent(self, target: Path, *, create_parents: bool) -> None:
        parent = target.parent
        if create_parents:
            parent.mkdir(parents=True, exist_ok=True)
        elif not parent.exists():
            raise FileOperationError(f"Parent directory does not exist: {parent}")

    def _load_lines(self, raw_path: str, *, encoding: str) -> tuple[list[str], bool]:
        target = self.resolve_path(raw_path)
        if not target.exists():
            raise FileOperationError(f"File does not exist: {target}")
        content = target.read_text(encoding=encoding)
        trailing = content.endswith("\n")
        lines = content.splitlines()
        return lines, trailing

    @staticmethod
    def _split_lines(text: str) -> tuple[list[str], bool]:
        if not text:
            return [], False
        trailing = text.endswith("\n")
        lines = text.splitlines()
        return list(lines), trailing

    @staticmethod
    def _validate_insertion_index(line: int, total_lines: int) -> int:
        if line < 1 or line > total_lines + 1:
            raise FileOperationError(
                f"Insertion line {line} is out of range (1-{total_lines + 1})"
            )
        return line - 1

    @staticmethod
    def _validate_range(
        start_line: int,
        end_line: int,
        total_lines: int,
    ) -> tuple[int, int, int]:
        if start_line < 1 or end_line < 1:
            raise FileOperationError("Line numbers must be positive")
        if end_line < start_line:
            raise FileOperationError("End line must be greater than or equal to start line")
        if total_lines == 0:
            raise FileOperationError("File is empty")
        if start_line > total_lines:
            raise FileOperationError(
                f"Start line {start_line} is beyond the end of the file ({total_lines} line(s))"
            )
        adjusted_end = min(end_line, total_lines)
        return start_line - 1, adjusted_end, adjusted_end

    def _write_lines(
        self,
        raw_path: str,
        lines: list[str],
        *,
        encoding: str,
        trailing_newline: bool,
    ) -> None:
        target = self.resolve_path(raw_path)
        text = "\n".join(lines)
        if trailing_newline and text and not text.endswith("\n"):
            text += "\n"
        target.write_text(text, encoding=encoding)

    @staticmethod
    def _format_timestamp(value: float) -> str:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()

    @staticmethod
    def _describe_file_type(mode: int) -> str:
        if stat_module.S_ISDIR(mode):
            return "directory"
        if stat_module.S_ISLNK(mode):
            return "symlink"
        if stat_module.S_ISREG(mode):
            return "file"
        if stat_module.S_ISCHR(mode):
            return "character-device"
        if stat_module.S_ISBLK(mode):
            return "block-device"
        if stat_module.S_ISSOCK(mode):
            return "socket"
        if stat_module.S_ISFIFO(mode):
            return "fifo"
        return "unknown"


_HUNK_HEADER_RE = re.compile(
    r"@@ -(?P<old_start>\d+)(?:,(?P<old_length>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_length>\d+))? @@"
)


def _parse_unified_diff(patch_text: str) -> UnifiedDiffFilePatch:
    """Parse ``patch_text`` into a :class:`UnifiedDiffFilePatch`."""

    lines = patch_text.splitlines(keepends=True)
    index = 0
    total = len(lines)

    while index < total and not lines[index].startswith("--- "):
        index += 1
    if index >= total:
        raise FileOperationError("Patch is missing the --- file header")
    old_path = lines[index][4:].strip() or None
    index += 1
    if index >= total or not lines[index].startswith("+++ "):
        raise FileOperationError("Patch is missing the +++ file header")
    new_path = lines[index][4:].strip() or None
    index += 1

    hunks: list[UnifiedDiffHunk] = []
    while index < total:
        line = lines[index]
        if line.startswith("diff ") or line.startswith("--- "):
            raise FileOperationError(
                "Patch contains multiple file sections; submit them individually",
            )
        if not line.startswith("@@ "):
            if line.strip() == "":
                index += 1
                continue
            raise FileOperationError(
                f"Unexpected line in patch: {line.rstrip()}"
            )
        header = line.strip()
        match = _HUNK_HEADER_RE.match(header)
        if not match:
            raise FileOperationError(f"Invalid hunk header: {header}")

        old_start = int(match.group("old_start"))
        old_length = int(match.group("old_length") or "1")
        new_start = int(match.group("new_start"))
        new_length = int(match.group("new_length") or "1")
        index += 1

        hunk_lines: list[UnifiedDiffLine] = []
        while index < total:
            entry = lines[index]
            if entry.startswith("@@ "):
                break
            if entry.startswith("diff ") or entry.startswith("--- "):
                raise FileOperationError(
                    "Patch contains multiple file sections; submit them individually",
                )
            if entry.startswith("\\ "):
                if not hunk_lines:
                    raise FileOperationError(
                        "No newline marker appears before any hunk lines",
                    )
                previous = hunk_lines[-1]
                if previous.text.endswith("\n"):
                    hunk_lines[-1] = UnifiedDiffLine(previous.tag, previous.text[:-1])
                index += 1
                continue
            if not entry:
                index += 1
                continue
            if entry[0] not in " +-":
                raise FileOperationError(
                    f"Unsupported patch line prefix: {entry[0]!r}",
                )
            prefix = entry[0]
            text = entry[1:]
            hunk_lines.append(UnifiedDiffLine(prefix, text))
            index += 1

        hunk = UnifiedDiffHunk(
            old_start=old_start,
            old_length=old_length,
            new_start=new_start,
            new_length=new_length,
            lines=tuple(hunk_lines),
        )
        _validate_hunk_lengths(hunk)
        hunks.append(hunk)

    if not hunks:
        raise FileOperationError("Patch does not contain any hunks")

    return UnifiedDiffFilePatch(
        old_path=old_path,
        new_path=new_path,
        hunks=tuple(hunks),
    )


def _validate_hunk_lengths(hunk: UnifiedDiffHunk) -> None:
    removed = sum(1 for line in hunk.lines if line.tag != "+")
    added = sum(1 for line in hunk.lines if line.tag != "-")
    if removed != hunk.old_length:
        raise FileOperationError(
            "Patch hunk removal count does not match header",
        )
    if added != hunk.new_length:
        raise FileOperationError(
            "Patch hunk addition count does not match header",
        )


def _normalise_patch_path(
    path: str | None,
    *,
    base_directory: Path | None = None,
) -> Path | None:
    if path is None:
        return None
    cleaned = path.strip()
    if not cleaned or cleaned in {"/dev/null", "null"}:
        return None
    cleaned = cleaned.split("\t", 1)[0]
    cleaned = cleaned.strip('"')
    if cleaned.startswith("a/") or cleaned.startswith("b/"):
        cleaned = cleaned[2:]
    candidate = Path(cleaned)
    if candidate.is_absolute():
        return candidate.resolve()
    base = base_directory if base_directory is not None else Path.cwd()
    return (base / candidate).resolve()


def _apply_unified_patch(
    original_text: str,
    patch: UnifiedDiffFilePatch,
) -> tuple[str, int]:
    original_lines = original_text.splitlines(keepends=True)
    result_lines: list[str] = []
    index = 0

    for hunk in patch.hunks:
        start_index = max(hunk.old_start - 1, 0)
        if start_index > len(original_lines):
            raise FileOperationError("Patch hunk starts beyond end of file")
        if start_index < index:
            raise FileOperationError("Patch hunks overlap in the target file")

        result_lines.extend(original_lines[index:start_index])
        index = start_index

        for line in hunk.lines:
            if line.tag == " ":
                if index >= len(original_lines):
                    raise FileOperationError("Patch context extends past end of file")
                if original_lines[index] != line.text:
                    raise FileOperationError(
                        "Patch context does not match file contents",
                    )
                result_lines.append(original_lines[index])
                index += 1
            elif line.tag == "-":
                if index >= len(original_lines):
                    raise FileOperationError("Patch deletion extends past end of file")
                if original_lines[index] != line.text:
                    raise FileOperationError(
                        "Patch deletion does not match file contents",
                    )
                index += 1
            elif line.tag == "+":
                result_lines.append(line.text)
            else:
                raise FileOperationError(f"Unsupported patch line tag: {line.tag}")

    result_lines.extend(original_lines[index:])
    return "".join(result_lines), len(patch.hunks)


__all__ = [
    "FileEditor",
    "FileLine",
    "FileOperationError",
    "FileOperationResult",
    "UnifiedDiffFilePatch",
    "UnifiedDiffHunk",
    "UnifiedDiffLine",
]
