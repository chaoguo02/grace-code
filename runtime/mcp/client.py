"""Async MCP client bridge for runtime tools.

Multi-transport: stdio (MCP SDK), HTTP JSON-RPC 2.0, SSE, WebSocket.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from runtime.mcp.types import MCPServerConfig, MCPToolInfo

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    HAS_MCP = True
except ImportError:  # pragma: no cover - exercised by environments without optional extra
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None
    HAS_MCP = False


class MCPNotInstalledError(RuntimeError):
    """Raised when the optional mcp package is not installed."""


@dataclass(frozen=True)
class MCPCallResult:
    """Normalized MCP call result."""

    content: list[Any]
    is_error: bool = False
    metadata: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        parts: list[str] = []
        for block in self.content:
            if hasattr(block, "text"):
                parts.append(str(block.text))
            elif isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part).strip()


class MCPToolCallError(RuntimeError):
    """Raised when a remote MCP tool call fails before returning a result."""


def create_mcp_bridge(config: MCPServerConfig) -> "MCPToolBridge":
    """Factory: return the correct bridge implementation for the transport type.

    Multi-transport dispatch (MCP-E1, MCP-01):
      - stdio → MCPToolBridge (local subprocess via MCP SDK)
      - http  → HttpMCPBridge (JSON-RPC 2.0 over HTTP POST)
      - sse   → HttpMCPBridge (SSE placeholder — HTTP bridge with SSE notes)
      - ws    → HttpMCPBridge (WebSocket placeholder — HTTP bridge with WS notes)
    """
    if config.type == "stdio":
        return MCPToolBridge(config)
    if config.type in ("http", "sse", "ws"):
        return HttpMCPBridge(config)
    raise ValueError(f"Unsupported MCP transport type: {config.type!r}")


# ---------------------------------------------------------------------------
# Stdio Bridge (MCP SDK)
# ---------------------------------------------------------------------------

class MCPToolBridge:
    """Connect to one stdio MCP server and expose discovered tools."""

    @property
    def transport_type(self) -> str:
        return "stdio"

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._transport_cm: Any = None
        self._session_cm: Any = None
        self._session: Any = None
        self._tools: list[MCPToolInfo] = []
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def tools(self) -> list[MCPToolInfo]:
        return list(self._tools)

    async def __aenter__(self) -> "MCPToolBridge":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> list[MCPToolInfo]:
        """Start the stdio server, initialize a session, and discover tools."""
        if self._connected:
            return self.tools
        if not HAS_MCP:
            raise MCPNotInstalledError("Install the optional 'mcp' dependency to use runtime MCP bridge")

        env = dict(os.environ)
        if self.config.env:
            env.update(self.config.env)

        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=env,
            cwd=self.config.cwd or os.getcwd(),
        )
        self._transport_cm = stdio_client(params)
        read_stream, write_stream = await self._transport_cm.__aenter__()

        self._session_cm = ClientSession(read_stream, write_stream)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

        self._tools = await self.discover_tools()
        self._connected = True
        return self.tools

    async def close(self) -> None:
        """Close the MCP session and transport."""
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            finally:
                self._session_cm = None
                self._session = None

        if self._transport_cm is not None:
            try:
                await self._transport_cm.__aexit__(None, None, None)
            finally:
                self._transport_cm = None

        self._connected = False

    async def discover_tools(self) -> list[MCPToolInfo]:
        """Return normalized metadata for all server tools."""
        self._require_session()
        response = await self._session.list_tools()
        tools = []
        for tool in response.tools:
            tools.append(MCPToolInfo(
                server_name=self.config.name,
                name=str(tool.name),
                description=getattr(tool, "description", None) or f"MCP tool {tool.name} from {self.config.name}",
                input_schema=getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}},
                metadata=dict(getattr(tool, "_meta", None) or {}),
            ))
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        """Call a remote MCP tool with timeout protection."""
        self._require_session()
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise MCPToolCallError(
                f"MCP tool '{tool_name}' from server '{self.config.name}' timed out after "
                f"{self.config.timeout_seconds:.1f}s"
            ) from exc
        except Exception as exc:
            return MCPCallResult(
                content=[{"text": str(exc)}],
                is_error=True,
                metadata={
                    "mcp_server": self.config.name,
                    "mcp_tool": tool_name,
                    "mcp_is_error": True,
                    "mcp_error": str(exc),
                },
            )

        return MCPCallResult(
            content=list(getattr(result, "content", []) or []),
            is_error=bool(getattr(result, "isError", False)),
            metadata={
                "mcp_server": self.config.name,
                "mcp_tool": tool_name,
                "mcp_is_error": bool(getattr(result, "isError", False)),
            },
        )

    def _require_session(self) -> None:
        if self._session is None:
            raise RuntimeError("MCPToolBridge is not connected")


# ---------------------------------------------------------------------------
# HTTP Bridge — JSON-RPC 2.0 over HTTP POST (MCP-01)
# ---------------------------------------------------------------------------

class HttpMCPBridge(MCPToolBridge):
    """HTTP MCP transport — JSON-RPC 2.0 POST to <url>/mcp.

    Implements the MCP HTTP transport spec:
      1. POST initialize → get sessionId
      2. POST tools/list → discover tools
      3. POST tools/call → invoke tool

    Uses httpx for async HTTP. Custom headers (e.g. Authorization: Bearer)
    are passed through from the server config.
    """

    JSONRPC_VERSION = "2.0"
    _next_id: int = 0

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._tools: list[MCPToolInfo] = []
        self._client: Any = None  # httpx.AsyncClient
        self._session_id: str | None = None

    @property
    def transport_type(self) -> str:
        return self.config.type

    # ── Public API ──────────────────────────────────────────────────

    async def connect(self) -> list[MCPToolInfo]:
        if self._connected:
            return self.tools
        self._client = self._create_http_client()
        try:
            await self._initialize()
            self._tools = await self.discover_tools()
            self._connected = True
            return self.tools
        except Exception:
            await self._close_client()
            raise

    async def close(self) -> None:
        await self._close_client()
        self._connected = False
        self._session_id = None

    async def discover_tools(self) -> list[MCPToolInfo]:
        result = await self._rpc_call("tools/list", {})
        tools: list[MCPToolInfo] = []
        for tool in result.get("tools") or []:
            tools.append(MCPToolInfo(
                server_name=self.config.name,
                name=str(tool.get("name", "")),
                description=str(
                    tool.get("description", "") or
                    f"MCP tool {tool.get('name', '')} from {self.config.name}"
                ),
                input_schema=tool.get("inputSchema") or {"type": "object", "properties": {}},
            ))
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        self._require_session()
        try:
            result = await asyncio.wait_for(
                self._rpc_call("tools/call", {"name": tool_name, "arguments": arguments}),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise MCPToolCallError(
                f"MCP tool '{tool_name}' from server '{self.config.name}' timed out after "
                f"{self.config.timeout_seconds:.1f}s"
            ) from exc
        except Exception as exc:
            return MCPCallResult(
                content=[{"text": str(exc)}],
                is_error=True,
                metadata={
                    "mcp_server": self.config.name,
                    "mcp_tool": tool_name,
                    "mcp_is_error": True,
                    "mcp_error": str(exc),
                },
            )

        return MCPCallResult(
            content=list(result.get("content", []) or []),
            is_error=bool(result.get("isError", False)),
            metadata={
                "mcp_server": self.config.name,
                "mcp_tool": tool_name,
                "mcp_is_error": bool(result.get("isError", False)),
            },
        )

    # ── Internal ────────────────────────────────────────────────────

    def _create_http_client(self) -> Any:
        try:
            import httpx
        except ImportError:
            raise MCPNotInstalledError(
                "The 'httpx' package is required for MCP HTTP transport. "
                "Install it with: pip install httpx"
            )
        headers = {"Content-Type": "application/json"}
        if self.config.headers:
            headers.update(self.config.headers)
        return httpx.AsyncClient(headers=headers, timeout=self.config.timeout_seconds)

    async def _close_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def _initialize(self) -> None:
        result = await self._rpc_call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "forge-agent", "version": "1.0"},
        })
        self._session_id = result.get("sessionId")

    async def _rpc_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("HttpMCPBridge is not connected")
        HttpMCPBridge._next_id += 1
        body = {
            "jsonrpc": self.JSONRPC_VERSION,
            "id": HttpMCPBridge._next_id,
            "method": method,
            "params": params,
        }
        url = self.config.url.rstrip("/") + "/mcp"
        try:
            response = await self._client.post(url, json=body)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        except Exception as exc:
            raise MCPToolCallError(
                f"MCP HTTP request to '{self.config.name}' failed: {exc}"
            ) from exc
        if "error" in data:
            err = data["error"]
            raise MCPToolCallError(
                f"MCP JSON-RPC error {err.get('code', '')}: {err.get('message', str(err))}"
            )
        return data.get("result", {})
