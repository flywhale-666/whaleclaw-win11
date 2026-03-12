"""MCP (Model Context Protocol) client integration for WhaleClaw.

Connects to external MCP servers, discovers their tools, and bridges
them into WhaleClaw's native ToolRegistry so the Agent can call them
directly via structured tool_call — no bash hacks needed.

Supports three MCP transport modes:
- **Streamable HTTP** — POST JSON-RPC to an HTTP endpoint (most common)
- **SSE** — Server-Sent Events stream over HTTP
- **stdio** — spawn a local subprocess and communicate via stdin/stdout
"""

from whaleclaw.mcp.manager import McpManager

__all__ = ["McpManager"]
