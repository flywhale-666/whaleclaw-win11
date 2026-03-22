"""Tests for session group compressor behavior."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from whaleclaw.providers.base import ImageContent, Message, ToolCall
from whaleclaw.sessions.group_compressor import (
    SessionGroupCompressor,
    _compact_prev_group,
    _hash_group,
)
from whaleclaw.sessions.store import SessionStore


def _mk_group(i: int, text: str) -> list[Message]:
    return [
        Message(role="user", content=f"u{i}:{text}"),
        Message(role="assistant", content=f"a{i}:{text}"),
    ]


def _mk_image(tag: str = "img") -> ImageContent:
    return ImageContent(mime="image/png", data=tag)


def _flatten(groups: list[list[Message]]) -> list[Message]:
    out: list[Message] = []
    for g in groups:
        out.extend(g)
    return out


class _NoopRouter:
    async def chat(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("chat should not be called when model_id is empty")


class _SlowRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *args: object, **kwargs: object) -> SimpleNamespace:
        self.calls += 1
        await asyncio.sleep(0.15)
        return SimpleNamespace(content="压缩摘要")


async def _mk_store(tmp_path: Path) -> SessionStore:
    store = SessionStore(db_path=tmp_path / "group_compressor.db")
    await store.open()
    return store


@pytest.mark.asyncio
async def test_window_plan_uses_absolute_group_index(tmp_path: Path) -> None:
    store = await _mk_store(tmp_path)
    try:
        compressor = SessionGroupCompressor(store)
        groups = [_mk_group(i, "短消息") for i in range(1, 31)]
        plan = compressor._window_plan(_flatten(groups))  # noqa: SLF001
        assert len(plan) == 25
        assert plan[0].group_idx == 6
        assert plan[-1].group_idx == 30
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_window_messages_schedules_background_generation(tmp_path: Path) -> None:
    store = await _mk_store(tmp_path)
    compressor = SessionGroupCompressor(store)
    try:
        now = datetime.now(UTC).isoformat()
        await store.save_session(
            session_id="s2",
            channel="webchat",
            peer_id="u2",
            model="qwen/qwen3.5-plus",
            created_at=now,
            updated_at=now,
        )
        groups = [_mk_group(i, "需要压缩的历史消息 " + ("x" * 120)) for i in range(1, 13)]
        router = _SlowRouter()

        t0 = time.monotonic()
        output = await compressor.build_window_messages(
            session_id="s2",
            messages=_flatten(groups),
            router=router,  # type: ignore[arg-type]
            model_id="compress-model",
        )
        elapsed = time.monotonic() - t0

        assert elapsed < 0.2
        assert output

        plan = compressor._window_plan(_flatten(groups))  # noqa: SLF001
        first = next(item for item in plan if item.level != "L2")
        source_hash = _hash_group(first.group)

        found = False
        for _ in range(20):
            cached = await store.get_group_compression(
                session_id="s2",
                group_idx=first.group_idx,
                level=first.level,
                source_hash=source_hash,
            )
            if cached:
                found = True
                break
            await asyncio.sleep(0.05)

        assert found
        assert router.calls > 0
    finally:
        await compressor.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_recent_five_groups_always_l2_even_when_over_budget(
    tmp_path: Path,
) -> None:
    store = await _mk_store(tmp_path)
    try:
        compressor = SessionGroupCompressor(store)
        groups = [
            _mk_group(1, "旧历史"),
            _mk_group(2, "最近第5组 " + ("超长内容 " * 600)),
            _mk_group(3, "最近第4组 " + ("超长内容 " * 600)),
            _mk_group(4, "最近第3组 " + ("超长内容 " * 600)),
            _mk_group(5, "最近第2组 " + ("超长内容 " * 600)),
            _mk_group(6, "最近第1组 " + ("超长内容 " * 600)),
        ]
        plan = compressor._window_plan(_flatten(groups))  # noqa: SLF001
        assert [x.level for x in plan][-5:] == ["L2", "L2", "L2", "L2", "L2"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_window_plan_compresses_20_groups_when_25_groups_present(tmp_path: Path) -> None:
    store = await _mk_store(tmp_path)
    try:
        compressor = SessionGroupCompressor(store)
        groups = [_mk_group(i, "消息") for i in range(1, 25 + 1)]
        plan = compressor._window_plan(_flatten(groups))  # noqa: SLF001
        l2 = sum(1 for x in plan if x.level == "L2")
        l1 = sum(1 for x in plan if x.level == "L1")
        l0 = sum(1 for x in plan if x.level == "L0")
        assert l2 == 5
        assert l1 == 7
        assert l0 == 13
        assert l1 + l0 == 20
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_window_messages_outputs_structured_blocks(tmp_path: Path) -> None:
    store = await _mk_store(tmp_path)
    try:
        compressor = SessionGroupCompressor(store)
        now = datetime.now(UTC).isoformat()
        await store.save_session(
            session_id="s1",
            channel="webchat",
            peer_id="u1",
            model="qwen/qwen3.5-plus",
            created_at=now,
            updated_at=now,
        )
        groups = [_mk_group(i, "历史消息") for i in range(1, 6)]
        groups.append([Message(role="user", content="u6:当前轮用户请求")])
        output = await compressor.build_window_messages(
            session_id="s1",
            messages=_flatten(groups),
            router=_NoopRouter(),  # type: ignore[arg-type]
            model_id="",
        )

        text = "\n".join(m.content for m in output)
        assert "【历史摘要" in text
        assert "【当前任务状态】" in text
        assert "【最近对话原文" not in text
        assert output[0].content.startswith("【历史摘要（第1~1轮）】")
        assert output[-2].content.startswith("【当前任务状态】")
        assert output[-1].role == "user"
        assert output[-1].content == "u6:当前轮用户请求"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_window_messages_keeps_current_plus_previous_four_raw_groups(
    tmp_path: Path,
) -> None:
    store = await _mk_store(tmp_path)
    try:
        compressor = SessionGroupCompressor(store)
        now = datetime.now(UTC).isoformat()
        await store.save_session(
            session_id="s3",
            channel="webchat",
            peer_id="u3",
            model="qwen/qwen3.5-plus",
            created_at=now,
            updated_at=now,
        )
        groups = [_mk_group(i, "历史消息") for i in range(1, 8)]
        groups.append([Message(role="user", content="u8:当前轮用户请求")])
        output = await compressor.build_window_messages(
            session_id="s3",
            messages=_flatten(groups),
            router=_NoopRouter(),  # type: ignore[arg-type]
            model_id="",
        )

        text = "\n".join(m.content for m in output)
        assert "【最近对话原文" not in text
        history_block = next(m.content for m in output if "【历史摘要" in m.content)
        assert history_block.find("第3轮:") < history_block.find("第2轮:")
        raw_user_indexes = {
            m.content: idx
            for idx, m in enumerate(output)
            if m.role == "user"
        }
        for i in range(4, 9):
            assert f"u{i}:{'当前轮用户请求' if i == 8 else '历史消息'}" in raw_user_indexes
        assert "u3:历史消息" not in raw_user_indexes
        assert raw_user_indexes["u4:历史消息"] < raw_user_indexes["u7:历史消息"]
        assert raw_user_indexes["u8:当前轮用户请求"] == len(output) - 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_task_status_block_includes_current_progress_and_next_step(tmp_path: Path) -> None:
    store = await _mk_store(tmp_path)
    try:
        compressor = SessionGroupCompressor(store)
        now = datetime.now(UTC).isoformat()
        await store.save_session(
            session_id="s4",
            channel="webchat",
            peer_id="u4",
            model="qwen/qwen3.5-plus",
            created_at=now,
            updated_at=now,
        )
        groups = [_mk_group(i, "历史消息") for i in range(1, 5)]
        groups.append(
            [
                Message(role="user", content="u5:帮我导出最终文件"),
                Message(role="assistant", content="已定位到目标目录"),
                Message(role="tool", content="导出成功：/tmp/demo.pptx"),
            ]
        )
        output = await compressor.build_window_messages(
            session_id="s4",
            messages=_flatten(groups),
            router=_NoopRouter(),  # type: ignore[arg-type]
            model_id="",
        )

        status_block = next(m.content for m in output if "【当前任务状态】" in m.content)
        assert "待处理用户请求：u5:帮我导出最终文件" in status_block
        assert "本轮已知进展：" in status_block
        assert "- 本轮输出：已定位到目标目录" in status_block
        assert "- 工具结果：导出成功：/tmp/demo.pptx" in status_block
        assert "下一步：基于以上本轮进展继续完成当前请求" in status_block
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_window_messages_preserves_images_in_current_group(tmp_path: Path) -> None:
    store = await _mk_store(tmp_path)
    try:
        compressor = SessionGroupCompressor(store)
        groups = [_mk_group(i, "历史消息") for i in range(1, 6)]
        groups.append([
            Message(role="user", content="u6:描述这张图", images=[_mk_image("cur")]),
            Message(role="assistant", content="我先看图"),
        ])
        output = await compressor.build_window_messages(
            session_id="s5",
            messages=_flatten(groups),
            router=_NoopRouter(),  # type: ignore[arg-type]
            model_id="",
        )

        current_user = next(
            m for m in output if m.role == "user" and m.content == "u6:描述这张图"
        )
        assert current_user.images is not None
        assert len(current_user.images) == 1
        assert current_user.images[0].data == "cur"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_window_messages_keeps_current_tool_message_untrimmed(tmp_path: Path) -> None:
    store = await _mk_store(tmp_path)
    try:
        compressor = SessionGroupCompressor(store)
        long_tool = "工具输出" * 220
        groups = [_mk_group(i, "历史消息") for i in range(1, 6)]
        groups.append([
            Message(role="user", content="u6:继续"),
            Message(role="tool", content=long_tool, tool_call_id="call_cur"),
        ])
        output = await compressor.build_window_messages(
            session_id="s6",
            messages=_flatten(groups),
            router=_NoopRouter(),  # type: ignore[arg-type]
            model_id="",
        )

        current_tool = output[-1]
        assert current_tool.role == "tool"
        assert current_tool.tool_call_id == "call_cur"
        assert current_tool.content == long_tool
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_window_messages_preserves_previous_four_user_text_and_images(
    tmp_path: Path,
) -> None:
    store = await _mk_store(tmp_path)
    try:
        compressor = SessionGroupCompressor(store)
        groups = [_mk_group(1, "更早历史")]
        groups.extend(_mk_group(i, "历史消息") for i in range(2, 5))
        groups.append([
            Message(role="user", content="u5:看这张图", images=[_mk_image("prev")]),
            Message(role="assistant", content="a5:已收到"),
        ])
        groups.append([Message(role="user", content="u6:当前轮用户请求")])
        output = await compressor.build_window_messages(
            session_id="s7",
            messages=_flatten(groups),
            router=_NoopRouter(),  # type: ignore[arg-type]
            model_id="",
        )

        for i in range(2, 5):
            assert any(m.role == "user" and m.content == f"u{i}:历史消息" for m in output)
        prev_user = next(m for m in output if m.role == "user" and m.content == "u5:看这张图")
        assert prev_user.images is not None
        assert prev_user.images[0].data == "prev"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_window_messages_truncates_previous_four_tool_results_without_collapsing_structure(
    tmp_path: Path,
) -> None:
    store = await _mk_store(tmp_path)
    try:
        compressor = SessionGroupCompressor(store)
        long_tool = "结果明细" * 220
        groups = [_mk_group(1, "更早历史")]
        groups.extend(_mk_group(i, "历史消息") for i in range(2, 5))
        groups.append([
            Message(role="user", content="u5:执行工具"),
            Message(role="tool", content=long_tool, tool_call_id="call_prev"),
        ])
        groups.append([Message(role="user", content="u6:当前轮用户请求")])
        output = await compressor.build_window_messages(
            session_id="s8",
            messages=_flatten(groups),
            router=_NoopRouter(),  # type: ignore[arg-type]
            model_id="",
        )

        prev_tool = next(m for m in output if m.role == "tool" and m.tool_call_id == "call_prev")
        assert prev_tool.content != long_tool
        assert prev_tool.content.endswith("...[已截断]")
        assert "结果明细" in prev_tool.content
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_window_messages_still_compresses_sixth_previous_and_older_groups(
    tmp_path: Path,
) -> None:
    store = await _mk_store(tmp_path)
    try:
        compressor = SessionGroupCompressor(store)
        groups = [_mk_group(i, f"历史消息{i}") for i in range(1, 8)]
        groups.append([Message(role="user", content="u8:当前轮用户请求")])
        output = await compressor.build_window_messages(
            session_id="s9",
            messages=_flatten(groups),
            router=_NoopRouter(),  # type: ignore[arg-type]
            model_id="",
        )

        history_block = next(m.content for m in output if m.content.startswith("【历史摘要"))
        assert "第3轮:" in history_block
        assert "第2轮:" in history_block
        assert not any(m.role == "user" and m.content == "u3:历史消息3" for m in output)
        assert any(m.role == "user" and m.content == "u4:历史消息4" for m in output)
    finally:
        await store.close()


def test_compact_prev_group_preserves_images_and_structured_fields() -> None:
    img = _mk_image("keep")
    tool_call = ToolCall(id="call_1", name="browser", arguments={"q": "x"})
    group = [
        Message(role="user", content="u1:原样保留", images=[img]),
        Message(role="assistant", content="a1:图像消息", images=[img]),
        Message(
            role="assistant",
            content="a2:" + ("长文本" * 220),
            tool_calls=[tool_call],
        ),
        Message(role="tool", content="t1:" + ("工具结果" * 220), tool_call_id="call_1"),
    ]

    compacted = _compact_prev_group(group)

    assert compacted[0].content == "u1:原样保留"
    assert compacted[0].images is not None
    assert compacted[1].content == "a1:图像消息"
    assert compacted[1].images is not None
    assert compacted[2].content.endswith("...[已截断]")
    assert compacted[2].tool_calls == [tool_call]
    assert compacted[3].content.endswith("...[已截断]")
    assert compacted[3].tool_call_id == "call_1"
