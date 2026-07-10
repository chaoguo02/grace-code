"""Synchronous MCP manager integration tests."""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from runtime.mcp import MCPServerConfig, SyncMCPToolManager

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp SDK not installed",
)

FAKE_SERVER_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "fake_mcp_server.py"
)


def _make_config() -> MCPServerConfig:
    return MCPServerConfig(
        name="fake-test",
        command=sys.executable,
        args=[FAKE_SERVER_PATH],
        timeout_seconds=30.0,
    )


def test_load_and_discover_returns_adapted_runtime_tools():
    manager = SyncMCPToolManager()
    try:
        tools = manager.load_and_discover([_make_config()])
        by_name = {tool.name: tool for tool in tools}

        assert {"mcp__fake_test__echo", "mcp__fake_test__add", "mcp__fake_test__slow_echo"}.issubset(by_name)
        echo = by_name["mcp__fake_test__echo"]
        assert echo.is_mcp is True
        assert echo.input_schema["type"] == "object"
        assert "message" in echo.input_schema.get("properties", {})
    finally:
        manager.close_all()


def test_call_tool_synchronous_echo_and_add_succeed():
    with SyncMCPToolManager() as manager:
        manager.load_and_discover([_make_config()])

        echo = manager.call_tool("mcp__fake_test__echo", {"message": "hello"})
        added = manager.call_tool("mcp__fake_test__add", {"a": 2, "b": 5})

        assert echo.is_error is False
        assert "echo: hello" in echo.text
        assert added.is_error is False
        assert "7" in added.text


def test_nonexistent_server_returns_error_result():
    with SyncMCPToolManager() as manager:
        result = manager.call_tool("mcp__missing__echo", {"message": "hello"})

        assert result.is_error is True
        assert "not connected" in result.text
        assert result.metadata is not None
        assert result.metadata["mcp_is_error"] is True


def test_context_manager_clears_bridges():
    manager = SyncMCPToolManager()
    with manager:
        manager.load_and_discover([_make_config()])
        assert manager.bridges

    assert manager.bridges == {}


def test_parse_namespaced_name_valid_and_invalid_cases():
    manager = SyncMCPToolManager()
    try:
        assert manager._parse_namespaced_name("mcp__server__tool") == ("server", "tool")
        assert manager._parse_namespaced_name("mcp__server__tool__extra") == ("server", "tool__extra")
        assert manager._parse_namespaced_name("bad__server__tool") is None
        assert manager._parse_namespaced_name("mcp__server") is None
        assert manager._parse_namespaced_name("mcp____tool") is None
    finally:
        manager.close_all()
