"""File write tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from whaleclaw.tools.base import Tool, ToolDefinition, ToolParameter, ToolResult

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


def _check_path_allowed(p: Path, *, write: bool = False) -> bool:
    """Check path against denied_paths from the default SecurityPolicy."""
    from whaleclaw.security.permissions import PermissionChecker, SecurityPolicy

    return PermissionChecker.check_path(str(p), SecurityPolicy(), write=write)


class FileWriteTool(Tool):
    """Write (overwrite) a file with the given content."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="file_write",
            description=(
                "Write content to a file, creating it if necessary. "
                "Overwrites existing content."
            ),
            parameters=[
                ToolParameter(name="path", type="string", description="File path to write."),
                ToolParameter(
                    name="content", type="string", description="Content to write to the file."
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        file_path: str = kwargs.get("path", "")
        content: str = kwargs.get("content", "")

        if not file_path:
            return ToolResult(success=False, output="", error="文件路径为空")

        p = _normalize_path(file_path)

        if not _check_path_allowed(p, write=True):
            return ToolResult(success=False, output="", error=f"安全策略拦截: 禁止写入 {p}")

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(success=False, output="", error=str(exc))

        return ToolResult(success=True, output=f"已写入 {len(content)} 字符到 {p}")
