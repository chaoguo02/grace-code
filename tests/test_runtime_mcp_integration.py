"""Integration tests for MCPToolBridge using a real stdio subprocess."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

import pytest

from agent.mcp.client import MCPToolBridge, MCPToolCallError
from agent.mcp.types import MCPServerConfig

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp SDK not installed",
)

FAKE_SERVER_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "fake_mcp_server.py"
)


def _make_config(timeout_seconds: float = 30.0) -> MCPServerConfig:
    return MCPServerConfig(
        name="fake-test",
        command=sys.executable,
        args=[FAKE_SERVER_PATH],
        timeout_seconds=timeout_seconds,
    )


def test_connect_and_discover_tools():
    async def scenario():
        config = _make_config()
        async with MCPToolBridge(config) as bridge:
            tools = await bridge.discover_tools()

            tool_names = {tool.name for tool in tools}
            assert {"echo", "add", "slow_echo"}.issubset(tool_names)

            echo_tool = next(tool for tool in tools if tool.name == "echo")
            assert "message" in echo_tool.input_schema.get("properties", {})

    asyncio.run(scenario())


def test_call_tool_echo():
    async def scenario():
        config = _make_config()
        async with MCPToolBridge(config) as bridge:
            result = await bridge.call_tool("echo", {"message": "hello"})

            assert result.is_error is False
            assert "echo: hello" in result.text

    asyncio.run(scenario())


def test_call_tool_add():
    async def scenario():
        config = _make_config()
        async with MCPToolBridge(config) as bridge:
            result = await bridge.call_tool("add", {"a": 3, "b": 4})

            assert result.is_error is False
            assert "7" in result.text

    asyncio.run(scenario())


def test_call_nonexistent_tool_returns_error_result():
    async def scenario():
        config = _make_config()
        async with MCPToolBridge(config) as bridge:
            result = await bridge.call_tool("nonexistent_tool", {})

            assert result.is_error is True
            assert result.metadata is not None
            assert result.metadata.get("mcp_is_error") is True

    asyncio.run(scenario())


def test_call_tool_timeout():
    async def scenario():
        config = _make_config(timeout_seconds=1.0)
        async with MCPToolBridge(config) as bridge:
            with pytest.raises(MCPToolCallError, match="timed out"):
                await bridge.call_tool("slow_echo", {"message": "will timeout"})

    asyncio.run(scenario())


def test_context_manager_closes_bridge():
    async def scenario():
        config = _make_config()
        bridge = MCPToolBridge(config)

        async with bridge:
            assert bridge.is_connected is True
            assert bridge.tools

        assert bridge.is_connected is False
        assert bridge._session is None
        assert bridge._transport_cm is None

    asyncio.run(scenario())


def test_multiple_sequential_calls():
    async def scenario():
        config = _make_config()
        async with MCPToolBridge(config) as bridge:
            r1 = await bridge.call_tool("echo", {"message": "first"})
            r2 = await bridge.call_tool("echo", {"message": "second"})
            r3 = await bridge.call_tool("add", {"a": 10, "b": 20})

            assert "echo: first" in r1.text
            assert "echo: second" in r2.text
            assert "30" in r3.text

    asyncio.run(scenario())


def test_server_crash_returns_error_not_hang():
    async def scenario():
        config = MCPServerConfig(
            name="crash-test",
            command=sys.executable,
            args=["-c", "import sys; sys.exit(1)"],
            timeout_seconds=5.0,
        )
        bridge = MCPToolBridge(config)

        with pytest.raises(Exception):
            await bridge.connect()

        await bridge.close()

    asyncio.run(scenario())
