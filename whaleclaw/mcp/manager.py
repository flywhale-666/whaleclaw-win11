"""MCP manager — lifecycle for all configured MCP servers.

Responsibilities:
- Read ``mcp.servers`` from ``WhaleclawConfig``
- Connect to each enabled server
- Discover tools and register them into a ``ToolRegistry``
- Provide add/remove/reconnect at runtime
- Clean shutdown
"""

from __future__ import annotations

from typing import Any

from whaleclaw.mcp.client import McpClient, McpError
from whaleclaw.mcp.config import McpConfig, McpServerConfig
from whaleclaw.mcp.tool_bridge import McpBridgedTool, create_bridged_tools
from whaleclaw.tools.registry import ToolRegistry
from whaleclaw.utils.log import get_logger

log = get_logger(__name__)


class McpManager:
    """Manages all MCP server connections and their bridged tools."""

    def __init__(self) -> None:
        self._clients: dict[str, McpClient] = {}
        self._tools: dict[str, list[McpBridgedTool]] = {}  # server_id -> tools
        self._registry: ToolRegistry | None = None

    # ------------------------------------------------------------------
    # Bulk initialisation (called once at startup)
    # ------------------------------------------------------------------

    async def start(
        self,
        mcp_config: McpConfig,
        registry: ToolRegistry,
    ) -> None:
        """Connect to all enabled MCP servers and register their tools.

        Args:
            mcp_config: The ``mcp`` block from ``WhaleclawConfig``.
            registry: The global ``ToolRegistry`` to register bridged tools into.
        """
        self._registry = registry
        for server_id, server_cfg in mcp_config.servers.items():
            if not server_cfg.enabled:
                log.info("mcp.server_disabled", server=server_id)
                continue
            try:
                await self._connect_server(server_id, server_cfg)
            except Exception as exc:
                log.warning(
                    "mcp.server_start_failed",
                    server=server_id,
                    error=str(exc),
                )

        total_tools = sum(len(tools) for tools in self._tools.values())
        log.info(
            "mcp.manager_started",
            servers=len(self._clients),
            total_tools=total_tools,
        )

    async def stop(self) -> None:
        """Disconnect all MCP servers and unregister their tools."""
        for server_id in list(self._clients.keys()):
            await self._disconnect_server(server_id)
        log.info("mcp.manager_stopped")

    # ------------------------------------------------------------------
    # Runtime add/remove
    # ------------------------------------------------------------------

    async def add_server(
        self,
        server_id: str,
        server_cfg: McpServerConfig,
    ) -> int:
        """Add and connect a new MCP server at runtime.

        Returns:
            Number of tools discovered.
        """
        if server_id in self._clients:
            await self._disconnect_server(server_id)
        await self._connect_server(server_id, server_cfg)
        return len(self._tools.get(server_id, []))

    async def remove_server(self, server_id: str) -> None:
        """Disconnect and remove a MCP server at runtime."""
        await self._disconnect_server(server_id)

    async def reconnect_server(self, server_id: str) -> int:
        """Reconnect an existing server (re-discover tools).

        Returns:
            Number of tools discovered after reconnection.
        """
        client = self._clients.get(server_id)
        if client is None:
            raise McpError(-1, f"MCP server '{server_id}' not found")
        cfg = client._cfg  # noqa: SLF001
        await self._disconnect_server(server_id)
        await self._connect_server(server_id, cfg)
        return len(self._tools.get(server_id, []))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_servers(self) -> list[dict[str, Any]]:
        """Return a summary of all connected servers and their tools."""
        items: list[dict[str, Any]] = []
        for server_id, client in self._clients.items():
            tools = self._tools.get(server_id, [])
            items.append({
                "id": server_id,
                "transport": client._cfg.transport,  # noqa: SLF001
                "tool_count": len(tools),
                "tools": [t.definition.name for t in tools],
            })
        return items

    def get_tool_count(self) -> int:
        """Total number of MCP-bridged tools registered."""
        return sum(len(tools) for tools in self._tools.values())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _connect_server(
        self,
        server_id: str,
        cfg: McpServerConfig,
    ) -> None:
        client = McpClient(server_id, cfg)
        await client.connect()
        self._clients[server_id] = client

        raw_tools = await client.list_tools()
        bridged = create_bridged_tools(server_id, raw_tools, client)
        self._tools[server_id] = bridged

        if self._registry is not None:
            for tool in bridged:
                self._registry.register(tool)

        log.info(
            "mcp.server_connected",
            server=server_id,
            tools=[t.definition.name for t in bridged],
        )

    async def _disconnect_server(self, server_id: str) -> None:
        # Unregister tools first
        if self._registry is not None:
            for tool in self._tools.get(server_id, []):
                self._registry.unregister(tool.definition.name)
        self._tools.pop(server_id, None)

        client = self._clients.pop(server_id, None)
        if client is not None:
            await client.close()
        log.info("mcp.server_disconnected", server=server_id)
