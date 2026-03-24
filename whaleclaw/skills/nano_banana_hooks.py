"""Nano Banana skill hooks — encapsulates all NB-specific logic.

This module is auto-registered when the nano-banana-image-t8 skill is discovered.
It can also be loaded as a skill-directory ``hooks.py`` if the user installs
the skill from a repo that ships this file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from whaleclaw.agent.helpers.skill_lock import (
    detect_nano_banana_base_url_switch,
    detect_nano_banana_model_display,
    extract_ratio_or_size,
    format_param_status,
    is_nano_banana_activation_message,
    is_nano_banana_control_message,
    load_saved_nano_banana_base_url,
    load_saved_nano_banana_model_display,
    param_satisfied,
    sanitize_nano_banana_prompt_value,
    save_nano_banana_base_url,
)
from whaleclaw.providers.base import ImageContent, Message
from whaleclaw.skills.hooks import DefaultSkillHooks

if TYPE_CHECKING:
    from whaleclaw.providers.base import ToolCall
    from whaleclaw.sessions.manager import Session
    from whaleclaw.skills.parser import Skill, SkillParamItem
    from whaleclaw.tools.base import ToolResult

_NANO_BANANA_SCRIPT_RE = re.compile(r"test_nano_banana", re.IGNORECASE)

_NANO_BANANA_BASH_RE = re.compile(r"test_nano_banana", re.IGNORECASE)


class Hooks(DefaultSkillHooks):
    """Nano Banana Image T8 skill hooks."""

    # ── param guard ──────────────────────────────────────────────────

    def build_param_guard_reply(self, state: dict[str, object]) -> str:
        from whaleclaw.agent.helpers.skill_lock import SkillParamItem as SPI

        current_model = str(
            state.get("__model_display__", load_saved_nano_banana_model_display())
        ).strip() or "香蕉2"
        if current_model == "香蕉pro":
            model_line = "2) 当前模型：香蕉pro（0.2元）可切换模型香蕉2（0.1元）"
        else:
            model_line = "2) 当前模型：香蕉2（0.1元）可切换模型香蕉pro（0.2元）"

        current_base_url = str(
            state.get("__base_url__", load_saved_nano_banana_base_url())
        ).strip() or load_saved_nano_banana_base_url()
        base_url_line = f"3) API 地址：{current_base_url}"

        prompt_value = sanitize_nano_banana_prompt_value(state.get("prompt"))
        prompt_status = (
            "4) 提示词：已收到"
            if param_satisfied(SPI(key="prompt", type="text"), prompt_value)
            else "4) 提示词：未提供"
        )
        image_status = format_param_status(
            SPI(key="images", label="图生图图片", type="images", required=False, min_count=1),
            state.get("images"),
        )

        has_key = param_satisfied(SPI(key="api_key", type="api_key"), state.get("api_key"))
        has_prompt = param_satisfied(SPI(key="prompt", type="text"), prompt_value)

        if state.get("__api_key_just_saved__"):
            return "API Key 已保存。"

        lines = [
            f"我将使用 {self.skill.id} 技能继续完成任务。",
            "",
            "我先确认参数（缺啥补啥）：",
            (
                "1) API Key：已就绪"
                if has_key
                else "1) API Key：未提供"
            ),
            model_line,
            base_url_line,
            prompt_status,
            f"5) {image_status}",
            "6) 切换本次模型：切换香蕉2（pro）。设置默认模型：默认模型香蕉2（pro）",
        ]
        missing_prompts: list[str] = []
        if not has_key:
            missing_prompts.append("请提供 Nano Banana API Key")
        if not has_prompt:
            missing_prompts.append("请提供提示词")
        if missing_prompts:
            lines.append("请补充：" + "；".join(missing_prompts) + "。")
        else:
            lines.append("参数已齐，我现在开始执行。")
        return "\n".join(lines)

    def missing_required(
        self,
        state: dict[str, object],
        *,
        control_message_only: bool,
    ) -> bool:
        from whaleclaw.agent.helpers.skill_lock import SkillParamItem as SPI

        if state.get("__api_key_just_saved__"):
            return True

        param_keys = {p.key for p in (self.skill.param_guard.params if self.skill.param_guard else [])}
        if "api_key" in param_keys:
            has_key = param_satisfied(SPI(key="api_key", type="api_key"), state.get("api_key"))
            if not has_key:
                return True
        if "prompt" in param_keys:
            prompt_value = sanitize_nano_banana_prompt_value(state.get("prompt"))
            has_prompt = param_satisfied(SPI(key="prompt", type="text"), prompt_value)
            if not has_prompt:
                return True
        if "images" in param_keys:
            raw_images = state.get("images")
            image_count = int(raw_images) if isinstance(raw_images, int) else 0
            if image_count < 1 and control_message_only:
                return True
        return False

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
        from whaleclaw.agent.helpers.skill_lock import (
            capture_param_value,
            persist_param_secret,
        )

        if params is None:
            params = self.skill.param_guard.params if self.skill.param_guard else []
        new_state = dict(state)
        control_message_only = is_nano_banana_control_message(message)
        param_keys = {p.key for p in params}

        _user_sent_key = bool(re.search(r"\b(?:sk|tvly)-[A-Za-z0-9_-]{12,}\b", message))
        for param in params:
            prev = new_state.get(param.key)
            captured = capture_param_value(param, message, images, prev)
            new_state[param.key] = captured
            persist_param_secret(param, captured)

        if _user_sent_key:
            new_state["__api_key_just_saved__"] = True
        else:
            new_state.pop("__api_key_just_saved__", None)

        if control_message_only and "prompt" in param_keys and "prompt" in new_state:
            new_state["prompt"] = state.get("prompt")

        previous_model = str(
            state.get("__model_display__", load_saved_nano_banana_model_display())
        )
        new_state["__model_display__"] = detect_nano_banana_model_display(
            message, previous=previous_model
        )

        previous_base_url = str(
            state.get("__base_url__", load_saved_nano_banana_base_url())
        )
        switched_url = detect_nano_banana_base_url_switch(message)
        if switched_url:
            save_nano_banana_base_url(switched_url)
        new_state["__base_url__"] = switched_url or previous_base_url

        ratio = extract_ratio_or_size(message)
        if ratio:
            new_state["ratio"] = ratio

        return new_state

    # ── activation / control ─────────────────────────────────────────

    def is_activation_message(self, message: str) -> bool:
        return is_nano_banana_activation_message(message)

    def is_control_message(self, message: str) -> bool:
        return is_nano_banana_control_message(message)

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
        from whaleclaw.agent.helpers.image_utils import (
            _build_nano_banana_command,
            _recover_last_nano_banana_mode,
            _resolve_nano_banana_input_paths,
        )

        mode = _recover_last_nano_banana_mode(session) or "text"
        model_display = str(
            state.get("__model_display__", load_saved_nano_banana_model_display())
        ).strip() or "香蕉2"
        prompt = str(state.get("prompt", "")).strip()
        ratio = str(state.get("ratio", "auto")).strip() or "auto"
        effective_base_url = str(
            state.get("__base_url__", load_saved_nano_banana_base_url())
        ).strip()
        input_paths: list[str] = []
        if mode == "edit":
            input_paths = _resolve_nano_banana_input_paths("", session)
        return _build_nano_banana_command(
            mode=mode,
            model_display=model_display,
            prompt=prompt,
            input_paths=input_paths,
            ratio=ratio,
            base_url=effective_base_url,
        )

    def build_execution_system_message(
        self,
        session: Session | None,
        *,
        recommended_command: str,
    ) -> Message | None:
        from whaleclaw.agent.helpers.image_utils import (
            _recover_recent_session_image_paths,
        )
        from whaleclaw.agent.helpers.skill_lock import (
            build_nano_banana_execution_system_message,
        )

        current_model = load_saved_nano_banana_model_display()
        if session is not None:
            metadata = session.metadata if isinstance(session.metadata, dict) else {}
            state_map_raw = metadata.get("skill_param_state", {})
            if isinstance(state_map_raw, dict):
                nano_state = state_map_raw.get(self.skill.id)
                if isinstance(nano_state, dict):
                    current_model = str(
                        nano_state.get("__model_display__", current_model)
                    ).strip() or current_model

        recent_paths = _recover_recent_session_image_paths(session)
        return build_nano_banana_execution_system_message(
            current_model, recent_paths, recommended_command=recommended_command
        )

    def build_command_template_system_message(self, cmd: str) -> Message | None:
        return None

    # ── reply post-processing ────────────────────────────────────────

    def build_already_locked_reply(self, session: Session | None) -> str:
        model_display = load_saved_nano_banana_model_display()
        if session is not None:
            metadata = session.metadata if isinstance(session.metadata, dict) else {}
            state_map_raw = metadata.get("skill_param_state", {})
            if isinstance(state_map_raw, dict):
                nano_state = state_map_raw.get(self.skill.id)
                if isinstance(nano_state, dict):
                    model_display = str(
                        nano_state.get("__model_display__", model_display)
                    ).strip() or model_display
        return (
            "当前会话仍在香蕉生图技能里。"
            f"当前模型：{model_display}。\n"
            "如果要继续生图，请直接发送提示词或图片；"
            '如果本轮已结束，请回复"任务完成"解除技能锁定。'
        )

    def handle_control_message(
        self,
        message: str,
        state: dict[str, object],
        session: Session | None,
    ) -> str | None:
        switched_base_url = detect_nano_banana_base_url_switch(message)
        if switched_base_url:
            save_nano_banana_base_url(switched_base_url)
            return f"切换成功，当前 API 地址：{switched_base_url}"
        model_display = str(
            state.get("__model_display__", load_saved_nano_banana_model_display())
        ).strip() or load_saved_nano_banana_model_display()
        return f"切换成功，当前模型：{model_display}"

    # ── image buffer ─────────────────────────────────────────────────

    @property
    def image_buffer_enabled(self) -> bool:
        return True

    def image_buffer_hint(self, labels: list[str]) -> str:
        return ""

    # ── tool guards ──────────────────────────────────────────────────

    def on_tool_failure(self, tc: ToolCall, result: ToolResult) -> list[str]:
        raw_command = str(tc.arguments.get("command", ""))
        if tc.name != "bash" or not _NANO_BANANA_BASH_RE.search(raw_command):
            return []
        return [
            "[系统提示] 本次生图失败，禁止自动重试。"
            "若为参数或预检错误，可修正后重试 1 次；禁止原样重试。"
            "请继续执行剩余的生图任务（如用户要求生成多张图，跳过本张继续生成后续图片）。"
            "所有任务完成后，将成功和失败的结果一并回复用户，由用户决定是否对失败的图重新操作。"
        ]

    def repair_tool_call(self, command: str) -> str:
        from whaleclaw.agent.helpers.tool_execution import _normalize_nano_banana_command

        return _normalize_nano_banana_command(command)

    # ── tool selection ───────────────────────────────────────────────

    def extra_tool_names(self) -> set[str]:
        return {"bash"}

    def excluded_tool_names(self) -> set[str]:
        return {"file_read", "file_write", "file_edit", "patch_apply"}

    # ── execution parameters ─────────────────────────────────────────

    @property
    def long_running_script_pattern(self) -> re.Pattern[str] | None:
        return _NANO_BANANA_SCRIPT_RE

    @property
    def long_running_timeout_seconds(self) -> int:
        return 300

    @property
    def parallel_limit(self) -> int:
        return 5

    @property
    def batch_delay_seconds(self) -> float:
        return 1.5
