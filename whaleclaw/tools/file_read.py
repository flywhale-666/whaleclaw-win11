"""File read tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from whaleclaw.tools.base import Tool, ToolDefinition, ToolParameter, ToolResult

_MAX_SIZE = 500_000
_TMP_ALIAS_ROOT = (Path.home() / ".whaleclaw" / "workspace" / "tmp").resolve()
_HOME_ALIAS_ROOT = _TMP_ALIAS_ROOT.parent.parent


def _normalize_path(raw_path: str) -> Path:
    if raw_path.startswith("/private/tmp/"):
        relative = raw_path.removeprefix("/private/tmp/").lstrip("/\\")
        return (_TMP_ALIAS_ROOT / relative).resolve()
    if raw_path == "/tmp":
        return _TMP_ALIAS_ROOT
    if raw_path.startswith("/tmp/"):
        relative = raw_path.removeprefix("/tmp/").lstrip("/\\")
        return (_TMP_ALIAS_ROOT / relative).resolve()
    if raw_path == "~/.whaleclaw":
        return _HOME_ALIAS_ROOT
    if raw_path.startswith("~/.whaleclaw/"):
        relative = raw_path.removeprefix("~/.whaleclaw/").lstrip("/\\")
        return (_HOME_ALIAS_ROOT / relative).resolve()
    if raw_path == "/root/.whaleclaw":
        return _HOME_ALIAS_ROOT
    if raw_path.startswith("/root/.whaleclaw/"):
        relative = raw_path.removeprefix("/root/.whaleclaw/").lstrip("/\\")
        return (_HOME_ALIAS_ROOT / relative).resolve()
    lowered = raw_path.lower()
    windows_root_home = "c:\\root\\.whaleclaw"
    if lowered == windows_root_home:
        return _HOME_ALIAS_ROOT
    if lowered.startswith(windows_root_home + "\\"):
        relative = raw_path[len(windows_root_home) :].lstrip("/\\")
        return (_HOME_ALIAS_ROOT / relative).resolve()
    return Path(raw_path).expanduser().resolve()


class FileReadTool(Tool):
    """Read file contents, optionally with line range."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="file_read",
            description="Read the contents of a file. Supports optional line offset and limit.",
            parameters=[
                ToolParameter(name="path", type="string", description="File path to read."),
                ToolParameter(
                    name="offset",
                    type="integer",
                    description="Line number to start reading from (1-based).",
                    required=False,
                ),
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="Number of lines to read.",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        file_path = kwargs.get("path", "")
        offset: int = int(kwargs.get("offset", 0))
        limit: int | None = kwargs.get("limit")
        if limit is not None:
            limit = int(limit)

        if not file_path:
            return ToolResult(success=False, output="", error="文件路径为空")

        p = _normalize_path(file_path)
        if not p.is_file():
            return ToolResult(success=False, output="", error=f"文件不存在: {p}")

        if p.stat().st_size > _MAX_SIZE:
            return ToolResult(success=False, output="", error=f"文件过大 (>{_MAX_SIZE} bytes)")

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(success=False, output="", error=str(exc))

        lines = text.splitlines(keepends=True)

        if offset > 0:
            lines = lines[offset - 1 :]
        if limit is not None and limit > 0:
            lines = lines[:limit]

        numbered = [f"{i + (offset or 1):>6}|{line}" for i, line in enumerate(lines)]
        return ToolResult(success=True, output="".join(numbered))
