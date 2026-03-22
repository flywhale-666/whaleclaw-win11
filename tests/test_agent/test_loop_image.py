"""Agent loop tests: image reuse for locked skills, regenerate, dedup, loop breaking."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import whaleclaw.agent.loop as loop_mod
from whaleclaw.agent.loop import run_agent
from whaleclaw.config.schema import WhaleclawConfig
from whaleclaw.providers.base import AgentResponse, Message, ToolCall
from whaleclaw.sessions.manager import Session
from whaleclaw.skills.parser import Skill, SkillParamGuard, SkillParamItem
from whaleclaw.tools.base import ToolResult
from whaleclaw.tools.registry import ToolRegistry

from ._loop_helpers import (
    _BrowserProbeTool,
    _LoopTool,
    _fixed_route,
    _make_router,
)


@pytest.mark.asyncio
async def test_run_agent_reuses_recent_session_images_for_locked_image_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["nanobanana"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="images",
                    label="图片",
                    type="images",
                    required=True,
                    prompt="请上传图片",
                ),
            ],
        ),
        source_path=Path("/tmp/nano_guard_images.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    image_path = tmp_path / "ref.png"
    image_path.write_bytes(b"png-bytes")
    previous_user_message = Message(
        role="user",
        content=f"(用户发送了图片)\n![飞书图片1]({image_path})",
    )

    seen_user_images: list[Any] = []

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        for item in messages:
            if getattr(item, "role", "") == "user":
                seen_user_images.append(getattr(item, "images", None))
        return AgentResponse(content="开始图生图", model="test-model")

    router = _make_router(chat_fn=fake_chat)
    now = datetime.now(UTC)
    session = Session(
        id="s-nano-guard-images-1",
        channel="feishu",
        peer_id="u1",
        messages=[previous_user_message],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={"locked_skill_ids": ["nano-banana-image-t8"]},
    )

    result = await run_agent(
        message="用 nano banana 处理这张图",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "开始图生图" in result
    assert any(images and len(images) == 1 for images in seen_user_images)
    last_non_empty = next(images for images in reversed(seen_user_images) if images)
    assert last_non_empty[0].mime == "image/png"


@pytest.mark.asyncio
async def test_run_agent_prefers_latest_generated_image_for_locked_image_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["nanobanana"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="images",
                    label="图片",
                    type="images",
                    required=True,
                    prompt="请上传图片",
                ),
            ],
        ),
        source_path=Path("/tmp/nano_guard_generated_image.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    original_path = tmp_path / "original.png"
    original_path.write_bytes(b"original-image")
    generated_path = tmp_path / "generated.png"
    generated_path.write_bytes(b"generated-image")

    previous_user_message = Message(
        role="user",
        content=f"(用户发送了图片)\n![飞书图片1]({original_path})",
    )
    previous_assistant_message = Message(
        role="assistant",
        content=f"结果图：\n文件路径：`{generated_path}`",
    )

    seen_user_images: list[Any] = []

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        for item in messages:
            if getattr(item, "role", "") == "user":
                seen_user_images.append(getattr(item, "images", None))
        return AgentResponse(content="继续图生图", model="test-model")

    router = _make_router(chat_fn=fake_chat)
    now = datetime.now(UTC)
    session = Session(
        id="s-nano-guard-generated-1",
        channel="feishu",
        peer_id="u1",
        messages=[previous_user_message, previous_assistant_message],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={
            "locked_skill_ids": ["nano-banana-image-t8"],
            "last_generated_image_path": str(generated_path),
            "skill_param_state": {
                "nano-banana-image-t8": {
                    "api_key": "sk-test",
                    "prompt": "让猫更有气势",
                    "__model_display__": "香蕉2",
                }
            },
        },
    )

    result = await run_agent(
        message="姿势改成胜利手势",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "继续图生图" in result
    assert any(images for images in seen_user_images)
    last_non_empty = next(images for images in reversed(seen_user_images) if images)
    assert base64.b64decode(last_non_empty[0].data) == b"generated-image"


@pytest.mark.asyncio
async def test_run_agent_regenerate_reuses_last_input_image_set_for_locked_image_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana ????",
        triggers=["nanobanana"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="images",
                    label="??",
                    type="images",
                    required=True,
                    prompt="?????",
                ),
            ],
        ),
        source_path=Path("/tmp/nano_guard_regenerate.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    original_1 = tmp_path / "original-1.png"
    original_1.write_bytes(b"original-1")
    original_2 = tmp_path / "original-2.png"
    original_2.write_bytes(b"original-2")
    generated_path = tmp_path / "generated.png"
    generated_path.write_bytes(b"generated-image")

    captured_commands: list[str] = []

    async def fake_execute_tool(*args: Any, **kwargs: Any) -> tuple[str, ToolResult]:
        tc = args[1]
        captured_commands.append(str(tc.arguments.get("command", "")))
        return (tc.id, ToolResult(success=True, output=f"saved to {generated_path}"))

    monkeypatch.setattr(loop_mod, "_execute_tool", fake_execute_tool)  # noqa: SLF001

    now = datetime.now(UTC)
    session = Session(
        id="s-nano-guard-regenerate-1",
        channel="feishu",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={
            "locked_skill_ids": ["nano-banana-image-t8"],
            "last_generated_image_path": str(generated_path),
            "last_input_image_paths": [str(original_1), str(original_2)],
            "last_nano_banana_mode": "edit",
            "skill_param_state": {
                "nano-banana-image-t8": {
                    "api_key": "sk-test",
                    "prompt": "??1??2??",
                    "__model_display__": "??2",
                }
            },
        },
    )

    result = await run_agent(
        message="\u91cd\u65b0\u751f\u6210\u4e00\u6b21",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=_make_router(response=AgentResponse(content="unused", model="test-model")),
        session=session,
    )

    assert str(generated_path) in result
    assert captured_commands and "--mode edit" in captured_commands[-1]
    assert captured_commands[-1].count("--input-image") == 2
    assert str(original_1) in captured_commands[-1]
    assert str(original_2) in captured_commands[-1]


@pytest.mark.asyncio
async def test_run_agent_regenerate_after_text_mode_does_not_reuse_stale_input_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana text-only",
        triggers=["nanobanana"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="images",
                    label="images",
                    type="images",
                    required=True,
                    prompt="upload images",
                ),
            ],
        ),
        source_path=Path("/tmp/nano_guard_regenerate_text.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    stale_input = tmp_path / "stale-original.png"
    stale_input.write_bytes(b"stale-original")
    generated_path = tmp_path / "generated.png"
    generated_path.write_bytes(b"generated-image")

    captured_commands: list[str] = []

    async def fake_execute_tool(*args: Any, **kwargs: Any) -> tuple[str, ToolResult]:
        tc = args[1]
        captured_commands.append(str(tc.arguments.get("command", "")))
        return (tc.id, ToolResult(success=True, output=f"saved to {generated_path}"))

    monkeypatch.setattr(loop_mod, "_execute_tool", fake_execute_tool)  # noqa: SLF001

    now = datetime.now(UTC)
    session = Session(
        id="s-nano-guard-regenerate-text-1",
        channel="feishu",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={
            "locked_skill_ids": ["nano-banana-image-t8"],
            "last_generated_image_path": str(generated_path),
            "last_input_image_paths": [str(stale_input)],
            "last_nano_banana_mode": "text",
            "skill_param_state": {
                "nano-banana-image-t8": {
                    "api_key": "sk-test",
                    "prompt": "generate a fresh poster",
                    "__model_display__": "banana2",
                }
            },
        },
    )

    result = await run_agent(
        message="regenerate one more",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=_make_router(response=AgentResponse(content="unused", model="test-model")),
        session=session,
    )

    assert str(generated_path) in result
    assert captured_commands and "--mode text" in captured_commands[-1]
    assert "--input" not in captured_commands[-1]


@pytest.mark.asyncio
async def test_run_agent_does_not_reuse_images_for_plain_chat_under_locked_image_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["nanobanana"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="images",
                    label="图片",
                    type="images",
                    required=True,
                    prompt="请上传图片",
                ),
            ],
        ),
        source_path=Path("/tmp/nano_guard_plain_chat.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    original_path = tmp_path / "original.png"
    original_path.write_bytes(b"original-image")
    generated_path = tmp_path / "generated.png"
    generated_path.write_bytes(b"generated-image")

    seen_user_images: list[Any] = []

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        for item in messages:
            if getattr(item, "role", "") == "user":
                seen_user_images.append(getattr(item, "images", None))
        return AgentResponse(content="讲个冷笑话", model="test-model")

    router = _make_router(chat_fn=fake_chat)
    now = datetime.now(UTC)
    session = Session(
        id="s-nano-guard-plain-chat-1",
        channel="feishu",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={
            "locked_skill_ids": ["nano-banana-image-t8"],
            "last_generated_image_path": str(generated_path),
            "last_input_image_paths": [str(original_path)],
            "skill_param_state": {
                "nano-banana-image-t8": {
                    "api_key": "sk-test",
                    "prompt": "生成一张猫图",
                    "__model_display__": "香蕉2",
                }
            },
        },
    )

    result = await run_agent(
        message="讲个笑话",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "讲个冷笑话" in result
    assert all(not images for images in seen_user_images)


# ---------------------------------------------------------------------------
# Dedup / loop-breaking tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_agent_breaks_repeated_identical_tool_loop() -> None:
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
        return AgentResponse(
            content="",
            model="test-model",
            tool_calls=[
                ToolCall(
                    id=f"loop-{call_count}",
                    name="loop_tool",
                    arguments={"text": "same"},
                )
            ],
        )

    registry = ToolRegistry()
    registry.register(_LoopTool())
    router = _make_router(chat_fn=fake_chat)

    result = await run_agent(
        message="执行循环任务",
        session_id="test-loop-repeat",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert "工具调用连续无效" in result
    assert call_count <= 6


@pytest.mark.asyncio
async def test_run_agent_blocks_repeated_same_search_query() -> None:
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
        return AgentResponse(
            content="",
            model="test-model",
            tool_calls=[
                ToolCall(
                    id=f"browser-{call_count}",
                    name="browser",
                    arguments={"action": "search_images", "text": "same query"},
                )
            ],
        )

    registry = ToolRegistry()
    registry.register(_BrowserProbeTool())
    router = _make_router(chat_fn=fake_chat)

    result = await run_agent(
        message="给我搜图",
        session_id="test-search-images-loop",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert "工具调用连续无效" in result
    assert call_count <= 5


@pytest.mark.asyncio
async def test_run_agent_blocks_search_images_over_planned_count() -> None:
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
        return AgentResponse(
            content="共 2 张配图",
            model="test-model",
            tool_calls=[
                ToolCall(
                    id=f"browser-over-{call_count}",
                    name="browser",
                    arguments={"action": "search_images", "text": f"query {call_count}"},
                )
            ],
        )

    registry = ToolRegistry()
    registry.register(_BrowserProbeTool())
    router = _make_router(chat_fn=fake_chat)

    result = await run_agent(
        message="做个带配图的PPT",
        session_id="test-search-images-over-plan",
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
    )

    assert "工具调用连续无效" in result
    assert call_count <= 7
