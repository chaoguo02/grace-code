"""Fake MCP server for stdio integration tests."""

from __future__ import annotations

import time

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-test-server")


@mcp.tool()
def echo(message: str) -> str:
    """Echo back the input message."""
    return f"echo: {message}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.tool()
def slow_echo(message: str) -> str:
    """Echo with a delay for timeout testing."""
    time.sleep(10)
    return f"slow_echo: {message}"


if __name__ == "__main__":
    mcp.run()
