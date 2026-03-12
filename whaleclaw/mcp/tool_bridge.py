"""Bridge MCP tools into WhaleClaw's native Tool interface.

Each MCP tool discovered via ``tools/list`` is wrapped as an
``McpBridgedTool`` that the ``ToolRegistry`` can register and the
Agent loop can call exactly like any built-in tool.
"""

from __future__ import annotations

import json
from typing import Any

from whaleclaw.mcp.client import McpClient, McpError
from whaleclaw.tools.base import Tool, ToolDefinition, ToolParameter, ToolResult
from whaleclaw.utils.log import get_logger

log = get_logger(__name__)


def _build_tool_name(server_id: str, tool_name: str) -> str:
    """Build the registered tool name: ``mcp__{server}__{tool}``.

    Double underscores avoid collisions with built-in tool names.
    """
    safe_server = server_id.replace("-", "_").replace(".", "_")
    safe_tool = tool_name.replace("-", "_").replace(".", "_")
    return f"mcp__{safe_server}__{safe_tool}"


def _extract_parameters(input_schema: dict[str, Any]) -> list[ToolParameter]:
    """Convert a JSON Schema ``properties`` block to ``ToolParameter`` list."""
    properties: dict[str, Any] = input_schema.get("properties", {})
    required_set: set[str] = set(input_schema.get("required", []))
    params: list[ToolParameter] = []
    for name, prop in properties.items():
        json_type = prop.get("type", "string")
        # Map JSON Schema types to simplified type strings
        type_str = json_type if isinstance(json_type, str) else "string"
        desc = prop.get("description", "")
        enum_values: list[str] | None = prop.get("enum")
        params.append(
            ToolParameter(
                name=name,
                type=type_str,
                description=desc,
                required=name in required_set,
                enum=enum_values,
            )
        )
    return params


class McpBridgedTool(Tool):
    """A WhaleClaw ``Tool`` backed by an MCP server tool."""

    def __init__(
        self,
        *,
        server_id: str,
        mcp_tool_name: str,
        description: str,
        input_schema: dict[str, Any],
        client: McpClient,
    ) -> None:
        self._server_id = server_id
        self._mcp_tool_name = mcp_tool_name
        self._client = client
        self._registered_name = _build_tool_name(server_id, mcp_tool_name)
        self._description = description or f"MCP tool {mcp_tool_name} from {server_id}"
        self._parameters = _extract_parameters(input_schema)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._registered_name,
            description=f"[MCP:{self._server_id}] {self._description}",
            parameters=self._parameters,
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Forward the call to the MCP server via ``tools/call``."""
        log.info(
            "mcp.tool_call",
            server=self._server_id,
            tool=self._mcp_tool_name,
            args_preview=str(kwargs)[:200],
        )
        try:
            output = await self._client.call_tool(self._mcp_tool_name, kwargs)
            return ToolResult(success=True, output=output)
        except McpError as exc:
            log.warning(
                "mcp.tool_error",
                server=self._server_id,
                tool=self._mcp_tool_name,
                error=str(exc),
            )
            return ToolResult(success=False, output="", error=str(exc))
        except Exception as exc:
            log.error(
                "mcp.tool_unexpected_error",
                server=self._server_id,
                tool=self._mcp_tool_name,
                error=str(exc),
            )
            return ToolResult(
                success=False,
                output="",
                error=f"MCP 调用失败 ({self._server_id}/{self._mcp_tool_name}): {exc}",
            )


def create_bridged_tools(
    server_id: str,
    raw_tools: list[dict[str, Any]],
    client: McpClient,
) -> list[McpBridgedTool]:
    """Create ``McpBridgedTool`` instances from raw ``tools/list`` response."""
    bridged: list[McpBridgedTool] = []
    for raw in raw_tools:
        name = raw.get("name", "")
        if not name:
            continue
        bridged.append(
            McpBridgedTool(
                server_id=server_id,
                mcp_tool_name=name,
                description=raw.get("description", ""),
                input_schema=raw.get("inputSchema", {}),
                client=client,
            )
        )
    return bridged
