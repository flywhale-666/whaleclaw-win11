"""MiniMax (海螺 AI) provider adapter (OpenAI-compatible).

MiniMax OpenAI-compatible API quirks:
- Base URL is ``https://api.minimaxi.com/v1``
- ``temperature`` must be in (0.0, 1.0] — 0 is rejected
- ``assistant`` message ``content`` must be a string, not null
- Image/audio inputs are NOT supported
- Streaming requires ``stream_options.include_usage=true`` to get token counts
- Stream usage may only contain ``total_tokens`` without the prompt/completion split
"""

from __future__ import annotations

from typing import Any, cast

from whaleclaw.providers.base import Message, ToolSchema
from whaleclaw.providers.openai_compat import OpenAICompatProvider


class MiniMaxProvider(OpenAICompatProvider):
    """MiniMax API (MiniMax-M2.7, MiniMax-M2.5, etc.)."""

    provider_name = "minimax"
    default_base_url = "https://api.minimaxi.com/v1"
    env_key = "MINIMAX_API_KEY"

    def _build_body(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolSchema] | None,
    ) -> dict[str, Any]:
        body = super()._build_body(messages, model, tools)

        raw_msgs: list[dict[str, Any]] = body["messages"]
        merged: list[dict[str, Any]] = []
        for msg in raw_msgs:
            if msg.get("role") == "assistant" and msg.get("content") is None:
                msg["content"] = ""

            raw_content = msg.get("content")
            if msg.get("role") == "user" and isinstance(raw_content, list):
                content_parts = cast(list[dict[str, Any]], raw_content)
                text_parts: list[str] = []
                for part in content_parts:
                    if part.get("type") == "text":
                        text_parts.append(str(part.get("text", "")))
                msg["content"] = "\n".join(text_parts) if text_parts else ""

            if (
                merged
                and msg.get("role") == merged[-1].get("role")
                and msg["role"] in ("system", "user")
                and not msg.get("tool_calls")
                and isinstance(msg.get("content"), str)
                and isinstance(merged[-1].get("content"), str)
            ):
                merged[-1]["content"] += "\n\n" + msg["content"]
            else:
                merged.append(msg)
        body["messages"] = merged

        if body.get("stream"):
            body["stream_options"] = {"include_usage": True}

        return body
