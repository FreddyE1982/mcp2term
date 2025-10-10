"""High level file editing helpers for the MCP terminal server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
        return payload


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
        )

    def write_file(
        self,
        raw_path: str,
        *,
        text: str,
        create_parents: bool,
        encoding: str,
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
        )

    def append_text(
        self,
        raw_path: str,
        *,
        text: str,
        encoding: str,
        create_if_missing: bool,
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
        )

    def insert_lines(
        self,
        raw_path: str,
        *,
        line: int,
        text: str,
        encoding: str,
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
        )

    def replace_range(
        self,
        raw_path: str,
        *,
        start_line: int,
        end_line: int,
        text: str,
        encoding: str,
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
        )

    def delete_range(
        self,
        raw_path: str,
        *,
        start_line: int,
        end_line: int,
        encoding: str,
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
        )

    def read_lines(
        self,
        raw_path: str,
        *,
        start_line: int | None,
        end_line: int | None,
        encoding: str,
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
        )

    def find_line_numbers(
        self,
        raw_path: str,
        *,
        text: str,
        encoding: str,
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


__all__ = [
    "FileEditor",
    "FileLine",
    "FileOperationError",
    "FileOperationResult",
]
