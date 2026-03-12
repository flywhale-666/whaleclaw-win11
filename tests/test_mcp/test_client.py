"""Tests for whaleclaw.mcp.client — MCP JSON-RPC client."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from whaleclaw.mcp.client import McpClient, McpError
from whaleclaw.mcp.config import McpServerConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _http_cfg(**overrides: Any) -> McpServerConfig:
    defaults = {
        "transport": "streamable_http",
        "url": "https://example.com/mcp",
        "enabled": True,
        "timeout": 10,
    }
    defaults.update(overrides)
    return McpServerConfig(**defaults)


def _make_jsonrpc_response(result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _make_jsonrpc_error(code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_success() -> None:
    """connect() should complete the initialize handshake."""
    cfg = _http_cfg()
    client = McpClient("test", cfg)

    init_resp = _make_jsonrpc_response({
        "protocolVersion": "2025-03-26",
        "serverInfo": {"name": "test-server"},
        "capabilities": {},
    })

    mock_response = httpx.Response(
        200,
        json=init_resp,
        request=httpx.Request("POST", cfg.url),
    )

    with patch.object(httpx.AsyncClient, "post", return_value=mock_response):
        await client.connect()

    assert client._initialized is True  # noqa: SLF001
    await client.close()


@pytest.mark.asyncio
async def test_connect_missing_url() -> None:
    """connect() should raise McpError when url is empty."""
    cfg = _http_cfg(url="")
    client = McpClient("test", cfg)
    with pytest.raises(McpError, match="no url"):
        await client.connect()


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools() -> None:
    """list_tools() should return parsed tool dicts."""
    cfg = _http_cfg()
    client = McpClient("test", cfg)
    client._initialized = True  # noqa: SLF001

    tools_resp = _make_jsonrpc_response({
        "tools": [
            {
                "name": "create_table",
                "description": "Create a new table",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Table name"},
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "delete_table",
                "description": "Delete a table",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "table_id": {"type": "string", "description": "Table ID"},
                    },
                    "required": ["table_id"],
                },
            },
        ]
    })
    mock_resp = httpx.Response(
        200, json=tools_resp, request=httpx.Request("POST", cfg.url)
    )
    client._http = httpx.AsyncClient()  # noqa: SLF001

    with patch.object(httpx.AsyncClient, "post", return_value=mock_resp):
        tools = await client.list_tools()

    assert len(tools) == 2
    assert tools[0]["name"] == "create_table"
    assert tools[1]["name"] == "delete_table"
    await client.close()


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_success() -> None:
    """call_tool() should return concatenated text content."""
    cfg = _http_cfg()
    client = McpClient("test", cfg)
    client._initialized = True  # noqa: SLF001

    call_resp = _make_jsonrpc_response({
        "content": [
            {"type": "text", "text": "Table created successfully."},
        ],
    })
    mock_resp = httpx.Response(
        200, json=call_resp, request=httpx.Request("POST", cfg.url)
    )
    client._http = httpx.AsyncClient()  # noqa: SLF001

    with patch.object(httpx.AsyncClient, "post", return_value=mock_resp):
        result = await client.call_tool("create_table", {"name": "test"})

    assert result == "Table created successfully."
    await client.close()


@pytest.mark.asyncio
async def test_call_tool_error_response() -> None:
    """call_tool() should raise McpError when isError is True."""
    cfg = _http_cfg()
    client = McpClient("test", cfg)
    client._initialized = True  # noqa: SLF001

    call_resp = _make_jsonrpc_response({
        "isError": True,
        "content": [
            {"type": "text", "text": "Table not found"},
        ],
    })
    mock_resp = httpx.Response(
        200, json=call_resp, request=httpx.Request("POST", cfg.url)
    )
    client._http = httpx.AsyncClient()  # noqa: SLF001

    with (
        patch.object(httpx.AsyncClient, "post", return_value=mock_resp),
        pytest.raises(McpError, match="Table not found"),
    ):
        await client.call_tool("get_table", {"id": "xxx"})
    await client.close()


@pytest.mark.asyncio
async def test_call_tool_jsonrpc_error() -> None:
    """call_tool() should raise McpError on JSON-RPC error."""
    cfg = _http_cfg()
    client = McpClient("test", cfg)
    client._initialized = True  # noqa: SLF001

    error_resp = _make_jsonrpc_error(-32600, "Invalid Request")
    mock_resp = httpx.Response(
        200, json=error_resp, request=httpx.Request("POST", cfg.url)
    )
    client._http = httpx.AsyncClient()  # noqa: SLF001

    with (
        patch.object(httpx.AsyncClient, "post", return_value=mock_resp),
        pytest.raises(McpError, match="Invalid Request"),
    ):
        await client.call_tool("anything", {})
    await client.close()


# ---------------------------------------------------------------------------
# SSE body parsing
# ---------------------------------------------------------------------------


def test_parse_sse_body() -> None:
    """_parse_sse_body should extract the last data line."""
    body = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n'
        "\n"
    )
    result = McpClient._parse_sse_body(body)  # noqa: SLF001
    assert result == {"tools": []}


def test_parse_sse_body_empty() -> None:
    """_parse_sse_body should raise on empty body."""
    with pytest.raises(McpError, match="Empty SSE"):
        McpClient._parse_sse_body("")  # noqa: SLF001
