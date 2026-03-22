"""Agent loop tests: circuit breaker, tool repair, PPT edit, file_edit rejection."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

import whaleclaw.agent.loop as loop_mod
from whaleclaw.agent.loop import run_agent
from whaleclaw.config.schema import WhaleclawConfig
from whaleclaw.providers.base import AgentResponse, Message, ToolCall
from whaleclaw.sessions.manager import Session
from whaleclaw.tools.base import Tool, ToolDefinition, ToolParameter, ToolResult
from whaleclaw.tools.registry import ToolRegistry

from ._loop_helpers import (
    _BashAlwaysFailTool,
    _BashProbeTool,
    _BrowserAlwaysFailTool,
    _BrowserProbeTool,
    _NanoBananaFixedRunnerTool,
    _PptEditBusinessNoHitTool,
    _PptEditNoopTool,
    _fixed_route,
    _make_router,
)
from whaleclaw.skills.parser import Skill, SkillParamGuard, SkillParamItem


@pytest.mark.asyncio
async def test_run_agent_circuit_breaker_blocks_repeated_browser_failures() -> None:
    browser_tool_response = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[
            ToolCall(
                id="tc_browser",
                name="browser",
                arguments={"action": "search_images", "text": "杨幂近照"},
            )
        ],
    )
    final_response = AgentResponse(
        content="改用 bash 处理",
        model="test-model",
    )

    call_count = 0
    prompts_seen: list[str] = []

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        nonlocal call_count
        call_count += 1
        prompts_seen.append("\n".join(m.content for m in messages if hasattr(m, "content")))
        # 熔断阈值为 3：前 3 轮继续返回 browser 调用，第 4 轮才收到熔断提示并回复最终文案
        if call_count <= 3:
            return browser_tool_response
        return final_response

    router = _make_router(chat_fn=fake_chat)
    registry = ToolRegistry()
    registry.register(_BrowserAlwaysFailTool())

    result = await run_agent(
        message="给我张杨幂近照",
        session_id="test-browser-circuit",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert result == "改用 bash 处理"
    assert call_count == 4
    assert any("browser 工具连续失败，已自动熔断" in p for p in prompts_seen)


@pytest.mark.asyncio
async def test_run_agent_keeps_tool_result_adjacent_to_native_tool_call() -> None:
    browser_tool_response = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[
            ToolCall(
                id="tc_browser",
                name="browser",
                arguments={"action": "search_images", "text": "杨幂近照"},
            )
        ],
    )
    final_response = AgentResponse(
        content="改用 bash 处理",
        model="test-model",
    )

    call_count = 0
    fourth_call_messages: list[Message] = []

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        nonlocal call_count, fourth_call_messages
        call_count += 1
        # 第 4 轮才因熔断而收到最终回复
        if call_count == 4:
            fourth_call_messages = cast(list[Message], messages)
            return final_response
        return browser_tool_response

    router = _make_router(chat_fn=fake_chat)
    registry = ToolRegistry()
    registry.register(_BrowserAlwaysFailTool())

    result = await run_agent(
        message="给我张杨幂近照",
        session_id="test-browser-tool-order",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert result == "改用 bash 处理"
    assert call_count == 4
    assert fourth_call_messages
    assistant_idx = max(
        idx
        for idx, msg in enumerate(fourth_call_messages)
        if msg.role == "assistant" and msg.tool_calls
    )
    assert fourth_call_messages[assistant_idx + 1].role == "tool"
    assert fourth_call_messages[assistant_idx + 1].tool_call_id == "tc_browser"
    assert any(
        msg.role == "user" and "browser 工具连续失败，已自动熔断" in msg.content
        for msg in fourth_call_messages[assistant_idx + 2:]
    )


@pytest.mark.asyncio
async def test_run_agent_circuit_breaker_blocks_repeated_bash_failures() -> None:
    bash_tool_response = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[
            ToolCall(
                id="tc_bash",
                name="bash",
                arguments={"command": "python3 /tmp/a.py"},
            )
        ],
    )
    final_response = AgentResponse(
        content="改用 ppt_edit 处理",
        model="test-model",
    )

    call_count = 0
    prompts_seen: list[str] = []

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        nonlocal call_count
        call_count += 1
        prompts_seen.append("\n".join(m.content for m in messages if hasattr(m, "content")))
        if call_count <= 3:
            return bash_tool_response
        return final_response

    router = _make_router(chat_fn=fake_chat)
    registry = ToolRegistry()
    registry.register(_BashAlwaysFailTool())

    result = await run_agent(
        message="给第二页配图",
        session_id="test-bash-circuit",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert result == "改用 ppt_edit 处理"
    assert call_count == 4
    assert any("同一 bash 命令模板已连续失败 3 次" in p for p in prompts_seen)


@pytest.mark.asyncio
async def test_run_agent_includes_ppt_edit_for_followup_office_message() -> None:
    captured_tool_names: list[str] = []

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],  # noqa: ARG001
        *,
        tools: list[object] | None = None,
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        if tools is not None:
            for t in tools:
                if hasattr(t, "name"):
                    name = str(getattr(t, "name", "")).strip()
                    if name:
                        captured_tool_names.append(name)
                    continue
                if isinstance(t, dict):
                    td = cast(dict[str, object], t)
                    name = str(td.get("name", "")).strip()
                    func = td.get("function")
                    if not name and isinstance(func, dict):
                        fd = cast(dict[str, object], func)
                        name = str(fd.get("name", "")).strip()
                    if name:
                        captured_tool_names.append(name)
        return AgentResponse(content="收到", model="test-model")

    router = _make_router(chat_fn=fake_chat)
    registry = ToolRegistry()
    registry.register(_BashProbeTool())
    registry.register(_PptEditNoopTool())

    now = datetime.now(UTC)
    session = Session(
        id="s-followup-office",
        channel="feishu",
        peer_id="u1",
        messages=[],
        model="anthropic/claude-sonnet-4-20250514",
        created_at=now,
        updated_at=now,
        metadata={"last_pptx_path": "/tmp/贵州2日游.pptx"},
    )

    result = await run_agent(
        message="第一页的黑色条不好看，换种格式",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
        session=session,
    )

    assert result == "收到"
    assert "ppt_edit" in captured_tool_names


@pytest.mark.asyncio
async def test_run_agent_requires_dark_bar_target_hit_for_ppt_edit() -> None:
    first = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[
            ToolCall(
                id="tc_ppt",
                name="ppt_edit",
                arguments={
                    "path": "/tmp/a.pptx",
                    "slide_index": 1,
                    "action": "apply_business_style",
                },
            )
        ],
    )
    second = AgentResponse(content="继续处理", model="test-model")
    call_count = 0
    prompts_seen: list[str] = []

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        nonlocal call_count
        call_count += 1
        prompts_seen.append("\n".join(m.content for m in messages if hasattr(m, "content")))
        if call_count == 1:
            return first
        return second

    router = _make_router(chat_fn=fake_chat)
    registry = ToolRegistry()
    registry.register(_PptEditBusinessNoHitTool())

    now = datetime.now(UTC)
    session = Session(
        id="s-dark-bar",
        channel="feishu",
        peer_id="u1",
        messages=[],
        model="anthropic/claude-sonnet-4-20250514",
        created_at=now,
        updated_at=now,
        metadata={"last_pptx_path": "/tmp/a.pptx"},
    )

    result = await run_agent(
        message="第一页封面的黑色横条不好看，换一种方式",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
        session=session,
    )

    assert result == "继续处理"
    assert any("未命中用户指定对象：黑色横条仍未被替换" in p for p in prompts_seen)


@pytest.mark.asyncio
async def test_run_agent_repairs_browser_query_without_action() -> None:
    tool_response = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[ToolCall(id="tc_browser", name="browser", arguments={"query": "杨幂近照"})],
    )
    final_response = AgentResponse(content="已完成", model="test-model")

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
    registry.register(_BrowserProbeTool())

    result = await run_agent(
        message="给我张杨幂近照",
        session_id="test-browser-repair-query",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert result == "已完成"
    assert call_count == 2


@pytest.mark.asyncio
async def test_run_agent_repairs_bash_cmd_alias() -> None:
    tool_response = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[ToolCall(id="tc_bash", name="bash", arguments={"cmd": "echo hi"})],
    )
    final_response = AgentResponse(content="done", model="test-model")

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
    registry.register(_BashProbeTool())

    result = await run_agent(
        message="执行命令",
        session_id="test-bash-repair-cmd",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert result.endswith("done")
    assert call_count == 2


@pytest.mark.asyncio
async def test_run_agent_uses_fixed_nano_banana_command_when_params_are_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_key = tmp_path / "nano_banana_api_key.txt"
    saved_key.write_text("sk-test-resolved-key", encoding="utf-8")
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["香蕉生图"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(key="api_key", type="api_key", required=True, saved_file=str(saved_key)),
                SkillParamItem(key="prompt", type="text", required=True),
                SkillParamItem(key="images", type="images", required=False, min_count=1),
            ],
        ),
        source_path=Path("/tmp/nano_fixed_runner.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    image_path = tmp_path / "input.png"
    image_path.write_bytes(b"image")
    output_path = tmp_path / "image_to_image.png"
    output_path.write_bytes(b"out")

    router = _make_router(response=AgentResponse(content="不应调用", model="test-model"))
    registry = ToolRegistry()
    bash_tool = _NanoBananaFixedRunnerTool(output_path)
    registry.register(bash_tool)

    now = datetime.now(UTC)
    session = Session(
        id="s-nano-fixed-runner",
        channel="feishu",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={
            "locked_skill_ids": ["nano-banana-image-t8"],
            "skill_param_state": {
                "nano-banana-image-t8": {
                    "api_key": "__present__",
                    "prompt": (
                        "把这张图改成天使翅膀\n\n"
                        f"(用户发送了图片)\n![飞书图片1]({image_path})"
                    ),
                    "images": 1,
                    "__model_display__": "香蕉2",
                }
            },
        },
    )

    result = await run_agent(
        message=f"把这张图改成天使翅膀\n\n(用户发送了图片)\n![飞书图片1]({image_path})",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
        session=session,
    )

    assert "当前使用模型：香蕉2" in result
    assert str(output_path) in result
    assert len(bash_tool.commands) == 1
    assert "--mode edit" in bash_tool.commands[0]
    assert f"--input-image '{image_path}'" in bash_tool.commands[0]
    assert "sk-test-resolved-key" in bash_tool.commands[0]
    assert "__present__" not in bash_tool.commands[0]
    router.chat.assert_not_called()


@pytest.mark.asyncio
async def test_run_agent_repairs_garbled_browser_query_to_user_message() -> None:
    tool_response = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[
            ToolCall(
                id="tc_browser",
                name="browser",
                arguments={"action": "search_images", "text": "2026 \\n0\\n0\\n0\\n0"},
            )
        ],
    )
    final_response = AgentResponse(content="ok", model="test-model")

    call_count = 0
    captured: list[str] = []

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

    class _BrowserCaptureTool(Tool):
        @property
        def definition(self) -> ToolDefinition:
            return ToolDefinition(
                name="browser",
                description="capture",
                parameters=[
                    ToolParameter(name="action", type="string", description="action"),
                    ToolParameter(name="text", type="string", description="text"),
                ],
            )

        async def execute(self, **kwargs: Any) -> ToolResult:
            captured.append(str(kwargs.get("text", "")))
            return ToolResult(success=True, output="ok")

    router = _make_router(chat_fn=fake_chat)
    registry = ToolRegistry()
    registry.register(_BrowserCaptureTool())

    result = await run_agent(
        message="给我杨幂新年写真高清图",
        session_id="test-browser-repair-garbled",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert result == "ok"
    assert call_count == 2
    assert captured and captured[0] == "给我杨幂新年写真高清图"


@pytest.mark.asyncio
async def test_run_agent_rejects_escaped_block_file_edit_args() -> None:
    bad_file_edit = AgentResponse(
        content="",
        model="test-model",
        tool_calls=[
            ToolCall(
                id="tc_edit",
                name="file_edit",
                arguments={
                    "path": "/tmp/a.py",
                    "old_string": "line1\\nline2\\nline3\\nline4",
                    "new_string": "x\\ny\\nz\\nw",
                },
            )
        ],
    )
    final_response = AgentResponse(content="我改用 file_write 重写脚本", model="test-model")

    call_count = 0
    tool_called = False

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
            return bad_file_edit
        return final_response

    class _FileEditProbeTool(Tool):
        @property
        def definition(self) -> ToolDefinition:
            return ToolDefinition(
                name="file_edit",
                description="probe file_edit",
                parameters=[
                    ToolParameter(name="path", type="string", description="path"),
                    ToolParameter(name="old_string", type="string", description="old"),
                    ToolParameter(name="new_string", type="string", description="new"),
                ],
            )

        async def execute(self, **kwargs: Any) -> ToolResult:  # noqa: ARG002
            nonlocal tool_called
            tool_called = True
            return ToolResult(success=True, output="edited")

    router = _make_router(chat_fn=fake_chat)
    registry = ToolRegistry()
    registry.register(_FileEditProbeTool())

    result = await run_agent(
        message="重做这个 python 脚本",
        session_id="test-file-edit-escaped-block",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert result == "我改用 file_write 重写脚本"
    assert call_count == 2
    assert not tool_called
