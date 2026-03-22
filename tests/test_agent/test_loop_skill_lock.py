"""Agent loop tests: skill lock/unlock, switch consent, param guard, nano-banana guard."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

import whaleclaw.agent.loop as loop_mod
from whaleclaw.agent.helpers.skill_lock import (
    capture_param_value,
    is_nano_banana_control_message,
    is_task_done_confirmation,
    looks_like_skill_activation_message,
    skill_trigger_mentioned,
    update_guard_state,
)
from whaleclaw.agent.loop import run_agent
from whaleclaw.config.schema import WhaleclawConfig
from whaleclaw.providers.base import AgentResponse, ToolCall
from whaleclaw.sessions.manager import Session
from whaleclaw.skills.parser import Skill, SkillParamGuard, SkillParamItem
from whaleclaw.tools.base import ToolResult
from whaleclaw.tools.registry import ToolRegistry

from ._loop_helpers import (
    _BashProbeTool,
    _conditional_route,
    _fixed_route,
    _make_router,
)


@pytest.mark.asyncio
async def test_run_agent_skill_lock_requires_explicit_done_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generated_path = tmp_path / "generated.png"
    generated_path.write_bytes(b"generated-image")
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
            return AgentResponse(
                content="",
                model="test-model",
                tool_calls=[ToolCall(id="tc_bash", name="bash", arguments={"command": "echo ok"})],
            )
        return AgentResponse(content="已出图", model="test-model")

    registry = ToolRegistry()
    registry.register(_BashProbeTool())
    captured_commands: list[str] = []

    async def fake_execute_tool(*args: Any, **kwargs: Any) -> tuple[str, ToolResult]:
        tc = args[1]
        captured_commands.append(str(tc.arguments.get("command", "")))
        return (tc.id, ToolResult(success=True, output=f"saved to {generated_path}"))

    monkeypatch.setattr(loop_mod, "_execute_tool", fake_execute_tool)  # noqa: SLF001
    router = _make_router(chat_fn=fake_chat)
    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-1",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={},
    )

    first = await run_agent(
        message="/use nano-banana-image-t8 一只熊猫在上海街头跳舞",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        registry=registry,
        session=session,
    )
    assert "已出图" in first
    assert "任务完成" in first
    assert session.metadata.get("locked_skill_ids") == ["nano-banana-image-t8"]
    assert session.metadata.get("skill_lock_waiting_done") is True
    assert call_count == 2

    router2 = _make_router(response=AgentResponse(content="不应调用", model="test-model"))
    second = await run_agent(
        message="任务结束",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router2,
        registry=registry,
        session=session,
    )
    assert second == "已确认任务完成，已解除本轮技能锁定。"
    assert "locked_skill_ids" not in session.metadata
    assert "skill_lock_waiting_done" not in session.metadata
    router2.chat.assert_not_called()


@pytest.mark.asyncio
async def test_run_agent_reports_unlock_not_completed_for_task_done_intent_near_miss() -> None:
    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-near-miss",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={
            "locked_skill_ids": ["nano-banana-image-t8"],
            "skill_lock_waiting_done": True,
        },
    )

    router = _make_router(response=AgentResponse(content="不应调用", model="test-model"))
    result = await run_agent(
        message="本轮结束啦",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "还没有完成正式解锁" in result
    assert "请直接回复\u201c任务完成\u201d或\u201c任务结束\u201d" in result
    router.chat.assert_not_called()


@pytest.mark.asyncio
async def test_run_agent_unlocks_locked_skill_even_when_waiting_done_flag_is_false() -> None:
    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-unlock-without-waiting-flag",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={
            "locked_skill_ids": ["nano-banana-image-t8"],
            "skill_lock_waiting_done": False,
            "skill_param_state": {
                "nano-banana-image-t8": {
                    "api_key": "__present__",
                    "prompt": "旧任务",
                    "images": 2,
                }
            },
        },
    )

    router = _make_router(response=AgentResponse(content="不应调用", model="test-model"))
    result = await run_agent(
        message="任务完成",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert result == "已确认任务完成，已解除本轮技能锁定。"
    assert "locked_skill_ids" not in session.metadata
    assert "skill_lock_waiting_done" not in session.metadata
    assert "skill_param_state" not in session.metadata
    router.chat.assert_not_called()


@pytest.mark.asyncio
async def test_run_agent_applies_locked_skill_set_to_system_prompt() -> None:
    seen_messages: list[Any] = []

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        seen_messages.extend(messages)
        return AgentResponse(content="继续处理", model="test-model")

    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-2",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={"locked_skill_ids": ["skill-a", "skill-b"]},
    )
    router = _make_router(chat_fn=fake_chat)
    await run_agent(
        message="继续改一下",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    joined = "\n".join(
        str(m.content) for m in seen_messages if getattr(m, "role", "") == "system"
    )
    assert "当前会话已锁定技能：skill-a, skill-b" in joined


@pytest.mark.asyncio
async def test_run_agent_applies_nano_banana_model_and_recent_image_hints_to_system_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whaleclaw.providers.base import Message

    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["nanobanana"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        source_path=Path("/tmp/nano_system_prompt.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    image_path = tmp_path / "banana-ref.png"
    image_path.write_bytes(b"png-bytes")
    seen_messages: list[Any] = []

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        seen_messages.extend(messages)
        return AgentResponse(content="继续处理", model="test-model")

    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-nano-system",
        channel="feishu",
        peer_id="u1",
        messages=[
            Message(
                role="user",
                content=f"(用户发送了图片)\n![飞书图片1]({image_path})",
            )
        ],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={
            "locked_skill_ids": ["nano-banana-image-t8"],
            "skill_param_state": {
                "nano-banana-image-t8": {
                    "__model_display__": "香蕉pro",
                }
            },
        },
    )
    router = _make_router(chat_fn=fake_chat)
    result = await run_agent(
        message="请继续处理这张图",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert result == "继续处理"
    joined = "\n".join(
        str(m.content) for m in seen_messages if getattr(m, "role", "") == "system"
    )
    assert "当前本轮模型是：香蕉pro" in joined
    assert "--model` 和 `--edit-model` 都设置为 `香蕉pro`" in joined
    assert str(image_path) in joined
    assert "不要再要求用户重新上传" in joined


@pytest.mark.asyncio
async def test_run_agent_auto_locks_when_user_explicitly_mentions_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["nanobanana", "文生图"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        source_path=Path("/tmp/SKILL.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    router = _make_router(response=AgentResponse(content="收到", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-3",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={},
    )
    result = await run_agent(
        message="使用nanobanana的技能，文生图",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "nano-banana-image-t8" in result
    assert "技能" in result
    assert "收到" in result
    assert session.metadata.get("locked_skill_ids") == ["nano-banana-image-t8"]
    assert session.metadata.get("skill_lock_waiting_done") is False


@pytest.mark.asyncio
async def test_run_agent_auto_locks_when_user_hits_specific_skill_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["香蕉生图", "香蕉文生图"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="api_key",
                    label="API Key",
                    type="api_key",
                    required=True,
                    prompt="请提供 Nano Banana API Key",
                ),
            ],
        ),
        source_path=Path("/tmp/nano-trigger.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    router = _make_router(response=AgentResponse(content="不应调用", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-trigger-1",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={},
    )

    result = await run_agent(
        message="我要用香蕉生图",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "我将使用 nano-banana-image-t8 技能继续完成任务。" in result
    assert session.metadata.get("locked_skill_ids") == ["nano-banana-image-t8"]
    router.chat.assert_not_called()


@pytest.mark.asyncio
async def test_run_agent_auto_locks_even_for_one_shot_skill_in_task_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        id="ppt-generator",
        name="PPT Generator",
        triggers=["ppt"],
        instructions="x",
        lock_session=False,
        source_path=Path("/tmp/SKILL2.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    router = _make_router(response=AgentResponse(content="收到", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-4",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={},
    )

    result = await run_agent(
        message="使用ppt-generator技能，帮我制作个PPT",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "ppt-generator" in result
    assert "技能" in result
    assert "收到" in result
    assert session.metadata.get("locked_skill_ids") == ["ppt-generator"]


@pytest.mark.asyncio
async def test_run_agent_rejects_skill_switch_without_user_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_a = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["nanobanana"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        source_path=Path("/tmp/a.md"),
    )
    skill_b = Skill(
        id="ppt-generator",
        name="PPT Generator",
        triggers=["ppt"],
        instructions="x",
        lock_session=False,
        source_path=Path("/tmp/b.md"),
    )

    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _conditional_route("ppt", [skill_b], [skill_a]),
    )

    router = _make_router(response=AgentResponse(content="收到", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-5",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={"locked_skill_ids": ["nano-banana-image-t8"]},
    )

    result = await run_agent(
        message="我在想是不是该用ppt-generator技能做个PPT",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "同意切换技能" in result
    assert session.metadata.get("locked_skill_ids") == ["nano-banana-image-t8"]


@pytest.mark.asyncio
async def test_run_agent_keeps_locked_skill_when_user_did_not_explicitly_request_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_b = Skill(
        id="ppt-generator",
        name="PPT Generator",
        triggers=["ppt"],
        instructions="x",
        lock_session=False,
        source_path=Path("/tmp/b1.md"),
    )

    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill_b),
    )

    router = _make_router(response=AgentResponse(content="继续生图处理", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-5b",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={"locked_skill_ids": ["nano-banana-image-t8"]},
    )

    result = await run_agent(
        message="继续生成一张横版香蕉海报图",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "同意切换技能" not in result
    assert "继续生图处理" in result
    assert session.metadata.get("locked_skill_ids") == ["nano-banana-image-t8"]


@pytest.mark.asyncio
async def test_run_agent_allows_skill_switch_with_user_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_a = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["nanobanana"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        source_path=Path("/tmp/a2.md"),
    )
    skill_b = Skill(
        id="ppt-generator",
        name="PPT Generator",
        triggers=["ppt"],
        instructions="x",
        lock_session=False,
        source_path=Path("/tmp/b2.md"),
    )

    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _conditional_route("ppt", [skill_b], [skill_a]),
    )

    router = _make_router(response=AgentResponse(content="收到", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-6",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={"locked_skill_ids": ["nano-banana-image-t8"]},
    )

    result = await run_agent(
        message="同意切换技能，改用ppt-generator技能做个PPT",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "切换" in result
    assert "ppt-generator" in result
    assert "收到" in result
    assert session.metadata.get("locked_skill_ids") == ["ppt-generator"]


@pytest.mark.asyncio
async def test_run_agent_resumes_pending_switch_task_after_consent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generated_path = tmp_path / "generated.png"
    generated_path.write_bytes(b"generated-image")
    skill_a = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["nanobanana"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        source_path=Path("/tmp/a3.md"),
    )
    skill_b = Skill(
        id="ppt-generator",
        name="PPT Generator",
        triggers=["ppt"],
        instructions="x",
        lock_session=False,
        source_path=Path("/tmp/b3.md"),
    )

    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _conditional_route("ppt", [skill_b], [skill_a]),
    )

    seen_messages: list[str] = []

    async def fake_chat(
        model_id: str,  # noqa: ARG001
        messages: list[Any],
        *,
        tools: Any = None,  # noqa: ARG001
        on_stream: Any = None,  # noqa: ARG001
    ) -> AgentResponse:
        seen_messages.extend(str(m.content) for m in messages if getattr(m, "role", "") == "user")
        return AgentResponse(content="开始做PPT", model="test-model")

    captured_commands: list[str] = []

    async def fake_execute_tool(*args: Any, **kwargs: Any) -> tuple[str, ToolResult]:
        tc = args[1]
        captured_commands.append(str(tc.arguments.get("command", "")))
        return (tc.id, ToolResult(success=True, output=f"saved to {generated_path}"))

    monkeypatch.setattr(loop_mod, "_execute_tool", fake_execute_tool)  # noqa: SLF001
    router = _make_router(chat_fn=fake_chat)
    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-6b",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={"locked_skill_ids": ["nano-banana-image-t8"]},
    )

    first = await run_agent(
        message="我在想是不是该用ppt-generator技能做个PPT",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "同意切换技能" in first
    assert session.metadata.get("pending_skill_switch_ids") == ["ppt-generator"]

    second = await run_agent(
        message="同意切换技能",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "切换" in second
    assert "开始做PPT" in second
    assert any("我在想是不是该用ppt-generator技能做个PPT" in msg for msg in seen_messages)
    assert session.metadata.get("locked_skill_ids") == ["ppt-generator"]
    assert "pending_skill_switch_ids" not in session.metadata
    assert "pending_skill_switch_message" not in session.metadata


@pytest.mark.asyncio
async def test_run_agent_allows_skill_switch_by_direct_switch_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_a = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["nanobanana"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        source_path=Path("/tmp/a3.md"),
    )
    skill_b = Skill(
        id="ppt-generator",
        name="PPT Generator",
        triggers=["ppt"],
        instructions="x",
        lock_session=False,
        source_path=Path("/tmp/b3.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _conditional_route("ppt", [skill_b], [skill_a]),
    )

    router = _make_router(response=AgentResponse(content="收到", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-skill-lock-7",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={"locked_skill_ids": ["nano-banana-image-t8"]},
    )

    result = await run_agent(
        message="换成ppt-generator技能做个PPT",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "切换" in result
    assert "ppt-generator" in result
    assert "收到" in result
    assert session.metadata.get("locked_skill_ids") == ["ppt-generator"]


@pytest.mark.asyncio
async def test_nano_banana_guard_lists_missing_params_before_execution() -> None:
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
                    key="api_key",
                    label="API Key",
                    type="api_key",
                    required=True,
                    prompt="请提供 API Key",
                ),
                SkillParamItem(
                    key="prompt",
                    label="提示词",
                    type="text",
                    required=True,
                    aliases=["提示词", "prompt"],
                    prompt="请提供提示词",
                ),
                SkillParamItem(
                    key="ratio",
                    label="尺寸/比例",
                    type="ratio",
                    required=False,
                    aliases=["比例", "尺寸", "size"],
                    prompt="可选填写比例或尺寸",
                ),
            ],
        ),
        source_path=Path("/tmp/nano_guard.md"),
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )
    router = _make_router(response=AgentResponse(content="不应调用", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-nano-guard-1",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={
            "locked_skill_ids": ["nano-banana-image-t8"],
            "skill_param_state": {
                "nano-banana-image-t8": {
                    "__model_display__": "香蕉2",
                }
            },
        },
    )

    result = await run_agent(
        message="提示词：做一张极简风海报",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "API Key" in result
    assert "当前模型：香蕉2（0.1元）可切换模型香蕉pro（0.2元）" in result
    assert "3) 提示词：已收到" in result
    assert "图生图图片：已收到 0 张（至少 1 张）" in result
    assert "切换本次模型：切换香蕉2（pro）。设置默认模型：默认模型香蕉2（pro）" in result
    router.chat.assert_not_called()
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_nano_banana_guard_uses_saved_key_without_asking_again(
    tmp_path: Path,
) -> None:
    saved_key = tmp_path / "nano_banana_api_key.txt"
    saved_key.write_text("sk-test-saved-key", encoding="utf-8")
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["香蕉生图", "香蕉文生图"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="api_key",
                    label="API Key",
                    type="api_key",
                    required=True,
                    saved_file=str(saved_key),
                    prompt="请提供 Nano Banana API Key",
                ),
                SkillParamItem(
                    key="prompt",
                    label="提示词",
                    type="text",
                    required=True,
                    aliases=["提示词", "prompt"],
                    prompt="请提供提示词",
                ),
                SkillParamItem(
                    key="images",
                    label="图生图图片",
                    type="images",
                    required=False,
                    min_count=1,
                    prompt="图生图时请上传至少 1 张图片",
                ),
            ],
        ),
        source_path=Path("/tmp/nano_guard_saved_key.md"),
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    captured_commands: list[str] = []

    async def fake_execute_tool(*args: Any, **kwargs: Any) -> tuple[str, ToolResult]:
        tc = args[1]
        captured_commands.append(str(tc.arguments.get("command", "")))
        return (tc.id, ToolResult(success=True, output="saved to C:/tmp/generated-from-saved-key.png"))

    monkeypatch.setattr(loop_mod, "_execute_tool", fake_execute_tool)  # noqa: SLF001
    router = _make_router(response=AgentResponse(content="不应调用", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-nano-guard-saved-key",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={"locked_skill_ids": ["nano-banana-image-t8"]},
    )

    result = await run_agent(
        message="提示词：做一张香蕉主题海报",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "当前使用模型：香蕉2" in result
    assert "C:/tmp/generated-from-saved-key.png" in result
    assert captured_commands
    assert "sk-test-saved-key" in captured_commands[-1]
    assert "做一张香蕉主题海报" in captured_commands[-1]
    router.chat.assert_not_called()
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_nano_banana_guard_keeps_fixed_template_for_activation_only_message(
    tmp_path: Path,
) -> None:
    saved_key = tmp_path / "nano_banana_api_key.txt"
    saved_key.write_text("sk-test-saved-key", encoding="utf-8")
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["香蕉生图", "香蕉文生图", "香蕉图生图"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="api_key",
                    label="API Key",
                    type="api_key",
                    required=True,
                    saved_file=str(saved_key),
                    prompt="请提供 Nano Banana API Key",
                ),
                SkillParamItem(
                    key="prompt",
                    label="提示词",
                    type="text",
                    required=True,
                    aliases=["提示词", "prompt"],
                    prompt="请提供提示词",
                ),
                SkillParamItem(
                    key="images",
                    label="图生图图片",
                    type="images",
                    required=False,
                    min_count=1,
                    prompt="图生图时请上传至少 1 张图片",
                ),
            ],
        ),
        source_path=Path("/tmp/nano_guard_activation_only.md"),
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )
    router = _make_router(response=AgentResponse(content="不应调用", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-nano-guard-activation-only",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={"locked_skill_ids": ["nano-banana-image-t8"]},
    )

    result = await run_agent(
        message="使用香蕉生图",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "当前会话仍在香蕉生图技能里。" in result
    assert "如果要继续生图，请直接发送提示词或图片" in result
    assert "请回复\u201c任务完成\u201d解除技能锁定" in result
    router.chat.assert_not_called()
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_nano_banana_control_message_does_not_overwrite_existing_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["香蕉生图", "香蕉pro"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="api_key",
                    label="API Key",
                    type="api_key",
                    required=True,
                    prompt="请提供 Nano Banana API Key",
                ),
                SkillParamItem(
                    key="prompt",
                    label="提示词",
                    type="text",
                    required=True,
                    aliases=["提示词", "prompt"],
                    prompt="请提供提示词",
                ),
                SkillParamItem(
                    key="images",
                    label="图生图图片",
                    type="images",
                    required=False,
                    min_count=1,
                    prompt="图生图时请上传至少 1 张图片",
                ),
            ],
        ),
        source_path=Path("/tmp/nano_guard_retry.md"),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    captured_commands: list[str] = []

    async def fake_execute_tool(*args: Any, **kwargs: Any) -> tuple[str, ToolResult]:
        tc = args[1]
        captured_commands.append(str(tc.arguments.get("command", "")))
        return (tc.id, ToolResult(success=True, output="saved to C:/tmp/generated.png"))

    monkeypatch.setattr(loop_mod, "_execute_tool", fake_execute_tool)  # noqa: SLF001
    router = _make_router(response=AgentResponse(content="继续执行", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-nano-guard-retry",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={
            "locked_skill_ids": ["nano-banana-image-t8"],
            "skill_param_state": {
                "nano-banana-image-t8": {
                    "api_key": "sk-test-1234567890",
                    "prompt": "把男孩衣服改成紫色",
                    "images": 1,
                    "__model_display__": "香蕉2",
                }
            },
        },
    )

    result = await run_agent(
        message="用香蕉pro重试",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "重新生成好了" in result
    assert captured_commands
    assert "把男孩衣服改成紫色" in captured_commands[-1]
    assert (
        cast(dict[str, object], session.metadata["skill_param_state"]["nano-banana-image-t8"])[
            "prompt"
        ]
        == "把男孩衣服改成紫色"
    )
    assert (
        cast(dict[str, object], session.metadata["skill_param_state"]["nano-banana-image-t8"])[
            "__model_display__"
        ]
        == "香蕉pro"
    )
    router.chat.assert_not_called()


@pytest.mark.asyncio
async def test_nano_banana_activation_message_reminds_when_session_is_already_locked() -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["香蕉生图", "香蕉pro"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="api_key",
                    label="API Key",
                    type="api_key",
                    required=True,
                    prompt="请提供 Nano Banana API Key",
                ),
                SkillParamItem(
                    key="prompt",
                    label="提示词",
                    type="text",
                    required=True,
                    aliases=["提示词", "prompt"],
                    prompt="请提供提示词",
                ),
                SkillParamItem(
                    key="images",
                    label="图生图图片",
                    type="images",
                    required=False,
                    min_count=1,
                    prompt="图生图时请上传至少 1 张图片",
                ),
            ],
        ),
        source_path=Path("/tmp/nano_guard_activation_complete.md"),
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )
    router = _make_router(response=AgentResponse(content="不应调用", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-nano-guard-activation-complete",
        channel="webchat",
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
                    "prompt": "算了，继续讲笑话给我",
                    "images": 4,
                    "__model_display__": "香蕉2",
                }
            },
        },
    )

    result = await run_agent(
        message="使用香蕉生图",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "当前会话仍在香蕉生图技能里。" in result
    assert "当前模型：香蕉2。" in result
    assert "如果要继续生图，请直接发送提示词或图片" in result
    assert "请回复\u201c任务完成\u201d解除技能锁定" in result
    router.chat.assert_not_called()


def test_nano_banana_control_message_does_not_match_scheduled_task_payload() -> None:
    payload = (
        '使用 nano-banana-image-t8 技能执行文生图：模型=香蕉2，'
        '提示词="一只巨大的黑色企鹅站在航母上"，比例=3:4'
    )

    assert is_nano_banana_control_message(payload) is False


def test_clean_nano_banana_prompt_delta_extracts_prompt_from_scheduled_payload() -> None:
    payload = (
        '使用 nano-banana-image-t8 技能执行文生图：模型=香蕉2，'
        '提示词="一只巨大的黑色企鹅站在航母上"，比例=3:4'
    )

    assert loop_mod._clean_nano_banana_prompt_delta(payload) == "一只巨大的黑色企鹅站在航母上"  # noqa: SLF001


def test_build_nano_banana_command_basic() -> None:
    command = loop_mod._build_nano_banana_command(  # noqa: SLF001
        mode="text",
        model_display="香蕉2",
        prompt="一只熊猫在上海街头跳舞",
        input_paths=[],
        ratio="4:3",
    )

    assert "--mode" in command
    assert "text" in command
    assert "--model" in command
    assert "gemini-3.1-flash-image-preview" in command
    assert "--prompt" in command
    assert "--aspect-ratio" in command
    assert "香蕉2" not in command


def test_capture_api_key_keeps_raw_secret() -> None:
    param = SkillParamItem(key="api_key", type="api_key", required=True)

    captured = capture_param_value(
        param,
        "我的 key 是 sk-1234567890abcdef",
        None,
        None,
    )

    assert captured == "sk-1234567890abcdef"


def test_update_guard_state_persists_api_key_saved_file(tmp_path: Path) -> None:
    saved_key = tmp_path / "nano_banana_api_key.txt"
    params = [
        SkillParamItem(
            key="api_key",
            type="api_key",
            required=True,
            aliases=["key"],
            saved_file=str(saved_key),
        )
    ]

    updated, missing = update_guard_state(
        params,
        {},
        "key: sk-1234567890abcdef",
        None,
    )

    assert missing is False
    assert updated["api_key"] == "sk-1234567890abcdef"
    assert saved_key.read_text(encoding="utf-8") == "sk-1234567890abcdef"


def test_skill_helpers_support_clean_chinese_activation_and_completion() -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["文生图", "图生图", "生图联调", "nano banana"],
        instructions="x",
        source_path=Path("/tmp/nano-helper.md"),
    )

    assert looks_like_skill_activation_message(
        "使用 nano banana 技能做文生图",
        skill_activation_patterns=loop_mod._SKILL_ACTIVATION_PATTERNS,  # noqa: SLF001
    )
    assert skill_trigger_mentioned(skill, "我想做文生图")
    assert is_task_done_confirmation(
        "任务完成",
        task_done_patterns=loop_mod._TASK_DONE_PATTERNS,  # noqa: SLF001
    )


@pytest.mark.asyncio
async def test_nano_banana_old_activation_prompt_does_not_trigger_execution_on_key_only() -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["香蕉生图", "香蕉文生图", "香蕉图生图"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="api_key",
                    label="API Key",
                    type="api_key",
                    required=True,
                    aliases=["apikey", "api key", "key"],
                    prompt="请提供 Nano Banana API Key",
                ),
                SkillParamItem(
                    key="prompt",
                    label="提示词",
                    type="text",
                    required=True,
                    aliases=["提示词", "prompt"],
                    prompt="请提供提示词",
                ),
            ],
        ),
        source_path=Path("/tmp/nano-stale-prompt.md"),
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        _fixed_route(skill),
    )

    router = _make_router(response=AgentResponse(content="不应调用", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-nano-stale-prompt",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={
            "locked_skill_ids": ["nano-banana-image-t8"],
            "skill_param_state": {
                "nano-banana-image-t8": {
                    "prompt": "使用香蕉生图",
                }
            },
        },
    )

    result = await run_agent(
        message="sk-1234567890abcdef",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "1) API Key：已就绪" in result
    assert "3) 提示词：未提供" in result
    assert "请提供提示词" in result
    router.chat.assert_not_called()
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_run_agent_auto_locks_nano_banana_activation_without_explicit_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["文生图", "图生图"],
        instructions="x",
        lock_session=True,
        is_user_installed=True,
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(
                    key="api_key",
                    label="API Key",
                    type="api_key",
                    required=True,
                    prompt="请提供 Nano Banana API Key",
                ),
            ],
        ),
        source_path=Path("/tmp/nano-fallback-trigger.md"),
    )

    def fake_route_skills(
        user_message: str,  # noqa: ARG001
        forced_skill_ids: list[str] | None = None,
    ) -> list[Skill]:
        if forced_skill_ids == ["nano-banana-image-t8"]:
            return [skill]
        return []

    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        fake_route_skills,
    )

    router = _make_router(response=AgentResponse(content="不应调用", model="test-model"))
    now = datetime.now(UTC)
    session = Session(
        id="s-nano-fallback-trigger",
        channel="webchat",
        peer_id="u1",
        messages=[],
        model="openai/gpt-5.2",
        created_at=now,
        updated_at=now,
        metadata={},
    )

    result = await run_agent(
        message="使用香蕉生图",
        session_id=session.id,
        config=WhaleclawConfig(),
        router=router,
        session=session,
    )

    assert "我将使用 nano-banana-image-t8 技能继续完成任务。" in result
    assert session.metadata.get("locked_skill_ids") == ["nano-banana-image-t8"]
    router.chat.assert_not_called()
