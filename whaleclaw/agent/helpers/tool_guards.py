"""Tool guard helpers for the single-agent runtime."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal

from whaleclaw.agent.helpers.image_search import (
    extract_planned_image_count,
    is_search_images_call,
    normalize_search_images_query,
)
from whaleclaw.providers.base import ToolCall
from whaleclaw.tools.base import ToolResult


@dataclass(slots=True)
class GuardLogEvent:
    level: Literal["info", "warning"]
    event: str
    fields: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ToolGuardUpdate:
    conversation_messages: list[str] = field(default_factory=list)
    final_texts: list[str] = field(default_factory=list)
    log_events: list[GuardLogEvent] = field(default_factory=list)
    stop_for_probe_loop: bool = False
    stop_for_repeat_loop: bool = False


@dataclass(slots=True)
class ToolGuardState:
    recent_signatures: list[str] = field(default_factory=list)
    recent_fuzzy_signatures: list[str] = field(default_factory=list)
    loop_detect_window: int = 3
    loop_warning_signature: str = ""
    loop_block_signature: str = ""
    fuzzy_loop_warning_signature: str = ""
    fuzzy_loop_block_signature: str = ""
    browser_fail_streak: int = 0
    search_images_count: int = 0
    planned_image_count: int | None = None
    search_images_limit: int | None = None
    search_images_blocked_reason: str = ""
    search_query_repeat_streak: int = 0
    last_search_query: str = ""
    bash_fail_streak: int = 0
    same_failed_bash_streak: int = 0
    last_failed_bash_signature: str = ""
    blocked_tools: set[str] = field(default_factory=set)
    low_value_bash_probe_streak: int = 0


def is_low_value_bash_probe(tc: ToolCall) -> bool:
    if tc.name != "bash":
        return False
    raw = str(tc.arguments.get("command", "")).strip().lower()
    if not raw:
        return False
    probe_hints = ("ls ", "ls\t", "stat ", "test -f ", "test -e ", "echo ")
    risky_hints = (
        "python ",
        "python3 ",
        "cp ",
        "mv ",
        "rm ",
        "sed ",
        "awk ",
        "perl ",
        "open ",
        "soffice ",
    )
    if any(h in raw for h in risky_hints):
        return False
    return any(h in raw for h in probe_hints)


def normalize_bash_command_signature(command: str) -> str:
    """Normalize bash command text for repeated-failure detection."""
    return re.sub(r"\s+", " ", command.strip())


_NANO_BANANA_BASH_RE = re.compile(
    r"test_nano_banana",
    re.IGNORECASE,
)


def is_nano_banana_bash_command(command: str) -> bool:
    """Return True if the bash command is a nano-banana image generation command."""
    return bool(_NANO_BANANA_BASH_RE.search(command))


_BASH_NOISE_RE = re.compile(
    r"\s*2>\s*\$null\b|"           # 2>$null
    r"\s*2>/dev/null\b|"           # 2>/dev/null
    r"\s*--output\s+\w+|"         # --output json / --output text
    r"\s*\|\s*Select-Object[^;|]*",  # | Select-Object ...
    re.IGNORECASE,
)

# For CLI tools like mcporter/npx, extract just the core command skeleton:
# "mcporter call dingtalk-ai-table list_bases limit=10" -> "mcporter call dingtalk-ai-table list_bases"
_CLI_CALL_RE = re.compile(
    r"^((?:npx\s+)?(?:mcporter|mcp\w*)\s+(?:call|list|config)\s+\S+(?:\s+\S+)?)"
    r"(?:\s+.*)?$",
    re.IGNORECASE,
)

# Generic key=value args that should be stripped for fuzzy matching
_KV_ARG_RE = re.compile(r"\s+\w+=\S+")
# --flag value pairs
_FLAG_ARG_RE = re.compile(r"\s+--\w+(?:\s+\S+)?")


def _fuzzy_tool_signature(tc: "ToolCall") -> str:
    """Build a fuzzy signature for a tool call, ignoring minor arg variations.

    For bash commands, strips stderr redirects, output format flags,
    key=value arguments, etc.  For CLI tools like mcporter, extracts
    just the command skeleton (e.g. "mcporter call server tool").
    For other tools, uses only the tool name + sorted argument keys.
    """
    if tc.name == "bash":
        cmd = str(tc.arguments.get("command", "")).strip()
        # Strip noise patterns
        cleaned = _BASH_NOISE_RE.sub("", cmd).strip()
        # For CLI call commands, extract just the skeleton
        cli_match = _CLI_CALL_RE.match(cleaned)
        if cli_match:
            cleaned = cli_match.group(1).strip()
        else:
            # For other bash commands, strip key=value and --flag args
            cleaned = _KV_ARG_RE.sub("", cleaned)
            cleaned = _FLAG_ARG_RE.sub("", cleaned)
        # Collapse whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return f"bash:{cleaned}"
    # For browser: include action + url/text so different pages count as
    # different operations (research), but same page counts as repeat.
    if tc.name == "browser":
        action = str(tc.arguments.get("action", "")).strip().lower()
        url = str(tc.arguments.get("url", "")).strip()
        text = str(tc.arguments.get("text", "")).strip()
        distinguisher = url or text
        return f"browser:{action}:{distinguisher}"
    # For other tools: tool name + sorted arg keys (ignore values)
    keys = sorted(tc.arguments.keys()) if tc.arguments else []
    return f"{tc.name}:{','.join(keys)}"


def tail_repeat_count(items: list[str]) -> int:
    """Count how many identical signatures repeat at the end of a sequence."""
    if not items:
        return 0
    last = items[-1]
    count = 0
    for item in reversed(items):
        if item != last:
            break
        count += 1
    return count


def is_progress_stage_tool_call(tc: ToolCall) -> bool:
    if tc.name in {
        "file_write",
        "file_edit",
        "patch_apply",
        "ppt_edit",
        "docx_edit",
        "xlsx_edit",
    }:
        return True
    return tc.name == "bash" and not is_low_value_bash_probe(tc)


def update_planned_image_count(
    state: ToolGuardState,
    content: str,
) -> None:
    if state.planned_image_count is not None:
        return
    detected_count = extract_planned_image_count(content)
    if detected_count is None:
        return
    state.planned_image_count = detected_count
    state.search_images_limit = max((detected_count * 3 + 1) // 2, detected_count + 1)


def blocked_tool_reasons(
    tool_calls: list[ToolCall],
    state: ToolGuardState,
) -> list[str]:
    reasons = [
        f"{tc.name} 已熔断，禁止继续调用"
        for tc in tool_calls
        if tc.name in state.blocked_tools
    ]
    if state.search_images_blocked_reason:
        reasons.extend(
            state.search_images_blocked_reason
            for tc in tool_calls
            if is_search_images_call(tc)
        )
    return reasons


def apply_tool_result_guards(
    state: ToolGuardState,
    tc: ToolCall,
    result: ToolResult,
    *,
    office_loop_guard_enabled: bool,
    image_api_probe_guard_enabled: bool,
    session_id: str | None,
) -> ToolGuardUpdate:
    update = ToolGuardUpdate()

    if tc.name == "browser":
        action = str(tc.arguments.get("action", "")).strip().lower()
        if action == "search_images" and result.success:
            state.search_images_count += 1
            query_sig = normalize_search_images_query(tc)
            if query_sig and query_sig == state.last_search_query:
                state.search_query_repeat_streak += 1
            elif query_sig:
                state.last_search_query = query_sig
                state.search_query_repeat_streak = 1
            else:
                state.last_search_query = ""
                state.search_query_repeat_streak = 0
        if result.success:
            state.browser_fail_streak = 0
        else:
            state.browser_fail_streak += 1
            if state.browser_fail_streak >= 2 and "browser" not in state.blocked_tools:
                state.blocked_tools.add("browser")
                update.log_events.append(
                    GuardLogEvent(
                        level="warning",
                        event="agent.tool_circuit_open",
                        fields={
                            "tool": "browser",
                            "fail_streak": state.browser_fail_streak,
                            "session_id": session_id or "",
                        },
                    )
                )
                update.conversation_messages.append(
                    "[系统降级] browser 工具连续失败，已自动熔断。"
                    "后续请不要再调用 browser。"
                    "请改用 bash 工具执行可复现的命令行方案完成任务。"
                )

    if tc.name == "bash":
        if result.success:
            state.bash_fail_streak = 0
            state.same_failed_bash_streak = 0
            state.last_failed_bash_signature = ""
        else:
            raw_command = str(tc.arguments.get("command", ""))
            # nano-banana 生图命令失败：任何原因一律禁止重试，继续执行剩余任务
            if is_nano_banana_bash_command(raw_command):
                update.log_events.append(
                    GuardLogEvent(
                        level="warning",
                        event="agent.nano_banana_image_gen_failed",
                        fields={
                            "session_id": session_id or "",
                            "error": (result.error or "")[:200],
                        },
                    )
                )
                update.conversation_messages.append(
                    "[系统提示] 本次生图失败，禁止自动重试。"
                    "请继续执行剩余的生图任务（如用户要求生成多张图，跳过本张继续生成后续图片）。"
                    "所有任务完成后，将成功和失败的结果一并回复用户，由用户决定是否对失败的图重新操作。"
                )
                # nano-banana 失败不计入通用 bash 失败熔断计数
                return update

            state.bash_fail_streak += 1
            failed_sig = normalize_bash_command_signature(raw_command)
            if failed_sig and failed_sig == state.last_failed_bash_signature:
                state.same_failed_bash_streak += 1
            elif failed_sig:
                state.same_failed_bash_streak = 1
                state.last_failed_bash_signature = failed_sig
            else:
                state.same_failed_bash_streak = 0
                state.last_failed_bash_signature = ""
            if state.same_failed_bash_streak >= 3 and "bash" not in state.blocked_tools:
                state.blocked_tools.add("bash")
                update.log_events.append(
                    GuardLogEvent(
                        level="warning",
                        event="agent.tool_circuit_open",
                        fields={
                            "tool": "bash",
                            "fail_streak": state.bash_fail_streak,
                            "same_failed_streak": state.same_failed_bash_streak,
                            "command_signature": state.last_failed_bash_signature[:200],
                            "session_id": session_id or "",
                        },
                    )
                )
                update.conversation_messages.append(
                    "[系统降级] 同一 bash 命令模板已连续失败 3 次，"
                    "已自动熔断并切换策略。"
                    "后续请不要再调用 bash。"
                    "请改用结构化编辑工具（ppt_edit/docx_edit/xlsx_edit）"
                    "或文件工具（file_read/file_write/file_edit）继续。"
                )

    if (office_loop_guard_enabled or image_api_probe_guard_enabled) and is_low_value_bash_probe(tc):
        state.low_value_bash_probe_streak += 1
    else:
        state.low_value_bash_probe_streak = 0
    if image_api_probe_guard_enabled and state.low_value_bash_probe_streak >= 2:
        update.final_texts.append(
            "检测到连续探测环境但未进入实测，已停止循环以避免卡住。"
            "下一步我将直接执行最小生图脚本（~/.whaleclaw/workspace/tmp）并返回状态码与图片路径。"
        )
        update.stop_for_probe_loop = True
        return update
    if office_loop_guard_enabled and state.low_value_bash_probe_streak >= 3:
        update.final_texts.append(
            "检测到连续的文件探测命令（如 ls/stat/test）且无实质修改，"
            "已停止循环以避免卡住。"
            "我将改用文档局部编辑工具继续，请直接告诉我要改哪一页/哪一段/哪个单元格。"
        )
        update.stop_for_probe_loop = True
        return update

    if result.success and is_progress_stage_tool_call(tc):
        state.search_query_repeat_streak = 0
        state.last_search_query = ""
    return update


def apply_post_round_guards(
    state: ToolGuardState,
    tool_calls: list[ToolCall],
    *,
    round_idx: int,
    session_id: str | None,
) -> ToolGuardUpdate:
    update = ToolGuardUpdate()

    if (
        not state.search_images_blocked_reason
        and state.search_images_limit is not None
        and state.search_images_count > state.search_images_limit
    ):
        state.search_images_blocked_reason = (
            "search_images 已超过计划配图数限制，禁止继续调用"
        )
        update.log_events.append(
            GuardLogEvent(
                level="warning",
                event="agent.search_images_over_plan",
                fields={
                    "session_id": session_id or "",
                    "planned_image_count": state.planned_image_count or 0,
                    "search_images_limit": state.search_images_limit,
                    "count": state.search_images_count,
                },
            )
        )
        update.conversation_messages.append(
            "[系统提示] 你已超过计划配图数的搜索额度。"
            f"计划配图数={state.planned_image_count}，"
            f"允许搜索上限={state.search_images_limit}，"
            f"当前已搜索={state.search_images_count}。"
            "禁止再调用 search_images。请立即进入生成或编辑步骤。"
        )
    if not state.search_images_blocked_reason and state.search_query_repeat_streak >= 3:
        state.search_images_blocked_reason = (
            "search_images 在重复相同搜索词且未取得新进展，禁止继续调用"
        )
        update.log_events.append(
            GuardLogEvent(
                level="warning",
                event="agent.search_images_repeat_query_blocked",
                fields={
                    "session_id": session_id or "",
                    "query": state.last_search_query[:200],
                    "repeat_streak": state.search_query_repeat_streak,
                },
            )
        )
        update.conversation_messages.append(
            "[系统提示] 检测到你在重复使用相同搜索词搜图且未取得新进展。"
            f"重复搜索词：{state.last_search_query or '(空)'}。"
            "禁止继续调用 search_images。请立即进入生成或编辑步骤。"
        )

    # --- Filter out tool calls that have their own dedicated guard ---
    # search_images has its own repeat/quota detection, and browser navigate
    # naturally visits different URLs each time (research/browsing).  These
    # should not count toward the generic loop detector.
    generic_tcs = [tc for tc in tool_calls if not is_search_images_call(tc)]

    # If every tool call in this round is search_images, skip generic loop detection
    if not generic_tcs:
        state.recent_signatures.append("")
        state.recent_fuzzy_signatures.append("")
        return update

    # --- Exact signature detection (original) ---
    sig_parts: list[str] = []
    for tc in generic_tcs:
        arg_str = json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False)[:200]
        sig_parts.append(f"{tc.name}:{arg_str}")
    round_sig = hashlib.md5("|".join(sig_parts).encode()).hexdigest()  # noqa: S324
    state.recent_signatures.append(round_sig)
    exact_repeat_count = tail_repeat_count(state.recent_signatures)

    # --- Fuzzy signature detection (new) ---
    fuzzy_parts = [_fuzzy_tool_signature(tc) for tc in generic_tcs]
    fuzzy_sig = hashlib.md5("|".join(fuzzy_parts).encode()).hexdigest()  # noqa: S324
    state.recent_fuzzy_signatures.append(fuzzy_sig)
    fuzzy_repeat_count = tail_repeat_count(state.recent_fuzzy_signatures)

    # Use the higher of exact vs fuzzy repeat count for detection
    effective_repeat = max(exact_repeat_count, fuzzy_repeat_count)
    repeated_tools = sorted({tc.name for tc in generic_tcs})
    repeated_tools_text = "、".join(repeated_tools) or "当前工具"

    # Warning: repeated >= window (default 3)
    if (
        effective_repeat >= state.loop_detect_window
        and round_sig != state.loop_warning_signature
        and fuzzy_sig != state.fuzzy_loop_warning_signature
    ):
        state.loop_warning_signature = round_sig
        state.fuzzy_loop_warning_signature = fuzzy_sig
        is_fuzzy = fuzzy_repeat_count > exact_repeat_count
        update.log_events.append(
            GuardLogEvent(
                level="warning",
                event="agent.loop_detected",
                fields={
                    "session_id": session_id or "",
                    "rounds": round_idx + 1,
                    "repeated_tools": repeated_tools,
                    "exact_repeat_count": exact_repeat_count,
                    "fuzzy_repeat_count": fuzzy_repeat_count,
                    "fuzzy_match": is_fuzzy,
                },
            )
        )
        update.conversation_messages.append(
            "[系统提示] 检测到你在重复执行相似操作且未取得进展。"
            f"重复工具：{repeated_tools_text}（连续 {effective_repeat} 轮）。"
            "你必须立刻改变策略：\n"
            "1. 跳过所有前置检查、守门规则、schema 验证、版本检查等预检步骤。\n"
            "2. 直接执行用户要求的核心操作（如创建、查询、修改等）。\n"
            "3. 改变参数、加减重定向、换输出格式不算改变策略。\n"
            "如果核心操作也失败，直接告诉用户结果，不要再重试。"
        )

    # Block: repeated >= window + 1 (default 4)
    if (
        effective_repeat >= (state.loop_detect_window + 1)
        and round_sig != state.loop_block_signature
        and fuzzy_sig != state.fuzzy_loop_block_signature
    ):
        state.loop_block_signature = round_sig
        state.fuzzy_loop_block_signature = fuzzy_sig
        newly_blocked = [name for name in repeated_tools if name not in state.blocked_tools]
        if newly_blocked:
            state.blocked_tools.update(newly_blocked)
            update.log_events.append(
                GuardLogEvent(
                    level="warning",
                    event="agent.loop_circuit_open",
                    fields={
                        "session_id": session_id or "",
                        "rounds": round_idx + 1,
                        "blocked_tools": newly_blocked,
                        "repeat_count": effective_repeat,
                    },
                )
            )
            update.conversation_messages.append(
                "[系统降级] 检测到重复路径仍在继续，已自动熔断这些工具："
                f"{'、'.join(newly_blocked)}。"
                "后续禁止继续调用它们。"
                "请改用其他工具或更直接的方案完成任务。"
            )

    # Hard stop: repeated >= window + 2 (default 5)
    if effective_repeat >= (state.loop_detect_window + 2):
        update.final_texts.append(
            "检测到任务陷入重复循环，已自动中止当前路径。"
            "请让我改用更直接的方式继续完成任务。"
        )
        update.stop_for_repeat_loop = True
    return update


__all__ = [
    "GuardLogEvent",
    "ToolGuardState",
    "ToolGuardUpdate",
    "apply_post_round_guards",
    "apply_tool_result_guards",
    "blocked_tool_reasons",
    "is_low_value_bash_probe",
    "is_nano_banana_bash_command",
    "is_progress_stage_tool_call",
    "normalize_bash_command_signature",
    "tail_repeat_count",
    "update_planned_image_count",
    "_fuzzy_tool_signature",
]
