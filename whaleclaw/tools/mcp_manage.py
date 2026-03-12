"""MCP server management tool — list/add/remove MCP servers at runtime."""

from __future__ import annotations

import json
from typing import Any

from whaleclaw.mcp.config import McpServerConfig
from whaleclaw.mcp.manager import McpManager
from whaleclaw.tools.base import Tool, ToolDefinition, ToolParameter, ToolResult
from whaleclaw.utils.log import get_logger

log = get_logger(__name__)


class McpManageTool(Tool):
    """Agent-callable tool for managing MCP servers."""

    def __init__(self, mcp_manager: McpManager) -> None:
        self._mgr = mcp_manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="mcp_manage",
            description=(
                "Manage MCP (Model Context Protocol) servers: "
                "list connected servers and their tools, "
                "add a new MCP server by URL, "
                "remove an existing server, "
                "or reconnect to refresh tools."
            ),
            parameters=[
                ToolParameter(
                    name="action",
                    type="string",
                    description="Action to perform.",
                    enum=["list", "add", "remove", "reconnect"],
                ),
                ToolParameter(
                    name="server_id",
                    type="string",
                    description=(
                        "Server identifier. Required for add/remove/reconnect. "
                        "Use a short snake_case name like 'dingtalk' or 'github'."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="url",
                    type="string",
                    description=(
                        "MCP server URL (Streamable HTTP endpoint). "
                        "Required for add action."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="transport",
                    type="string",
                    description="Transport type: streamable_http, sse, or stdio.",
                    enum=["streamable_http", "sse", "stdio"],
                    required=False,
                ),
                ToolParameter(
                    name="command",
                    type="string",
                    description="Command to run for stdio transport (e.g. 'npx').",
                    required=False,
                ),
                ToolParameter(
                    name="args",
                    type="string",
                    description=(
                        "JSON array of command arguments for stdio transport. "
                        "Example: '[\"-y\", \"@modelcontextprotocol/server-github\"]'"
                    ),
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()

        if action == "list":
            return self._list()
        if action == "add":
            return await self._add(kwargs)
        if action == "remove":
            return await self._remove(kwargs)
        if action == "reconnect":
            return await self._reconnect(kwargs)
        return ToolResult(success=False, output="", error=f"未知操作: {action}")

    def _list(self) -> ToolResult:
        servers = self._mgr.list_servers()
        if not servers:
            return ToolResult(
                success=True,
                output=(
                    "当前没有已连接的 MCP 服务器。"
                    "注意：通过 mcporter CLI 管理的 MCP 服务不会出现在此列表中。"
                    "如果技能要求使用 mcporter，请直接用 bash 工具执行 "
                    "`mcporter call <server> <tool> <args>` 命令，不要再调用 mcp_manage。"
                ),
            )
        lines: list[str] = []
        for s in servers:
            tools_list = s.get("tools", [])
            tools_str = ", ".join(str(t) for t in tools_list[:10])
            if len(tools_list) > 10:
                tools_str += f" ...+{len(tools_list) - 10}"
            lines.append(
                f"- {s['id']} ({s['transport']}) — "
                f"{s['tool_count']} 个工具: {tools_str}"
            )
        return ToolResult(success=True, output="\n".join(lines))

    async def _add(self, kwargs: dict[str, Any]) -> ToolResult:
        server_id = str(kwargs.get("server_id", "")).strip()
        if not server_id:
            return ToolResult(
                success=False, output="", error="add 操作需要 server_id"
            )

        transport = str(kwargs.get("transport", "streamable_http")).strip()
        url = str(kwargs.get("url", "")).strip()
        command = str(kwargs.get("command", "")).strip()
        raw_args = str(kwargs.get("args", "")).strip()

        if transport in ("streamable_http", "sse") and not url:
            return ToolResult(
                success=False,
                output="",
                error=f"{transport} 传输需要 url 参数",
            )
        if transport == "stdio" and not command:
            return ToolResult(
                success=False, output="", error="stdio 传输需要 command 参数"
            )

        cmd_args: list[str] = []
        if raw_args:
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, list):
                    cmd_args = [str(a) for a in parsed]
            except json.JSONDecodeError:
                cmd_args = raw_args.split()

        cfg = McpServerConfig(
            transport=transport,  # type: ignore[arg-type]
            url=url,
            command=command,
            args=cmd_args,
            enabled=True,
        )

        try:
            tool_count = await self._mgr.add_server(server_id, cfg)
            return ToolResult(
                success=True,
                output=f"已添加 MCP 服务器 '{server_id}'，发现 {tool_count} 个工具。",
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"添加 MCP 服务器失败: {exc}",
            )

    async def _remove(self, kwargs: dict[str, Any]) -> ToolResult:
        server_id = str(kwargs.get("server_id", "")).strip()
        if not server_id:
            return ToolResult(
                success=False, output="", error="remove 操作需要 server_id"
            )
        try:
            await self._mgr.remove_server(server_id)
            return ToolResult(
                success=True,
                output=f"已移除 MCP 服务器 '{server_id}'。",
            )
        except Exception as exc:
            return ToolResult(
                success=False, output="", error=f"移除失败: {exc}"
            )

    async def _reconnect(self, kwargs: dict[str, Any]) -> ToolResult:
        server_id = str(kwargs.get("server_id", "")).strip()
        if not server_id:
            return ToolResult(
                success=False, output="", error="reconnect 操作需要 server_id"
            )
        try:
            tool_count = await self._mgr.reconnect_server(server_id)
            return ToolResult(
                success=True,
                output=f"已重连 MCP 服务器 '{server_id}'，发现 {tool_count} 个工具。",
            )
        except Exception as exc:
            return ToolResult(
                success=False, output="", error=f"重连失败: {exc}"
            )
