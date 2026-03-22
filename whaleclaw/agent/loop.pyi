"""Type stub for loop.py — re-exports from single_agent at runtime via sys.modules."""

from whaleclaw.agent.single_agent import (
    OnRoundResult as OnRoundResult,
    _SKILL_ACTIVATION_PATTERNS as _SKILL_ACTIVATION_PATTERNS,
    _TASK_DONE_PATTERNS as _TASK_DONE_PATTERNS,
    _assembler as _assembler,
    _build_nano_banana_command as _build_nano_banana_command,
    _execute_tool as _execute_tool,
    _is_image_generation_request as _is_image_generation_request,
    _parse_fallback_tool_calls as _parse_fallback_tool_calls,
    run_agent as run_agent,
)

__all__ = [
    "OnRoundResult",
    "_SKILL_ACTIVATION_PATTERNS",
    "_TASK_DONE_PATTERNS",
    "_assembler",
    "_build_nano_banana_command",
    "_execute_tool",
    "_is_image_generation_request",
    "_parse_fallback_tool_calls",
    "run_agent",
]
