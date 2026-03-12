"""Tests for whaleclaw.mcp.manager — MCP lifecycle manager."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from whaleclaw.mcp.client import McpClient, McpError
from whaleclaw.mcp.config import McpConfig, McpServerConfig
from whaleclaw.mcp.manager import McpManager
from whaleclaw.tools.registry import ToolRegistry


def _make_server_cfg(**overrides: Any) -> McpServerConfig:
    defaults: dict[str, Any] = {
        "transport": "streamable_http",
        "url": "https://example.com/mcp",
        "enabled": True,
    }
    defaults.update(overrides)
    return McpServerConfig(**defaults)


def _sample_tools_response() -> list[dict[str, Any]]:
    return [
        {
            "name": "create_record",
            "description": "Create a record",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "table_id": {"type": "string", "description": "Table ID"},
                    "data": {"type": "string", "description": "JSON data"},
                },
                "required": ["table_id"],
            },
        },
        {
            "name": "list_records",
            "description": "List records",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


@pytest.mark.asyncio
async def test_start_registers_tools() -> None:
    """start() should connect, discover tools, and register them."""
    registry = ToolRegistry()
    manager = McpManager()

    mcp_config = McpConfig(servers={
        "dingtalk": _make_server_cfg(),
    })

    with (
        patch.object(McpClient, "connect", new_callable=AsyncMock),
        patch.object(
            McpClient, "list_tools", new_callable=AsyncMock,
            return_value=_sample_tools_response(),
        ),
    ):
        await manager.start(mcp_config, registry)

    assert manager.get_tool_count() == 2
    assert registry.get("mcp__dingtalk__create_record") is not None
    assert registry.get("mcp__dingtalk__list_records") is not None

    servers = manager.list_servers()
    assert len(servers) == 1
    assert servers[0]["id"] == "dingtalk"
    assert servers[0]["tool_count"] == 2

    await manager.stop()


@pytest.mark.asyncio
async def test_start_skips_disabled() -> None:
    """start() should skip disabled servers."""
    registry = ToolRegistry()
    manager = McpManager()

    mcp_config = McpConfig(servers={
        "dingtalk": _make_server_cfg(enabled=False),
    })

    await manager.start(mcp_config, registry)
    assert manager.get_tool_count() == 0
    assert len(manager.list_servers()) == 0


@pytest.mark.asyncio
async def test_start_continues_on_failure() -> None:
    """start() should log warning and continue if one server fails."""
    registry = ToolRegistry()
    manager = McpManager()

    mcp_config = McpConfig(servers={
        "bad_server": _make_server_cfg(),
        "good_server": _make_server_cfg(url="https://good.example.com/mcp"),
    })

    call_count = 0

    async def _mock_connect(self: McpClient) -> None:
        nonlocal call_count
        call_count += 1
        if self._id == "bad_server":  # noqa: SLF001
            raise McpError(-1, "Connection refused")

    with (
        patch.object(McpClient, "connect", _mock_connect),
        patch.object(
            McpClient, "list_tools", new_callable=AsyncMock,
            return_value=_sample_tools_response(),
        ),
    ):
        await manager.start(mcp_config, registry)

    # good_server should succeed even though bad_server failed
    assert manager.get_tool_count() == 2
    assert len(manager.list_servers()) == 1

    await manager.stop()


@pytest.mark.asyncio
async def test_stop_unregisters_tools() -> None:
    """stop() should disconnect and unregister all tools."""
    registry = ToolRegistry()
    manager = McpManager()

    mcp_config = McpConfig(servers={
        "dingtalk": _make_server_cfg(),
    })

    with (
        patch.object(McpClient, "connect", new_callable=AsyncMock),
        patch.object(
            McpClient, "list_tools", new_callable=AsyncMock,
            return_value=_sample_tools_response(),
        ),
    ):
        await manager.start(mcp_config, registry)

    assert registry.get("mcp__dingtalk__create_record") is not None

    with patch.object(McpClient, "close", new_callable=AsyncMock):
        await manager.stop()

    assert registry.get("mcp__dingtalk__create_record") is None
    assert manager.get_tool_count() == 0


@pytest.mark.asyncio
async def test_add_server_at_runtime() -> None:
    """add_server() should connect and register tools dynamically."""
    registry = ToolRegistry()
    manager = McpManager()

    await manager.start(McpConfig(), registry)
    assert manager.get_tool_count() == 0

    with (
        patch.object(McpClient, "connect", new_callable=AsyncMock),
        patch.object(
            McpClient, "list_tools", new_callable=AsyncMock,
            return_value=_sample_tools_response(),
        ),
    ):
        count = await manager.add_server("github", _make_server_cfg(
            url="https://github-mcp.example.com"
        ))

    assert count == 2
    assert registry.get("mcp__github__create_record") is not None

    with patch.object(McpClient, "close", new_callable=AsyncMock):
        await manager.stop()


@pytest.mark.asyncio
async def test_remove_server() -> None:
    """remove_server() should disconnect and unregister tools."""
    registry = ToolRegistry()
    manager = McpManager()

    mcp_config = McpConfig(servers={
        "dingtalk": _make_server_cfg(),
    })

    with (
        patch.object(McpClient, "connect", new_callable=AsyncMock),
        patch.object(
            McpClient, "list_tools", new_callable=AsyncMock,
            return_value=_sample_tools_response(),
        ),
    ):
        await manager.start(mcp_config, registry)

    assert manager.get_tool_count() == 2

    with patch.object(McpClient, "close", new_callable=AsyncMock):
        await manager.remove_server("dingtalk")

    assert manager.get_tool_count() == 0
    assert registry.get("mcp__dingtalk__create_record") is None
