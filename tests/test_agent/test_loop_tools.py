"""Agent loop tests: tool call loop, unknown tools, fallback mode, arg repair."""

from __future__ import annotations

from typing import Any

import pytest

from whaleclaw.agent.loop import run_agent
from whaleclaw.config.schema import WhaleclawConfig
from whaleclaw.providers.base import AgentResponse, ToolCall
from whaleclaw.tools.base import ToolResult
from whaleclaw.tools.registry import ToolRegistry

from ._loop_helpers import (
    _BashProbeTool,
    _BashPyScriptRetryTool,
    _BrowserProbeTool,
    _EchoTool,
    _make_router,
)


@pytest.mark.asyncio
async def test_run_agent_tool_call_loop() -> None:
    """Agent should execute tools and loop back to LLM."""
    tool_response = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[
            ToolCall(id="tc_1", name="echo", arguments={"text": "hello"})
        ],
    )
    final_response = AgentResponse(
        content="Echo result: hello",
        model="test-model",
    )

    call_count = 0

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return tool_response
        return final_response

    router = _make_router(chat_fn=fake_chat)

    registry = ToolRegistry()
    registry.register(_EchoTool())

    tool_calls_seen: list[str] = []
    tool_results_seen: list[bool] = []

    async def on_tc(name: str, _args: dict[str, Any]) -> None:
        tool_calls_seen.append(name)

    async def on_tr(name: str, result: ToolResult) -> None:
        tool_results_seen.append(result.success)

    result = await run_agent(
        message="echo hello",
        session_id="test-tool",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
        on_tool_call=on_tc,
        on_tool_result=on_tr,
    )

    assert result == "Echo result: hello"
    assert call_count == 2
    assert tool_calls_seen == ["echo"]
    assert tool_results_seen == [True]


@pytest.mark.asyncio
async def test_run_agent_unknown_tool() -> None:
    """Unknown tool should not crash, returns error to LLM."""
    tool_response = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[
            ToolCall(id="tc_bad", name="nonexistent", arguments={})
        ],
    )
    final_response = AgentResponse(
        content="I could not find that tool.",
        model="test-model",
    )

    call_count = 0

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return tool_response
        return final_response

    router = _make_router(chat_fn=fake_chat)
    registry = ToolRegistry()

    result = await run_agent(
        message="do something",
        session_id="test-unknown",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert result == "I could not find that tool."
    assert call_count == 2


@pytest.mark.asyncio
async def test_run_agent_fallback_mode() -> None:
    """Provider without native tools: parse JSON from text output."""
    json_text = (
        '我来查一下。\n'
        '```json\n'
        '{"tool": "echo", "arguments": {"text": "hello"}}\n'
        '```'
    )
    tool_response = AgentResponse(
        content=json_text,
        model="test-model",
    )
    final_response = AgentResponse(
        content="查到了: hello",
        model="test-model",
    )

    call_count = 0

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return tool_response
        return final_response

    router = _make_router(chat_fn=fake_chat, native_tools=False)

    registry = ToolRegistry()
    registry.register(_EchoTool())

    result = await run_agent(
        message="echo hello",
        session_id="test-fallback",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert result == "查到了: hello"
    assert call_count == 2


@pytest.mark.asyncio
async def test_run_agent_retries_when_tool_args_invalid_then_succeeds() -> None:
    invalid_tool_response = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[ToolCall(id="tc_browser", name="browser", arguments={})],
    )
    valid_tool_response = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[
            ToolCall(
                id="tc_browser_2",
                name="browser",
                arguments={"action": "search_images", "text": "杨幂近照"},
            )
        ],
    )
    final_response = AgentResponse(
        content="已完成",
        model="test-model",
    )

    call_count = 0

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return invalid_tool_response
        if call_count == 2:
            return valid_tool_response
        return final_response

    router = _make_router(chat_fn=fake_chat)
    registry = ToolRegistry()
    registry.register(_BrowserProbeTool())

    result = await run_agent(
        message="给我张杨幂近照",
        session_id="test-browser-repair",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert result == "已完成"
    assert call_count == 3


@pytest.mark.asyncio
async def test_run_agent_retries_direct_python_script_bash_invocation() -> None:
    tool_response = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[
            ToolCall(
                id="tc_bash",
                name="bash",
                arguments={"command": "/tmp/test_nano_banana_2.py --mode edit"},
            )
        ],
    )
    final_response = AgentResponse(content="done", model="test-model")

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
            return tool_response
        return final_response

    router = _make_router(chat_fn=fake_chat)
    registry = ToolRegistry()
    bash_tool = _BashPyScriptRetryTool()
    registry.register(bash_tool)

    result = await run_agent(
        message="执行 nano banana 图生图",
        session_id="test-bash-retry-py-script",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert result.endswith("done")
    assert len(bash_tool.commands) == 2
    assert bash_tool.commands[0] == "/tmp/test_nano_banana_2.py --mode edit"
    rewritten = bash_tool.commands[1]
    assert "/tmp/test_nano_banana_2.py --mode edit" in rewritten
    assert "python" in rewritten.lower()
