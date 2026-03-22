"""Focused regressions for nano-banana execution hints in compound tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import whaleclaw.agent.loop as loop_mod
from whaleclaw.config.schema import WhaleclawConfig
from whaleclaw.providers.base import AgentResponse, Message
from whaleclaw.skills.parser import Skill, SkillParamGuard, SkillParamItem


class _CaptureRouter:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    def supports_native_tools(self, _model_id: str) -> bool:
        return True

    async def chat(
        self,
        model_id: str,
        messages: list[Message],
        *,
        tools: Any = None,
        on_stream: Any = None,
    ) -> AgentResponse:
        del model_id, tools, on_stream
        self.messages = messages
        return AgentResponse(content="ok", model="test-model")


@pytest.mark.asyncio
async def test_compound_task_injects_nano_banana_execution_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    skill = Skill(
        id="nano-banana-image-t8",
        name="Nano Banana 生图联调",
        triggers=["nano banana", "文生图"],
        instructions="使用 nano banana 生成图片。",
        lock_session=True,
        is_user_installed=True,
        source_path=Path("/tmp/nano_compound.md"),
        param_guard=SkillParamGuard(
            enabled=True,
            params=[
                SkillParamItem(key="api_key", type="api_key", required=True),
                SkillParamItem(key="prompt", type="text", required=True),
            ],
        ),
    )
    monkeypatch.setattr(
        loop_mod._assembler,  # noqa: SLF001
        "route_skills",
        lambda user_message, forced_skill_ids=None: [skill],  # noqa: ARG005
    )

    router = _CaptureRouter()
    result = await loop_mod.run_agent(
        message="做一个word文件，然后使用 nano banana 画两张图，再把图片插入word里",
        session_id="test-nano-banana-compound",
        config=WhaleclawConfig(),
        router=router,  # type: ignore[arg-type]
    )

    assert result == "ok"
    system_text = "\n".join(msg.content for msg in router.messages if msg.role == "system")
    assert "当前正在执行 nano-banana-image-t8 技能。" in system_text
    assert "禁止做任何脚本/环境探测或计划回读" in system_text
    assert "推荐直接使用以下命令执行" in system_text
    assert "test_nano_banana_2.py" in system_text
