"""Tests for whaleclaw.mcp.tool_bridge — MCP-to-WhaleClaw tool bridging."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from whaleclaw.mcp.client import McpClient, McpError
from whaleclaw.mcp.config import McpServerConfig
from whaleclaw.mcp.tool_bridge import (
    McpBridgedTool,
    _build_tool_name,
    _extract_parameters,
    create_bridged_tools,
)


# ---------------------------------------------------------------------------
# _build_tool_name
# ---------------------------------------------------------------------------


def test_build_tool_name_simple() -> None:
    assert _build_tool_name("dingtalk", "create_table") == "mcp__dingtalk__create_table"


def test_build_tool_name_with_hyphens() -> None:
    assert _build_tool_name("dingtalk-ai-table", "get-records") == (
        "mcp__dingtalk_ai_table__get_records"
    )


def test_build_tool_name_with_dots() -> None:
    assert _build_tool_name("my.server", "tool.name") == "mcp__my_server__tool_name"


# ---------------------------------------------------------------------------
# _extract_parameters
# ---------------------------------------------------------------------------


def test_extract_parameters_basic() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name"},
            "count": {"type": "integer", "description": "Row count"},
        },
        "required": ["name"],
    }
    params = _extract_parameters(schema)
    assert len(params) == 2

    name_param = next(p for p in params if p.name == "name")
    assert name_param.required is True
    assert name_param.type == "string"

    count_param = next(p for p in params if p.name == "count")
    assert count_param.required is False
    assert count_param.type == "integer"


def test_extract_parameters_with_enum() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Status",
                "enum": ["active", "archived"],
            },
        },
    }
    params = _extract_parameters(schema)
    assert len(params) == 1
    assert params[0].enum == ["active", "archived"]


def test_extract_parameters_empty() -> None:
    params = _extract_parameters({})
    assert params == []


# ---------------------------------------------------------------------------
# McpBridgedTool
# ---------------------------------------------------------------------------


def _make_client() -> McpClient:
    cfg = McpServerConfig(transport="streamable_http", url="https://x.com/mcp")
    return McpClient("test_server", cfg)


def test_bridged_tool_definition() -> None:
    client = _make_client()
    tool = McpBridgedTool(
        server_id="dingtalk",
        mcp_tool_name="create_table",
        description="Create a new table",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Table name"},
            },
            "required": ["name"],
        },
        client=client,
    )
    defn = tool.definition
    assert defn.name == "mcp__dingtalk__create_table"
    assert "[MCP:dingtalk]" in defn.description
    assert len(defn.parameters) == 1
    assert defn.parameters[0].name == "name"


@pytest.mark.asyncio
async def test_bridged_tool_execute_success() -> None:
    client = _make_client()
    tool = McpBridgedTool(
        server_id="dingtalk",
        mcp_tool_name="create_table",
        description="Create a new table",
        input_schema={"type": "object", "properties": {}},
        client=client,
    )

    with patch.object(client, "call_tool", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "Table created!"
        result = await tool.execute(name="test")

    assert result.success is True
    assert result.output == "Table created!"
    mock_call.assert_called_once_with("create_table", {"name": "test"})


@pytest.mark.asyncio
async def test_bridged_tool_execute_mcp_error() -> None:
    client = _make_client()
    tool = McpBridgedTool(
        server_id="dingtalk",
        mcp_tool_name="bad_tool",
        description="A tool that fails",
        input_schema={"type": "object", "properties": {}},
        client=client,
    )

    with patch.object(client, "call_tool", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = McpError(-1, "Server offline")
        result = await tool.execute()

    assert result.success is False
    assert "Server offline" in (result.error or "")


# ---------------------------------------------------------------------------
# create_bridged_tools
# ---------------------------------------------------------------------------


def test_create_bridged_tools() -> None:
    client = _make_client()
    raw: list[dict[str, Any]] = [
        {
            "name": "create_table",
            "description": "Create table",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "delete_table",
            "description": "Delete table",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "",  # should be skipped
            "description": "Empty name",
        },
    ]
    tools = create_bridged_tools("dingtalk", raw, client)
    assert len(tools) == 2
    assert tools[0].definition.name == "mcp__dingtalk__create_table"
    assert tools[1].definition.name == "mcp__dingtalk__delete_table"
