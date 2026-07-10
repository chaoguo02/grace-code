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
