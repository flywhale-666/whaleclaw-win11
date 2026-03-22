"""Basic agent loop tests: simple replies, retries, streaming, fallback parsing."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from whaleclaw.agent.helpers.tool_execution import is_transient_cli_usage_error
from whaleclaw.agent.loop import _is_image_generation_request, _parse_fallback_tool_calls, run_agent
from whaleclaw.config.schema import WhaleclawConfig
from whaleclaw.providers.base import AgentResponse
from whaleclaw.tools.base import ToolResult

from ._loop_helpers import _make_router, _NameMemoryManager


@pytest.mark.asyncio
async def test_run_agent_returns_reply() -> None:
    mock_response = AgentResponse(
        content="你好！我是 WhaleClaw。",
        model="claude-sonnet-4-20250514",
        input_tokens=50,
        output_tokens=20,
    )

    router = _make_router(response=mock_response)

    result = await run_agent(
        message="你好",
        session_id="test-session",
        config=WhaleclawConfig(),
        router=router,
    )

    assert result == "你好！我是 WhaleClaw。"
    router.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_agent_retries_once_on_empty_reply_then_recovers() -> None:
    call_count = 0

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],  # noqa: ARG001
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AgentResponse(content="", model="test-model", input_tokens=0, output_tokens=0)
        return AgentResponse(content="请告诉我你要我做什么。", model="test-model")

    router = _make_router(chat_fn=fake_chat)
    result = await run_agent(
        message="？？？",
        session_id="test-empty-retry",
        config=WhaleclawConfig(),
        router=router,
    )
    assert result == "请告诉我你要我做什么。"
    assert call_count == 2


@pytest.mark.asyncio
async def test_run_agent_returns_fallback_after_two_empty_replies() -> None:
    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],  # noqa: ARG001
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        return AgentResponse(content="", model="test-model", input_tokens=0, output_tokens=0)

    router = _make_router(chat_fn=fake_chat)
    result = await run_agent(
        message="？？？",
        session_id="test-empty-fallback",
        config=WhaleclawConfig(),
        router=router,
    )
    assert result == "我这边没收到模型有效回复。请再发一次需求，我会继续处理。"


@pytest.mark.asyncio
async def test_run_agent_streams() -> None:
    mock_response = AgentResponse(
        content="Hello world",
        model="claude-sonnet-4-20250514",
        input_tokens=10,
        output_tokens=5,
    )

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,
    ) -> AgentResponse:
        if on_stream:
            await on_stream("Hello ")
            await on_stream("world")
        return mock_response

    router = _make_router(chat_fn=fake_chat)

    chunks: list[str] = []

    async def collect(chunk: str) -> None:
        chunks.append(chunk)

    result = await run_agent(
        message="hi",
        session_id="test-session",
        config=WhaleclawConfig(),
        on_stream=collect,
        router=router,
    )

    assert result == "Hello world"
    assert chunks == ["Hello ", "world"]


def test_is_transient_cli_usage_error_detects_argparse_banner() -> None:
    result = ToolResult(
        success=False,
        output="[stderr]\nusage: test_nano_banana_2.py [-h]\nerror: unrecognized arguments: --bad",
        error="usage: test_nano_banana_2.py [-h]\nerror: unrecognized arguments: --bad",
    )

    assert is_transient_cli_usage_error(result) is True


def test_is_image_generation_request_matches_expected_queries() -> None:
    assert _is_image_generation_request("请帮我文生图，主题是赛博朋克街景") is True
    assert _is_image_generation_request("这张图做图生图，风格改成宫崎骏") is True
    assert _is_image_generation_request("帮我改这个 ppt 第三页文案") is False
    assert _is_image_generation_request("帮我测试一下 API key 是否可用") is False


@pytest.mark.asyncio
async def test_run_agent_updates_assistant_name_from_user_message() -> None:
    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        system_text = messages[0].content if messages else ""
        return AgentResponse(content=system_text, model="test-model")

    cfg = WhaleclawConfig()
    cfg.agent.memory.enabled = False
    mm = _NameMemoryManager()
    router = _make_router(chat_fn=fake_chat)

    result = await run_agent(
        message="以后你叫旺财",
        session_id="test-rename",
        config=cfg,
        router=router,
        memory_manager=mm,  # type: ignore[arg-type]
    )

    assert "你是 旺财" in result
    assert mm.name == "旺财"
    assert mm.set_calls == 1


@pytest.mark.asyncio
async def test_run_agent_does_not_rename_on_plain_name_question() -> None:
    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        system_text = messages[0].content if messages else ""
        return AgentResponse(content=system_text, model="test-model")

    cfg = WhaleclawConfig()
    cfg.agent.memory.enabled = False
    mm = _NameMemoryManager("WhaleClaw")
    router = _make_router(chat_fn=fake_chat)

    result = await run_agent(
        message="你叫什么名字？",
        session_id="test-no-rename",
        config=cfg,
        router=router,
        memory_manager=mm,  # type: ignore[arg-type]
    )

    assert "你是 WhaleClaw" in result
    assert mm.set_calls == 0


class TestParseFallbackToolCalls:
    def test_fenced_json(self) -> None:
        text = '```json\n{"tool": "bash", "arguments": {"command": "ls"}}\n```'
        calls = _parse_fallback_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "bash"
        assert calls[0].arguments == {"command": "ls"}

    def test_bare_json(self) -> None:
        text = '好的，我来执行 {"tool": "bash", "arguments": {"command": "pwd"}}'
        calls = _parse_fallback_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "bash"

    def test_no_tool(self) -> None:
        text = "这是普通文本，没有工具调用。"
        calls = _parse_fallback_tool_calls(text)
        assert calls == []
