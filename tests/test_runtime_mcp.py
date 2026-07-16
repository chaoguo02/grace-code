from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from runtime.mcp import (
    MCPCallResult,
    MCPServerConfig,
    MCPToolBridge,
    MCPToolCallError,
    MCPToolInfo,
    mcp_tool_to_runtime_tool,
    slugify_mcp_name,
)
from runtime.tool import ToolUseContext


def test_slugify_and_runtime_tool_name_prefix():
    info = MCPToolInfo(
        server_name="GitHub Server",
        name="search/issues",
        description="Search issues",
        input_schema={"type": "object", "properties": {}},
    )

    assert slugify_mcp_name("GitHub Server") == "github_server"
    assert info.runtime_name == "mcp__github_server__search_issues"


class FakeBridge:
    async def call_tool(self, name, arguments):
        assert name == "search/issues"
        assert arguments == {"query": "bug"}
        return MCPCallResult(content=[SimpleNamespace(text="found issue")], is_error=False)


def test_adapter_fail_closed_defaults_and_successful_call():
    async def scenario():
        info = MCPToolInfo(
            server_name="GitHub Server",
            name="search/issues",
            description="Search issues",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        )
        tool = mcp_tool_to_runtime_tool(FakeBridge(), info)

        assert tool.name == "mcp__github_server__search_issues"
        assert tool.is_concurrency_safe({}) is False
        assert tool.is_read_only({}) is False
        assert tool.is_destructive({}) is False
        assert tool.input_schema == info.input_schema

        result = await tool.call({"query": "bug"}, ToolUseContext())
        assert result.output == "found issue"
        assert result.metadata == {
            "mcp_server": "GitHub Server",
            "mcp_tool": "search/issues",
            "mcp_is_error": False,
        }

    asyncio.run(scenario())


class ErrorBridge:
    async def call_tool(self, name, arguments):
        return MCPCallResult(content=[{"text": "remote error"}], is_error=True)


def test_adapter_marks_mcp_tool_level_error_in_metadata():
    async def scenario():
        info = MCPToolInfo(
            server_name="server",
            name="fail",
            description="Fail",
            input_schema={"type": "object", "properties": {}},
        )
        tool = mcp_tool_to_runtime_tool(ErrorBridge(), info)

        result = await tool.call({}, ToolUseContext())

        assert result.output == "remote error"
        assert result.metadata["mcp_is_error"] is True
        assert result.metadata["mcp_error"] == "remote error"

    asyncio.run(scenario())


class RaisingBridge:
    async def call_tool(self, name, arguments):
        raise MCPToolCallError("connection failed")


def test_adapter_converts_bridge_call_error_to_metadata():
    async def scenario():
        info = MCPToolInfo(
            server_name="server",
            name="fail",
            description="Fail",
            input_schema={"type": "object", "properties": {}},
        )
        tool = mcp_tool_to_runtime_tool(RaisingBridge(), info)

        result = await tool.call({}, ToolUseContext())

        assert result.output == ""
        assert result.metadata["mcp_error"] == "connection failed"

    asyncio.run(scenario())


class SlowSession:
    async def call_tool(self, name, arguments):
        await asyncio.sleep(1)
        return SimpleNamespace(content=[], isError=False)


class FastSession:
    async def call_tool(self, name, arguments):
        return SimpleNamespace(content=[SimpleNamespace(text="ok")], isError=False)


def test_bridge_call_tool_timeout():
    async def scenario():
        bridge = MCPToolBridge(MCPServerConfig(name="s", command="unused", timeout_seconds=0.01))
        bridge._session = SlowSession()

        with pytest.raises(MCPToolCallError, match="timed out"):
            await bridge.call_tool("slow", {})

    asyncio.run(scenario())


def test_bridge_call_tool_success_normalizes_result():
    async def scenario():
        bridge = MCPToolBridge(MCPServerConfig(name="s", command="unused"))
        bridge._session = FastSession()

        result = await bridge.call_tool("fast", {})

        assert result.content[0].text == "ok"
        assert result.is_error is False

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# MCP-01: HTTP transport bridge tests
# ---------------------------------------------------------------------------

def test_create_mcp_bridge_dispatches_by_type():
    """MCP-01: create_mcp_bridge returns correct bridge for each transport."""
    from runtime.mcp.client import HttpMCPBridge, MCPToolBridge, create_mcp_bridge

    stdio_config = MCPServerConfig(name="s", type="stdio", command="echo")
    http_config = MCPServerConfig(name="h", type="http", url="https://example.com")
    sse_config = MCPServerConfig(name="sse", type="sse", url="https://example.com")
    ws_config = MCPServerConfig(name="ws", type="ws", url="wss://example.com")

    assert isinstance(create_mcp_bridge(stdio_config), MCPToolBridge)
    assert not isinstance(create_mcp_bridge(stdio_config), HttpMCPBridge)
    assert isinstance(create_mcp_bridge(http_config), HttpMCPBridge)
    assert isinstance(create_mcp_bridge(sse_config), HttpMCPBridge)
    assert isinstance(create_mcp_bridge(ws_config), HttpMCPBridge)


def test_http_bridge_transport_type_reflects_config():
    """MCP-01: HttpMCPBridge.transport_type returns the config type."""
    from runtime.mcp.client import HttpMCPBridge

    http_bridge = HttpMCPBridge(MCPServerConfig(
        name="h", type="http", url="https://example.com",
    ))
    assert http_bridge.transport_type == "http"

    sse_bridge = HttpMCPBridge(MCPServerConfig(
        name="s", type="sse", url="https://example.com",
    ))
    assert sse_bridge.transport_type == "sse"


def test_http_bridge_initial_state_not_connected():
    """MCP-01: HttpMCPBridge starts with is_connected == False."""
    from runtime.mcp.client import HttpMCPBridge

    bridge = HttpMCPBridge(MCPServerConfig(
        name="h", type="http", url="https://example.com",
    ))
    assert bridge.is_connected is False
    assert bridge.tools == []


def test_http_bridge_close_when_not_connected_is_safe():
    """MCP-01: Closing an unconnected HTTP bridge is a no-op."""
    from runtime.mcp.client import HttpMCPBridge

    bridge = HttpMCPBridge(MCPServerConfig(
        name="h", type="http", url="https://example.com",
    ))

    async def _close():
        await bridge.close()

    asyncio.run(_close())
    assert bridge.is_connected is False  # still fine


def test_create_mcp_bridge_rejects_unknown_transport():
    """MCP-E1: Unknown transport type raises ValueError."""
    from runtime.mcp.client import create_mcp_bridge

    with pytest.raises(ValueError, match="Unsupported MCP transport type"):
        create_mcp_bridge(MCPServerConfig(name="bad", type="grpc"))
