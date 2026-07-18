"""
mcp/protocol.py

JSON-RPC 2.0 client for MCP (Model Context Protocol).

CC-aligned: implements the core MCP lifecycle:
  initialize → tools/list → tools/call
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from mcp.transport import McpTransport

logger = logging.getLogger(__name__)

# ── JSON-RPC 2.0 message types ──────────────────────────────────────────


@dataclass
class JsonRpcRequest:
    jsonrpc: str = "2.0"
    id: int = 0
    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class JsonRpcResponse:
    jsonrpc: str = "2.0"
    id: int = 0
    result: Any = None
    error: dict[str, Any] | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


# ── MCP client ──────────────────────────────────────────────────────────


class McpClient:
    """Base MCP client — JSON-RPC 2.0 request/response over a transport.

    CC reference: services/mcp/client.ts (~3348 lines).
    Our implementation focuses on the core MCP lifecycle:
    initialize → tools/list → tools/call.
    """

    # MCP protocol version (2024-11-05 spec)
    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, transport: McpTransport, server_name: str = "") -> None:
        self._transport = transport
        self._server_name = server_name
        self._request_id = 0
        self._server_capabilities: dict[str, Any] = {}
        self._client_capabilities: dict[str, Any] = {
            "roots": {"listChanged": True},
            "sampling": {},
        }

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def capabilities(self) -> dict[str, Any]:
        return dict(self._server_capabilities)

    # ── Lifecycle ────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish transport connection."""
        await self._transport.connect()

    async def initialize(self) -> dict[str, Any]:
        """Send initialize request. Returns server capabilities.

        MCP spec: initialize must be the first request sent to the server.
        """
        result = await self._request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": self._client_capabilities,
            "clientInfo": {
                "name": "forge-agent",
                "version": "2.0.0",
            },
        })
        self._server_capabilities = result.get("capabilities", {})
        # Send initialized notification (required by spec)
        await self._notify("notifications/initialized", {})
        return self._server_capabilities

    async def disconnect(self) -> None:
        """Close transport connection."""
        await self._transport.close()

    # ── Tool discovery ───────────────────────────────────────────────

    async def list_tools(self) -> list[dict[str, Any]]:
        """Discover available tools via tools/list.

        Returns list of tool definitions, each with:
          name, description, inputSchema, annotations (optional)
        """
        result = await self._request("tools/list", {})
        return result.get("tools", [])

    # ── Tool invocation ──────────────────────────────────────────────

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool via tools/call.

        Returns the tool result as a dict with:
          content: list[TextContent | ImageContent | ...]
          isError: bool (optional)
        """
        return await self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

    # ── Resources (optional) ─────────────────────────────────────────

    async def list_resources(self) -> list[dict[str, Any]]:
        """Discover available resources via resources/list."""
        result = await self._request("resources/list", {})
        return result.get("resources", [])

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read a resource via resources/read."""
        return await self._request("resources/read", {"uri": uri})

    # ── JSON-RPC core ────────────────────────────────────────────────

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a JSON-RPC request and return the result."""
        self._request_id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        logger.debug("MCP → %s: %s (id=%d)", self._server_name, method, self._request_id)
        raw = json.dumps(req, ensure_ascii=False)
        await self._transport.send(raw.encode("utf-8"))

        response_bytes = await self._transport.receive()
        response = json.loads(response_bytes.decode("utf-8"))

        if "error" in response:
            err = response["error"]
            logger.warning("MCP error from %s: %s", self._server_name, err.get("message", str(err)))
            raise McpError(
                code=err.get("code", -1),
                message=err.get("message", "Unknown MCP error"),
                data=err.get("data"),
            )

        return response.get("result")

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }, ensure_ascii=False)
        await self._transport.send(msg.encode("utf-8"))


# ── Error type ───────────────────────────────────────────────────────────


class McpError(Exception):
    """MCP protocol-level error."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"MCP error {code}: {message}")
