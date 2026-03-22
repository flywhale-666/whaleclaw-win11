"""Agent main loop — message -> LLM -> tool -> reply (multi-turn).

The loop is provider-agnostic.  Tool invocation follows a single code
path regardless of whether the provider supports native ``tools`` API:

* **Native mode** — tool schemas are passed via ``tools=`` parameter;
  the provider returns structured ``ToolCall`` objects in the response.
* **Fallback mode** — tool descriptions are injected into the system
  prompt; the LLM outputs a JSON block which the loop parses.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from whaleclaw.agent.context import OnToolCall, OnToolResult
from whaleclaw.agent.helpers.office_rules import (
    ABS_FILE_PATH_RE as _ABS_FILE_PATH_RE,
)
from whaleclaw.agent.helpers.office_rules import (
    NON_DELIVERY_EXTS as _NON_DELIVERY_EXTS,
)
from whaleclaw.agent.helpers.office_rules import (
    OFFICE_PATH_RE as _OFFICE_PATH_RE,
)
from whaleclaw.agent.helpers.office_rules import (
    append_office_system_hints as _append_office_system_hints,
)
from whaleclaw.agent.helpers.office_rules import (
    build_image_generation_system_message as _build_image_generation_system_message,
)
from whaleclaw.agent.helpers.office_rules import (
    build_office_path_block_message as _build_office_path_block_message,
)
from whaleclaw.agent.helpers.office_rules import (
    capture_latest_pptx as _capture_latest_pptx,
)
from whaleclaw.agent.helpers.office_rules import (
    extract_artifact_baseline as _extract_artifact_baseline,
)
from whaleclaw.agent.helpers.office_rules import (
    extract_delivery_artifact_paths as _extract_delivery_artifact_paths,
)
from whaleclaw.agent.helpers.office_rules import (
    extract_office_paths as _extract_office_paths,
)
from whaleclaw.agent.helpers.office_rules import (
    extract_round_delivery_section as _extract_round_delivery_section,
)
from whaleclaw.agent.helpers.office_rules import (
    fix_version_suffix as _fix_version_suffix,
)
from whaleclaw.agent.helpers.office_rules import (
    force_include_office_edit_tools as _force_include_office_edit_tools,
)
from whaleclaw.agent.helpers.office_rules import (
    get_default_office_edit_path as _get_default_office_edit_path,
)
from whaleclaw.agent.helpers.office_rules import (
    has_any_last_office_path as _has_any_last_office_path,
)
from whaleclaw.agent.helpers.office_rules import (
    is_followup_edit_message as _is_followup_edit_message,
)
from whaleclaw.agent.helpers.office_rules import (
    is_image_generation_request as _is_image_generation_request,
)
from whaleclaw.agent.helpers.office_rules import (
    is_office_edit_request as _is_office_edit_request,
)
from whaleclaw.agent.helpers.office_rules import (
    mentions_specific_dark_bar_target as _mentions_specific_dark_bar_target,
)
from whaleclaw.agent.helpers.office_rules import (
    remember_office_path as _remember_office_path,
)
from whaleclaw.agent.helpers.office_rules import (
    snapshot_round_artifacts as _snapshot_round_artifacts,
)
from whaleclaw.agent.helpers.office_rules import with_round_version_suffix
from whaleclaw.agent.helpers.skill_lock import (
    build_nano_banana_execution_system_message as _build_nano_banana_execution_system_message,
)
from whaleclaw.agent.helpers.skill_lock import (
    build_skill_lock_system_message as _build_skill_lock_system_message,
)
from whaleclaw.agent.helpers.skill_lock import (
    build_skill_param_guard_reply as _build_skill_param_guard_reply,
)
from whaleclaw.agent.helpers.skill_lock import (
    detect_assistant_name_update as _detect_assistant_name_update,
)
from whaleclaw.agent.helpers.skill_lock import (
    detect_nano_banana_model_display as _detect_nano_banana_model_display,
)
from whaleclaw.agent.helpers.skill_lock import guarded_skills as _guarded_skills
from whaleclaw.agent.helpers.skill_lock import (
    is_nano_banana_activation_message as _is_nano_banana_activation_message,
)
from whaleclaw.agent.helpers.skill_lock import (
    is_nano_banana_control_message as _is_nano_banana_control_message,
)
from whaleclaw.agent.helpers.skill_lock import (
    is_task_done_confirmation as _is_task_done_confirmation,
)
from whaleclaw.agent.helpers.skill_lock import (
    load_saved_nano_banana_model_display as _load_saved_nano_banana_model_display,
)
from whaleclaw.agent.helpers.skill_lock import (
    looks_like_skill_activation_message as _looks_like_skill_activation_message,
)
from whaleclaw.agent.helpers.skill_lock import (
    nano_banana_missing_required as _nano_banana_missing_required,
)
from whaleclaw.agent.helpers.skill_lock import normalize_skill_ids as _normalize_skill_ids
from whaleclaw.agent.helpers.skill_lock import parse_use_command as _parse_use_command
from whaleclaw.agent.helpers.skill_lock import preview_text as _preview_text
from whaleclaw.agent.helpers.skill_lock import (
    select_native_tool_names as _select_native_tool_names,
)
from whaleclaw.agent.helpers.skill_lock import skill_announcement as _skill_announcement
from whaleclaw.agent.helpers.skill_lock import (
    skill_explicitly_mentioned as _skill_explicitly_mentioned,
)
from whaleclaw.agent.helpers.skill_lock import (
    skill_trigger_mentioned as _skill_trigger_mentioned,
)
from whaleclaw.agent.helpers.skill_lock import update_guard_state as _update_guard_state
from whaleclaw.agent.helpers.tool_execution import (
    can_auto_create_parent_for_failure,
    create_default_registry,
    set_active_skill_hooks as _set_active_skill_hooks,
)
from whaleclaw.agent.helpers.tool_execution import (
    execute_tool as _execute_tool,
)
from whaleclaw.agent.helpers.tool_execution import (
    format_tool_output as _format_tool_output,
)
from whaleclaw.agent.helpers.tool_execution import (
    is_transient_cli_usage_error as _is_transient_cli_usage_error,
)
from whaleclaw.agent.helpers.tool_execution import (
    parse_fallback_tool_calls as _parse_fallback_tool_calls,
)
from whaleclaw.agent.helpers.tool_execution import (
    persist_message as _persist_message,
)
from whaleclaw.agent.helpers.tool_execution import repair_tool_call as _repair_tool_call
from whaleclaw.agent.helpers.tool_execution import strip_tool_json as _strip_tool_json
from whaleclaw.agent.helpers.tool_execution import (
    validate_tool_call_args as _validate_tool_call_args,
)
from whaleclaw.agent.helpers.tool_guards import (
    ToolGuardState,
    apply_post_round_guards,
    apply_tool_result_guards,
    blocked_tool_reasons,
    update_planned_image_count,
)
from whaleclaw.agent.prompt import PromptAssembler
from whaleclaw.config.schema import WhaleclawConfig
from whaleclaw.providers.base import AgentResponse, ImageContent, Message, ToolCall
from whaleclaw.providers.router import ModelRouter
from whaleclaw.sessions.compressor import ContextCompressor
from whaleclaw.sessions.context_window import RECENT_PROTECTED, ContextWindow
from whaleclaw.sessions.manager import Session, SessionManager
from whaleclaw.sessions.store import SessionStore, SummaryRow
from whaleclaw.skills.hooks import get_skill_hooks as _get_skill_hooks
from whaleclaw.skills.parser import Skill
from whaleclaw.tools.base import ToolResult
from whaleclaw.tools.registry import ToolRegistry
from whaleclaw.types import ProviderAuthError, ProviderError, ProviderRateLimitError, StreamCallback
from whaleclaw.utils.log import get_logger

if TYPE_CHECKING:
    from whaleclaw.memory.manager import MemoryManager
    from whaleclaw.sessions.group_compressor import SessionGroupCompressor

log = get_logger(__name__)


def _default_pptx_scan_roots() -> tuple[Path, ...]:
    """Return platform-aware scan roots for PPTX capture."""
    import os
    import sys
    import tempfile

    roots: list[Path] = [
        Path.home() / ".whaleclaw" / "workspace" / "tmp",
        Path.home() / ".whaleclaw" / "workspace",
        Path.home() / "Downloads",
    ]
    if sys.platform == "win32":
        roots.append(Path.home() / "Desktop")
        tmp = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
        roots.append(Path(tmp))
    else:
        roots.extend([Path("/tmp"), Path("/private/tmp")])
    return tuple(roots)


OnRoundResult = Callable[[int, str], Awaitable[None]]
OnAgentDone = Callable[[str, int, int, int], Awaitable[None]]

_assembler = PromptAssembler()
_context_window = ContextWindow()
_compressor = ContextCompressor()
_memory_organizer_tasks: dict[str, asyncio.Task[None]] = {}

_MAX_OUTPUT_TOKENS = 200_000

# 无效工具调用熔断：连续此轮数工具调用参数无效（被 blocked_tools 拦截或参数校验失败）→ 停止自动重试
MODEL_REPAIR_RETRY_LIMIT = 2
_EVOMAP_MAX_TOKENS = 1000
_EXTRA_MEMORY_COMPRESS_TIMEOUT_SECONDS = 8
_DEFAULT_ASSISTANT_NAME = "WhaleClaw"

from whaleclaw.agent.helpers.evomap_utils import (
    build_evomap_choice_prompt as _build_evomap_choice_prompt,
    build_evomap_first_system_message as _build_evomap_first_system_message,
    extract_evomap_choice_index as _extract_evomap_choice_index,
    extra_memory_has_evomap_hint as _extra_memory_has_evomap_hint,
    is_evomap_enabled as _is_evomap_enabled,
    is_no_match_evomap_output as _is_no_match_evomap_output,
    is_tasky_message_for_evomap as _is_tasky_message_for_evomap,
    llm_judge_task_phase as _llm_judge_task_phase,
    parse_evomap_fetch_candidates as _parse_evomap_fetch_candidates,
    pick_top_evomap_candidates as _pick_top_evomap_candidates,
)
from whaleclaw.agent.helpers.image_utils import (
    NANO_BANANA_PARALLEL_BATCH_DELAY_S as _NANO_BANANA_PARALLEL_BATCH_DELAY_S,
    NANO_BANANA_PARALLEL_BATCH_SIZE as _NANO_BANANA_PARALLEL_BATCH_SIZE,
    build_nano_banana_command as _build_nano_banana_command,
    clean_nano_banana_prompt_delta as _clean_nano_banana_prompt_delta,  # pyright: ignore[reportUnusedImport]
    extract_input_image_paths_from_text as _extract_input_image_paths_from_text,
    fix_image_paths as _fix_image_paths,
    is_clearly_unrelated_to_image as _is_clearly_unrelated_to_image,
    is_parallelizable_nano_bash_call as _is_parallelizable_nano_bash_call,
    make_plan_hint as _make_plan_hint,
    merge_nano_banana_prompt as _merge_nano_banana_prompt,
    message_may_need_prior_images as _message_may_need_prior_images,
    message_requests_image_edit as _message_requests_image_edit,
    message_requests_image_regenerate as _message_requests_image_regenerate,
    recover_last_input_images as _recover_last_input_images,
    recover_last_nano_banana_mode as _recover_last_nano_banana_mode,
    recover_latest_generated_image as _recover_latest_generated_image,
    recover_latest_generated_image_path as _recover_latest_generated_image_path,
    recover_recent_session_image_paths as _recover_recent_session_image_paths,
    resolve_nano_banana_input_paths as _resolve_nano_banana_input_paths,
    skill_requires_images as _skill_requires_images,
)
from whaleclaw.agent.helpers.memory_utils import (
    build_compound_task_system_message as _build_compound_task_system_message,
    build_external_memory_system_message as _build_external_memory_system_message,
    build_global_style_system_message as _build_global_style_system_message,
    build_memory_system_message as _build_memory_system_message,
    compress_external_memory_with_llm as _compress_external_memory_with_llm,
    est_tokens as _est_tokens,
    merge_recall_blocks as _merge_recall_blocks,
    schedule_memory_organizer_task as _schedule_memory_organizer_task,
    truncate_to_tokens as _truncate_to_tokens,
)
from whaleclaw.agent.helpers.multi_agent_utils import (
    attach_rounds_marker as _attach_rounds_marker,
    build_multi_agent_requirement_baseline as _build_multi_agent_requirement_baseline,  # pyright: ignore[reportUnusedImport]
    extract_multi_agent_rounds as _extract_multi_agent_rounds,
    extract_requested_deliverables as _extract_requested_deliverables,  # pyright: ignore[reportUnusedImport]
    extract_rounds_marker as _extract_rounds_marker,
    is_multi_agent_cancel as _is_multi_agent_cancel,
    is_multi_agent_confirm as _is_multi_agent_confirm,
    is_multi_agent_discuss_done as _is_multi_agent_discuss_done,
    looks_like_bad_coordinator_output as _looks_like_bad_coordinator_output,  # pyright: ignore[reportUnusedImport]
    looks_like_role_stall_output as _looks_like_role_stall_output,  # pyright: ignore[reportUnusedImport]
    need_image_output as _need_image_output,  # pyright: ignore[reportUnusedImport]
    resolve_multi_agent_cfg as _resolve_multi_agent_cfg,
    scenario_delivery_focus as _scenario_delivery_focus,  # pyright: ignore[reportUnusedImport]
    scenario_discuss_focus as _scenario_discuss_focus,
)
from whaleclaw.agent.helpers.regex_patterns import (
    ASSISTANT_NAME_RESET_PATTERNS as _ASSISTANT_NAME_RESET_PATTERNS,
    ASSISTANT_NAME_SET_PATTERNS as _ASSISTANT_NAME_SET_PATTERNS,
    COORDINATOR_ASK_RE as _COORDINATOR_ASK_RE,
    MULTI_AGENT_SCENARIO_LABELS as _MULTI_AGENT_SCENARIO_LABELS,
    NANO_BANANA_TEXT_TO_IMAGE_PATTERNS as _NANO_BANANA_TEXT_TO_IMAGE_PATTERNS,
    SKILL_ACTIVATION_PATTERNS as _SKILL_ACTIVATION_PATTERNS,
    TASK_DONE_PATTERNS as _TASK_DONE_PATTERNS,
    USE_CLEAR_IDS as _USE_CLEAR_IDS,
    USE_CMD_RE as _USE_CMD_RE,
    is_compound_task_message as _is_compound_task_message,
    is_creation_task_message as _is_creation_task_message,
    is_evomap_status_question as _is_evomap_status_question,
    is_skill_lock_status_question as _is_skill_lock_status_question,
)

# ── Bug 5/8: Deferred task intent detection ─────────────────────────

# 修改 5: 中文数字归一化
_CN_DIGIT_MAP: dict[str, str] = {
    "零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
    "十": "10", "两": "2",
}
_CN_COMPOUND_RE = re.compile(r"[零一二三四五六七八九十两]+")


def _cn_num_to_arabic(match: re.Match[str]) -> str:
    """Convert a Chinese numeral token (e.g. 十一, 二十三, 零五) to Arabic digits."""
    s = match.group(0)
    if len(s) == 1:
        return _CN_DIGIT_MAP.get(s, s)
    if s.startswith("十"):
        tail = _CN_DIGIT_MAP.get(s[1], "0") if len(s) > 1 else "0"
        return f"1{tail}"
    if len(s) == 2 and s[1] == "十":
        head = _CN_DIGIT_MAP.get(s[0], s[0])
        return f"{head}0"
    if len(s) == 3 and s[1] == "十":
        head = _CN_DIGIT_MAP.get(s[0], s[0])
        tail = _CN_DIGIT_MAP.get(s[2], "0")
        return f"{head}{tail}"
    # Fallback: digit-by-digit (e.g. 零五 → 05)
    return "".join(_CN_DIGIT_MAP.get(ch, ch) for ch in s)


def _normalize_cn_time(text: str) -> str:
    """Normalize Chinese numerals to Arabic digits for time pattern matching."""
    return _CN_COMPOUND_RE.sub(_cn_num_to_arabic, text)


_DEFERRED_TASK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(\d+)\s*分钟后"),
    re.compile(r"(\d+)\s*小时后"),
    re.compile(r"(\d+)\s*秒后"),
    re.compile(r"每天\s*\d{1,2}[:\s点时]\s*\d{0,2}"),
    re.compile(r"每隔\s*\d+\s*分钟"),
    re.compile(r"每\s*\d+\s*分钟"),
    # 修改 1+5: 统一时间点 pattern — 支持"半"/"分"/"的时候"/"叫"、中文数字已归一化
    re.compile(
        r"(?:(?:今天|明天|后天)?\s*(?:今晚|明早|早上|上午|中午|下午|晚上|凌晨|傍晚)\s*)?"
        r"\d{1,2}\s*[点时:：]\s*(?:半|\d{0,2})\s*(?:分钟?)?"
        r"(?:\s*的时候|\s*时候)?"
        r"\s*(?:去|做|执行|运行|发布|发送|提醒|推送|搜索|搜|找|画|写|打开|关闭|启动|停止|用|使用|叫)"
    ),
    re.compile(r"提醒我"),
)

_DEFERRED_ONLY_TOOLS = {"cron", "reminder"}
_DEFERRED_CRON_TOOL_WHITELIST = {"cron", "reminder", "cron_manage"}

# 修改 4: "定时提醒"等关键词单独处理，排除疑问句
_DEFERRED_SCHEDULE_KW = re.compile(r"(?:定时|定期)\s*(?:执行|运行|发布|发送|提醒|推送)")
_QUESTION_TAIL_RE = re.compile(r"[吗呢？\?]\s*$")
_QUESTION_PREFIX_RE = re.compile(r"(?:有没有|是否|是不是|有做|查看|查询|查下)")

_NANO_BANANA_SCRIPT_CMD_RE = re.compile(r"test_nano_banana_2\.py", re.IGNORECASE)


def _fix_nano_banana_mode(tc: ToolCall, session: Session) -> ToolCall:
    """When session expects edit mode but LLM emitted --mode text, fix it."""
    raw_command = str(tc.arguments.get("command", ""))
    if not _NANO_BANANA_SCRIPT_CMD_RE.search(raw_command):
        return tc
    metadata = session.metadata if isinstance(session.metadata, dict) else {}  # pyright: ignore[reportUnnecessaryIsInstance]
    expected_mode = str(metadata.get("last_nano_banana_mode", "")).strip()
    if expected_mode != "edit":
        return tc
    if re.search(r"--mode\s+text(?:\s|$)", raw_command) is None:
        return tc
    last_img = _recover_latest_generated_image_path(session)
    if not last_img:
        return tc
    fixed = re.sub(r"--mode\s+text(?=\s|$)", "--mode edit", raw_command)
    if "--input-image" not in fixed:
        fixed += f' --input-image "{last_img}"'
    updated_args = dict(tc.arguments)
    updated_args["command"] = fixed
    log.info(
        "agent.nano_banana_mode_autofixed",
        original_mode="text",
        fixed_mode="edit",
        input_image=last_img,
    )
    return ToolCall(id=tc.id, name=tc.name, arguments=updated_args)


def _is_deferred_task_intent(message: str) -> bool:
    """检测用户消息是否是定时/周期任务意图，而非立即执行。"""
    normalized = _normalize_cn_time(message)
    if any(p.search(normalized) for p in _DEFERRED_TASK_PATTERNS):
        return True
    if not _DEFERRED_SCHEDULE_KW.search(normalized):
        return False
    if _QUESTION_TAIL_RE.search(normalized) or _QUESTION_PREFIX_RE.search(normalized):
        return False
    return True


# Public aliases for cross-module reuse.
MULTI_AGENT_SCENARIO_LABELS = _MULTI_AGENT_SCENARIO_LABELS
ABS_FILE_PATH_RE = _ABS_FILE_PATH_RE
NON_DELIVERY_EXTS = _NON_DELIVERY_EXTS
OFFICE_PATH_RE = _OFFICE_PATH_RE
COORDINATOR_ASK_RE = _COORDINATOR_ASK_RE
_with_round_version_suffix = with_round_version_suffix
_can_auto_create_parent_for_failure = can_auto_create_parent_for_failure



def _inject_screenshot_image(
    conversation: list[Message],
    tool_output: str,
) -> None:
    """If *tool_output* mentions a saved screenshot path, read the PNG file
    and append a user message with the image so multimodal LLMs can see it."""
    import base64
    import re as _re
    from pathlib import Path

    m = _re.search(r"(?:截图已保存|Screenshot saved)[:\s]*(.+\.png)", tool_output)
    if not m:
        return
    img_path = Path(m.group(1).strip())
    if not img_path.is_file():
        return
    try:
        raw = img_path.read_bytes()
        if len(raw) > 5 * 1024 * 1024:
            return
        b64 = base64.b64encode(raw).decode("ascii")
        conversation.append(
            Message(
                role="user",
                content="[browser 截图]",
                images=[ImageContent(mime="image/png", data=b64)],
            )
        )
    except OSError:
        pass


def _dedup_consecutive_tool_errors(
    conversation: list[Message],
    tool_output: str,
    tool_name: str,
) -> None:
    """将 conversation 中与当前错误内容相同的前置 tool 消息缩短为占位行。

    向后遍历规则：
    - 遇到真正的 user 消息（非工具结果包装）立即停止
    - 跳过 assistant 消息
    - tool 消息或非原生模式的工具包装 user 消息：内容签名匹配则替换
    """
    if not tool_output.startswith("[ERROR]"):
        return
    sig = tool_output[:120]
    placeholder = f"[重复错误，与下一条相同，已省略] 工具: {tool_name}"
    for i in range(len(conversation) - 1, -1, -1):
        msg = conversation[i]
        if msg.role == "user":
            content = msg.content or ""
            if not content.startswith("[工具"):
                break
            # 非原生模式工具结果包装：格式为 "[工具 xxx 执行结果]\n<output>"
            newline_pos = content.find("\n")
            check = content[newline_pos + 1 :] if newline_pos >= 0 else content
            if check[:120] == sig:
                conversation[i] = Message(role="user", content=placeholder)
        elif msg.role == "assistant":
            continue
        elif msg.role == "tool":
            if (msg.content or "")[:120] == sig:
                conversation[i] = Message(
                    role="tool",
                    content=placeholder,
                    tool_call_id=msg.tool_call_id,
                )


# Public aliases for cross-module reuse (re-exported from helper modules).
scenario_discuss_focus = _scenario_discuss_focus
truncate_to_tokens = _truncate_to_tokens
resolve_multi_agent_cfg = _resolve_multi_agent_cfg
is_multi_agent_confirm = _is_multi_agent_confirm
extract_multi_agent_rounds = _extract_multi_agent_rounds
is_multi_agent_discuss_done = _is_multi_agent_discuss_done
select_native_tool_names = _select_native_tool_names
extract_round_delivery_section = _extract_round_delivery_section
extract_delivery_artifact_paths = _extract_delivery_artifact_paths
fix_version_suffix = _fix_version_suffix
snapshot_round_artifacts = _snapshot_round_artifacts
extract_artifact_baseline = _extract_artifact_baseline














async def _persist_session_metadata(
    session: Session | None,
    session_manager: SessionManager | None,
) -> bool:
    if session is None or session_manager is None:
        return False
    try:
        await session_manager.update_metadata(session, session.metadata)
    except Exception:
        return False
    return True


def _fire_bg_group_prewarm(
    *,
    session_id: str,
    session_store: SessionStore,
    group_compressor: "SessionGroupCompressor",
    router: ModelRouter,
    summarizer_model: str,
) -> None:
    """Schedule background SessionGroupCompressor prewarm for a session.

    The banana skill shortcut bypasses the normal LLM call, so
    group_compressor.build_window_messages() is never called during banana
    turns.  Without this helper, all banana-turn message groups remain
    uncached and get processed in bulk during the next gateway startup
    prewarm, causing congestion.  Calling this after each banana execution
    keeps the group-compression cache incrementally up-to-date.
    """

    async def _bg() -> None:
        try:
            msg_rows = await session_store.get_messages(session_id)
            if not msg_rows:
                return
            messages = [
                Message(
                    role=r.role if r.role != "tool" else "assistant",  # pyright: ignore[reportArgumentType]
                    content=r.content,
                )
                for r in msg_rows
            ]
            await group_compressor.prewarm_session(
                session_id=session_id,
                messages=messages,
                router=router,
                model_id=summarizer_model,
            )
        except Exception as exc:
            log.debug("agent.bg_group_prewarm_failed", error=str(exc))

    asyncio.create_task(_bg())


def _fire_bg_compress(
    *,
    session_id: str,
    session_store: SessionStore,
    router: ModelRouter,
    summarizer_model: str,
) -> None:
    """Schedule background L0/L1 compression if enough uncovered messages exist.

    This is a fire-and-forget helper extracted so that early-return paths
    (e.g. skill-lock branches, nano-banana shortcuts) can also trigger
    background compression instead of skipping it entirely.
    """

    async def _bg() -> None:
        try:
            latest = await session_store.get_latest_summary(session_id, "L0")
            msg_rows = await session_store.get_messages(session_id)

            already_covered = latest.source_msg_end if latest else 0
            uncovered = [r for r in msg_rows if r.id > already_covered]
            if not _compressor.should_compress(len(uncovered)):
                return
            protected = min(RECENT_PROTECTED, len(uncovered))
            to_compress = uncovered[:-protected] if protected < len(uncovered) else []

            if len(to_compress) < 8:
                return
            compress_msgs = [
                Message(role=r.role if r.role != "tool" else "assistant", content=r.content)  # pyright: ignore[reportArgumentType]
                for r in to_compress
            ]
            await _compressor.compress_segment(
                session_id=session_id,
                messages=compress_msgs,
                msg_id_start=to_compress[0].id,
                msg_id_end=to_compress[-1].id,
                store=session_store,
                router=router,
                model=summarizer_model,
            )
        except Exception as exc:
            log.debug("agent.bg_compress_failed", error=str(exc))

    asyncio.create_task(_bg())


async def _sync_multi_agent_compression_boundary(
    session: Session | None,
    session_manager: SessionManager | None,
    group_compressor: "SessionGroupCompressor | None" = None,
    *,
    ma_enabled: bool,
) -> None:
    """Track MA on/off transition and set compression boundary on MA -> single.

    When MA is enabled, mark current session as MA-active.
    When MA turns off, record a fixed message-index boundary so future
    group-compression only applies to newly produced messages.
    """
    if session is None or not isinstance(session.metadata, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
        return None
    metadata = session.metadata
    prev_active = bool(metadata.get("multi_agent_active_prev", False))
    changed = False
    if ma_enabled:
        if not prev_active:
            metadata["multi_agent_active_prev"] = True
            changed = True
    else:
        if prev_active:
            metadata["multi_agent_active_prev"] = False
            metadata["compression_resume_message_index"] = len(session.messages)
            changed = True
    if group_compressor is not None:  # session already checked above
        try:
            await group_compressor.set_session_suspended(
                session_id=session.id,
                suspended=ma_enabled,
            )
        except Exception as exc:
            log.debug(
                "agent.multi_agent_compressor_toggle_failed",
                session_id=session.id,
                error=str(exc),
                enabled=ma_enabled,
            )
    if changed:
        await _persist_session_metadata(session, session_manager)
    if session_manager is None:
        return None
    try:
        await asyncio.wait_for(
            session_manager.update_metadata(session, session.metadata),
            timeout=1.5,
        )
        return None
    except Exception as exc:
        log.warning(
            "agent.multi_agent_metadata_persist_failed",
            session_id=session.id,
            error=str(exc),
        )
        return None


async def _run_multi_agent_controller_discussion(
    *,
    user_message: str,
    pending_topic: str,
    cfg: dict[str, object],
    session_id: str,
    config: WhaleclawConfig,
    router: ModelRouter,
    registry: ToolRegistry,
    extra_memory: str,
    trigger_event_id: str,
    trigger_text_preview: str,
    include_intro: bool,
) -> str:
    from whaleclaw.agent import multi_agent as _multi_agent

    return await _multi_agent.run_multi_agent_controller_discussion(
        user_message=user_message,
        pending_topic=pending_topic,
        cfg=cfg,
        session_id=session_id,
        config=config,
        router=router,
        registry=registry,
        extra_memory=extra_memory,
        trigger_event_id=trigger_event_id,
        trigger_text_preview=trigger_text_preview,
        include_intro=include_intro,
    )


async def _run_multi_agent_executor(
    *,
    message: str,
    session_id: str,
    config: WhaleclawConfig,
    on_stream: StreamCallback | None,
    router: ModelRouter,
    registry: ToolRegistry,
    images: list[ImageContent] | None,
    extra_memory: str,
    trigger_event_id: str,
    trigger_text_preview: str,
    ma_cfg: dict[str, object],
    on_round_result: OnRoundResult | None = None,
) -> str:
    from whaleclaw.agent import multi_agent as _multi_agent

    return await _multi_agent.run_multi_agent_executor(
        message=message,
        session_id=session_id,
        config=config,
        on_stream=on_stream,
        router=router,
        registry=registry,
        images=images,
        extra_memory=extra_memory,
        trigger_event_id=trigger_event_id,
        trigger_text_preview=trigger_text_preview,
        ma_cfg=ma_cfg,
        on_round_result=on_round_result,
    )


async def run_agent(
    message: str,
    session_id: str,
    config: WhaleclawConfig,
    on_stream: StreamCallback | None = None,
    *,
    session: Session | None = None,
    router: ModelRouter | None = None,
    registry: ToolRegistry | None = None,
    on_tool_call: OnToolCall | None = None,
    on_tool_result: OnToolResult | None = None,
    on_round_result: OnRoundResult | None = None,
    on_done: OnAgentDone | None = None,
    images: list[ImageContent] | None = None,
    session_manager: SessionManager | None = None,
    session_store: SessionStore | None = None,
    memory_manager: "MemoryManager | None" = None,
    extra_memory: str = "",
    trigger_event_id: str = "",
    trigger_text_preview: str = "",
    group_compressor: "SessionGroupCompressor | None" = None,
    multi_agent_internal: bool = False,
) -> str:
    """Run the Agent loop with tool support and multi-turn context.

    The loop is provider-agnostic:
    1. Check if provider supports native tools API
    2. If yes  -> pass schemas via ``tools=``; parse structured tool_calls
    3. If no   -> inject tool descriptions into system prompt; parse JSON text
    4. Execute tools, append results, loop (until no tool calls or token budget exhausted)
    5. Return final text reply
    """
    agent_cfg = config.agent
    models_cfg = config.models
    summarizer_cfg = agent_cfg.summarizer

    model_id: str = session.model if session else agent_cfg.model
    if router is None:
        router = ModelRouter(models_cfg)
    if registry is None:
        registry = create_default_registry()

    if not multi_agent_internal:
        ma_cfg = _resolve_multi_agent_cfg(config, session)
        await _sync_multi_agent_compression_boundary(
            session,
            session_manager,
            group_compressor,
            ma_enabled=bool(ma_cfg.get("enabled", False)),
        )
        if bool(ma_cfg.get("enabled", False)):
            if session is not None:
                state = str(session.metadata.get("multi_agent_state", "")).strip().lower()
                waiting = state == "confirm" or (state == "" and bool(
                    session.metadata.get("multi_agent_waiting_confirm", False)
                ))
                intro_done = bool(session.metadata.get("multi_agent_intro_done", False))
                pending_topic = str(session.metadata.get("multi_agent_pending_topic", "")).strip()

                if waiting and _is_multi_agent_cancel(message):
                    session.metadata.pop("multi_agent_state", None)
                    session.metadata.pop("multi_agent_waiting_confirm", None)
                    session.metadata.pop("multi_agent_intro_done", None)
                    session.metadata.pop("multi_agent_pending_topic", None)
                    session.metadata.pop("multi_agent_pending_rounds", None)
                    await _persist_session_metadata(session, session_manager)
                    return "已取消本次多Agent执行。你可以继续普通对话，或发新需求后再确认启动。"

                rounds_override = _extract_multi_agent_rounds(message)
                if rounds_override is not None and waiting:
                    session.metadata["multi_agent_pending_rounds"] = rounds_override
                    topic = _attach_rounds_marker(pending_topic or message, rounds_override)
                    session.metadata["multi_agent_pending_topic"] = topic
                    await _persist_session_metadata(session, session_manager)
                    ma_cfg["max_rounds"] = rounds_override
                    return await _run_multi_agent_controller_discussion(
                        user_message=message,
                        pending_topic=topic,
                        cfg=ma_cfg,
                        session_id=session_id,
                        config=config,
                        router=router,
                        registry=registry,
                        extra_memory=extra_memory,
                        trigger_event_id=trigger_event_id,
                        trigger_text_preview=trigger_text_preview,
                        include_intro=not intro_done,
                    )

                if state == "discuss":
                    if _is_multi_agent_cancel(message):
                        session.metadata.pop("multi_agent_state", None)
                        session.metadata.pop("multi_agent_intro_done", None)
                        session.metadata.pop("multi_agent_pending_topic", None)
                        session.metadata.pop("multi_agent_pending_rounds", None)
                        await _persist_session_metadata(session, session_manager)
                        return "已取消本次多Agent讨论。你可以继续普通对话。"

                    topic = pending_topic

                    if _is_multi_agent_discuss_done(message):
                        rounds_raw = session.metadata.get("multi_agent_pending_rounds")
                        if isinstance(rounds_raw, int):
                            ma_cfg["max_rounds"] = max(1, min(rounds_raw, 10))
                        cleaned_topic, marker_rounds = _extract_rounds_marker(topic or "")
                        if not isinstance(rounds_raw, int) and marker_rounds is not None:
                            ma_cfg["max_rounds"] = marker_rounds
                        session.metadata.pop("multi_agent_state", None)
                        session.metadata.pop("multi_agent_waiting_confirm", None)
                        session.metadata.pop("multi_agent_intro_done", None)
                        session.metadata.pop("multi_agent_pending_topic", None)
                        session.metadata.pop("multi_agent_pending_rounds", None)
                        await _persist_session_metadata(session, session_manager)
                        return await _run_multi_agent_executor(
                            message=cleaned_topic or "（请补充你的任务目标）",
                            session_id=session_id,
                            config=config,
                            on_stream=on_stream,
                            router=router,
                            registry=registry,
                            images=images,
                            extra_memory=extra_memory,
                            trigger_event_id=trigger_event_id,
                            trigger_text_preview=trigger_text_preview,
                            ma_cfg=ma_cfg,
                            on_round_result=on_round_result,
                        )

                    if message.strip():
                        if topic:
                            topic = f"{topic}\n补充要求: {message.strip()}".strip()
                        else:
                            topic = message.strip()
                        session.metadata["multi_agent_pending_topic"] = topic
                    if rounds_override is not None:
                        session.metadata["multi_agent_pending_rounds"] = rounds_override
                        session.metadata["multi_agent_pending_topic"] = _attach_rounds_marker(
                            topic or message,
                            rounds_override,
                        )
                        topic = str(session.metadata["multi_agent_pending_topic"])
                        ma_cfg["max_rounds"] = rounds_override

                    include_intro = not bool(session.metadata.get("multi_agent_intro_done", False))
                    if include_intro:
                        session.metadata["multi_agent_intro_done"] = True
                    await _persist_session_metadata(session, session_manager)
                    return await _run_multi_agent_controller_discussion(
                        user_message=message,
                        pending_topic=topic or message,
                        cfg=ma_cfg,
                        session_id=session_id,
                        config=config,
                        router=router,
                        registry=registry,
                        extra_memory=extra_memory,
                        trigger_event_id=trigger_event_id,
                        trigger_text_preview=trigger_text_preview,
                        include_intro=include_intro,
                    )

                if waiting and _is_multi_agent_confirm(message):
                    topic = pending_topic or "（请补充你的任务目标）"
                    rounds_raw = session.metadata.get("multi_agent_pending_rounds")
                    if isinstance(rounds_raw, int):
                        ma_cfg["max_rounds"] = max(1, min(rounds_raw, 10))
                    cleaned_topic, marker_rounds = _extract_rounds_marker(topic or "")
                    if not isinstance(rounds_raw, int) and marker_rounds is not None:
                        ma_cfg["max_rounds"] = marker_rounds
                    session.metadata.pop("multi_agent_state", None)
                    session.metadata.pop("multi_agent_waiting_confirm", None)
                    session.metadata.pop("multi_agent_intro_done", None)
                    session.metadata.pop("multi_agent_pending_topic", None)
                    session.metadata.pop("multi_agent_pending_rounds", None)
                    await _persist_session_metadata(session, session_manager)
                    return await _run_multi_agent_executor(
                        message=cleaned_topic or topic,
                        session_id=session_id,
                        config=config,
                        on_stream=on_stream,
                        router=router,
                        registry=registry,
                        images=images,
                        extra_memory=extra_memory,
                        trigger_event_id=trigger_event_id,
                        trigger_text_preview=trigger_text_preview,
                        ma_cfg=ma_cfg,
                        on_round_result=on_round_result,
                    )

                if waiting and not _is_multi_agent_confirm(message):
                    topic = pending_topic
                    if message.strip() and not _is_multi_agent_cancel(message):
                        topic = f"{topic}\n补充要求: {message.strip()}".strip()
                        session.metadata["multi_agent_pending_topic"] = topic
                        await _persist_session_metadata(session, session_manager)
                    rounds_raw = session.metadata.get("multi_agent_pending_rounds")
                    if isinstance(rounds_raw, int):
                        ma_cfg["max_rounds"] = max(1, min(rounds_raw, 10))
                    else:
                        _, marker_rounds = _extract_rounds_marker(topic or "")
                        if marker_rounds is not None:
                            ma_cfg["max_rounds"] = marker_rounds
                    topic = topic or "（请补充你的任务目标）"
                    return await _run_multi_agent_controller_discussion(
                        user_message=message,
                        pending_topic=topic,
                        cfg=ma_cfg,
                        session_id=session_id,
                        config=config,
                        router=router,
                        registry=registry,
                        extra_memory=extra_memory,
                        trigger_event_id=trigger_event_id,
                        trigger_text_preview=trigger_text_preview,
                        include_intro=False,
                    )

                session.metadata["multi_agent_state"] = "discuss"
                session.metadata["multi_agent_waiting_confirm"] = False
                session.metadata["multi_agent_intro_done"] = True
                session.metadata["multi_agent_pending_topic"] = message.strip() or message
                session.metadata.pop("multi_agent_pending_rounds", None)
                await _persist_session_metadata(session, session_manager)
                return await _run_multi_agent_controller_discussion(
                    user_message=message,
                    pending_topic=message.strip() or message,
                    cfg=ma_cfg,
                    session_id=session_id,
                    config=config,
                    router=router,
                    registry=registry,
                    extra_memory=extra_memory,
                    trigger_event_id=trigger_event_id,
                    trigger_text_preview=trigger_text_preview,
                    include_intro=True,
                )

            return await _run_multi_agent_executor(
                message=message,
                session_id=session_id,
                config=config,
                on_stream=on_stream,
                router=router,
                registry=registry,
                images=images,
                extra_memory=extra_memory,
                trigger_event_id=trigger_event_id,
                trigger_text_preview=trigger_text_preview,
                ma_cfg=ma_cfg,
                on_round_result=on_round_result,
            )

    if _is_evomap_status_question(message):
        enabled = _is_evomap_enabled(config)
        switch_text = "已开启" if enabled else "已关闭"
        return (
            f"当前 EvoMap 开关{switch_text}（本地配置状态）。"
            "如果你要我检查远端服务连通性，我可以再单独做一次连通检测。"
        )

    metadata_dirty = False

    llm_message = message
    locked_skill_ids: list[str] = []
    previous_locked_skill_ids: list[str] = []
    lock_is_explicit = False
    pending_lock_skill_ids: list[str] = []
    lock_waiting_done = False
    skill_announce_pending = False
    routed_skills: list[Skill] = []
    routed_skill_ids: list[str] = []
    if not multi_agent_internal:
        if session is not None:
            raw_locked = session.metadata.get("locked_skill_ids")
            if isinstance(raw_locked, list):
                locked_skill_ids = [
                    str(x).strip().lower()  # pyright: ignore[reportUnknownArgumentType]
                    for x in raw_locked  # pyright: ignore[reportUnknownVariableType]
                    if isinstance(x, str) and str(x).strip()
                ]
                if locked_skill_ids:
                    lock_is_explicit = True
            elif isinstance(session.metadata.get("forced_skill_id"), str):
                legacy_forced = str(session.metadata.get("forced_skill_id", "")).strip().lower()
                if legacy_forced:
                    locked_skill_ids = [legacy_forced]
                    lock_is_explicit = True
                    session.metadata["locked_skill_ids"] = locked_skill_ids
                    session.metadata.pop("forced_skill_id", None)
                    metadata_dirty = True
            raw_waiting = session.metadata.get("skill_lock_waiting_done")
            lock_waiting_done = bool(raw_waiting)
            skill_announce_pending = bool(session.metadata.get("skill_lock_announce_pending"))
            # Clean up legacy pending-switch keys from old sessions.
            if "pending_skill_switch_ids" in session.metadata or "pending_skill_switch_message" in session.metadata:
                session.metadata.pop("pending_skill_switch_ids", None)
                session.metadata.pop("pending_skill_switch_message", None)
                metadata_dirty = True
        previous_locked_skill_ids = list(locked_skill_ids)

        # Short-circuit: user is asking about the current skill lock state.
        # Return directly without consuming LLM tokens.
        if lock_is_explicit and locked_skill_ids and _is_skill_lock_status_question(message):
            locked_names = "、".join(locked_skill_ids)
            return (
                f"当前会话已锁定技能：{locked_names}。\n"
                "继续说需求即可推进任务；回复\u201c任务完成\u201d可解除锁定。"
            )

        if lock_is_explicit and locked_skill_ids:
            _locked_skills_for_hooks = _assembler.route_skills(
                message, forced_skill_ids=locked_skill_ids
            )
            for _lsk in _locked_skills_for_hooks:
                _lsk_hooks = _get_skill_hooks(_lsk)
                if _lsk_hooks is not None and _lsk_hooks.is_activation_message(message):
                    reply = _lsk_hooks.build_already_locked_reply(session)
                    if reply:
                        return reply
            if (
                "nano-banana-image-t8" in locked_skill_ids
                and _is_nano_banana_activation_message(message)
            ):
                model_display = _load_saved_nano_banana_model_display()
                if session is not None:
                    state_map_raw = session.metadata.get("skill_param_state", {})
                    if isinstance(state_map_raw, dict):
                        nano_state = state_map_raw.get("nano-banana-image-t8")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
                        if isinstance(nano_state, dict):
                            model_display = str(
                                nano_state.get("__model_display__", model_display)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                            ).strip() or model_display
                return (
                    "当前会话仍在香蕉生图技能里。"
                    f"当前模型：{model_display}。\n"
                    "如果要继续生图，请直接发送提示词或图片；"
                    '如果本轮已结束，请回复"任务完成"解除技能锁定。'
                )

        use_cmd = _parse_use_command(message, use_cmd_re=_USE_CMD_RE)
        if use_cmd is not None:
            use_skill_ids, remainder = use_cmd
            if len(use_skill_ids) == 1 and use_skill_ids[0] in _USE_CLEAR_IDS:
                locked_skill_ids = []
                lock_is_explicit = False
                pending_lock_skill_ids = []
                lock_waiting_done = False
                skill_announce_pending = False
                if session is not None:
                    session.metadata.pop("locked_skill_ids", None)
                    session.metadata.pop("skill_lock_waiting_done", None)
                    session.metadata.pop("skill_lock_announce_pending", None)
                    session.metadata.pop("pending_skill_switch_ids", None)
                    session.metadata.pop("pending_skill_switch_message", None)
                    session.metadata.pop("skill_param_state", None)
                    session.metadata.pop("__xhs_activation_msg_idx__", None)
                    metadata_dirty = True
                if remainder:
                    llm_message = remainder
            else:
                locked_skill_ids = use_skill_ids
                lock_is_explicit = True
                lock_waiting_done = False
                skill_announce_pending = True
                if session is not None:
                    session.metadata["locked_skill_ids"] = locked_skill_ids
                    session.metadata["skill_lock_waiting_done"] = False
                    session.metadata["skill_lock_announce_pending"] = True
                    session.metadata["__xhs_activation_msg_idx__"] = len(session.messages)
                    metadata_dirty = True
                llm_message = remainder or f"使用技能 {', '.join(locked_skill_ids)} 处理当前请求。"
        elif lock_is_explicit and locked_skill_ids and _is_task_done_confirmation(
            message,
            task_done_patterns=_TASK_DONE_PATTERNS,
        ):
            locked_skill_ids = []
            lock_is_explicit = False
            lock_waiting_done = False
            if session is not None:
                session.metadata.pop("locked_skill_ids", None)
                session.metadata.pop("skill_lock_waiting_done", None)
                session.metadata.pop("skill_lock_announce_pending", None)
                session.metadata.pop("pending_skill_switch_ids", None)
                session.metadata.pop("pending_skill_switch_message", None)
                session.metadata.pop("skill_param_state", None)
                session.metadata.pop("__xhs_activation_msg_idx__", None)
                if session_manager is not None:
                    await session_manager._store.update_session_field(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                        session.id,
                        metadata=session.metadata,
                    )
            if session_store and router and summarizer_cfg.enabled:
                _fire_bg_compress(
                    session_id=session_id,
                    session_store=session_store,
                    router=router,
                    summarizer_model=summarizer_cfg.model,
                )
                if group_compressor is not None:
                    _fire_bg_group_prewarm(
                        session_id=session_id,
                        session_store=session_store,
                        group_compressor=group_compressor,
                        router=router,
                        summarizer_model=summarizer_cfg.model,
                    )
            return "已确认任务完成，已解除本轮技能锁定。"
        elif lock_waiting_done:
            lock_waiting_done = False
            if session is not None:
                session.metadata["skill_lock_waiting_done"] = False
                metadata_dirty = True

        routed_skills = _assembler.route_skills(llm_message)
        routed_skill_ids = _normalize_skill_ids(routed_skills)

        # Bug 5: deferred task intent → clear skill routing to prevent lock activation
        is_deferred = _is_deferred_task_intent(llm_message) and not lock_is_explicit
        if is_deferred:
            routed_skills = []
            routed_skill_ids = []
            log.info(
                "agent.deferred_intent_clear_skills",
                session_id=session_id,
                message_preview=llm_message[:80],
            )

        # Only lock skills that have lock_session=True
        # Only user-installed skills may lock the session; bundled skills never lock.
        lockable_skills = [s for s in routed_skills if s.lock_session and s.is_user_installed]
        lockable_skill_ids = _normalize_skill_ids(lockable_skills)
        # 复合任务（如"做PPT + 生图 + 插入"）不立即锁定技能，
        # 让 LLM 自主编排多步骤；任务成功后再通过 pending_lock 延迟锁定。
        _is_compound = _is_compound_task_message(message)
        if (
            not locked_skill_ids
            and lockable_skill_ids
            and not _is_compound
            and (
                _looks_like_skill_activation_message(
                    message,
                    skill_activation_patterns=_SKILL_ACTIVATION_PATTERNS,
                )
                or any(_skill_trigger_mentioned(skill, message) for skill in lockable_skills)
            )
        ):
            locked_skill_ids = lockable_skill_ids
            lock_is_explicit = True
            lock_waiting_done = False
            skill_announce_pending = True
            if session is not None:
                session.metadata["locked_skill_ids"] = locked_skill_ids
                session.metadata["skill_lock_waiting_done"] = False
                session.metadata["skill_lock_announce_pending"] = True
                session.metadata["__xhs_activation_msg_idx__"] = len(session.messages)
                metadata_dirty = True
        elif not locked_skill_ids and lockable_skill_ids:
            pending_lock_skill_ids = lockable_skill_ids

        # When the session is locked on a skill, handle routing to a different skill.
        # Only prompt the user if:
        #   1. The lock was already established BEFORE this message
        #      (locked_skill_ids == previous_locked_skill_ids), so that a /use cmd
        #      that just set a new lock this turn is never intercepted by its own
        #      remainder task.
        #   2. The message explicitly names a *user-installed* skill (not a bundled
        #      skill whose name may partially match everyday words).
        # Otherwise silently stay in the locked skill context so ambiguous wording
        # (e.g. "图片" triggering search_images while in banana) does not interrupt.
        if (
            lock_is_explicit
            and locked_skill_ids
            and locked_skill_ids == previous_locked_skill_ids  # lock was pre-existing
            and routed_skill_ids
            and routed_skill_ids != locked_skill_ids
        ):
            explicitly_other = any(
                _skill_explicitly_mentioned(skill, message)
                for skill in routed_skills
                if skill.is_user_installed  # only user-installed skills should block
            )
            if explicitly_other:
                locked_names = "、".join(locked_skill_ids)
                return (
                    f"当前会话正在使用 {locked_names} 技能，任务尚未结束。\n"
                    "如需切换到其他任务，请先回复\u201c任务完成\u201d以解除技能锁定。"
                )
            # Silently discard the off-topic routing; keep executing in locked skill.
            # Bug 6: also clear lockable ids to prevent residual values from
            # interfering with the existing lock state.
            routed_skill_ids = []
            routed_skills = []
            lockable_skill_ids = []

        active_skills_for_images = routed_skills
        if lock_is_explicit and locked_skill_ids:
            active_skills_for_images = _assembler.route_skills(
                llm_message, forced_skill_ids=locked_skill_ids
            )
        if not images:
            recovered_images: list[ImageContent] = []
            reuse_reason = ""
            skill_needs_images = _skill_requires_images(active_skills_for_images)
            if _message_requests_image_regenerate(llm_message):
                if (
                    "nano-banana-image-t8" in locked_skill_ids
                    and _recover_last_nano_banana_mode(session) == "text"
                ):
                    recovered_images = []
                else:
                    recovered_images = _recover_last_input_images(session)
                    reuse_reason = "last_input_images"
            elif _message_requests_image_edit(llm_message) and (
                _message_may_need_prior_images(llm_message) or skill_needs_images
            ):
                recovered_images = _recover_latest_generated_image(session)
                reuse_reason = "latest_generated_image"
                if not recovered_images:
                    recovered_images = _recover_last_input_images(session)
                    reuse_reason = "last_input_images_fallback"
            if recovered_images:
                images = recovered_images
                log.info(
                    "agent.reused_recent_images",
                    session_id=session_id,
                    count=len(recovered_images),
                    reason=reuse_reason,
                )
        elif session is not None:
            current_input_paths = _extract_input_image_paths_from_text(llm_message)
            if current_input_paths:
                session.metadata["last_input_image_paths"] = current_input_paths
                metadata_dirty = True

        if lock_is_explicit and locked_skill_ids and not lock_waiting_done and session is not None:
            locked_skills = _assembler.route_skills(llm_message, forced_skill_ids=locked_skill_ids)
            guards = _guarded_skills(locked_skills)
            if guards:
                state_map_raw = session.metadata.get("skill_param_state")
                state_map: dict[str, dict[str, object]] = {}
                if isinstance(state_map_raw, dict):
                    raw_state_map = cast(dict[object, object], state_map_raw)
                    for key, value in raw_state_map.items():
                        if not isinstance(key, str) or not isinstance(value, dict):
                            continue
                        normalized_value: dict[str, object] = {
                            str(k): v for k, v in value.items() if isinstance(k, str)  # pyright: ignore[reportUnknownVariableType]
                        }
                        state_map[key] = normalized_value
                missing_any = False
                for skill in guards:
                    guard = skill.param_guard
                    if guard is None:
                        continue
                    skill_state_raw = state_map.get(skill.id, {})
                    skill_state: dict[str, object] = (
                        skill_state_raw.copy()  # pyright: ignore[reportUnknownMemberType]
                        if isinstance(skill_state_raw, dict)  # pyright: ignore[reportUnnecessaryIsInstance]
                        else {}
                    )
                    _skill_hooks = _get_skill_hooks(skill)
                    if _skill_hooks is not None:
                        hooks_updated = _skill_hooks.update_guard_state(
                            skill_state, llm_message, images,
                            params=guard.params, session=session,
                            has_new_input_images=bool(images),
                        )
                        if hooks_updated is not None:
                            updated = hooks_updated
                            control_msg = _skill_hooks.is_control_message(llm_message)
                            missing = _skill_hooks.missing_required(
                                updated, control_message_only=control_msg
                            )
                        else:
                            updated, missing = _update_guard_state(
                                guard.params, skill_state, llm_message, images
                            )
                            control_msg = _skill_hooks.is_control_message(llm_message)
                            missing = _skill_hooks.missing_required(
                                updated, control_message_only=control_msg
                            )
                    elif skill.id == "nano-banana-image-t8":
                        updated, missing = _update_guard_state(
                            guard.params, skill_state, llm_message, images
                        )
                        previous_prompt = str(skill_state.get("prompt", "")).strip()
                        control_message_only = _is_nano_banana_control_message(llm_message)
                        if control_message_only and "prompt" in updated:
                            updated["prompt"] = skill_state.get("prompt")
                        else:
                            _is_text_to_image = any(
                                p.search(llm_message)
                                for p in _NANO_BANANA_TEXT_TO_IMAGE_PATTERNS
                            )
                            merged_prompt = _merge_nano_banana_prompt(
                                previous_prompt=previous_prompt,
                                message=llm_message,
                                regenerate=(
                                    False
                                    if _is_text_to_image
                                    else _message_requests_image_regenerate(llm_message)
                                ),
                                image_edit=(
                                    False
                                    if _is_text_to_image
                                    else _message_requests_image_edit(llm_message)
                                ),
                            )
                            if merged_prompt:
                                updated["prompt"] = merged_prompt
                        previous_model = str(
                            skill_state.get(
                                "__model_display__",
                                _load_saved_nano_banana_model_display(),
                            )
                        )
                        updated["__model_display__"] = _detect_nano_banana_model_display(
                            llm_message,
                            previous=previous_model,
                        )
                        missing = _nano_banana_missing_required(
                            updated,
                            control_message_only=control_message_only,
                        )
                    else:
                        updated, missing = _update_guard_state(
                            guard.params, skill_state, llm_message, images
                        )
                    state_map[skill.id] = updated
                    missing_any = missing_any or missing
                session.metadata["skill_param_state"] = state_map
                metadata_dirty = True
                if missing_any:
                    if session_manager is not None:
                        await session_manager._store.update_session_field(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                            session.id,
                            metadata=session.metadata,
                        )
                    blocks = [
                        _build_skill_param_guard_reply(
                            s.id, s.param_guard.params, state_map.get(s.id, {}),  # pyright: ignore[reportUnknownArgumentType]
                            skill=s,
                        )
                        for s in guards
                        if s.param_guard is not None
                    ]
                    if session_store and router and summarizer_cfg.enabled:
                        _fire_bg_compress(
                            session_id=session_id,
                            session_store=session_store,
                            router=router,
                            summarizer_model=summarizer_cfg.model,
                        )
                        if group_compressor is not None:
                            _fire_bg_group_prewarm(
                                session_id=session_id,
                                session_store=session_store,
                                group_compressor=group_compressor,
                                router=router,
                                summarizer_model=summarizer_cfg.model,
                            )
                    return "\n\n".join(blocks)
                # nano-banana 技能：记录本轮 mode/input_paths 到 session 供后续使用，
                # 但不走固定执行路径——让 LLM 通过 system_messages 中注入的推荐命令
                # 自主决定何时调用 bash，从而支持复合任务编排。
                if (
                    "nano-banana-image-t8" in locked_skill_ids
                    and not _is_clearly_unrelated_to_image(llm_message)
                ):
                    nano_state = state_map.get("nano-banana-image-t8", {})
                    input_paths = _resolve_nano_banana_input_paths(llm_message, session)
                    mode = "edit" if input_paths else "text"
                    if input_paths:
                        session.metadata["last_input_image_paths"] = input_paths
                    session.metadata["last_nano_banana_mode"] = mode
                    if isinstance(nano_state, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
                        nano_state["__last_mode__"] = mode

    if session is not None:
        pending_raw = session.metadata.get("evomap_pending_choices")
        if isinstance(pending_raw, dict):
            options_raw: object = pending_raw.get("options")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            options: list[dict[str, str]] = []
            if isinstance(options_raw, list):
                for item in options_raw:  # pyright: ignore[reportUnknownVariableType]
                    if not isinstance(item, dict):
                        continue
                    opt: dict[str, object] = item  # pyright: ignore[reportAssignmentType, reportUnknownVariableType]
                    aid = str(opt.get("asset_id", "")).strip()
                    summary = str(opt.get("summary", "")).strip()
                    if not summary and not aid:
                        continue
                    options.append({"asset_id": aid, "summary": summary})
            choice_idx = _extract_evomap_choice_index(message, len(options))
            if choice_idx is not None and options:
                selected = options[choice_idx]
                selected_hint = (
                    f"【EvoMap 已选方案】\n- {selected['asset_id']}: {selected['summary']}"
                )
                extra_memory = (
                    f"{selected_hint}\n{extra_memory}" if extra_memory.strip() else selected_hint
                )
                origin_message = str(pending_raw.get("origin_message", "")).strip()  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                if origin_message:
                    llm_message = (
                        f"{origin_message}\n"
                        f"用户已选择方案：{selected['summary']}。\n"
                        "请按该方案执行。"
                    )
                session.metadata.pop("evomap_pending_choices", None)
                if session_manager is not None:
                    await session_manager._store.update_session_field(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                        session.id,
                        metadata=session.metadata,
                    )

    assistant_name = _DEFAULT_ASSISTANT_NAME
    if memory_manager is not None:
        try:
            current_name = await memory_manager.get_assistant_name()
            if current_name:
                assistant_name = current_name
        except Exception as exc:
            log.debug("agent.assistant_name_load_failed", session_id=session_id, error=str(exc))
    name_action, requested_name = _detect_assistant_name_update(
        message,
        reset_patterns=_ASSISTANT_NAME_RESET_PATTERNS,
        set_patterns=_ASSISTANT_NAME_SET_PATTERNS,
    )
    if name_action == "set":
        assistant_name = requested_name
        if memory_manager is not None:
            try:
                changed = await memory_manager.set_assistant_name(
                    requested_name,
                    source=f"session:{session_id}",
                )
                if changed:
                    log.info(
                        "agent.assistant_name_updated",
                        session_id=session_id,
                        assistant_name=requested_name,
                    )
            except Exception as exc:
                log.debug(
                    "agent.assistant_name_save_failed",
                    session_id=session_id,
                    error=str(exc),
                )
    elif name_action == "reset":
        assistant_name = _DEFAULT_ASSISTANT_NAME
        if memory_manager is not None:
            try:
                removed = await memory_manager.clear_assistant_name()
                log.info(
                    "agent.assistant_name_reset",
                    session_id=session_id,
                    removed=removed,
                )
            except Exception as exc:
                log.debug(
                    "agent.assistant_name_reset_failed",
                    session_id=session_id,
                    error=str(exc),
                )

    native_tools = router.supports_native_tools(model_id)
    selected_tool_names: set[str] | None = None
    evomap_enabled = _is_evomap_enabled(config) and not multi_agent_internal
    evomap_phase = "editing"
    if session is not None:
        raw_phase = session.metadata.get("evomap_phase")
        if isinstance(raw_phase, str) and raw_phase.strip():
            evomap_phase = raw_phase.strip().lower()
    if _is_tasky_message_for_evomap(llm_message):
        # First-turn task requests are treated as NEW_TASK directly to avoid
        # an extra classifier LLM call before normal planning/execution.
        if session is None or not session.messages:
            phase = "NEW_TASK"
        else:
            phase = await _llm_judge_task_phase(
                router,
                model_id,
                session=session,
                message=llm_message,
            )
    else:
        phase = "EDITING"
    if session is not None and session_manager is not None:
        desired = "start" if phase == "NEW_TASK" else "editing"
        if desired != evomap_phase:
            session.metadata["evomap_phase"] = desired
            await session_manager._store.update_session_field(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                session.id,
                metadata=session.metadata,
            )
    evomap_allowed_for_turn = evomap_enabled and phase == "NEW_TASK"
    available_tool_names = {d.name for d in registry.list_tools()}
    evo_first_mode = evomap_allowed_for_turn and "evomap_fetch" in available_tool_names
    evomap_hint_hit = _extra_memory_has_evomap_hint(extra_memory)

    if native_tools:
        selected_tool_names = _select_native_tool_names(registry, llm_message)
        selected_tool_names = _force_include_office_edit_tools(
            selected_tool_names,
            available=available_tool_names,
            session=session,
            llm_message=llm_message,
        )
        active_skill_ids = set(locked_skill_ids or pending_lock_skill_ids or [])
        _active_skills_for_tools = (
            _assembler.route_skills(llm_message, forced_skill_ids=list(active_skill_ids))
            if active_skill_ids
            else []
        )
        for _ast in _active_skills_for_tools:
            _ast_hooks = _get_skill_hooks(_ast)
            if _ast_hooks is not None:
                excluded = _ast_hooks.excluded_tool_names()
                if excluded:
                    selected_tool_names -= excluded
                extra = _ast_hooks.extra_tool_names()
                for name in extra:
                    if name in available_tool_names:
                        selected_tool_names.add(name)
        if "nano-banana-image-t8" in active_skill_ids and not _active_skills_for_tools:
            selected_tool_names = {
                name
                for name in selected_tool_names
                if name not in {"file_read", "file_write", "file_edit", "patch_apply"}
            }
            if "bash" in available_tool_names:
                selected_tool_names.add("bash")
        if not evomap_allowed_for_turn:
            selected_tool_names = {
                name for name in selected_tool_names if not name.startswith("evomap_")
            }
        elif evo_first_mode:
            selected_tool_names.add("evomap_fetch")
        # Bug 8 / 修改 3: deferred intent → restrict tools to cron/reminder only
        if is_deferred and selected_tool_names is not None:
            selected_tool_names = selected_tool_names & _DEFERRED_ONLY_TOOLS
            if not selected_tool_names:
                selected_tool_names = _DEFERRED_ONLY_TOOLS & available_tool_names
            log.info(
                "agent.deferred_intent_tool_restriction",
                session_id=session_id,
                restricted_to=sorted(selected_tool_names),
            )
        tool_schemas = registry.to_llm_schemas(include_names=selected_tool_names)
        dropped_names = sorted(available_tool_names - selected_tool_names)
        log.info(
            "agent.tools_selected",
            session_id=session_id,
            selected=sorted(selected_tool_names),
            selected_count=len(selected_tool_names),
            dropped_count=len(dropped_names),
            dropped=dropped_names,
        )
    else:
        tool_schemas = None
    fallback_names: set[str] | None = None
    if not native_tools and not evomap_allowed_for_turn:
        fallback_names = {d.name for d in registry.list_tools() if not d.name.startswith("evomap_")}
    # Bug 8 / 修改 3 fallback path: deferred intent → restrict fallback tools too
    if is_deferred and fallback_names is not None:
        fallback_names = fallback_names & _DEFERRED_ONLY_TOOLS
        if not fallback_names:
            fallback_names = _DEFERRED_ONLY_TOOLS & available_tool_names
    fallback_text = (
        "" if native_tools else registry.to_prompt_fallback(include_names=fallback_names)
    )

    effective_skill_ids = locked_skill_ids or pending_lock_skill_ids or None
    _model_short_for_ctx = model_id.split("/", 1)[-1] if "/" in model_id else model_id
    system_messages = _assembler.build(
        config,
        llm_message,
        tool_fallback_text=fallback_text,
        assistant_name=assistant_name,
        forced_skill_ids=effective_skill_ids,
        model_id=model_id,
        max_context_tokens=_context_window.get_max_context(_model_short_for_ctx),
    )
    if lock_is_explicit and locked_skill_ids:
        system_messages.append(_build_skill_lock_system_message(locked_skill_ids))
    # Inject execution system messages via skill hooks
    _hooks_exec_msg_injected = False
    if effective_skill_ids:
        _exec_skills = _assembler.route_skills(
            llm_message, forced_skill_ids=list(effective_skill_ids)
        )
        for _esk in _exec_skills:
            _esk_hooks = _get_skill_hooks(_esk)
            if _esk_hooks is None:
                continue
            _esk_state: dict[str, object] = {}
            if session is not None:
                _esk_state_raw: object = session.metadata.get("skill_param_state", {})
                if isinstance(_esk_state_raw, dict):
                    _esk_state_raw_typed = cast(dict[str, object], _esk_state_raw)
                    _esk_skill_state: object = _esk_state_raw_typed.get(_esk.id)
                    if isinstance(_esk_skill_state, dict):
                        _esk_state = cast(dict[str, object], _esk_skill_state)
            _recommended_cmd = _esk_hooks.build_command(_esk_state, session)
            _exec_msg = _esk_hooks.build_execution_system_message(
                session, recommended_command=_recommended_cmd
            )
            if _exec_msg is not None:
                system_messages.append(_exec_msg)
                _hooks_exec_msg_injected = True
    if not _hooks_exec_msg_injected and effective_skill_ids and "nano-banana-image-t8" in effective_skill_ids:
        skill_state_raw: dict[str, object] | object = (
            session.metadata.get("skill_param_state", {})
            if session is not None
            else {}
        )
        current_model = _load_saved_nano_banana_model_display()
        if isinstance(skill_state_raw, dict):
            nano_state = skill_state_raw.get("nano-banana-image-t8")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            if isinstance(nano_state, dict):
                current_model = str(
                    nano_state.get("__model_display__", current_model)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                ).strip() or current_model
        _nb_prompt = ""
        _nb_ratio = "auto"
        _nb_input_paths: list[str] = []
        if isinstance(skill_state_raw, dict):
            _nb_raw_state = skill_state_raw.get("nano-banana-image-t8")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            if isinstance(_nb_raw_state, dict):
                _nb_prompt = str(_nb_raw_state.get("prompt", "")).strip()  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                _nb_ratio = str(_nb_raw_state.get("ratio", "auto")).strip() or "auto"  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        _nb_mode = _recover_last_nano_banana_mode(session) or "text"
        if _nb_mode == "edit":
            _nb_input_paths = _resolve_nano_banana_input_paths(llm_message, session)
        _nb_recommended_cmd = (
            _build_nano_banana_command(
                mode=_nb_mode,
                model_display=current_model,
                prompt=_nb_prompt or llm_message,
                input_paths=_nb_input_paths,
                ratio=_nb_ratio,
            )
            if _nb_prompt or _nb_mode == "text"
            else ""
        )
        system_messages.append(
            _build_nano_banana_execution_system_message(
                current_model,
                _recover_recent_session_image_paths(session),
                recommended_command=_nb_recommended_cmd,
            )
        )
    _append_office_system_hints(system_messages, session, llm_message)
    # 复合任务：禁用生图探测守卫（避免生图完成后 agent 被提前终止）；注入多步骤编排提示
    _is_msg_compound = _is_compound_task_message(llm_message)
    image_api_probe_guard_enabled = _is_image_generation_request(llm_message) and not _is_msg_compound
    if image_api_probe_guard_enabled:
        system_messages.append(_build_image_generation_system_message())
    if _is_msg_compound:
        system_messages.append(_build_compound_task_system_message())
    if evo_first_mode:
        system_messages.append(_build_evomap_first_system_message())
        if session is not None and session_manager is not None:
            session.metadata["evomap_phase"] = "editing"
            await session_manager._store.update_session_field(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                session.id,
                metadata=session.metadata,
            )
    import time as _time

    _t_pre_llm_start = _time.monotonic()
    _memory_stage_ms = 0
    _extra_memory_stage_ms = 0
    _group_compress_stage_ms = 0
    if memory_manager is not None and agent_cfg.memory.enabled:
        _t_memory_start = _time.monotonic()
        memory_cfg = agent_cfg.memory
        style_directive = ""
        try:
            style_directive = (
                await memory_manager.get_global_style_directive()
                if memory_cfg.global_style_enabled
                else ""
            )
            if style_directive:
                system_messages.append(_build_global_style_system_message(style_directive))
                log.info(
                    "agent.memory_style_applied",
                    session_id=session_id,
                    chars=len(style_directive),
                )
        except Exception as exc:
            log.debug("agent.memory_style_load_failed", error=str(exc), session_id=session_id)
        try:
            should_recall, include_raw = memory_manager.recall_policy(llm_message)
            if not should_recall and _is_creation_task_message(llm_message):
                should_recall = True
                include_raw = False
            recalled = ""
            if should_recall:
                profile_block = await memory_manager.build_profile_for_injection(
                    max_tokens=memory_cfg.recall_profile_max_tokens,
                    router=router,
                    model_id=memory_cfg.organizer_model,
                    exclude_style=bool(style_directive.strip()),
                )
                raw_block = ""
                if include_raw:
                    raw_block = await memory_manager.recall(
                        llm_message,
                        max_tokens=memory_cfg.recall_raw_max_tokens,
                        limit=memory_cfg.recall_limit,
                        include_profile=False,
                        include_raw=True,
                    )
                recalled = _merge_recall_blocks(profile_block, raw_block)
            if recalled.strip():
                system_messages.append(_build_memory_system_message(recalled))
                log.info("agent.memory_recalled", session_id=session_id, chars=len(recalled))
        except Exception as exc:
            log.debug("agent.memory_recall_failed", error=str(exc), session_id=session_id)
        _memory_stage_ms = int((_time.monotonic() - _t_memory_start) * 1000)
    if extra_memory.strip():
        _t_extra_start = _time.monotonic()
        normalized_extra = extra_memory.strip()
        compress_model = summarizer_cfg.model.strip()
        can_compress = bool(compress_model)
        should_compress_extra = _est_tokens(normalized_extra) > _EVOMAP_MAX_TOKENS
        if can_compress and should_compress_extra:
            try:
                router.resolve(compress_model)
                compressed = await asyncio.wait_for(
                    _compress_external_memory_with_llm(
                        router=router,
                        model_id=compress_model,
                        text=normalized_extra,
                        max_tokens=_EVOMAP_MAX_TOKENS,
                    ),
                    timeout=_EXTRA_MEMORY_COMPRESS_TIMEOUT_SECONDS,
                )
                if compressed:
                    normalized_extra = compressed
                else:
                    normalized_extra = _truncate_to_tokens(
                        normalized_extra,
                        _EVOMAP_MAX_TOKENS,
                    )
            except Exception:
                normalized_extra = _truncate_to_tokens(
                    normalized_extra,
                    _EVOMAP_MAX_TOKENS,
                )
        else:
            normalized_extra = _truncate_to_tokens(
                normalized_extra,
                _EVOMAP_MAX_TOKENS,
            )
        system_messages.append(_build_external_memory_system_message(normalized_extra))
        _extra_memory_stage_ms = int((_time.monotonic() - _t_extra_start) * 1000)

    conversation: list[Message] = []
    if session:
        conversation = list(session.messages)
    conversation.append(Message(role="user", content=llm_message, images=images))
    conversation_message_count = len(conversation)

    if (
        group_compressor is not None
        and session_store is not None
        and session is not None
        and summarizer_cfg.model.strip()
    ):
        _t_group_start = _time.monotonic()
        try:
            conversation_for_compress = conversation
            if isinstance(session.metadata, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
                raw_cutoff = session.metadata.get("compression_resume_message_index")
                if isinstance(raw_cutoff, int) and raw_cutoff > 0:
                    cutoff = max(0, min(raw_cutoff, len(conversation) - 1))
                    if cutoff > 0:
                        conversation_for_compress = conversation[cutoff:]
            conversation = await group_compressor.build_window_messages(
                session_id=session_id,
                messages=conversation_for_compress,
                router=router,
                model_id=summarizer_cfg.model.strip(),
            )
        except Exception as exc:
            log.debug("agent.group_compress_failed", session_id=session_id, error=str(exc))
        _group_compress_stage_ms = int((_time.monotonic() - _t_group_start) * 1000)

    _pre_llm_elapsed_ms = int((_time.monotonic() - _t_pre_llm_start) * 1000)
    log.debug(
        "agent.pre_llm_stages",
        session_id=session_id,
        memory_ms=_memory_stage_ms,
        extra_memory_ms=_extra_memory_stage_ms,
        group_compress_ms=_group_compress_stage_ms,
        total_ms=_pre_llm_elapsed_ms,
    )

    model_short: str = model_id.split("/", 1)[-1] if "/" in model_id else model_id
    trigger_preview = trigger_text_preview.strip() or _preview_text(llm_message)

    log.info(
        "agent.run",
        model=model_id,
        session_id=session_id,
        native_tools=native_tools,
        history_messages=len(conversation),
        trigger_event_id=trigger_event_id,
        trigger_preview=trigger_preview,
    )

    final_text_parts: list[str] = []
    real_image_paths: list[str] = []
    _cron_reminder_notices: list[str] = []
    total_input = 0
    total_output = 0
    announced_plan = False
    db_summaries: list[SummaryRow] = []
    pending_office_paths: list[str] = []
    if session_store and summarizer_cfg.enabled:
        try:
            db_summaries = await session_store.get_summaries(session_id)  # pyright: ignore[reportUnknownMemberType]
        except Exception as exc:
            log.debug("agent.summaries_load_failed", error=str(exc))

    _cdp_url = ""
    _browser_cfg = (config.plugins or {}).get("browser")
    if isinstance(_browser_cfg, dict):
        _cdp_url = str(_browser_cfg.get("cdp_url", "")).strip()
    guard_state = ToolGuardState(cdp_mode=bool(_cdp_url))
    invalid_tool_rounds = 0
    empty_reply_rounds = 0
    office_block_bash_probe = False
    office_loop_guard_enabled = False
    office_block_message = ""
    office_edit_only = False
    office_edit_path = ""
    if session is not None:
        is_office_request = _is_office_edit_request(llm_message) or (
            _is_followup_edit_message(llm_message) and _has_any_last_office_path(session.metadata)
        )
        office_loop_guard_enabled = is_office_request
        if is_office_request and _has_any_last_office_path(session.metadata):
            office_block_bash_probe = True
            office_block_message = _build_office_path_block_message(session.metadata)
            last_pptx = session.metadata.get("last_pptx_path")
            if isinstance(last_pptx, str) and last_pptx.strip():
                office_edit_only = True
                office_edit_path = last_pptx.strip()
    max_tool_rounds = max(1, int(agent_cfg.max_tool_rounds))
    if multi_agent_internal:
        # Multi-agent sub-calls (role opinions / coordinator synthesis) should
        # use a tighter tool-round budget to prevent runaway loops.  Role calls
        # rarely need tools at all; coordinators need some but 25 is plenty.
        max_tool_rounds = min(max_tool_rounds, 25)

    # Set active skill hooks for tool execution context (bash timeout, etc.)
    _active_hooks_for_exec = None
    if effective_skill_ids:
        _hooks_skills = _assembler.route_skills(
            llm_message, forced_skill_ids=list(effective_skill_ids)
        )
        for _hs in _hooks_skills:
            _hs_hooks = _get_skill_hooks(_hs)
            if _hs_hooks is not None:
                _active_hooks_for_exec = _hs_hooks
                break
    _set_active_skill_hooks(_active_hooks_for_exec)

    round_idx = -1
    successful_tool_calls = 0
    browser_locked_by_evomap = evo_first_mode and not evomap_hint_hit
    while total_output < _MAX_OUTPUT_TOKENS:
        round_idx += 1
        if round_idx >= max_tool_rounds:
            final_text_parts.append(
                f"工具调用轮次已达上限（{max_tool_rounds} 轮），为避免长时间卡住已暂停。"
            )
            break
        _trim_result = _context_window.trim_with_metadata(
            [*system_messages, *conversation],
            model_short,
            summaries=db_summaries or None,  # pyright: ignore[reportArgumentType]
        )
        all_messages = _trim_result.messages
        if round_idx == 0 and _trim_result.was_trimmed:
            _trunc_note = (
                f"【上下文截断提示】本会话共 {_trim_result.original_count} 条消息，"
                f"因上下文预算限制已省略 {_trim_result.dropped_count} 条较早的消息。\n"
                "如需引用早期内容，请让用户重新提供关键信息。"
            )
            all_messages.append(Message(role="system", content=_trunc_note))

        _llm_t0 = _time.monotonic()
        # LLM 调用层重试：最大重试 2 次（共 3 次尝试），退避 1.5×重试次数 秒；认证/限流不重试
        _llm_retry_delays = (1.5, 3.0)  # 1.5*1s, 1.5*2s
        response: AgentResponse | None = None
        for _llm_attempt in range(len(_llm_retry_delays) + 1):
            try:
                response = await router.chat(
                    model_id,
                    all_messages,
                    tools=tool_schemas or None,
                    on_stream=on_stream,
                )
                break
            except (ProviderAuthError, ProviderRateLimitError):
                raise
            except ProviderError as _llm_exc:
                if _llm_attempt < len(_llm_retry_delays):
                    _delay = _llm_retry_delays[_llm_attempt]
                    log.warning(
                        "agent.llm_call_retry",
                        attempt=_llm_attempt + 1,
                        delay_s=_delay,
                        error=str(_llm_exc),
                        session_id=session_id,
                    )
                    await asyncio.sleep(_delay)
                elif successful_tool_calls > 0 and final_text_parts:
                    log.warning(
                        "agent.llm_exhausted_with_partial_progress",
                        rounds=round_idx + 1,
                        successful_tool_calls=successful_tool_calls,
                        error=str(_llm_exc),
                        session_id=session_id,
                    )
                    final_text_parts.append(
                        "\n\n（模型调用中断，以上是已完成的部分结果。）"
                    )
                    break
                else:
                    raise
        if response is None and successful_tool_calls > 0 and final_text_parts:
            break
        assert response is not None  # noqa: S101  # pyright: ignore[reportAssertAlwaysTrue]
        _llm_ms = int((_time.monotonic() - _llm_t0) * 1000)
        round_input = response.input_tokens
        round_output = response.output_tokens
        total_input += round_input
        total_output += round_output
        log.info(
            "agent.llm_call",
            round=round_idx,
            elapsed_ms=_llm_ms,
            model=model_id,
            round_input_tokens=round_input,
            round_output_tokens=round_output,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            trigger_event_id=trigger_event_id,
            trigger_preview=trigger_preview,
            session_id=session_id,
        )

        tool_calls = response.tool_calls
        if not tool_calls and not native_tools and response.content:
            tool_calls = _parse_fallback_tool_calls(response.content)
        if tool_calls:
            valid_tool_calls = [tc for tc in tool_calls if tc.name.strip()]
            dropped = len(tool_calls) - len(valid_tool_calls)
            if dropped > 0:
                if valid_tool_calls:
                    log.debug(
                        "agent.invalid_tool_calls_dropped",
                        dropped=dropped,
                        kept=len(valid_tool_calls),
                        session_id=session_id,
                    )
                else:
                    log.warning(
                        "agent.invalid_tool_calls_dropped",
                        dropped=dropped,
                        kept=0,
                        session_id=session_id,
                    )
            repaired_calls: list[ToolCall] = []
            for tc in valid_tool_calls:
                repaired, reason = _repair_tool_call(tc, message)
                if reason:
                    log.info(
                        "agent.tool_call_repaired",
                        tool=tc.name,
                        reason=reason,
                        before=str(tc.arguments)[:200],
                        after=str(repaired.arguments)[:200],
                        session_id=session_id,
                    )
                repaired_calls.append(repaired)
            tool_calls = repaired_calls
            blocked_reasons = blocked_tool_reasons(tool_calls, guard_state)
            if blocked_reasons:
                invalid_tool_rounds += 1
                log.warning(
                    "agent.blocked_tool_calls_dropped",
                    reasons=blocked_reasons,
                    round=round_idx,
                    session_id=session_id,
                )
                conversation.append(
                    Message(
                        role="user",
                        content=(
                            "[系统提示] 该工具已被熔断："
                            f"{'; '.join(blocked_reasons)}。"
                            "请改用其他可用工具完成任务。"
                        ),
                    )
                )
                if invalid_tool_rounds >= MODEL_REPAIR_RETRY_LIMIT:
                    final_text_parts.append("工具调用连续无效，已停止自动重试。请明确参数后重试。")
                    break
                continue
            invalid_reasons = [
                reason
                for tc in tool_calls
                if (reason := _validate_tool_call_args(tc, registry)) is not None
            ]
            if invalid_reasons:
                invalid_tool_rounds += 1
                log.warning(
                    "agent.invalid_tool_call_args",
                    reasons=invalid_reasons,
                    round=round_idx,
                    session_id=session_id,
                )
                conversation.append(
                    Message(
                        role="user",
                        content=(
                            "[系统提示] 你上一轮的工具调用参数无效："
                            f"{'; '.join(invalid_reasons)}。"
                            "请只重发一个有效的工具调用，必填参数必须完整且非空。"
                        ),
                    )
                )
                if invalid_tool_rounds >= MODEL_REPAIR_RETRY_LIMIT:
                    final_text_parts.append(
                        "工具调用参数连续无效，已停止自动重试。请明确参数后重试。"
                    )
                    break
                continue
            invalid_tool_rounds = 0

        content = response.content or ""
        if content:
            if guard_state.planned_image_count is None:
                update_planned_image_count(guard_state, content)
                if guard_state.planned_image_count is not None:
                    log.info(
                        "agent.planned_image_count_detected",
                        session_id=session_id,
                        planned_image_count=guard_state.planned_image_count,
                        search_images_limit=guard_state.search_images_limit,
                    )
            empty_reply_rounds = 0
            if tool_calls and not native_tools:
                clean = _strip_tool_json(content)
                if clean:
                    final_text_parts.append(clean)
            else:
                final_text_parts.append(content)

        if not tool_calls and not content.strip():
            empty_reply_rounds += 1
            log.warning(
                "agent.empty_response",
                session_id=session_id,
                round=round_idx,
                model=model_id,
                empty_reply_rounds=empty_reply_rounds,
            )
            if empty_reply_rounds == 1:
                conversation.append(
                    Message(
                        role="user",
                        content=(
                            "[系统提示] 你上一轮没有输出任何内容。"
                            "请直接给出简短回复；如果需求不明确，请先问用户要做什么。"
                        ),
                    )
                )
                continue
            final_text_parts.append("我这边没收到模型有效回复。请再发一次需求，我会继续处理。")
            break

        if not tool_calls:
            break

        if not announced_plan and on_stream:
            announced_plan = True
            has_text = content.strip() if content else ""
            if not has_text:
                tool_names = [tc.name for tc in tool_calls]
                plan = _make_plan_hint(tool_names, llm_message)
                await on_stream(plan)

        log.info(
            "agent.tool_calls",
            round=round_idx,
            count=len(tool_calls),
            tools=[tc.name for tc in tool_calls],
        )

        # Pre-flight: run evomap_fetch first; if it fails, silently remove it
        # so the LLM never sees the failed call — it just proceeds normally.
        _evomap_preflight_results: dict[str, tuple[str, ToolResult]] = {}
        _evomap_failed_ids: set[str] = set()
        for _pf_tc in tool_calls:
            if _pf_tc.name != "evomap_fetch":
                continue
            _pf_id, _pf_result = await _execute_tool(
                registry,
                _pf_tc,
                evomap_enabled=evomap_allowed_for_turn,
                browser_allowed=True,
                office_block_bash_probe=False,
                office_block_message="",
                office_edit_only=False,
                office_edit_path="",
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
            )
            if not _pf_result.success:
                log.info(
                    "agent.evomap_fetch_failed_fallback",
                    session_id=session_id,
                    error=_pf_result.error or _pf_result.output,
                )
                _evomap_failed_ids.add(_pf_tc.id)
            else:
                _evomap_preflight_results[_pf_tc.id] = (_pf_id, _pf_result)

        if _evomap_failed_ids:
            tool_calls = [tc for tc in tool_calls if tc.id not in _evomap_failed_ids]

        if not tool_calls:
            break

        tool_names_str = ", ".join(tc.name for tc in tool_calls)
        assistant_content = response.content or ""
        assistant_persist = (
            assistant_content
            if assistant_content
            else f"(调用工具: {tool_names_str}) {assistant_content}".strip()
        )
        assistant_msg = Message(
            role="assistant",
            content=assistant_content,
            tool_calls=tool_calls if native_tools else None,
        )
        conversation.append(assistant_msg)

        if session_manager and session:
            # Persist tool_calls in metadata so they survive DB reload.
            persist_tcs = (
                [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls]
                if native_tools and tool_calls
                else None
            )
            await _persist_message(
                session_manager,
                session,
                "assistant",
                assistant_persist,
                tool_calls=persist_tcs,
            )
            if assistant_content:
                for office_path in _extract_office_paths(assistant_content):
                    if _remember_office_path(session.metadata, office_path):
                        metadata_dirty = True

        for _tname in ("reminder", "cron"):
            _tool = registry.get(_tname)
            if _tool is not None and hasattr(_tool, "current_session_id"):
                _tool.current_session_id = session_id  # type: ignore[union-attr]

        stop_for_evomap_choice = False
        stop_for_probe_loop = False
        stop_for_nano_fail = False
        def _prepare_tool_call(tc: ToolCall) -> ToolCall:
            if tc.name == "file_write" and isinstance(tc.arguments.get("content"), str):
                for office_path in _extract_office_paths(str(tc.arguments.get("content", ""))):
                    if office_path not in pending_office_paths:
                        pending_office_paths.append(office_path)
            if session is not None and tc.name in {"ppt_edit", "docx_edit", "xlsx_edit"}:
                has_path = isinstance(tc.arguments.get("path"), str) and bool(
                    str(tc.arguments.get("path", "")).strip()
                )
                if not has_path:
                    default_path = _get_default_office_edit_path(tc.name, session.metadata)
                    if default_path:
                        fixed_args = dict(tc.arguments)
                        fixed_args["path"] = default_path
                        tc = ToolCall(id=tc.id, name=tc.name, arguments=fixed_args)
                        log.info(
                            "agent.office_path_autofill",
                            tool=tc.name,
                            path=default_path,
                            session_id=session_id,
                        )
            if (
                session is not None
                and tc.name == "bash"
                and any("nano-banana" in sid for sid in locked_skill_ids)
            ):
                tc = _fix_nano_banana_mode(tc, session)
            return tc

        async def _execute_nano_bash_batch(
            batch: list[ToolCall],
        ) -> list[tuple[ToolCall, str, ToolResult]]:
            executed: list[tuple[ToolCall, str, ToolResult]] = []
            for start in range(0, len(batch), _NANO_BANANA_PARALLEL_BATCH_SIZE):
                if start > 0:
                    await asyncio.sleep(_NANO_BANANA_PARALLEL_BATCH_DELAY_S)
                chunk = batch[start : start + _NANO_BANANA_PARALLEL_BATCH_SIZE]
                chunk_results = await asyncio.gather(*(
                    _execute_tool(
                        registry,
                        chunk_tc,
                        evomap_enabled=evomap_allowed_for_turn,
                        browser_allowed=not browser_locked_by_evomap,
                        office_block_bash_probe=office_block_bash_probe,
                        office_block_message=office_block_message,
                        office_edit_only=office_edit_only,
                        office_edit_path=office_edit_path,
                        on_tool_call=on_tool_call,
                        on_tool_result=on_tool_result,
                    )
                    for chunk_tc in chunk
                ))
                executed.extend(
                    (chunk_tc, tc_id, result)
                    for chunk_tc, (tc_id, result) in zip(chunk, chunk_results, strict=True)
                )
            return executed

        async def _finalize_tool_result(tc: ToolCall, tc_id: str, result: ToolResult) -> None:
            nonlocal browser_locked_by_evomap
            nonlocal metadata_dirty
            nonlocal stop_for_evomap_choice
            nonlocal stop_for_probe_loop
            nonlocal stop_for_nano_fail
            nonlocal successful_tool_calls

            if tc.id in _evomap_preflight_results:
                tc_id, result = _evomap_preflight_results[tc.id]
            if (
                tc.name == "ppt_edit"
                and result.success
                and _mentions_specific_dark_bar_target(llm_message)
            ):
                action = str(tc.arguments.get("action", "replace_text")).strip().lower()
                if action == "apply_business_style" and "重设深色条 0 处" in (result.output or ""):
                    result = ToolResult(
                        success=False,
                        output=result.output,
                        error="未命中用户指定对象：黑色横条仍未被替换，请继续定向修改该元素",
                    )
                elif action == "set_background":
                    result = ToolResult(
                        success=False,
                        output=result.output,
                        error="用户要求修改黑色横条，仅设置背景不算完成，请继续定向修改该横条",
                    )
            if tc.name == "evomap_fetch" and result.success:
                candidates = _parse_evomap_fetch_candidates(result.output or "")
                if len(candidates) > 3 and session is not None:
                    top3 = _pick_top_evomap_candidates(llm_message, candidates, limit=3)
                    session.metadata["evomap_pending_choices"] = {
                        "origin_message": llm_message,
                        "options": [{"asset_id": aid, "summary": summary} for aid, summary in top3],
                    }
                    if session_manager is not None:
                        await session_manager._store.update_session_field(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                            session.id,
                            metadata=session.metadata,
                        )
                    final_text_parts.append(_build_evomap_choice_prompt(top3))
                    stop_for_evomap_choice = True
                    return
                browser_locked_by_evomap = not _is_no_match_evomap_output(result)
            guard_update = apply_tool_result_guards(
                guard_state,
                tc,
                result,
                office_loop_guard_enabled=office_loop_guard_enabled,
                image_api_probe_guard_enabled=image_api_probe_guard_enabled,
                session_id=session_id,
                skill_hooks=_active_hooks_for_exec,
            )
            for event in guard_update.log_events:
                getattr(log, event.level)(event.event, **event.fields)
            final_text_parts.extend(guard_update.final_texts)
            if guard_update.stop_for_probe_loop:
                stop_for_probe_loop = True

            # Bug 4: collect cron/reminder success notices
            if (
                result.success
                and tc.name in ("cron", "reminder", "cron_manage")
                and result.output
            ):
                _cron_reminder_notices.append(result.output)

            if result.success and result.output:
                successful_tool_calls += 1
                for path_match in re.finditer(
                    r"([A-Za-z]:\\[^\s]+\.(?:jpg|jpeg|png|gif|webp)|/[^\s]+\.(?:jpg|jpeg|png|gif|webp))",
                    result.output,
                ):
                    image_path = path_match.group(1)
                    real_image_paths.append(image_path)
                    if (
                        session is not None
                        and Path(image_path).expanduser().is_file()
                        and session.metadata.get("last_generated_image_path") != image_path
                    ):
                        session.metadata["last_generated_image_path"] = image_path
                        metadata_dirty = True
            elif result.success:
                successful_tool_calls += 1
                if session is not None:
                    for office_path in _extract_office_paths(result.output):
                        if _remember_office_path(session.metadata, office_path):
                            metadata_dirty = True
            if session is not None and pending_office_paths:
                for office_path in list(pending_office_paths):
                    if Path(office_path).expanduser().exists():
                        if _remember_office_path(session.metadata, office_path):
                            metadata_dirty = True
                        pending_office_paths.remove(office_path)
            if (
                session is not None
                and tc.name == "bash"
                and result.success
                and _capture_latest_pptx(
                    session.metadata,
                    roots=_default_pptx_scan_roots(),
                    window_seconds=240,
                )
            ):
                metadata_dirty = True

            if session is not None and tc.name in {"ppt_edit", "docx_edit", "xlsx_edit"}:
                arg_path = tc.arguments.get("path")
                if (
                    isinstance(arg_path, str)
                    and arg_path.strip()
                    and _remember_office_path(session.metadata, arg_path.strip())
                ):
                    metadata_dirty = True

            tool_output = _format_tool_output(result, tc.name)

            if native_tools:
                tool_msg = Message(
                    role="tool",
                    content=tool_output,
                    tool_call_id=tc_id,
                )
            else:
                tool_msg = Message(
                    role="user",
                    content=(f"[工具 {tc.name} 执行结果]\n{tool_output}"),
                )
            _dedup_consecutive_tool_errors(conversation, tool_output, tc.name)
            conversation.append(tool_msg)

            if session_manager and session and not _is_transient_cli_usage_error(result):
                snippet = tool_output[:500] if len(tool_output) > 500 else tool_output
                await _persist_message(
                    session_manager,
                    session,
                    "tool",
                    f"[{tc.name}] {snippet}",
                    tool_call_id=tc_id,
                    tool_name=tc.name,
                )

            log.debug(
                "agent.tool_result",
                tool=tc.name,
                success=result.success,
                output_len=len(result.output),
            )

            # Keep native-tool transcripts strictly ordered as:
            # assistant(tool_calls) -> tool result(s) -> follow-up prompt(s).
            # Some OpenAI-compatible providers like Qwen reject any non-tool
            # message inserted between the assistant tool call and its tool reply.
            for prompt in guard_update.conversation_messages:
                conversation.append(Message(role="user", content=prompt))

            # Inject screenshot image into conversation so multimodal LLMs can
            # actually *see* the page.  Appended as a user message after the
            # tool result to avoid breaking the tool-call/tool-result pairing.
            if (
                tc.name == "browser"
                and str(tc.arguments.get("action", "")).strip().lower() == "screenshot"
                and result.success
            ):
                _inject_screenshot_image(conversation, result.output)

            if (
                not result.success
                and tc.name == "bash"
                and _NANO_BANANA_SCRIPT_CMD_RE.search(
                    str(tc.arguments.get("command", ""))
                )
            ):
                conversation.append(
                    Message(
                        role="user",
                        content=(
                            "[系统提示] nano-banana 生图命令执行失败。"
                            "此命令耗时长、成本高，禁止自动重试。"
                            "请将失败信息告知用户，由用户决定是否重试。"
                        ),
                    )
                )
                stop_for_nano_fail = True

        tool_idx = 0
        while tool_idx < len(tool_calls):
            tc = _prepare_tool_call(tool_calls[tool_idx])
            if _is_parallelizable_nano_bash_call(tc):
                nano_batch = [tc]
                tool_idx += 1
                while tool_idx < len(tool_calls):
                    next_tc = _prepare_tool_call(tool_calls[tool_idx])
                    if not _is_parallelizable_nano_bash_call(next_tc):
                        break
                    nano_batch.append(next_tc)
                    tool_idx += 1
                batch_results = await _execute_nano_bash_batch(nano_batch)
                for batch_tc, tc_id, result in batch_results:
                    await _finalize_tool_result(batch_tc, tc_id, result)
                    if stop_for_evomap_choice or stop_for_probe_loop or stop_for_nano_fail:
                        break
                if stop_for_evomap_choice or stop_for_probe_loop or stop_for_nano_fail:
                    break
                continue

            tool_idx += 1
            if tc.id in _evomap_preflight_results:
                tc_id, result = _evomap_preflight_results[tc.id]
            else:
                tc_id, result = await _execute_tool(
                    registry,
                    tc,
                    evomap_enabled=evomap_allowed_for_turn,
                    browser_allowed=not browser_locked_by_evomap,
                    office_block_bash_probe=office_block_bash_probe,
                    office_block_message=office_block_message,
                    office_edit_only=office_edit_only,
                    office_edit_path=office_edit_path,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                )
            await _finalize_tool_result(tc, tc_id, result)
            if stop_for_probe_loop or stop_for_evomap_choice or stop_for_nano_fail:
                break

        if stop_for_evomap_choice:
            break
        if stop_for_probe_loop:
            break
        if stop_for_nano_fail:
            final_text_parts.append(
                "生图命令执行失败，需要你确认是否重试。回复「重试」我会再试一次。"
            )
            break
        post_round_update = apply_post_round_guards(
            guard_state,
            tool_calls,
            round_idx=round_idx,
            session_id=session_id,
        )
        for event in post_round_update.log_events:
            getattr(log, event.level)(event.event, **event.fields)
        for prompt in post_round_update.conversation_messages:
            conversation.append(Message(role="user", content=prompt))
        final_text_parts.extend(post_round_update.final_texts)
        if metadata_dirty and session is not None and session_manager is not None:
            await session_manager.update_metadata(session, session.metadata)
            metadata_dirty = False
        if post_round_update.stop_for_repeat_loop:
            break

        final_text_parts.clear()
    else:
        log.warning(
            "agent.token_budget_exhausted",
            session_id=session_id,
            rounds=round_idx + 1,
            total_output=total_output,
        )

    final_text = "".join(final_text_parts)
    final_text = _fix_image_paths(final_text, real_image_paths)

    if _active_hooks_for_exec is not None and final_text:
        final_text = _active_hooks_for_exec.postprocess_reply(final_text, session)

    # Bug 4: force-append cron/reminder notices if LLM omitted them.
    # Bug 4b: when the *only* successful tools were cron/reminder (deferred intent),
    # the LLM sometimes hallucinates reply text from a previous turn.  In that case,
    # replace the entire reply with a clean confirmation built from the tool output.
    if _cron_reminder_notices:
        _only_cron_tools = is_deferred and successful_tool_calls <= len(_cron_reminder_notices)
        if _only_cron_tools:
            final_text = "好的，" + "；".join(_cron_reminder_notices) + "。"
        else:
            for notice in _cron_reminder_notices:
                if notice not in final_text:
                    final_text = f"{final_text.rstrip()}\n（{notice}）" if final_text else f"（{notice}）"

    if lock_is_explicit and locked_skill_ids and skill_announce_pending:
        announce = _skill_announcement(locked_skill_ids, previous_locked_skill_ids)
        final_text = f"{announce}\n\n{final_text}" if final_text else announce
        skill_announce_pending = False
        if session is not None:
            session.metadata["skill_lock_announce_pending"] = False
            metadata_dirty = True

    _lock_confirm_tip = (
        '回复"任务完成"以解除技能锁定；若继续修改请直接说需求。'
    )

    def _append_lock_tip(text: str) -> str:
        """Append lock-confirm tip only if the text doesn't already contain it."""
        if "任务完成" in text and "解除" in text:
            return text
        return f"{text.rstrip()}\n{_lock_confirm_tip}" if text else _lock_confirm_tip

    # Deferred lock: first run completed with auto-routed skills -> lock them now.
    if (
        not lock_is_explicit
        and pending_lock_skill_ids
        and session is not None
        and successful_tool_calls > 0
    ):
        locked_skill_ids = pending_lock_skill_ids
        lock_is_explicit = True
        session.metadata["locked_skill_ids"] = locked_skill_ids
        session.metadata["skill_lock_waiting_done"] = True
        metadata_dirty = True
        final_text = _append_lock_tip(final_text)

    # Explicit lock: require user confirmation to release after successful tool use.
    elif (
        lock_is_explicit
        and locked_skill_ids
        and session is not None
        and successful_tool_calls > 0
        and not lock_waiting_done
    ):
        session.metadata["locked_skill_ids"] = locked_skill_ids
        session.metadata["skill_lock_waiting_done"] = True
        metadata_dirty = True
        final_text = _append_lock_tip(final_text)

    if metadata_dirty and session is not None and session_manager is not None:
        await session_manager.update_metadata(session, session.metadata)

    # Background: generate L0/L1 summaries for older messages if needed
    if (
        session_store
        and router
        and summarizer_cfg.enabled
        and session
        and group_compressor is None
        and _compressor.should_compress(conversation_message_count)
    ):
        try:
            latest = await session_store.get_latest_summary(session_id, "L0")
            msg_rows = await session_store.get_messages(session_id)

            already_covered = latest.source_msg_end if latest else 0
            uncovered = [r for r in msg_rows if r.id > already_covered]
            protected = min(RECENT_PROTECTED, len(uncovered))
            to_compress = uncovered[:-protected] if protected < len(uncovered) else []

            if len(to_compress) >= 8:
                compress_msgs = [
                    Message(role=r.role if r.role != "tool" else "assistant", content=r.content)  # pyright: ignore[reportArgumentType]
                    for r in to_compress
                ]
                start_id = to_compress[0].id
                end_id = to_compress[-1].id
                _store_ref = session_store
                _router_ref = router
                _model_ref: str = summarizer_cfg.model

                async def _bg_compress() -> None:
                    try:
                        await _compressor.compress_segment(
                            session_id=session_id,
                            messages=compress_msgs,
                            msg_id_start=start_id,
                            msg_id_end=end_id,
                            store=_store_ref,
                            router=_router_ref,
                            model=_model_ref,
                        )
                    except Exception as exc:
                        log.debug("agent.bg_compress_failed", error=str(exc))

                asyncio.create_task(_bg_compress())
        except Exception as exc:
            log.debug("agent.bg_compress_prep_failed", error=str(exc))

    _final_llm_rounds = max(0, round_idx + 1)
    log.info(
        "agent.done",
        model=model_id,
        llm_rounds=_final_llm_rounds,
        input_tokens=total_input,
        output_tokens=total_output,
        session_id=session_id,
        trigger_event_id=trigger_event_id,
        trigger_preview=trigger_preview,
    )

    if on_done is not None:
        try:
            await on_done(model_id, total_input, total_output, _final_llm_rounds)
        except Exception as _done_exc:
            log.debug("agent.on_done_callback_failed", error=str(_done_exc))

    if session_store and total_input + total_output > 0:
        try:
            await session_store.record_token_usage(
                session_id=session_id,
                model=model_id,
                input_tokens=total_input,
                output_tokens=total_output,
            )
        except Exception:
            log.debug("agent.token_usage_save_failed")

    if memory_manager is not None and agent_cfg.memory.enabled:
        memory_cfg = agent_cfg.memory
        captured = False
        organized = False
        organizer_ready = True
        try:
            captured = await memory_manager.auto_capture_user_message(
                message,
                source=f"session:{session_id}",
                mode=memory_cfg.auto_capture_mode,
                cooldown_seconds=memory_cfg.cooldown_seconds,
                max_per_hour=memory_cfg.max_per_hour,
                batch_size=memory_cfg.capture_batch_size,
                merge_window_seconds=memory_cfg.capture_merge_window_seconds,
            )
            if captured:
                log.info("agent.memory_captured", session_id=session_id)
        except Exception as exc:
            log.debug("agent.memory_capture_failed", error=str(exc), session_id=session_id)
        if memory_cfg.organizer_enabled:
            try:
                router.resolve(memory_cfg.organizer_model)
            except Exception:
                organizer_ready = False
            if memory_cfg.organizer_background:
                if organizer_ready:
                    _schedule_memory_organizer_task(
                        session_id,
                        memory_manager=memory_manager,
                        router=router,
                        model_id=memory_cfg.organizer_model,
                        organizer_min_new_entries=memory_cfg.organizer_min_new_entries,
                        organizer_interval_seconds=memory_cfg.organizer_interval_seconds,
                        organizer_max_raw_window=memory_cfg.organizer_max_raw_window,
                        keep_profile_versions=memory_cfg.keep_profile_versions,
                        max_raw_entries=memory_cfg.max_raw_entries,
                    )
            else:
                try:
                    organized = await memory_manager.organize_if_needed(
                        router=router,
                        model_id=memory_cfg.organizer_model,
                        organizer_min_new_entries=memory_cfg.organizer_min_new_entries,
                        organizer_interval_seconds=memory_cfg.organizer_interval_seconds,
                        organizer_max_raw_window=memory_cfg.organizer_max_raw_window,
                        keep_profile_versions=memory_cfg.keep_profile_versions,
                        max_raw_entries=memory_cfg.max_raw_entries,
                    )
                    if organized:
                        log.info("agent.memory_organized", session_id=session_id)
                except Exception as exc:
                    organizer_ready = False
                    log.debug("agent.memory_organize_failed", error=str(exc), session_id=session_id)

        if captured and (not memory_cfg.organizer_enabled or not organizer_ready or not organized):
            try:
                updated = await memory_manager.upsert_profile_from_capture(
                    message,
                    router=router,
                    model_id=model_id,
                    max_tokens=memory_cfg.recall_profile_max_tokens,
                    keep_profile_versions=memory_cfg.keep_profile_versions,
                )
                if updated:
                    log.info("agent.memory_profile_fallback_updated", session_id=session_id)
            except Exception as exc:
                log.debug(
                    "agent.memory_profile_fallback_failed",
                    error=str(exc),
                    session_id=session_id,
                )

    _set_active_skill_hooks(None)
    return final_text
