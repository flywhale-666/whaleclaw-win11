"""EvoMap interaction helpers for the agent."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from whaleclaw.agent.helpers.regex_patterns import (
    _EVOMAP_CHOICE_PATTERNS,
    _EVOMAP_LINE_RE,
)
from whaleclaw.agent.helpers.skill_lock import normalize_for_match as _normalize_for_match
from whaleclaw.providers.base import Message
from whaleclaw.tools.base import ToolResult

if TYPE_CHECKING:
    from whaleclaw.config.schema import WhaleclawConfig
    from whaleclaw.providers.router import ModelRouter
    from whaleclaw.sessions.manager import Session


def _is_evomap_enabled(config: WhaleclawConfig) -> bool:
    plugins_cfg = getattr(config, "plugins", None)
    if not isinstance(plugins_cfg, dict):
        return False
    evomap_cfg_raw: object = plugins_cfg.get("evomap", None)  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    if not isinstance(evomap_cfg_raw, dict):
        return False
    evomap_cfg: dict[str, object] = evomap_cfg_raw  # pyright: ignore[reportAssignmentType, reportUnknownVariableType]
    return bool(evomap_cfg.get("enabled", False))


def _is_tasky_message_for_evomap(text: str) -> bool:
    low = _normalize_for_match(text)
    if not low:
        return False
    keys = (
        "做",
        "制作",
        "生成",
        "写",
        "整理",
        "设计",
        "计划",
        "PPT",
        "ppt",
        "幻灯片",
        "演示文稿",
        "报告",
        "文档",
        "方案",
        "简历",
        "海报",
        "脚本",
        "代码",
        "页面",
        "表格",
        "excel",
        "xlsx",
        "word",
        "docx",
        "evomap",
        "evo map",
        "方案库",
        "协作经验",
    )
    return any(k in low for k in keys)


def _infer_task_kind(text: str) -> str:
    low = text.lower()
    if any(k in low for k in ("ppt", "幻灯片", "演示文稿", "slides", "deck")):
        return "ppt"
    if any(k in low for k in ("网页", "网站", "html", "web", "landing page", "前端")):
        return "web"
    if any(k in low for k in ("检索", "汇总", "调研", "信息", "collect", "research", "summarize")):
        return "research"
    return "general"


def _extract_topic_terms(text: str, *, limit: int = 2) -> list[str]:
    low = _normalize_for_match(text)
    stop = {
        "做",
        "制作",
        "生成",
        "创建",
        "一个",
        "关于",
        "给我",
        "帮我",
        "ppt",
        "网页",
        "网站",
        "html",
        "web",
        "方案",
        "检索",
        "汇总",
        "信息",
        "today",
        "todays",
        "today's",
    }
    terms: list[str] = []
    for t in re.findall(r"[\w\u4e00-\u9fff]{2,8}", low):
        if t in stop:
            continue
        if t not in terms:
            terms.append(t)
        if len(terms) >= max(1, limit):
            break
    return terms


def _recommended_evomap_signals(text: str) -> str:  # pyright: ignore[reportUnusedFunction]
    kind = _infer_task_kind(text)
    if kind == "ppt":
        base = [
            "ppt",
            "presentation",
            "slides",
            "storyline",
            "deck structure",
            "visual layout",
            "python-pptx",
        ]
    elif kind == "web":
        base = [
            "web page",
            "html",
            "css",
            "frontend",
            "responsive layout",
            "content structure",
        ]
    elif kind == "research":
        base = [
            "information retrieval",
            "multi-source collection",
            "source validation",
            "structured summary",
            "fact-check",
        ]
    else:
        base = [
            "workflow",
            "execution plan",
            "quality checklist",
        ]
    return ",".join(base + _extract_topic_terms(text, limit=2))


def _extra_memory_has_evomap_hint(extra_memory: str) -> bool:
    text = extra_memory.strip()
    if not text:
        return False
    return "EvoMap 协作经验候选" in text


def _is_no_match_evomap_output(result: ToolResult) -> bool:
    if not result.success:
        return False
    out = (result.output or "").strip()
    if not out:
        return True
    hints = ("未找到匹配方案", "暂无可用任务", "无已认领任务")
    return any(h in out for h in hints)


def _build_evomap_first_system_message() -> Message:
    return Message(
        role="system",
        content=(
            "执行策略：本轮是流程任务，优先复用 EvoMap 成功经验。\n"
            "1) 必须先调用 evomap_fetch 获取经验候选；\n"
            "2) 只有当 evomap_fetch 无命中或失败时，才可调用 browser；\n"
            "3) 若 evomap_fetch 命中，请先按命中方案执行。"
        ),
    )


def _extract_evomap_choice_index(text: str, options_count: int) -> int | None:
    if options_count <= 0:
        return None
    raw = text.strip()
    if not raw:
        return None
    for p in _EVOMAP_CHOICE_PATTERNS:
        m = p.match(raw)
        if not m:
            continue
        token = m.group(1).strip().upper()
        if token in {"A", "B", "C"}:
            idx = ord(token) - ord("A")
        elif token.isdigit():
            idx = int(token) - 1
        else:
            return None
        if 0 <= idx < options_count:
            return idx
    return None


def _parse_evomap_fetch_candidates(output: str) -> list[tuple[str, str]]:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    items: list[tuple[str, str]] = []
    for ln in lines:
        m = _EVOMAP_LINE_RE.match(ln)
        if not m:
            continue
        aid = m.group(1).strip()
        summary = m.group(2).strip()
        if not aid and not summary:
            continue
        items.append((aid, summary))
    return items


def _pick_top_evomap_candidates(
    user_message: str,
    candidates: list[tuple[str, str]],
    *,
    limit: int = 3,
) -> list[tuple[str, str]]:
    query = _normalize_for_match(user_message)
    terms = {t for t in re.findall(r"[\w\u4e00-\u9fff]{2,}", query)}

    scored: list[tuple[int, tuple[str, str]]] = []
    for item in candidates:
        aid, summary = item
        hay = _normalize_for_match(f"{aid} {summary}")
        score = 0
        for t in terms:
            if t in hay:
                score += 1
        scored.append((score, item))

    scored.sort(key=lambda x: (-x[0], x[1][0]))
    return [item for _score, item in scored[: max(1, limit)]]


def _build_evomap_choice_prompt(candidates: list[tuple[str, str]]) -> str:
    labels = ("A", "B", "C")
    lines = ["EvoMap 命中了多条可用方案，请先选一个我再执行："]
    for idx, item in enumerate(candidates[:3]):
        aid, summary = item
        label = labels[idx]
        lines.append(f"{label}. {aid} — {summary}")
    lines.append("请直接回复：选A / 选B / 选C")
    return "\n".join(lines)


async def _llm_judge_task_phase(
    router: ModelRouter,
    model_id: str,
    *,
    session: Session | None,
    message: str,
) -> str:
    """Use the main model to classify task phase: NEW_TASK or EDITING."""
    from whaleclaw.agent.helpers.skill_lock import preview_text as _preview_text

    system = Message(
        role="system",
        content=(
            "你是任务阶段分类器，只输出一个标签：NEW_TASK 或 EDITING。\n"
            "NEW_TASK=开始一个全新主要任务/新主题/新产物。\n"
            "EDITING=在已有任务上修改/补充/继续/讨论细节。\n"
            "只输出标签，不要解释。"
        ),
    )
    context: list[Message] = [system]
    if session is not None and session.messages:
        recent: list[Message] = []
        for msg in session.messages[-6:]:
            if msg.role not in {"user", "assistant"}:
                continue
            recent.append(
                Message(role=msg.role, content=_preview_text(msg.content or "", limit=400))
            )
        context.extend(recent)
    context.append(
        Message(role="user", content=f"当前用户消息：{_preview_text(message, limit=600)}")
    )
    try:
        resp = await router.chat(model_id, context, tools=None, on_stream=None)
    except Exception:
        return "EDITING"
    raw = (resp.content or "").strip().upper()
    if "NEW_TASK" in raw:
        return "NEW_TASK"
    if "EDITING" in raw:
        return "EDITING"
    return "EDITING"


# Public aliases for cross-module import.
is_evomap_enabled = _is_evomap_enabled
is_tasky_message_for_evomap = _is_tasky_message_for_evomap
infer_task_kind = _infer_task_kind
extract_topic_terms = _extract_topic_terms
extra_memory_has_evomap_hint = _extra_memory_has_evomap_hint
is_no_match_evomap_output = _is_no_match_evomap_output
build_evomap_first_system_message = _build_evomap_first_system_message
extract_evomap_choice_index = _extract_evomap_choice_index
parse_evomap_fetch_candidates = _parse_evomap_fetch_candidates
pick_top_evomap_candidates = _pick_top_evomap_candidates
build_evomap_choice_prompt = _build_evomap_choice_prompt
llm_judge_task_phase = _llm_judge_task_phase
