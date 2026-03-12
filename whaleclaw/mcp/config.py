"""MCP configuration models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class McpServerConfig(BaseModel):
    """Configuration for a single MCP server.

    Examples in ``whaleclaw.json``::

        {
          "mcp": {
            "servers": {
              "dingtalk": {
                "transport": "streamable_http",
                "url": "https://mcp.dingtalk.com/v1/abcdef",
                "headers": {"Authorization": "Bearer xxx"}
              },
              "github": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_TOKEN": "ghp_xxx"}
              }
            }
          }
        }
    """

    transport: Literal["streamable_http", "sse", "stdio"] = "streamable_http"

    # --- streamable_http / sse ---
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)

    # --- stdio ---
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    # --- common ---
    enabled: bool = True
    timeout: int = 30


class McpConfig(BaseModel):
    """Root MCP configuration block inside ``WhaleclawConfig``."""

    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
