"""Async MCP client bridge for runtime tools."""

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


class MCPToolBridge:
    """Connect to one stdio MCP server and expose discovered tools."""

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
