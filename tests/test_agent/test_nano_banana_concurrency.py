"""Focused regressions for nano-banana concurrency in the agent loop."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

import whaleclaw.agent.loop as loop_mod
from whaleclaw.config.schema import WhaleclawConfig
from whaleclaw.providers.base import AgentResponse, ToolCall
from whaleclaw.tools.base import Tool, ToolDefinition, ToolParameter, ToolResult
from whaleclaw.tools.registry import ToolRegistry


class _SleepingNanoBashTool(Tool):
    def __init__(self, *, sleep_seconds: float = 0.05) -> None:
        self.sleep_seconds = sleep_seconds
        self.starts: list[float] = []
        self.commands: list[str] = []

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bash",
            description="sleeping nano banana bash",
            parameters=[
                ToolParameter(name="command", type="string", description="command"),
                ToolParameter(name="timeout", type="integer", description="timeout", required=False),
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.starts.append(time.monotonic())
        self.commands.append(str(kwargs.get("command", "")))
        await asyncio.sleep(self.sleep_seconds)
        return ToolResult(success=True, output=f"done:{kwargs.get('command', '')}")


class _Router:
    def __init__(self) -> None:
        self.calls = 0

    def supports_native_tools(self, _model_id: str) -> bool:
        return True

    async def chat(
        self,
        model_id: str,
        messages: list[Any],
        *,
        tools: Any = None,
        on_stream: Any = None,
    ) -> AgentResponse:
        del model_id, messages, tools, on_stream
        self.calls += 1
        if self.calls == 1:
            return AgentResponse(
                content="",
                model="test-model",
                tool_calls=[
                    ToolCall(
                        id=f"tc_{idx}",
                        name="bash",
                        arguments={
                            "command": f"/tmp/test_nano_banana_2.py --mode text --prompt image-{idx}",
                            "timeout": 120,
                        },
                    )
                    for idx in range(7)
                ],
            )
        return AgentResponse(content="done", model="test-model")


@pytest.mark.asyncio
async def test_run_agent_batches_nano_bash_calls_with_max_five_parallel() -> None:
    router = _Router()
    registry = ToolRegistry()
    bash_tool = _SleepingNanoBashTool()
    registry.register(bash_tool)

    result = await loop_mod.run_agent(
        message="请生成 7 张 nano banana 图片",
        session_id="test-nano-banana-batch",
        config=WhaleclawConfig(),
        router=router,  # type: ignore[arg-type]
        registry=registry,
    )

    assert result.endswith("done")
    assert router.calls == 2
    assert len(bash_tool.starts) == 7
    assert max(bash_tool.starts[:5]) - min(bash_tool.starts[:5]) < 0.05
    assert max(bash_tool.starts[5:]) - min(bash_tool.starts[5:]) < 0.05
    assert min(bash_tool.starts[5:]) - min(bash_tool.starts[:5]) >= 1.4
