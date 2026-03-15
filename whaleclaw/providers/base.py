"""Abstract base classes for LLM providers."""

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field

from whaleclaw.types import StreamCallback


class CacheControl(BaseModel):
    """Prompt caching hint (Anthropic / Google)."""

    type: Literal["ephemeral"] = "ephemeral"


class ToolSchema(BaseModel):
    """Tool JSON Schema passed to the LLM via native ``tools`` parameter."""

    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    """A single tool-use request from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


class ImageContent(BaseModel):
    """An inline image attached to a message."""

    mime: str
    data: str  # base64-encoded


class Message(BaseModel):
    """A single message in the conversation."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    cache_control: CacheControl | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None
    images: list[ImageContent] | None = None


class AgentResponse(BaseModel):
    """Structured response from an LLM provider."""

    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


def repair_tool_call_pairs(messages: list[Message]) -> list[Message]:
    """Remove invalid native-tool transcripts from a Message list.

    OpenAI-compatible providers are stricter than just "both sides exist":
    an ``assistant`` message with ``tool_calls`` must be followed immediately by
    the corresponding contiguous ``tool`` messages, without interleaved
    ``user``/``assistant`` content.  After trimming, persistence reloads, or
    guard-message injection the list may contain:
    - An assistant message with tool_calls whose IDs have no matching tool result.
    - A tool message whose tool_call_id has no matching assistant tool_calls entry.
    - A valid pair that is no longer contiguous because another message was
      inserted between the assistant tool call and the tool result.

    Both cases cause API 400 errors on all providers.  This function is
    provider-agnostic and should be called in every ``_build_body`` /
    ``_build_responses_body`` before converting messages to the wire format.
    """
    result: list[Message] = []
    idx = 0
    while idx < len(messages):
        msg = messages[idx]

        if msg.role == "tool":
            idx += 1
            continue

        if msg.role != "assistant" or not msg.tool_calls:
            result.append(msg)
            idx += 1
            continue

        expected_ids = {tc.id for tc in msg.tool_calls if tc.id}
        kept_calls = [tc for tc in msg.tool_calls if tc.id in expected_ids]
        seen_ids: set[str] = set()
        paired_tools: list[Message] = []
        lookahead = idx + 1

        while lookahead < len(messages):
            next_msg = messages[lookahead]
            if next_msg.role != "tool" or not next_msg.tool_call_id:
                break
            if next_msg.tool_call_id not in expected_ids or next_msg.tool_call_id in seen_ids:
                break
            paired_tools.append(next_msg)
            seen_ids.add(next_msg.tool_call_id)
            lookahead += 1
            if seen_ids == expected_ids:
                break

        if expected_ids and seen_ids == expected_ids:
            if len(kept_calls) != len(msg.tool_calls):
                result.append(Message(
                    role=msg.role,
                    content=msg.content,
                    tool_calls=kept_calls,
                    tool_call_id=msg.tool_call_id,
                    images=msg.images,
                    cache_control=msg.cache_control,
                ))
            else:
                result.append(msg)
            result.extend(paired_tools)
            idx = lookahead
            continue

        if msg.content:
            result.append(Message(
                role=msg.role,
                content=msg.content,
                tool_calls=None,
                tool_call_id=msg.tool_call_id,
                images=msg.images,
                cache_control=msg.cache_control,
            ))
        idx += 1
    return result


class LLMProvider(ABC):
    """Abstract base for all LLM provider adapters."""

    supports_native_tools: bool = True
    supports_cache_control: bool = False

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        model: str,
        *,
        tools: list[ToolSchema] | None = None,
        on_stream: StreamCallback | None = None,
    ) -> AgentResponse:
        """Send messages and return a complete response.

        Args:
            messages: Conversation history including system prompt.
            model: Model identifier (e.g. ``claude-sonnet-4-20250514``).
            tools: Tool JSON schemas via native API parameter.
            on_stream: Optional callback invoked with each text chunk.
        """
