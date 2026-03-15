"""MCP client — JSON-RPC over Streamable HTTP, SSE, or stdio.

Each ``McpClient`` instance manages one connection to one MCP server.
It handles:
- ``initialize`` handshake (protocol version negotiation)
- ``tools/list`` discovery
- ``tools/call`` invocation
- Automatic reconnection on transient failures
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx

from whaleclaw.mcp.config import McpServerConfig
from whaleclaw.utils.log import get_logger

log = get_logger(__name__)

_MCP_PROTOCOL_VERSION = "2025-03-26"
_MAX_OUTPUT_LEN = 16_000


class McpError(Exception):
    """Error from an MCP server response."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        self.code = code
        self.data = data
        super().__init__(f"MCP error {code}: {message}")


class McpClient:
    """Async client for a single MCP server."""

    def __init__(self, server_id: str, cfg: McpServerConfig) -> None:
        self._id = server_id
        self._cfg = cfg
        self._request_id = 0
        self._initialized = False

        # stdio transport state
        self._proc: asyncio.subprocess.Process | None = None

        # HTTP transport state
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open transport and run the ``initialize`` handshake."""
        transport = self._cfg.transport
        if transport in ("streamable_http", "sse"):
            if not self._cfg.url:
                raise McpError(-1, f"MCP server '{self._id}' has no url configured")
            self._http = httpx.AsyncClient(
                timeout=float(self._cfg.timeout),
                headers={
                    "Content-Type": "application/json",
                    **self._cfg.headers,
                },
                follow_redirects=True,
            )
        elif transport == "stdio":
            if not self._cfg.command:
                raise McpError(-1, f"MCP server '{self._id}' has no command configured")
            await self._spawn_process()
        else:
            raise McpError(-1, f"Unknown transport: {transport}")

        await self._handshake()
        self._initialized = True
        log.info("mcp.connected", server=self._id, transport=transport)

    async def close(self) -> None:
        """Shut down transport."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except TimeoutError:
                self._proc.kill()
            self._proc = None
        self._initialized = False
        log.info("mcp.disconnected", server=self._id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """Call ``tools/list`` and return the tools array."""
        result = await self._call("tools/list")
        tools: list[dict[str, Any]] = result.get("tools", [])
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call ``tools/call`` and return the text content.

        MCP ``tools/call`` returns a ``content`` array.  We concatenate
        all text items into one string for WhaleClaw's ``ToolResult.output``.
        """
        result = await self._call("tools/call", {"name": name, "arguments": arguments})
        is_error = result.get("isError", False)
        content_items: list[dict[str, Any]] = result.get("content", [])
        parts: list[str] = []
        for item in content_items:
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif item.get("type") == "image":
                parts.append("[image data]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        text = "\n".join(parts)
        if len(text) > _MAX_OUTPUT_LEN:
            text = text[:_MAX_OUTPUT_LEN] + "\n...(truncated)"
        if is_error:
            raise McpError(-1, text)
        return text

    # ------------------------------------------------------------------
    # JSON-RPC layer
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC request and return the ``result`` field."""
        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            request["params"] = params

        transport = self._cfg.transport
        if transport == "streamable_http":
            return await self._call_http(request)
        if transport == "sse":
            return await self._call_sse(request)
        if transport == "stdio":
            return await self._call_stdio(request)
        raise McpError(-1, f"Unknown transport: {transport}")

    # ------ Streamable HTTP ------

    async def _call_http(self, request: dict[str, Any]) -> Any:
        assert self._http is not None  # noqa: S101
        try:
            resp = await self._http.post(
                self._cfg.url,
                json=request,
                headers={"Accept": "application/json, text/event-stream"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise McpError(
                exc.response.status_code,
                f"HTTP {exc.response.status_code}: {exc.response.text[:500]}",
            ) from exc
        except httpx.HTTPError as exc:
            raise McpError(-1, str(exc)) from exc

        # Some servers return SSE even for Streamable HTTP; handle both.
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse_body(resp.text)

        data: dict[str, Any] = resp.json()
        if "error" in data:
            err = data["error"]
            raise McpError(err.get("code", -1), err.get("message", "unknown"))
        return data.get("result", {})

    # ------ SSE ------

    async def _call_sse(self, request: dict[str, Any]) -> Any:
        """Post JSON-RPC and read SSE stream for the result."""
        assert self._http is not None  # noqa: S101
        try:
            async with self._http.stream(
                "POST", self._cfg.url, json=request
            ) as resp:
                resp.raise_for_status()
                collected: list[str] = []
                async for line in resp.aiter_lines():
                    collected.append(line)
                return self._parse_sse_body("\n".join(collected))
        except httpx.HTTPError as exc:
            raise McpError(-1, str(exc)) from exc

    @staticmethod
    def _parse_sse_body(body: str) -> Any:
        """Extract the last JSON-RPC result from an SSE text body."""
        last_data: str = ""
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if line.startswith("data:"):
                last_data = line[5:].strip()
        if not last_data:
            raise McpError(-1, "Empty SSE response")
        obj: dict[str, Any] = json.loads(last_data)
        if "error" in obj:
            err = obj["error"]
            raise McpError(err.get("code", -1), err.get("message", "unknown"))
        return obj.get("result", {})

    # ------ stdio ------

    async def _spawn_process(self) -> None:
        """Spawn the MCP server as a subprocess."""
        env = {**os.environ, **self._cfg.env}
        cmd = self._cfg.command
        args = self._cfg.args

        # On Windows, resolve npx/node from PATH
        if sys.platform == "win32" and not cmd.endswith(".exe"):
            cmd_candidates = [cmd, f"{cmd}.cmd", f"{cmd}.exe"]
        else:
            cmd_candidates = [cmd]

        last_err: Exception | None = None
        for candidate in cmd_candidates:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    candidate,
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                return
            except FileNotFoundError as exc:
                last_err = exc
        raise McpError(-1, f"Cannot spawn '{cmd}': {last_err}")

    async def _call_stdio(self, request: dict[str, Any]) -> Any:
        """Send JSON-RPC over stdin and read one line from stdout."""
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise McpError(-1, "stdio process not running")

        payload = json.dumps(request, ensure_ascii=False) + "\n"
        self._proc.stdin.write(payload.encode("utf-8"))
        await self._proc.stdin.drain()

        try:
            raw = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=float(self._cfg.timeout),
            )
        except TimeoutError as exc:
            raise McpError(-1, f"stdio read timeout ({self._cfg.timeout}s)") from exc

        if not raw:
            raise McpError(-1, "stdio: empty response (process may have exited)")

        data: dict[str, Any] = json.loads(raw.decode("utf-8"))
        if "error" in data:
            err = data["error"]
            raise McpError(err.get("code", -1), err.get("message", "unknown"))
        return data.get("result", {})

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    async def _handshake(self) -> None:
        """Run the MCP ``initialize`` + ``initialized`` handshake."""
        try:
            result = await self._call("initialize", {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "whaleclaw", "version": "0.1.0"},
            })
            log.debug(
                "mcp.handshake_ok",
                server=self._id,
                server_info=result.get("serverInfo"),
                protocol=result.get("protocolVersion"),
            )
            # Send the ``initialized`` notification (no id → no response expected).
            # For stdio/SSE this is a plain JSON line; for HTTP we POST and ignore.
            notification: dict[str, Any] = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
            if self._cfg.transport == "stdio":
                if self._proc and self._proc.stdin:
                    line = json.dumps(notification, ensure_ascii=False) + "\n"
                    self._proc.stdin.write(line.encode("utf-8"))
                    await self._proc.stdin.drain()
            elif self._http is not None:
                try:
                    await self._http.post(self._cfg.url, json=notification)
                except httpx.HTTPError:
                    pass  # notifications may not return a response
        except McpError:
            log.warning("mcp.handshake_failed", server=self._id)
            raise
        except Exception as exc:
            raise McpError(-1, f"Handshake failed: {exc}") from exc
