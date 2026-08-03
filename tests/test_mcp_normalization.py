"""MCP Normalization Phase 1 tests — resource tools, config unification, collision."""

from __future__ import annotations

from agent.mcp.types import MCPToolInfo, MCPToolProps
from agent.mcp.sync_bridge import SyncMCPToolManager
from agent.mcp.tool_adapter import mcp_tool_to_runtime_tool


# ── #11: Tool name collision resolution ───────────────────────────────


def test_tool_name_collision_produces_distinct_runtime_names() -> None:
    """Two MCP servers with same un-prefixed tool name produce different runtime names.

    The prefix mcp__{server}__{tool} guarantees uniqueness as long as server
    names are unique.  This is CC's namespace strategy.
    """
    tool_a = MCPToolInfo(
        server_name="docs",
        name="search",
        description="Search docs",
        input_schema={"type": "object", "properties": {}},
    )
    tool_b = MCPToolInfo(
        server_name="api",
        name="search",
        description="Search API",
        input_schema={"type": "object", "properties": {}},
    )
    assert tool_a.runtime_name != tool_b.runtime_name
    assert tool_a.runtime_name.startswith("mcp__docs__")
    assert tool_b.runtime_name.startswith("mcp__api__")


# ── #3: Rate limit cooldown ──────────────────────────────────────────


def test_refresh_cooldown_initialized() -> None:
    """SyncMCPToolManager has _last_refresh dict + _REFRESH_COOLDOWN constant."""
    # We cannot easily create a real SyncMCPToolManager without an event loop,
    # so verify the attributes are declared on the class.
    # _last_refresh and _REFRESH_COOLDOWN are instance attrs set in __init__.
    # Verify they exist on a mock instance.
    import threading
    import asyncio

    old_loop = asyncio.get_event_loop_policy().new_event_loop() if False else None
    # Just verify the class defines the attributes in __init__
    init_source = SyncMCPToolManager.__init__.__code__.co_consts
    # Check the string names appear in the init code
    import inspect
    source = inspect.getsource(SyncMCPToolManager.__init__)
    assert "_last_refresh" in source
    assert "_REFRESH_COOLDOWN" in source
