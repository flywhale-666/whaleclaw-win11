"""Skill-lock and assistant-name helpers for the single-agent runtime."""

from __future__ import annotations

import os
import re
from pathlib import Path

from whaleclaw.providers.base import ImageContent, Message
from whaleclaw.skills.parser import Skill, SkillParamItem
from whaleclaw.tools.base import ToolDefinition
from whaleclaw.tools.registry import ToolRegistry

CORE_NATIVE_TOOLS = {
    "browser",
    "bash",
    "file_read",
    "file_write",
    "file_edit",
    "patch_apply",
    "ppt_edit",
    "docx_edit",
    "xlsx_edit",
}
_NANO_BANANA_DEFAULT_MODEL_FILE = (
    Path.home() / ".whaleclaw" / "credentials" / "nano_banana_default_model.txt"
)
MAX_NATIVE_TOOLS = 12
TOOL_POLICY_KEYWORDS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("ppt", "pptx", "幻灯片", "演示文稿"): ("ppt_edit", "file_edit", "patch_apply"),
    ("word", "docx", "文档"): ("docx_edit", "file_edit"),
    ("excel", "xlsx", "表格", "单元格"): ("xlsx_edit", "file_edit"),
    ("网页", "网站", "页面", "链接", "url"): ("browser", "web_fetch"),
    ("代码", "脚本", "终端", "命令", "日志"): ("bash", "file_read", "file_write"),
    ("进程", "后台", "卡住", "kill", "日志"): ("process", "bash"),
}


def preview_text(text: str, limit: int = 80) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def sanitize_assistant_name(raw: str) -> str:
    name = raw.strip().strip("\"'“”‘’「」[]()（）")
    if not name:
        return ""
    if any(ch in name for ch in ("?", "？", "!", "！", "吗")):
        return ""
    low = name.lower()
    if low in {"什么", "啥", "name", "名字"}:
        return ""
    if "什么" in name:
        return ""
    if len(name) > 24:
        return ""
    if not re.fullmatch(r"[\w\u4e00-\u9fff·\-.]{1,24}", name):
        return ""
    return name


def detect_assistant_name_update(
    message: str,
    *,
    reset_patterns: tuple[re.Pattern[str], ...],
    set_patterns: tuple[re.Pattern[str], ...],
) -> tuple[str, str]:
    text = message.strip()
    if not text:
        return ("none", "")
    for pattern in reset_patterns:
        if pattern.search(text):
            return ("reset", "")
    for pattern in set_patterns:
        match = pattern.search(text)
        if not match:
            continue
        name = sanitize_assistant_name(match.group(1))
        if name:
            return ("set", name)
    return ("none", "")


def normalize_for_match(text: str) -> str:
    return " ".join(text.lower().split())


def _normalized_compact(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text.lower())


def _is_specific_enough_token(token: str) -> bool:
    if re.search(r"[\u4e00-\u9fff]", token):
        return len(token) >= 2
    return len(token) >= 4


_CLEAN_SKILL_ACTIVATION_KEYWORDS: tuple[str, ...] = (
    "使用",
    "调用",
    "启用",
    "启动",
    "切到",
    "切换到",
)
_CLEAN_SKILL_NOUNS: tuple[str, ...] = ("技能", "skill")
_CLEAN_TASK_DONE_PHRASES: tuple[str, ...] = ("任务完成", "完成任务", "任务结束", "结束任务")


def parse_use_command(
    text: str,
    *,
    use_cmd_re: re.Pattern[str],
) -> tuple[list[str], str] | None:
    match = use_cmd_re.match(text)
    if not match:
        return None
    token = match.group(1).strip().lower()
    skill_ids = [item.strip() for item in token.split(",") if item.strip()]
    remainder = match.group(2).strip()
    return (skill_ids, remainder)


def is_task_done_confirmation(
    text: str,
    *,
    task_done_patterns: tuple[re.Pattern[str], ...],
) -> bool:
    stripped = text.strip()
    if not stripped:
        return False

    candidates = [stripped]
    quoted_tail = re.sub(r"^[^:\n?]{1,40}[:?]\s*", "", stripped, count=1).strip()
    if quoted_tail and quoted_tail != stripped:
        candidates.append(quoted_tail)

    for candidate in candidates:
        if candidate in _CLEAN_TASK_DONE_PHRASES:
            return True
        if any(pattern.search(candidate) for pattern in task_done_patterns):
            return True
    return False


def build_skill_lock_system_message(skill_ids: list[str]) -> Message:
    joined = ", ".join(skill_ids)
    return Message(
        role="system",
        content=(
            f"当前会话已锁定技能：{joined}。\n"
            "执行时仅允许在这些技能范围内规划与调用，不要偏移到无关方案。\n"
            "若用户明确回复\u201c任务完成\u201d或\u201c完成任务\u201d，再解除该锁定。"
        ),
    )


def build_nano_banana_execution_system_message(
    current_model: str,
    recent_image_paths: list[str],
    recommended_command: str = "",
) -> Message:
    image_lines = (
        "\n".join(f"- {path}" for path in recent_image_paths)
        if recent_image_paths
        else "- 当前没有可复用的历史图片路径"
    )
    cmd_hint = (
        "\n5) 推荐直接使用以下命令执行（已按本轮参数预填好），不需要再做任何探测或试错：\n"
        f"```\n{recommended_command}\n```"
        if recommended_command.strip()
        else ""
    )
    body = (
        "当前正在执行 nano-banana-image-t8 技能。\n"
        "执行约束：\n"
        f"1) 当前本轮模型是：{current_model}。若调用脚本，"
        f"必须把 `--model` 和 `--edit-model` 都设置为 `{current_model}`，"
        "不要继续沿用其它模型。\n"
        '2) 对外回复只使用展示名\u201c香蕉2\u201d或\u201c香蕉pro\u201d，除非用户明确追问，不要暴露底层模型标识。\n'
        '3) 若用户是在说\u201c重试\u201d\u201c继续处理这张图\u201d\u201c改用香蕉pro重试\u201d这类续跑语义，且没有上传新图，'
        "默认复用最近一轮可用图片，不要再要求用户重新上传。\n"
        f"4) 当前可直接复用的历史图片绝对路径如下：\n{image_lines}\n"
        "5) 禁止使用 bash 做环境探测或计划回读，例如 `Test-Path`、`Get-Content`、"
        "`ls/stat/test`、读取 `task-plan.md` 等；需要生图时直接执行推荐命令。"
        f"{cmd_hint}"
    )
    return Message(role="system", content=body)


def looks_like_skill_activation_message(
    text: str,
    *,
    skill_activation_patterns: tuple[re.Pattern[str], ...],
) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.lower().startswith("/use "):
        return True
    normalized = _normalized_compact(stripped)
    if any(keyword in stripped for keyword in _CLEAN_SKILL_ACTIVATION_KEYWORDS) and any(
        noun in stripped.lower() for noun in _CLEAN_SKILL_NOUNS
    ):
        return True
    if "nanobanana" in normalized or "nanobanana2" in normalized:
        return True
    return any(pattern.search(stripped) for pattern in skill_activation_patterns)



def normalize_skill_ids(skills: list[Skill]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for skill in skills:
        skill_id = skill.id.strip().lower()
        if not skill_id or skill_id in seen:
            continue
        seen.add(skill_id)
        output.append(skill_id)
    return output


def skill_announcement(skill_ids: list[str], previous_skill_ids: list[str]) -> str:
    joined = "、".join(skill_ids)
    if not previous_skill_ids or previous_skill_ids != skill_ids:
        return f"我将使用 {joined} 技能继续完成任务。"
    return f"我会继续使用 {joined} 技能推进当前任务。"


def skill_token_mentioned(token: str, text: str) -> bool:
    lower = text.lower()
    msg_norm = _normalized_compact(text)

    base = token.strip().lower().replace("_", "-")
    if not base:
        return False

    candidates: set[str] = {base, base.replace("-", ""), _normalized_compact(base)}
    parts = [part for part in base.split("-") if part]
    if len(parts) >= 2:
        short = "-".join(parts[:2])
        candidates.add(short)
        candidates.add(short.replace("-", ""))
        candidates.add(_normalized_compact(short))

    for candidate in candidates:
        if not _is_specific_enough_token(candidate):
            continue
        if candidate in lower or candidate in msg_norm:
            return True
    return False


def skill_explicitly_mentioned(skill: Skill, text: str) -> bool:
    return skill_token_mentioned(skill.id, text) or skill_token_mentioned(skill.name, text)


_TRIGGER_NOISE_WORDS = re.compile(r"(?:技能|工具|插件|skill|tool|plugin)")


def _strip_noise_words(text: str) -> str:
    """Remove common filler words that may appear between trigger tokens."""
    return _TRIGGER_NOISE_WORDS.sub("", text)


def skill_trigger_mentioned(skill: Skill, text: str) -> bool:
    """Return whether the message includes any sufficiently specific trigger."""
    lower = text.lower()
    msg_norm = _normalized_compact(text)
    # Also build a version with noise words stripped for fuzzy matching
    lower_stripped = _strip_noise_words(lower)
    msg_norm_stripped = _normalized_compact(lower_stripped)
    for raw in skill.triggers:
        trigger = raw.strip().lower()
        if not _is_specific_enough_token(trigger):
            continue
        if trigger in lower:
            return True
        # Fuzzy: match even if noise words (技能/工具/…) sit between trigger chars
        if trigger in lower_stripped:
            return True
        norm = _normalized_compact(trigger)
        if _is_specific_enough_token(norm) and (norm in msg_norm or norm in msg_norm_stripped):
            return True
    return False


def extract_ratio_or_size(text: str) -> str:
    ratio = re.search(r"\b(\d{1,2}\s*:\s*\d{1,2})\b", text)
    if ratio:
        return ratio.group(1).replace(" ", "")
    size = re.search(r"\b(\d{3,5}\s*x\s*\d{3,5})\b", text, re.IGNORECASE)
    if size:
        return size.group(1).replace(" ", "").lower()
    return ""


def extract_value_by_aliases(text: str, aliases: list[str]) -> str:
    for alias in aliases:
        pattern = rf"{re.escape(alias)}\s*(?:是|为|:|：)\s*(.+)$"
        match = re.search(pattern, text, re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


def has_param_secret_source(param: SkillParamItem) -> bool:
    for env_name in param.env_vars:
        val = os.getenv(env_name, "").strip()
        if val:
            return True
    if param.saved_file:
        path = Path(param.saved_file).expanduser()
        try:
            return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
        except Exception:
            return False
    return False


def persist_param_secret(param: SkillParamItem, value: object) -> None:
    """Persist a captured secret value into the configured saved file."""
    if param.type.strip().lower() != "api_key" or not param.saved_file:
        return
    secret = str(value).strip() if isinstance(value, str) else ""
    if not secret.startswith("sk-"):
        return
    path = Path(param.saved_file).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret, encoding="utf-8")
    except OSError:
        return


def capture_param_value(
    param: SkillParamItem,
    text: str,
    images: list[ImageContent] | None,
    previous: object,
) -> object:
    param_type = param.type.strip().lower()
    aliases = [param.key, *param.aliases]
    if param_type == "images":
        prev_count = int(previous) if isinstance(previous, int) else 0
        return max(prev_count, len(images or []))
    if param_type in {"ratio", "size"}:
        value = extract_ratio_or_size(text) or extract_value_by_aliases(text, aliases)
        return value or previous
    if param_type == "api_key":
        direct_match = re.search(r"\b(sk-[A-Za-z0-9_-]{12,})\b", text)
        if direct_match:
            return direct_match.group(1)
        alias_val = extract_value_by_aliases(text, aliases)
        if alias_val and "sk-" in alias_val.lower():
            alias_match = re.search(r"\b(sk-[A-Za-z0-9_-]{12,})\b", alias_val)
            if alias_match:
                return alias_match.group(1)
        if has_param_secret_source(param):
            return "__present__"
        return previous
    alias_val = extract_value_by_aliases(text, aliases)
    if alias_val:
        return alias_val
    if param_type == "text":
        stripped = text.strip()
        if (
            stripped
            and len(stripped) >= 6
            and not stripped.startswith("/use ")
            and "技能" not in stripped
        ):
            if re.search(r"\bsk-[A-Za-z0-9_-]{12,}\b", stripped):
                return previous
            if any(token in stripped for token in ("api key", "apikey", "尺寸", "比例")):
                return previous
            return stripped
    return previous


def param_satisfied(param: SkillParamItem, value: object) -> bool:
    param_type = param.type.strip().lower()
    if param_type == "images":
        count = int(value) if isinstance(value, int) else 0
        return count >= max(1, int(param.min_count))
    if value is None:
        return False
    return bool(str(value).strip())


def format_param_status(param: SkillParamItem, value: object) -> str:
    label = param.label or param.key
    param_type = param.type.strip().lower()
    if param_type == "images":
        count = int(value) if isinstance(value, int) else 0
        need = max(1, int(param.min_count))
        return f"{label}：已收到 {count} 张（至少 {need} 张）"
    if param_satisfied(param, value):
        if param_type == "api_key":
            return f"{label}：已就绪"
        return f"{label}：已收到"
    return f"{label}：未提供"


def detect_nano_banana_model_display(text: str, previous: str = "香蕉2") -> str:
    """Infer the selected Nano Banana model display name from user text."""
    normalized = text.strip().lower()
    if not normalized:
        return previous
    if any(token in normalized for token in ("香蕉pro", "nano-banana-2", "banana pro")):
        return "香蕉pro"
    if any(
        token in normalized
        for token in ("香蕉2", "banana生图", "香蕉生图", "gemini-3.1-flash-image-preview")
    ):
        return "香蕉2"
    return previous


_NANO_BANANA_CONTROL_MESSAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:请)?(?:我想|我要|我想要)?(?:用|使用)?\s*"
        r"(?:香蕉生图|香蕉文生图|香蕉图生图|banana生图|banana文生图|banana图生图|"
        r"nanobanana|nano\s*banana|nano-banana-2)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:切换|切换到|改用|换成|默认模型)\s*(?:香蕉2|香蕉pro)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:用|改用|切换到)?\s*(?:香蕉2|香蕉pro)\s*(?:重试|再试一次|继续|继续跑)?\s*$",
        re.IGNORECASE,
    ),
)

_NANO_BANANA_ACTIVATION_MESSAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:请)?(?:我想|我要|我想要)?(?:用|使用)?\s*"
        r"(?:香蕉生图|香蕉文生图|香蕉图生图|banana生图|banana文生图|banana图生图|"
        r"nanobanana|nano\s*banana|nano-banana-2)\s*$",
        re.IGNORECASE,
    ),
)

_NANO_BANANA_ACTIVATION_TOKENS: tuple[str, ...] = (
    "香蕉生图",
    "香蕉文生图",
    "香蕉图生图",
    "banana生图",
    "banana文生图",
    "banana图生图",
    "nanobanana",
    "nanobanana2",
    "nanobananaimage",
)

_NANO_BANANA_CONTROL_PREFIXES: tuple[str, ...] = (
    "用",
    "使用",
    "我要",
    "我想",
    "我想要",
    "切换",
    "切换到",
    "改用",
    "换成",
    "默认模型",
)


def is_nano_banana_control_message(text: str) -> bool:
    """Return whether the message is only a Nano Banana control/activation command.

    Only short, standalone control phrases match (e.g. "切换香蕉pro", "香蕉2重试").
    Task messages containing "香蕉生图" as part of a generation request (e.g.
    "使用香蕉生图画一张狗熊偷吃蜂蜜的图") intentionally do NOT match, so that
    control_message_only=False and the agent executes the task normally.
    """
    stripped = text.strip()
    if not stripped:
        return False
    normalized = _normalized_compact(stripped)
    if any(prefix in normalized for prefix in _NANO_BANANA_CONTROL_PREFIXES) and any(
        token in normalized for token in ("香蕉2", "香蕉pro")
    ):
        return True
    return any(pattern.fullmatch(stripped) for pattern in _NANO_BANANA_CONTROL_MESSAGE_PATTERNS)


def is_nano_banana_activation_message(text: str) -> bool:
    """Return whether the message is a pure Nano Banana activation command.

    Only short, standalone activation phrases match (e.g. "使用香蕉生图").
    Messages that contain actual task content (e.g. "使用香蕉生图画两张老虎下山图")
    intentionally do NOT match, so the agent loop can execute the task normally.
    """
    stripped = text.strip()
    if not stripped:
        return False
    return any(pattern.fullmatch(stripped) for pattern in _NANO_BANANA_ACTIVATION_MESSAGE_PATTERNS)


def load_saved_nano_banana_model_display() -> str:
    """Load persisted Nano Banana default model display name."""
    try:
        raw = _NANO_BANANA_DEFAULT_MODEL_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "香蕉2"
    if raw == "nano-banana-2":
        return "香蕉pro"
    return "香蕉2"


def sanitize_nano_banana_prompt_value(value: object) -> str:
    """Drop activation/control phrases that were previously mis-captured as prompts."""
    prompt = str(value).strip() if isinstance(value, str) else ""
    if not prompt:
        return ""
    if is_nano_banana_control_message(prompt) or is_nano_banana_activation_message(prompt):
        return ""
    return prompt


def _build_nano_banana_param_guard_reply(state: dict[str, object]) -> str:
    """Render the dedicated parameter guard copy for Nano Banana."""
    current_model = str(
        state.get("__model_display__", load_saved_nano_banana_model_display())
    ).strip() or "香蕉2"
    if current_model == "香蕉pro":
        model_line = "2) 当前模型：香蕉pro（0.2元）可切换模型香蕉2（0.1元）"
    else:
        model_line = "2) 当前模型：香蕉2（0.1元）可切换模型香蕉pro（0.2元）"

    prompt_value = sanitize_nano_banana_prompt_value(state.get("prompt"))
    prompt_status = (
        "3) 提示词：已收到"
        if param_satisfied(
            SkillParamItem(key="prompt", type="text"),
            prompt_value,
        )
        else "3) 提示词：未提供"
    )
    image_status = format_param_status(
        SkillParamItem(
            key="images",
            label="图生图图片",
            type="images",
            required=False,
            min_count=1,
        ),
        state.get("images"),
    )
    lines = [
        "我将使用 nano-banana-image-t8 技能继续完成任务。",
        "",
        "我先确认参数（缺啥补啥）：",
        (
            "1) API Key：已就绪"
            if param_satisfied(
                SkillParamItem(key="api_key", type="api_key"),
                state.get("api_key"),
            )
            else "1) API Key：未提供"
        ),
        model_line,
        prompt_status,
        f"4) {image_status}",
        "5) 切换本次模型：切换香蕉2（pro）。设置默认模型：默认模型香蕉2（pro）",
    ]
    missing_prompts: list[str] = []
    if not param_satisfied(SkillParamItem(key="api_key", type="api_key"), state.get("api_key")):
        missing_prompts.append("请提供 Nano Banana API Key")
    if not param_satisfied(SkillParamItem(key="prompt", type="text"), prompt_value):
        missing_prompts.append("请提供提示词")
    # Images are optional (required=False); only required for image-to-image which is
    # determined at execution time, not at the param-guard stage.
    if missing_prompts:
        lines.append("请补充：" + "；".join(missing_prompts) + "。")
    else:
        lines.append("参数已齐，我现在开始执行。")
    return "\n".join(lines)


def nano_banana_missing_required(
    state: dict[str, object],
    *,
    control_message_only: bool,
) -> bool:
    """Decide whether Nano Banana should stay in the fixed parameter-guard stage."""
    has_key = param_satisfied(SkillParamItem(key="api_key", type="api_key"), state.get("api_key"))
    prompt_value = sanitize_nano_banana_prompt_value(state.get("prompt"))
    has_prompt = param_satisfied(SkillParamItem(key="prompt", type="text"), prompt_value)
    raw_images = state.get("images")
    image_count = int(raw_images) if isinstance(raw_images, int) else 0
    if not has_key or not has_prompt:
        return True
    return bool(image_count < 1 and control_message_only)


def build_skill_param_guard_reply(
    skill_id: str,
    params: list[SkillParamItem],
    state: dict[str, object],
) -> str:
    if skill_id == "nano-banana-image-t8":
        return _build_nano_banana_param_guard_reply(state)

    lines = [f"我将使用 {skill_id} 技能继续完成任务。", "", "我先确认参数（缺啥补啥）："]
    missing_prompts: list[str] = []
    for idx, param in enumerate(params, start=1):
        value = state.get(param.key)
        lines.append(f"{idx}) {format_param_status(param, value)}")
        if param.required and not param_satisfied(param, value):
            missing_prompts.append(param.prompt.strip() or (param.label or param.key))
    lines.append("")
    if missing_prompts:
        lines.append("请补充：" + "；".join(missing_prompts) + "。")
    else:
        lines.append("参数已齐，我现在开始执行。")
    return "\n".join(lines)


def update_guard_state(
    params: list[SkillParamItem],
    state: dict[str, object],
    message: str,
    images: list[ImageContent] | None,
) -> tuple[dict[str, object], bool]:
    new_state = dict(state)
    missing_required = False
    for param in params:
        prev = new_state.get(param.key)
        captured = capture_param_value(param, message, images, prev)
        new_state[param.key] = captured
        persist_param_secret(param, captured)
        if param.required and not param_satisfied(param, captured):
            missing_required = True
    return new_state, missing_required


def guarded_skills(skills: list[Skill]) -> list[Skill]:
    output: list[Skill] = []
    for skill in skills:
        if (
            skill.param_guard is None
            or not skill.param_guard.enabled
            or not skill.param_guard.params
        ):
            continue
        output.append(skill)
    return output


def score_tool_relevance(user_message: str, tool: ToolDefinition) -> int:
    query = normalize_for_match(user_message)
    if not query:
        return 0

    score = 0
    corpus_parts = [tool.name, tool.description]
    corpus_parts.extend(param.name for param in tool.parameters)
    corpus_parts.extend(param.description for param in tool.parameters)
    corpus = normalize_for_match(" ".join(corpus_parts))

    keywords = {word for word in re.findall(r"[\w\u4e00-\u9fff]{2,}", query)}
    for keyword in keywords:
        if keyword and keyword in corpus:
            score += 1

    if ("图片" in query or "photo" in query or "image" in query) and tool.name in {
        "browser",
        "desktop_capture",
    }:
        score += 2

    if ("evomap" in query or "evo map" in query) and tool.name.startswith("evomap_"):
        score += 4

    return score


def select_native_tool_names(
    registry: ToolRegistry,
    user_message: str,
) -> set[str]:
    definitions = registry.list_tools()
    available = {definition.name for definition in definitions}
    selected = {name for name in CORE_NATIVE_TOOLS if name in available}
    query = normalize_for_match(user_message)

    for keywords, names in TOOL_POLICY_KEYWORDS.items():
        if any(keyword in query for keyword in keywords):
            for name in names:
                if name in available:
                    selected.add(name)

    if len(selected) >= MAX_NATIVE_TOOLS:
        return selected

    scored: list[tuple[int, str]] = []
    for definition in definitions:
        if definition.name in selected:
            continue
        scored.append((score_tool_relevance(user_message, definition), definition.name))

    scored.sort(key=lambda item: (-item[0], item[1]))
    for score, name in scored:
        if score <= 0:
            break
        selected.add(name)
        if len(selected) >= MAX_NATIVE_TOOLS:
            break

    return selected


__all__ = [
    "build_skill_lock_system_message",
    "build_skill_param_guard_reply",
    "capture_param_value",
    "detect_nano_banana_model_display",
    "detect_assistant_name_update",
    "extract_ratio_or_size",
    "extract_value_by_aliases",
    "format_param_status",
    "guarded_skills",
    "has_param_secret_source",
    "is_nano_banana_activation_message",
    "is_nano_banana_control_message",
    "is_task_done_confirmation",
    "looks_like_skill_activation_message",
    "nano_banana_missing_required",
    "normalize_for_match",
    "normalize_skill_ids",
    "param_satisfied",
    "parse_use_command",
    "persist_param_secret",
    "preview_text",
    "sanitize_assistant_name",
    "score_tool_relevance",
    "select_native_tool_names",
    "sanitize_nano_banana_prompt_value",
    "skill_announcement",
    "skill_explicitly_mentioned",
    "skill_trigger_mentioned",
    "skill_token_mentioned",
    "update_guard_state",
]
