"""SkillHooks protocol and default implementation.

Each skill can optionally provide a ``hooks.py`` that exports a
``class Hooks(DefaultSkillHooks)`` overriding only the methods it cares about.
Business code obtains the hooks via ``get_skill_hooks(skill)`` and dispatches
through the generic interface instead of ``if skill_id == "xxx"`` branches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from whaleclaw.providers.base import ImageContent, Message

if TYPE_CHECKING:
    from whaleclaw.providers.base import ToolCall
    from whaleclaw.sessions.manager import Session
    from whaleclaw.skills.parser import Skill, SkillParamItem
    from whaleclaw.tools.base import ToolResult


@dataclass
class StageRule:
    """A conditional rule that injects a system hint when its condition is met."""

    name: str
    condition: Callable[[dict[str, object], str], bool]
    system_hint: str | Callable[..., str] = ""


@runtime_checkable
class SkillHooks(Protocol):
    """Protocol that skill-specific hooks must satisfy."""

    # ── param guard ──────────────────────────────────────────────────

    def build_param_guard_reply(self, state: dict[str, object]) -> str:
        """Custom parameter-guard display copy."""
        ...

    def missing_required(
        self,
        state: dict[str, object],
        *,
        control_message_only: bool,
    ) -> bool:
        """Whether required params are still missing."""
        ...

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
        """Custom guard state update.  Return *None* to fall back to generic."""
        ...

    # ── activation / control ─────────────────────────────────────────

    def is_activation_message(self, message: str) -> bool:
        """Whether the message is a pure activation command."""
        ...

    def is_control_message(self, message: str) -> bool:
        """Whether the message is a pure control/switch command."""
        ...

    def is_execution_request(self, message: str, state: dict[str, object]) -> bool | None:
        """Whether the message is an execution request.  *None* = don't know."""
        ...

    # ── command building ─────────────────────────────────────────────

    def build_command(
        self,
        state: dict[str, object],
        session: Session | None,
    ) -> str:
        """Build the bash command to execute this skill."""
        ...

    def build_execution_system_message(
        self,
        session: Session | None,
        *,
        recommended_command: str = "",
    ) -> Message | None:
        """Execution-phase constraint system message."""
        ...

    def build_command_template_system_message(self, cmd: str) -> Message | None:
        """Command template system message."""
        ...

    # ── reply post-processing ────────────────────────────────────────

    def postprocess_reply(self, text: str, session: Session | None) -> str:
        """Post-process the agent reply before returning to user."""
        ...

    def build_lock_status_extra(self, session: Session | None) -> str:
        """Extra info appended to skill-lock status query reply."""
        ...

    def build_already_locked_reply(self, session: Session | None) -> str:
        """Reply when user sends activation but session is already locked."""
        ...

    def handle_control_message(
        self,
        message: str,
        state: dict[str, object],
        session: Session | None,
    ) -> str | None:
        """Handle a control message directly.  Return reply or *None*."""
        ...

    # ── image buffer ─────────────────────────────────────────────────

    @property
    def image_buffer_enabled(self) -> bool:
        """Whether the Feishu image buffer should be active for this skill."""
        ...

    def image_buffer_hint(self, image_labels: str | list[str]) -> str:
        """Hint text shown during image buffering."""
        ...

    # ── tool guards ──────────────────────────────────────────────────

    def on_tool_failure(self, tc: ToolCall, result: ToolResult) -> list[str]:
        """Custom guard messages on tool failure.  Empty = use default."""
        ...

    def repair_tool_call(self, command: str) -> str:
        """Repair / normalize a bash command before execution."""
        ...

    def on_bash_success(
        self,
        tc: ToolCall,
        result: ToolResult,
        session: Session | None,
    ) -> None:
        """Hook called after a successful bash execution."""
        ...

    # ── tool selection ───────────────────────────────────────────────

    def extra_tool_names(self) -> set[str]:
        """Additional tool names to always include when this skill is active."""
        ...

    def excluded_tool_names(self) -> set[str]:
        """Tool names to exclude when this skill is active."""
        ...

    # ── execution parameters ─────────────────────────────────────────

    @property
    def stage_rules(self) -> list[StageRule] | str:
        """Stage-specific rules.  Return a list of StageRule or a plain string."""
        ...

    @property
    def long_running_script_pattern(self) -> re.Pattern[str] | None:
        """Regex matching long-running script commands for timeout override."""
        ...

    @property
    def long_running_timeout_seconds(self) -> int:
        """Minimum timeout for long-running script commands."""
        ...

    @property
    def parallel_limit(self) -> int:
        """Max parallel bash commands for batch execution."""
        ...

    @property
    def batch_delay_seconds(self) -> float:
        """Delay between batches of parallel commands."""
        ...


class DefaultSkillHooks:
    """Base implementation — every method returns a no-op / pass-through default."""

    def __init__(self, skill: Skill) -> None:
        self.skill = skill

    # ── param guard ──────────────────────────────────────────────────

    def build_param_guard_reply(self, state: dict[str, object]) -> str:
        from whaleclaw.agent.helpers.skill_lock import (
            format_param_status,
            param_satisfied,
        )

        params = self.skill.param_guard.params if self.skill.param_guard else []
        lines = [
            f"我将使用 {self.skill.id} 技能继续完成任务。",
            "",
            "我先确认参数（缺啥补啥）：",
        ]
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

    def missing_required(
        self,
        state: dict[str, object],
        *,
        control_message_only: bool,
    ) -> bool:
        from whaleclaw.agent.helpers.skill_lock import param_satisfied

        params = self.skill.param_guard.params if self.skill.param_guard else []
        return any(
            param.required and not param_satisfied(param, state.get(param.key))
            for param in params
        )

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
        return None

    # ── activation / control ─────────────────────────────────────────

    def is_activation_message(self, message: str) -> bool:
        return False

    def is_control_message(self, message: str) -> bool:
        return False

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
        return ""

    def build_execution_system_message(
        self,
        session: Session | None,
        *,
        recommended_command: str = "",
    ) -> Message | None:
        return None

    def build_command_template_system_message(self, cmd: str) -> Message | None:
        return None

    # ── reply post-processing ────────────────────────────────────────

    def postprocess_reply(self, text: str, session: Session | None) -> str:
        return text

    def build_lock_status_extra(self, session: Session | None) -> str:
        return ""

    def build_already_locked_reply(self, session: Session | None) -> str:
        return ""

    def handle_control_message(
        self,
        message: str,
        state: dict[str, object],
        session: Session | None,
    ) -> str | None:
        return None

    # ── image buffer ─────────────────────────────────────────────────

    @property
    def image_buffer_enabled(self) -> bool:
        return False

    def image_buffer_hint(self, image_labels: str | list[str]) -> str:
        return ""

    # ── tool guards ──────────────────────────────────────────────────

    def on_tool_failure(self, tc: ToolCall, result: ToolResult) -> list[str]:
        return []

    def repair_tool_call(self, command: str) -> str:
        return command

    def on_bash_success(
        self,
        tc: ToolCall,
        result: ToolResult,
        session: Session | None,
    ) -> None:
        return

    # ── tool selection ───────────────────────────────────────────────

    def extra_tool_names(self) -> set[str]:
        return set()

    def excluded_tool_names(self) -> set[str]:
        return set()

    # ── execution parameters ─────────────────────────────────────────

    @property
    def stage_rules(self) -> list[StageRule] | str:
        return ""

    @property
    def long_running_script_pattern(self) -> re.Pattern[str] | None:
        return None

    @property
    def long_running_timeout_seconds(self) -> int:
        return 30

    @property
    def parallel_limit(self) -> int:
        return 1

    @property
    def batch_delay_seconds(self) -> float:
        return 0.0


_hooks_cache: dict[str, DefaultSkillHooks] = {}

_BUILTIN_HOOKS: dict[str, str] = {
    "nano-banana-image-t8": "whaleclaw.skills.nano_banana_hooks",
    "liang-tavily-search": "whaleclaw.skills.tavily_hooks",
}


def get_skill_hooks(skill: Skill) -> DefaultSkillHooks | None:
    """Return the hooks instance for *skill*, or *None* if no custom hooks exist.

    The hooks object is cached by skill id so ``hooks.py`` is imported at most once.
    Priority: skill.hooks attribute > cache > built-in hooks registry.
    """
    hooks_attr: Any = getattr(skill, "hooks", None)
    if hooks_attr is not None:
        return hooks_attr  # type: ignore[return-value]
    cached = _hooks_cache.get(skill.id)
    if cached is not None:
        return cached
    builtin_module = _BUILTIN_HOOKS.get(skill.id)
    if builtin_module is not None and skill.param_guard is not None and skill.param_guard.enabled:
        import importlib

        try:
            mod = importlib.import_module(builtin_module)
            hooks_cls = getattr(mod, "Hooks", None)
            if hooks_cls is not None:
                instance = hooks_cls(skill)
                _hooks_cache[skill.id] = instance
                return instance
        except Exception:
            pass
    return None


def register_skill_hooks(skill_id: str, hooks: DefaultSkillHooks) -> None:
    """Manually register hooks for a skill id (used by SkillManager)."""
    _hooks_cache[skill_id] = hooks
