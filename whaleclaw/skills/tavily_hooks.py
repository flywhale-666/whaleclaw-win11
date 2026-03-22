"""Tavily Search skill hooks — handles tvly- prefixed API keys.

Auto-registered when the liang-tavily-search skill is discovered.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from whaleclaw.providers.base import ImageContent, Message
from whaleclaw.skills.hooks import DefaultSkillHooks

if TYPE_CHECKING:
    from whaleclaw.providers.base import ToolCall
    from whaleclaw.sessions.manager import Session
    from whaleclaw.skills.parser import Skill, SkillParamItem
    from whaleclaw.tools.base import ToolResult

_TAVILY_KEY_RE = re.compile(r"\b(tvly-[A-Za-z0-9_-]{12,})\b")
_TAVILY_SCRIPT_RE = re.compile(r"search\.mjs", re.IGNORECASE)
_TAVILY_ACTIVATION_RE = re.compile(
    r"(?:使用|用|use)\s*tav|tavily\s*(?:搜索|search|技能)",
    re.IGNORECASE,
)
_TAVILY_CONTROL_RE = re.compile(
    r"^(?:换|更换|更新|替换)\s*(?:tavily|tav)\s*(?:key|apikey|api\s*key|密钥)",
    re.IGNORECASE,
)

_CREDENTIALS_FILE = Path("~/.whaleclaw/credentials/tavily_api_key.txt").expanduser()


def _load_saved_key() -> str:
    """Load saved Tavily API key from disk or env."""
    env_val = os.getenv("TAVILY_API_KEY", "").strip()
    if env_val:
        return env_val
    try:
        if _CREDENTIALS_FILE.is_file():
            raw = _CREDENTIALS_FILE.read_bytes()
            if raw[:2] == b"\xff\xfe":
                text = raw.decode("utf-16").strip()
                _CREDENTIALS_FILE.write_text(text, encoding="utf-8")
                return text
            return raw.decode("utf-8").strip()
    except Exception:
        pass
    return ""


def _save_key(key: str) -> None:
    """Persist Tavily API key to disk."""
    _CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CREDENTIALS_FILE.write_text(key.strip(), encoding="utf-8")


def _capture_tavily_key(text: str, previous: object) -> object:
    """Extract tvly- prefixed key from message text."""
    m = _TAVILY_KEY_RE.search(text)
    if m:
        return m.group(1)
    if _load_saved_key():
        return "__present__"
    return previous


def _key_satisfied(value: object) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    return bool(s)


class Hooks(DefaultSkillHooks):
    """Tavily Search skill hooks."""

    # ── param guard ──────────────────────────────────────────────────

    def build_param_guard_reply(self, state: dict[str, object]) -> str:
        has_key = _key_satisfied(state.get("api_key"))
        query = str(state.get("prompt") or "").strip()
        lines = [
            f"我将使用 {self.skill.id} 技能进行搜索。",
            "",
            "参数确认：",
            "1) API Key：已就绪" if has_key else "1) API Key：未提供",
            f"2) 搜索词：{query}" if query else "2) 搜索词：未提供",
        ]
        missing: list[str] = []
        if not has_key:
            missing.append("请提供 Tavily API Key（tvly- 开头，可在 https://tavily.com 获取）")
        if not query:
            missing.append("请提供要搜索的内容")
        lines.append("")
        if missing:
            lines.append("请补充：" + "；".join(missing) + "。")
        else:
            lines.append("参数已齐，我现在开始搜索。")
        return "\n".join(lines)

    def missing_required(
        self,
        state: dict[str, object],
        *,
        control_message_only: bool,
    ) -> bool:
        if not _key_satisfied(state.get("api_key")):
            return True
        query = str(state.get("prompt") or "").strip()
        return not query

    def update_guard_state(
        self,
        state: dict[str, object],
        message: str,
        images: list[ImageContent] | None,
        *,
        params: list[SkillParamItem] | None = None,
        session: Session | None = None,
        has_new_input_images: bool = False,
    ) -> dict[str, object] | None:
        new_state = dict(state)

        prev_key = new_state.get("api_key")
        captured_key = _capture_tavily_key(message, prev_key)
        new_state["api_key"] = captured_key

        if isinstance(captured_key, str) and _TAVILY_KEY_RE.fullmatch(captured_key):
            _save_key(captured_key)

        if not _is_pure_key_message(message):
            stripped = message.strip()
            if (
                stripped
                and len(stripped) >= 2
                and not stripped.startswith("/use ")
                and not _TAVILY_KEY_RE.search(stripped)
                and "技能" not in stripped
                and "tav" not in stripped.lower()
            ):
                new_state["prompt"] = stripped

        return new_state

    # ── activation / control ─────────────────────────────────────────

    def is_activation_message(self, message: str) -> bool:
        return bool(_TAVILY_ACTIVATION_RE.search(message))

    def is_control_message(self, message: str) -> bool:
        return bool(_TAVILY_CONTROL_RE.search(message))

    def is_execution_request(
        self, message: str, state: dict[str, object]
    ) -> bool | None:
        return None

    # ── command building ─────────────────────────────────────────────

    def build_command(
        self,
        state: dict[str, object],
        session: Session | None,
    ) -> str:
        query = str(state.get("prompt") or "").strip()
        if not query:
            return ""
        script = "~/.whaleclaw/workspace/skills/liang-tavily-search/scripts/search.mjs"
        safe_query = query.replace("'", "'\\''")
        return f"TAVILY_API_KEY=\"$(<~/.whaleclaw/credentials/tavily_api_key.txt)\" node {script} '{safe_query}'"

    def build_execution_system_message(
        self,
        session: Session | None,
        *,
        recommended_command: str = "",
    ) -> Message | None:
        cmd = recommended_command or self.build_command({}, session)
        if not cmd:
            return None
        return Message(
            role="system",
            content=(
                "[Tavily Search 执行约束]\n"
                "1. 必须使用技能脚本 search.mjs 执行搜索\n"
                "2. API Key 通过环境变量 TAVILY_API_KEY 传入\n"
                "3. 若已保存 Key，从 ~/.whaleclaw/credentials/tavily_api_key.txt 读取\n"
                "4. 搜索完成后将结果整理回复用户\n"
                f"5. 推荐命令：{cmd}"
            ),
        )

    def build_command_template_system_message(self, cmd: str) -> Message | None:
        return None

    # ── reply post-processing ────────────────────────────────────────

    def build_already_locked_reply(self, session: Session | None) -> str:
        return (
            "当前会话仍在 Tavily 搜索技能里。\n"
            "请直接发送要搜索的内容；"
            '如果本轮已结束，请回复"任务完成"解除技能锁定。'
        )

    # ── tool guards ──────────────────────────────────────────────────

    def on_tool_failure(self, tc: ToolCall, result: ToolResult) -> list[str]:
        raw_command = str(tc.arguments.get("command", ""))
        if tc.name != "bash" or not _TAVILY_SCRIPT_RE.search(raw_command):
            return []
        return [
            "[系统提示] 搜索失败。请检查 API Key 是否正确、网络是否可用。"
            "若为参数错误可修正后重试 1 次。"
        ]

    def repair_tool_call(self, command: str) -> str:
        return command

    # ── tool selection ───────────────────────────────────────────────

    def extra_tool_names(self) -> set[str]:
        return {"bash"}

    def excluded_tool_names(self) -> set[str]:
        return {"file_write", "file_edit", "patch_apply"}


def _is_pure_key_message(text: str) -> bool:
    """Return True if the message is just an API key with no other content."""
    stripped = text.strip()
    return bool(_TAVILY_KEY_RE.fullmatch(stripped))
