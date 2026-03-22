"""Memory, compression, and token-estimation helpers for the agent."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from whaleclaw.providers.base import Message
from whaleclaw.utils.log import get_logger

if TYPE_CHECKING:
    from whaleclaw.memory.manager import MemoryManager
    from whaleclaw.providers.router import ModelRouter

log = get_logger(__name__)

_memory_organizer_tasks: dict[str, asyncio.Task[None]] = {}


def _est_tokens(text: str) -> int:
    return max(0, len(text) // 3)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    char_cap = max_tokens * 3
    if len(text) <= char_cap:
        return text
    return text[:char_cap]


def _build_memory_system_message(recalled: str) -> Message:
    """Wrap recalled memory as durable preference/fact context."""
    return Message(
        role="system",
        content=(
            "以下是从长期记忆召回的历史信息，包含用户长期偏好、稳定约束与历史事实。\n"
            "执行规则：\n"
            "1) 若内容属于长期偏好/写作与产出规则，且不与本轮用户要求冲突，请默认执行；\n"
            "2) 若内容属于历史事实且你不确定当前是否仍然有效，可先向用户确认。\n"
            f"{recalled}"
        ),
    )


def _build_global_style_system_message(style_directive: str) -> Message:
    return Message(
        role="system",
        content=(
            "以下是用户长期稳定的全局回复风格偏好，请默认遵守：\n"
            f"{style_directive.strip()}\n"
            "若用户在本轮消息中明确提出不同风格/长度要求，以本轮用户要求为准。"
        ),
    )


def _build_compound_task_system_message() -> Message:
    return Message(
        role="system",
        content=(
            "检测到复合任务（多步骤编排）。\n"
            "请按用户描述的顺序依次执行所有步骤，不要只执行其中一步。\n"
            "规则：\n"
            "1. 每一步用工具完成后，立刻继续执行下一步，不要等待用户确认。\n"
            "2. 不要追问参数，用合理默认值补全缺失信息。\n"
            "3. 生图步骤（nano-banana/文生图）的图片路径需传给后续插入步骤使用。\n"
            "4. 全部步骤完成后，在一条回复中汇总所有结果。"
        ),
    )


def _build_external_memory_system_message(extra_memory: str) -> Message:
    return Message(
        role="system",
        content=(
            "以下是来自协作网络的外部经验候选，仅作为补充参考：\n"
            f"{extra_memory.strip()}\n"
            "若与用户本轮明确要求冲突，以用户本轮要求为准；"
            "若与本地长期记忆冲突，以本地长期记忆为准。"
        ),
    )


def _merge_recall_blocks(profile: str, raw: str) -> str:
    blocks = [x.strip() for x in (profile, raw) if x.strip()]
    return "\n".join(blocks)


async def _compress_external_memory_with_llm(
    *,
    router: ModelRouter,
    model_id: str,
    text: str,
    max_tokens: int,
) -> str:
    sys_prompt = (
        "你是外部经验压缩器。"
        "请将输入经验压缩到给定 token 上限内，保留可执行做法与约束。"
        "禁止新增事实。输出纯文本。"
    )
    user_prompt = f"目标上限约 {max_tokens} tokens。\n输入如下：\n{text}\n\n请输出压缩结果。"
    try:
        resp = await router.chat(
            model_id,
            [
                Message(role="system", content=sys_prompt),
                Message(role="user", content=user_prompt),
            ],
        )
    except Exception:
        return ""
    out = resp.content.strip()
    if not out:
        return ""
    if _est_tokens(out) <= max_tokens:
        return out
    return _truncate_to_tokens(out, max_tokens)


def _schedule_memory_organizer_task(
    session_id: str,
    *,
    memory_manager: MemoryManager,
    router: ModelRouter,
    model_id: str,
    organizer_min_new_entries: int,
    organizer_interval_seconds: int,
    organizer_max_raw_window: int,
    keep_profile_versions: int,
    max_raw_entries: int,
) -> None:
    running = _memory_organizer_tasks.get(session_id)
    if running is not None and not running.done():
        return

    async def _run() -> None:
        try:
            organized = await memory_manager.organize_if_needed(
                router=router,
                model_id=model_id,
                organizer_min_new_entries=organizer_min_new_entries,
                organizer_interval_seconds=organizer_interval_seconds,
                organizer_max_raw_window=organizer_max_raw_window,
                keep_profile_versions=keep_profile_versions,
                max_raw_entries=max_raw_entries,
            )
            if organized:
                log.info("agent.memory_organized", session_id=session_id)
        except Exception as exc:
            log.debug("agent.memory_organize_failed", error=str(exc), session_id=session_id)

    task = asyncio.create_task(_run(), name=f"memory-organizer:{session_id}")
    _memory_organizer_tasks[session_id] = task

    def _cleanup(_task: asyncio.Task[None]) -> None:
        current = _memory_organizer_tasks.get(session_id)
        if current is _task:
            _memory_organizer_tasks.pop(session_id, None)

    task.add_done_callback(_cleanup)


# Public aliases for cross-module import.
build_compound_task_system_message = _build_compound_task_system_message
build_external_memory_system_message = _build_external_memory_system_message
build_global_style_system_message = _build_global_style_system_message
build_memory_system_message = _build_memory_system_message
compress_external_memory_with_llm = _compress_external_memory_with_llm
est_tokens = _est_tokens
merge_recall_blocks = _merge_recall_blocks
memory_organizer_tasks = _memory_organizer_tasks
schedule_memory_organizer_task = _schedule_memory_organizer_task
truncate_to_tokens = _truncate_to_tokens
