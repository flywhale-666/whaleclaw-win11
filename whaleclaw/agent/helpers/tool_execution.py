"""Tool registry and execution helpers for the single-agent runtime."""

from __future__ import annotations

import asyncio
import json
import re

import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from whaleclaw.agent.context import OnToolCall, OnToolResult
from whaleclaw.agent.helpers.office_rules import (
    is_office_path_probe_command,
    looks_like_ppt_generation_command,
    looks_like_ppt_generation_script,
)
from whaleclaw.providers.base import ToolCall
from whaleclaw.sessions.manager import Session, SessionManager
from whaleclaw.tools.base import ToolResult
from whaleclaw.tools.registry import ToolRegistry
from whaleclaw.utils.log import get_logger

if TYPE_CHECKING:
    from whaleclaw.cron.scheduler import CronScheduler
    from whaleclaw.memory.base import MemoryStore
    from whaleclaw.memory.manager import MemoryManager

log = get_logger(__name__)


def _detect_project_python() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    candidates = (
        project_root / "python" / "python.exe",
        project_root / "python" / "bin" / "python3.12",
        project_root / "python" / "bin" / "python3",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if sys.platform == "win32":
        return project_root / "python" / "python.exe"
    return project_root / "python" / "bin" / "python3.12"


_PROJECT_PYTHON = _detect_project_python()
_DIRECT_PY_SCRIPT_RE = re.compile(
    r"^"
    r"(?P<prefix>(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|[^\s]+)\s+)*)"
    r"(?P<script>(?:~|/|\./|\.\./)[^\s;&|]+\.py)"
    r"(?P<suffix>(?:\s+.*)?)$"
)
_PY_SHELL_MISMATCH_HINTS = (
    "from: command not found",
    "import: command not found",
)
_NANO_BANANA_SCRIPT_RE = re.compile(r"test_nano_banana_2\.py", re.IGNORECASE)
_NANO_BANANA_ARGPARSE_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"usage:\s+test_nano_banana_2\.py\b", re.IGNORECASE),
    re.compile(r"error:\s+invalid choice\b", re.IGNORECASE),
    re.compile(r"error:\s+unrecognized arguments\b", re.IGNORECASE),
    re.compile(r"error:\s+the following arguments are required\b", re.IGNORECASE),
    re.compile(r"error:\s+argument\s+--[a-z0-9][a-z0-9_-]*\b", re.IGNORECASE),
)
_CRONTAB_CMD_RE = re.compile(
    r"\bcrontab\b",
    re.IGNORECASE,
)


def _is_crontab_command(command: str) -> bool:
    """Return True if *command* attempts to manipulate the system crontab."""
    return bool(_CRONTAB_CMD_RE.search(command))


_RATIO_VALUE_RE = re.compile(r"^\d+:\d+$")
_NANO_BANANA_SPLIT_RE = re.compile(r"\s*(?:&&|;|\r?\n)+\s*")
_NANO_BANANA_BATCH_SIZE = 5
_NANO_BANANA_BATCH_DELAY_SECONDS = 1.5

_active_skill_hooks: object | None = None


def set_active_skill_hooks(hooks: object | None) -> None:
    """Set the active skill hooks for the current execution context."""
    global _active_skill_hooks  # noqa: PLW0603
    _active_skill_hooks = hooks


def get_active_skill_hooks() -> object | None:
    """Return the current active skill hooks."""
    return _active_skill_hooks


def create_default_registry(
    session_manager: SessionManager | None = None,
    cron_scheduler: CronScheduler | None = None,
    *,
    memory_manager: MemoryManager | None = None,
    memory_store: MemoryStore | None = None,
) -> ToolRegistry:
    from whaleclaw.tools.bash import BashTool
    from whaleclaw.tools.browser import BrowserTool
    from whaleclaw.tools.desktop_capture import DesktopCaptureTool
    from whaleclaw.tools.docx_edit import DocxEditTool
    from whaleclaw.tools.file_edit import FileEditTool
    from whaleclaw.tools.file_read import FileReadTool
    from whaleclaw.tools.file_write import FileWriteTool
    from whaleclaw.tools.patch_apply import PatchApplyTool
    from whaleclaw.tools.ppt_edit import PptEditTool
    from whaleclaw.tools.process import ProcessTool
    from whaleclaw.tools.web_fetch import WebFetchTool
    from whaleclaw.tools.xlsx_edit import XlsxEditTool

    registry = ToolRegistry()
    registry.register(BashTool())
    registry.register(ProcessTool())
    registry.register(FileReadTool())
    registry.register(FileWriteTool())
    registry.register(FileEditTool())
    registry.register(PatchApplyTool())
    registry.register(PptEditTool())
    registry.register(DocxEditTool())
    registry.register(XlsxEditTool())
    registry.register(WebFetchTool())
    registry.register(BrowserTool())
    registry.register(DesktopCaptureTool())

    if session_manager is not None:
        from whaleclaw.tools.sessions import (
            SessionsHistoryTool,
            SessionsListTool,
            SessionsSendTool,
        )

        registry.register(SessionsListTool(session_manager))
        registry.register(SessionsHistoryTool(session_manager))
        registry.register(SessionsSendTool(session_manager))

    if cron_scheduler is not None:
        from whaleclaw.tools.cron_tool import CronManageTool
        from whaleclaw.tools.reminder import ReminderTool

        registry.register(CronManageTool(cron_scheduler))
        registry.register(ReminderTool(cron_scheduler))

    from whaleclaw.skills.manager import SkillManager
    from whaleclaw.tools.skill_tool import SkillManageTool

    registry.register(SkillManageTool(SkillManager()))

    if memory_manager is not None:
        from whaleclaw.tools.memory_tool import MemoryAddTool, MemorySearchTool

        registry.register(MemorySearchTool(memory_manager))
        registry.register(MemoryAddTool(memory_manager))
    if memory_store is not None:
        from whaleclaw.tools.memory_tool import MemoryListTool

        registry.register(MemoryListTool(memory_store))

    return registry


def parse_fallback_tool_calls(text: str) -> list[ToolCall]:
    calls: list[ToolCall] = []

    fenced = re.findall(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL)
    candidates: list[str] = list(fenced)

    if not candidates:
        for match in re.finditer(r"\{[^{}]*\"tool\"[^{}]*\{[^}]*\}[^}]*\}", text):
            candidates.append(match.group(0))
        for match in re.finditer(r"\{[^{}]*\"tool\"[^{}]*\}", text):
            candidates.append(match.group(0))

    for raw in candidates:
        raw = raw.strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        data = cast(dict[str, object], obj)
        raw_name = data.get("tool", "")
        raw_args = data.get("arguments", {})
        if isinstance(raw_name, str) and raw_name and isinstance(raw_args, dict):
            calls.append(
                ToolCall(
                    id=f"fallback_{len(calls)}",
                    name=raw_name,
                    arguments=cast(dict[str, object], raw_args),
                )
            )

    return calls


def strip_tool_json(text: str) -> str:
    cleaned = re.sub(
        r'```(?:json)?\s*\n?\s*\{[^`]*"tool"\s*:[^`]*\}\s*\n?\s*```',
        "",
        text,
        flags=re.DOTALL,
    )
    cleaned = re.sub(r'\{\s*"tool"\s*:\s*"[^"]*"[^}]*\}', "", cleaned)
    return cleaned.strip()


async def persist_message(
    manager: SessionManager,
    session: Session,
    role: str,
    content: str,
    *,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    tool_calls: Sequence[object] | None = None,
) -> None:
    try:
        await manager.add_message(
            session,
            role,
            content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_calls=tool_calls,
        )
    except Exception as exc:
        log.debug("agent.persist_failed", error=str(exc))


async def execute_tool(
    registry: ToolRegistry,
    tc: ToolCall,
    *,
    evomap_enabled: bool,
    browser_allowed: bool,
    office_block_bash_probe: bool,
    office_block_message: str,
    office_edit_only: bool,
    office_edit_path: str,
    on_tool_call: OnToolCall | None,
    on_tool_result: OnToolResult | None,
) -> tuple[str, ToolResult]:
    if tc.name == "bash":
        tc = _normalize_nano_banana_bash_tool_call(tc)
        raw_command = str(tc.arguments.get("command", ""))
        if _is_crontab_command(raw_command):
            result = ToolResult(
                success=False,
                output="",
                error=(
                    "禁止通过 bash 操作系统 crontab（macOS 会弹 TCC 权限窗导致超时）。\n"
                    "请使用内置工具：\n"
                    "- reminder(message=..., minutes=N) 设置一次性定时任务\n"
                    "- cron(action='add', ...) 设置重复定时任务\n"
                    "- cron(action='list') 查看已有定时任务\n"
                    "- cron(action='remove', job_id=...) 删除定时任务"
                ),
            )
            if on_tool_call:
                await on_tool_call(tc.name, tc.arguments)
            if on_tool_result:
                await on_tool_result(tc.name, result)
            return tc.id, result
    if on_tool_call:
        await on_tool_call(tc.name, tc.arguments)

    t0 = time.monotonic()

    if tc.name == "browser" and not browser_allowed:
        result = ToolResult(
            success=False,
            output="",
            error="请先执行 evomap_fetch；若无命中会自动切换到 browser",
        )
    elif tc.name == "file_write" and office_edit_only:
        content = str(tc.arguments.get("content", ""))
        if looks_like_ppt_generation_script(content):
            result = ToolResult(
                success=False,
                output="",
                error=(
                    "检测到这是修改已有PPT的请求，禁止重新生成新PPT。\n"
                    f"请直接使用 ppt_edit 修改：{office_edit_path}"
                ),
            )
        else:
            result = await _execute_registered_tool(registry, tc)
    elif tc.name == "bash" and office_block_bash_probe:
        raw_command = str(tc.arguments.get("command", ""))
        if is_office_path_probe_command(raw_command):
            result = ToolResult(success=False, output="", error=office_block_message)
        elif office_edit_only and looks_like_ppt_generation_command(raw_command):
            result = ToolResult(
                success=False,
                output="",
                error=(
                    "检测到这是修改已有PPT的请求，禁止重新生成新PPT。\n"
                    f"请直接使用 ppt_edit 修改：{office_edit_path}"
                ),
            )
        else:
            result = await _execute_registered_tool(registry, tc)
            result = await _maybe_retry_after_mkdir(registry, tc, result)
            result = await _maybe_retry_python_script_invocation(registry, tc, result)
    elif tc.name.startswith("evomap_") and not evomap_enabled:
        result = ToolResult(
            success=False,
            output="",
            error="EvoMap 已关闭，请先在设置中开启",
        )
    else:
        result = await _execute_registered_tool(registry, tc)
        if tc.name == "bash":
            result = await _maybe_retry_after_mkdir(registry, tc, result)
            result = await _maybe_retry_python_script_invocation(registry, tc, result)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "agent.tool_exec",
        tool=tc.name,
        success=result.success,
        elapsed_ms=elapsed_ms,
        args_preview=str(tc.arguments)[:200],
    )

    if on_tool_result:
        await on_tool_result(tc.name, result)

    return tc.id, result


async def _execute_registered_tool(registry: ToolRegistry, tc: ToolCall) -> ToolResult:
    tool = registry.get(tc.name)
    if tool is None:
        return ToolResult(success=False, output="", error=f"未知工具: {tc.name}")
    try:
        if tc.name == "bash":
            parallel_result = await _execute_parallel_nano_bash_commands(tool, tc)
            if parallel_result is not None:
                return parallel_result
        return await tool.execute(**tc.arguments)
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


def _split_parallel_nano_bash_commands(command: str) -> list[str]:
    if not _NANO_BANANA_SCRIPT_RE.search(command):
        return []
    parts = [part.strip() for part in _NANO_BANANA_SPLIT_RE.split(command) if part.strip()]
    if len(parts) <= 1:
        return []
    if not all(_NANO_BANANA_SCRIPT_RE.search(part) for part in parts):
        return []
    return parts


async def _execute_parallel_nano_bash_commands(tool: object, tc: ToolCall) -> ToolResult | None:
    raw_command = str(tc.arguments.get("command", "")).strip()
    if not raw_command or bool(tc.arguments.get("background", False)):
        return None
    commands = _split_parallel_nano_bash_commands(raw_command)
    if not commands:
        return None
    if not hasattr(tool, "execute"):
        return None

    timeout = int(tc.arguments.get("timeout", 30))
    results: list[ToolResult] = []
    for start in range(0, len(commands), _NANO_BANANA_BATCH_SIZE):
        if start > 0:
            await asyncio.sleep(_NANO_BANANA_BATCH_DELAY_SECONDS)
        batch = commands[start : start + _NANO_BANANA_BATCH_SIZE]
        batch_results: list[ToolResult] = list(await asyncio.gather(*(
            tool.execute(command=command, timeout=timeout, background=False)  # type: ignore[attr-defined]
            for command in batch
        )))
        results.extend(batch_results)

    success = all(result.success for result in results)
    blocks: list[str] = []
    errors: list[str] = []
    for idx, result in enumerate(results, start=1):
        detail = result.output.strip() or (result.error or "").strip() or "(empty output)"
        blocks.append(f"[并发生图 {idx}]\n{detail}")
        if not result.success:
            errors.append(f"{idx}:{(result.error or detail)[:200]}")
    return ToolResult(
        success=success,
        output="\n\n".join(blocks),
        error=" | ".join(errors) if errors else None,
    )


def _normalize_nano_banana_bash_tool_call(tc: ToolCall) -> ToolCall:
    if tc.name != "bash":
        return tc
    raw_command = str(tc.arguments.get("command", "")).strip()
    if not raw_command or not _NANO_BANANA_SCRIPT_RE.search(raw_command):
        return tc
    normalized = _normalize_nano_banana_command(raw_command)
    if normalized == raw_command:
        return tc
    updated_args = dict(tc.arguments)
    updated_args["command"] = normalized
    log.info(
        "agent.nano_banana_command_autofixed",
        original=raw_command[:240],
        normalized=normalized[:240],
    )
    return ToolCall(id=tc.id, name=tc.name, arguments=updated_args)


_NANO_BANANA_DISPLAY_TO_MODEL: dict[str, str] = {
    "香蕉 2": "gemini-3.1-flash-image-preview",
    "香蕉2": "gemini-3.1-flash-image-preview",
    "香蕉 pro": "nano-banana-2",
    "香蕉pro": "nano-banana-2",
}


def _normalize_nano_banana_command(command: str) -> str:
    if not _NANO_BANANA_SCRIPT_RE.search(command):
        return command

    result = command
    changed = False

    # --api-base -> --base-url
    if "--api-base" in result:
        result = result.replace("--api-base", "--base-url")
        changed = True

    # --mode text2image -> --mode text
    if "text2image" in result:
        result = re.sub(r"--mode\s+text2image\b", "--mode text", result)
        if result != command:
            changed = True

    # --size <ratio> -> --aspect-ratio <ratio>  (only when value looks like a ratio)
    size_match = re.search(r"--size\s+(\d+:\d+)(?=\s|$)", result)
    if size_match:
        result = result.replace(size_match.group(0), f"--aspect-ratio {size_match.group(1)}")
        changed = True

    # --model / --edit-model: map display names to model IDs
    for flag in ("--model", "--edit-model"):
        for display_name, model_id in _NANO_BANANA_DISPLAY_TO_MODEL.items():
            # Match flag followed by the display name (possibly quoted)
            pattern = re.compile(
                re.escape(flag) + r"""\s+['"]?""" + re.escape(display_name) + r"""['"]?(?=\s|$)"""
            )
            replacement = f"{flag} {model_id}"
            new_result = pattern.sub(replacement, result)
            if new_result != result:
                result = new_result
                changed = True

    if not changed:
        return command
    return result


async def _maybe_retry_after_mkdir(
    registry: ToolRegistry,
    tc: ToolCall,
    result: ToolResult,
) -> ToolResult:
    tool = registry.get(tc.name)
    if result.success or tool is None:
        return result
    missing_target = can_auto_create_parent_for_failure(result)
    if not missing_target:
        return result
    parent = str(Path(missing_target).expanduser().resolve().parent)
    try:
        mkdir_result = await tool.execute(command=f"mkdir -p '{parent}'", timeout=30)
        if mkdir_result.success:
            return await tool.execute(**tc.arguments)
    except Exception:
        pass
    return result


def _rewrite_direct_python_script_command(command: str) -> str:
    if not _PROJECT_PYTHON.is_file():
        return command
    match = _DIRECT_PY_SCRIPT_RE.match(command.strip())
    if match is None:
        return command
    prefix = match.group("prefix")
    script = match.group("script")
    suffix = match.group("suffix") or ""
    return f"{prefix}{_PROJECT_PYTHON} {script}{suffix}"


def _is_python_shell_mismatch(result: ToolResult) -> bool:
    if result.success:
        return False
    text = f"{result.error or ''}\n{result.output or ''}".lower()
    return any(hint in text for hint in _PY_SHELL_MISMATCH_HINTS)


async def _maybe_retry_python_script_invocation(
    registry: ToolRegistry,
    tc: ToolCall,
    result: ToolResult,
) -> ToolResult:
    tool = registry.get(tc.name)
    if result.success or tool is None or tc.name != "bash":
        return result
    if not _is_python_shell_mismatch(result):
        return result
    raw_command = str(tc.arguments.get("command", "")).strip()
    if not raw_command:
        return result
    rewritten = _rewrite_direct_python_script_command(raw_command)
    if rewritten == raw_command:
        return result
    log.warning(
        "agent.bash_python_script_retry",
        original=raw_command[:200],
        rewritten=rewritten[:200],
    )
    retry_args = dict(tc.arguments)
    retry_args["command"] = rewritten
    try:
        return await tool.execute(**retry_args)
    except Exception:
        return result


def _summarize_error_output(error: str, output: str) -> str:
    """从失败 tool 输出中提取关键信息，控制在 300 字符以内。

    仅用于历史轮压缩；当轮由 format_tool_output 直接透传原始输出。

    提取策略：
    - 5 行以内：原样返回
    - 含 Traceback：取 Traceback 前内容 + 最终错误行 + /tmp/ 用户帧
    - 无 Traceback 的长输出：取首 3 行 + 尾 2 行，中间省略
    """
    full = f"{error}\n{output}".strip() if output.strip() else error.strip()
    lines = full.splitlines()
    if len(lines) <= 5:
        return full if len(full) <= 300 else full[:297] + "..."

    tb_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("Traceback")),
        -1,
    )

    parts: list[str] = []
    if tb_idx >= 0:
        pre = "\n".join(ln for ln in lines[:tb_idx] if ln.strip()).strip()
        if pre:
            parts.append(pre)
        post_lines = [ln for ln in lines[tb_idx + 1 :] if ln.strip()]
        if post_lines:
            parts.append(post_lines[-1].strip())
        user_frames = [
            ln.strip()
            for ln in lines
            if "/tmp/" in ln and ln.strip().startswith("File ")
        ]
        if user_frames:
            parts.append(user_frames[-1])
    else:
        head = "\n".join(lines[:3]).strip()
        tail = "\n".join(lines[-2:]).strip()
        parts.append(head)
        if tail and tail != head:
            parts.append("...(省略中间内容)...")
            parts.append(tail)

    summary = "\n".join(parts).strip()
    if len(summary) > 300:
        summary = summary[:297] + "..."
    return summary or full[:300]


_NOISE_SEPARATOR_RE = re.compile(r"^[\s\-=*#~+]{6,}$")
_NOISE_ETA_RE = re.compile(r"eta\s+\d+:\d+:\d+", re.IGNORECASE)
_NOISE_PROGRESS_RE = re.compile(r"\d+\.\d+/\d+.*kB", re.IGNORECASE)


def _is_noise_line(line: str) -> bool:
    """Return True if *line* is a progress bar, separator, or other low-signal row."""
    stripped = line.strip()
    if not stripped:
        return True
    # 纯分隔符行（---、===、###、***）
    if _NOISE_SEPARATOR_RE.match(stripped):
        return True
    # pip/uv 进度行：含 eta 0:00:00
    if _NOISE_ETA_RE.search(stripped):
        return True
    # 进度数字：100.0/100.0 kB
    if _NOISE_PROGRESS_RE.search(stripped):
        return True
    # 大量 Unicode 框线字符（━ ┃ 等，ord > 0x2500）
    box_chars = sum(1 for ch in stripped if 0x2500 <= ord(ch) <= 0x257F)
    if box_chars >= 4:
        return True
    return False


def _summarize_success_output(tool_name: str, output: str) -> str:
    """将成功工具输出精简为历史友好的单行/短块摘要（≤ 300 字符）。

    仅用于历史轮压缩；当轮由 format_tool_output 直接透传原始输出。
    """
    text = output.strip()
    if not text:
        return "(empty output)"

    if len(text) <= 240:
        return text

    # 过滤进度条/纯分隔符等噪声行
    lines = [
        ln.rstrip()
        for ln in text.splitlines()
        if ln.strip() and not _is_noise_line(ln)
    ]
    if not lines:
        lines = [text.splitlines()[0]] if text.splitlines() else [text[:200]]

    # ── bash / shell 输出 ──────────────────────────────────────────
    if tool_name == "bash":
        exit_hint = ""
        last = lines[-1] if lines else ""
        if re.match(r"^(exit|exit code)[:\s]*\d+$", last, re.IGNORECASE):
            exit_hint = f" {last}"

        success_kw = ("successfully", "installed", "created", "saved", "写入", "完成", "成功", "已生成")
        path_kw = ("/", "路径", "文件")
        key_lines: list[str] = []
        for ln in lines:
            if re.match(r"^(exit|exit code)[:\s]*\d+$", ln, re.IGNORECASE):
                continue
            lo = ln.lower()
            if any(k in lo for k in success_kw) or any(k in ln for k in path_kw):
                key_lines.append(ln[:160])
            if len(key_lines) >= 3:
                break

        if not key_lines:
            non_exit = [ln for ln in lines if not re.match(r"^(exit|exit code)[:\s]*\d+$", ln, re.IGNORECASE)]
            key_lines = [non_exit[0][:160]] if non_exit else [lines[0][:160]]
            if len(non_exit) > 1:
                key_lines.append(non_exit[-1][:160])

        body = " | ".join(key_lines)
        result_text = f"✓ {body}{exit_hint}"
        return result_text if len(result_text) <= 300 else result_text[:297] + "..."

    # ── file_write / file_edit ─────────────────────────────────────
    if tool_name in {"file_write", "file_edit"}:
        return f"✓ {lines[0][:240]}"

    # ── browser（search_images / navigate / screenshot）────────────
    if tool_name == "browser":
        key_lines = []
        for ln in lines:
            if "/" in ln or "http" in ln or "图片" in ln or "已下载" in ln:
                key_lines.append(ln[:160])
            if len(key_lines) >= 3:
                break
        if not key_lines:
            key_lines = [lines[0][:200]]
        return "✓ " + " | ".join(key_lines)

    # ── 其他工具：通用短摘要 ───────────────────────────────────────
    path_lines: list[str] = []
    for ln in lines[1:]:
        if ln.startswith("/") or "路径" in ln or "文件" in ln:
            path_lines.append(ln[:120])
        if len(path_lines) >= 2:
            break

    head = lines[0][:180]
    if path_lines:
        body = head + " | " + " ; ".join(path_lines)
    else:
        body = head
    result_text = f"✓ {body}"
    return result_text if len(result_text) <= 300 else result_text[:297] + "..."


def format_tool_output(result: ToolResult, tool_name: str = "") -> str:
    """将工具结果压缩为历史轮友好的短摘要。仅用于持久化/历史轮。"""
    if result.success:
        return _summarize_success_output(tool_name, result.output or "")
    summarized = _summarize_error_output(result.error or "unknown error", result.output or "")
    output = f"[ERROR] {summarized}"
    diagnosis = diagnose_failure_hint(result)
    if diagnosis:
        output += f"\n[DIAGNOSIS] {diagnosis}"
    return output


def is_transient_cli_usage_error(result: ToolResult) -> bool:
    """Return whether a failed result is a nano-banana argparse parse error."""
    if result.success:
        return False
    text = f"{result.error or ''}\n{result.output or ''}".strip()
    if not text:
        return False
    if not _NANO_BANANA_ARGPARSE_ERROR_PATTERNS[0].search(text):
        return False
    return any(pattern.search(text) for pattern in _NANO_BANANA_ARGPARSE_ERROR_PATTERNS[1:])


def diagnose_failure_hint(result: ToolResult) -> str:
    text = f"{result.error or ''}\n{result.output or ''}"
    if "No such file or directory" in text and "FileNotFoundError" in text:
        return "更可能是目标路径或上级目录不存在，请先创建目录再写文件，不是依赖缺失。"
    if "ModuleNotFoundError" in text:
        return "这是依赖缺失，请安装缺失模块后重试。"
    if "Permission denied" in text:
        return "这是权限问题，请检查目标路径可写权限。"
    return ""


def extract_missing_target_path(text: str) -> str:
    match = re.search(r"No such file or directory:\s*['\"](/[^'\"]+)['\"]", text)
    if not match:
        return ""
    path = match.group(1).strip()
    return path if path else ""


def can_auto_create_parent_for_failure(result: ToolResult) -> str:
    text = f"{result.error or ''}\n{result.output or ''}"
    if "FileNotFoundError" not in text or "No such file or directory" not in text:
        return ""
    target = extract_missing_target_path(text)
    if not target:
        return ""
    suffix = Path(target).suffix.lower()
    if suffix not in {".pptx", ".docx", ".xlsx", ".pdf", ".html", ".md", ".txt", ".py"}:
        return ""
    return target


def validate_tool_call_args(tc: ToolCall, registry: ToolRegistry) -> str | None:
    tool = registry.get(tc.name)
    if tool is None:
        return None
    for param in tool.definition.parameters:
        if not param.required:
            continue
        if param.name not in tc.arguments:
            return f"{tc.name}.{param.name} 缺失"
        value = tc.arguments.get(param.name)
        if isinstance(value, str) and not value.strip():
            return f"{tc.name}.{param.name} 为空"
    if tc.name == "browser":
        action = str(tc.arguments.get("action", "")).strip()
        if not action:
            return "browser.action 为空"
        action_reqs: dict[str, tuple[str, ...]] = {
            "navigate": ("url",),
            "click": ("selector",),
            "type": ("selector", "text"),
            "evaluate": ("script",),
            "search_images": ("text",),
        }
        for field in action_reqs.get(action, ()):
            value = tc.arguments.get(field)
            if isinstance(value, str):
                if not value.strip():
                    return f"browser.{field} 为空"
            elif value is None:
                return f"browser.{field} 缺失"
    if tc.name == "ppt_edit":
        action = str(tc.arguments.get("action", "")).strip()
        if action != "add_slide":
            si = tc.arguments.get("slide_index")
            if si is None:
                return f"ppt_edit.slide_index 缺失（action={action or 'replace_text'} 时必填）"
            try:
                if int(si) <= 0:
                    return "ppt_edit.slide_index 必须 >= 1"
            except (TypeError, ValueError):
                return f"ppt_edit.slide_index 不是有效整数: {si}"
        path_val = tc.arguments.get("path")
        if not isinstance(path_val, str) or not path_val.strip():
            return "ppt_edit.path 缺失或为空"
    if tc.name == "file_edit":
        old_string = tc.arguments.get("old_string")
        new_string = tc.arguments.get("new_string")
        for field_name, field_val in (("old_string", old_string), ("new_string", new_string)):
            if not isinstance(field_val, str):
                continue
            if field_val.count("\\n") >= 3 and "\n" not in field_val:
                return f"file_edit.{field_name} 疑似转义块文本，改用 file_write 重写文件"
    return None


def is_non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def first_non_empty_arg(arguments: dict[str, object], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = arguments.get(name)
        if is_non_empty_str(value):
            return str(value).strip()
    return None


def looks_like_image_request(text: str) -> bool:
    lower = text.lower()
    keywords = (
        "图",
        "图片",
        "照片",
        "近照",
        "头像",
        "搜图",
        "找图",
        "壁纸",
        "海报",
        "背景",
        "写真",
        "image",
        "photo",
        "picture",
        "wallpaper",
        "poster",
    )
    return any(keyword.lower() in lower for keyword in keywords)


def is_garbled_query(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    escaped_noise = (
        stripped.count("\\n")
        + stripped.count("\\t")
        + stripped.count("\\r")
        + stripped.count("\\x")
    )
    if escaped_noise >= 2:
        return True
    if (
        re.search(r"(?:\\+[nrt]\d*){2,}", stripped)
        or re.search(r"(?:\n\d*){2,}", stripped)
        or stripped.count("\\x") >= 2
    ):
        return True
    return bool(len(stripped) > 40 and len(set(stripped)) < 10)


def repair_tool_call(tc: ToolCall, user_message: str) -> tuple[ToolCall, str | None]:
    args: dict[str, object] = dict(tc.arguments)
    changed = False
    reasons: list[str] = []

    alias_by_tool: dict[str, dict[str, tuple[str, ...]]] = {
        "bash": {"command": ("cmd", "script", "shell")},
        "browser": {"text": ("query", "keyword", "keywords", "q"), "selector": ("css",)},
        "file_read": {"path": ("file", "file_path")},
        "file_write": {"path": ("file", "file_path"), "content": ("text", "code")},
        "file_edit": {
            "path": ("file", "file_path"),
            "old_string": ("old", "old_text"),
            "new_string": ("new", "new_text"),
        },
    }
    alias_map = alias_by_tool.get(tc.name, {})
    for canonical, aliases in alias_map.items():
        if is_non_empty_str(args.get(canonical)):
            continue
        value = first_non_empty_arg(args, aliases)
        if value is None:
            continue
        args[canonical] = value
        changed = True
        reasons.append(f"{canonical}<-alias")

    if tc.name == "browser":
        action = str(args.get("action", "")).strip().lower()
        inferred_action: str | None = None

        if not action:
            if is_non_empty_str(args.get("url")):
                inferred_action = "navigate"
            elif is_non_empty_str(args.get("script")):
                inferred_action = "evaluate"
            elif is_non_empty_str(args.get("selector")) and is_non_empty_str(args.get("text")):
                inferred_action = "type"
            elif is_non_empty_str(args.get("selector")):
                if any(token in user_message for token in ("读取", "提取", "文本", "内容", "text")):
                    inferred_action = "get_text"
                else:
                    inferred_action = "click"
            else:
                query = first_non_empty_arg(args, ("text", "query", "keyword", "keywords", "q"))
                if query is not None and looks_like_image_request(user_message):
                    args["text"] = query
                    inferred_action = "search_images"

            if inferred_action is not None:
                args["action"] = inferred_action
                action = inferred_action
                changed = True
                reasons.append("action<-inferred")

        if action == "navigate" and not is_non_empty_str(args.get("url")):
            candidate = first_non_empty_arg(args, ("text",))
            if candidate and candidate.startswith(("http://", "https://")):
                args["url"] = candidate
                changed = True
                reasons.append("url<-text")
        if action == "search_images":
            query = str(args.get("text", "")).strip()
            if is_garbled_query(query) and looks_like_image_request(user_message):
                args["text"] = user_message.strip()
                changed = True
                reasons.append("text<-user_message")

    if not changed:
        return tc, None
    return ToolCall(id=tc.id, name=tc.name, arguments=args), ",".join(reasons)


__all__ = [
    "can_auto_create_parent_for_failure",
    "create_default_registry",
    "diagnose_failure_hint",
    "execute_tool",
    "first_non_empty_arg",
    "format_tool_output",
    "is_transient_cli_usage_error",
    "is_non_empty_str",
    "parse_fallback_tool_calls",
    "persist_message",
    "repair_tool_call",
    "strip_tool_json",
    "validate_tool_call_args",
]
