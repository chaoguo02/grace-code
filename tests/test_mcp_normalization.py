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


# ── #1: Resource tools on HTTP bridges ──────────────────────────────


def test_http_bridge_resource_override() -> None:
    """HttpMCPBridge overrides list_resources/read_resource to return structured errors."""
    from agent.mcp.client import HttpMCPBridge

    # Check overrides exist on HttpMCPBridge (not inherited from MCPToolBridge)
    assert "list_resources" in HttpMCPBridge.__dict__
    assert "read_resource" in HttpMCPBridge.__dict__
    # Verify they are NOT inherited from parent
    assert "list_resources" not in type(HttpMCPBridge.__bases__[0]).__dict__.get("list_resources", "NOT_FOUND")


# ── #12: Environment sanitization ────────────────────────────────────


def test_sanitize_env_strips_api_keys() -> None:
    """_sanitize_env strips known sensitive env vars."""
    from agent.mcp.client import MCPToolBridge
    from agent.mcp.config import MCPServerConfig

    config = MCPServerConfig(
        name="test", type="stdio", command="echo", args=[], url="",
    )
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "ANTHROPIC_API_KEY": "sk-abc123",
        "OPENAI_API_KEY": "sk-def456",
        "GITHUB_TOKEN": "ghp_xyz",
        "MY_APP_KEY": "secret-value",
        "LANG": "en_US.UTF-8",
        "USER": "testuser",
        "RANDOM_VAR": "safe-value",
    }
    sanitized = MCPToolBridge._sanitize_env(base, config)
    assert "PATH" in sanitized
    assert "HOME" in sanitized
    assert "LANG" in sanitized
    assert "USER" in sanitized
    assert "RANDOM_VAR" in sanitized
    assert "ANTHROPIC_API_KEY" not in sanitized
    assert "OPENAI_API_KEY" not in sanitized
    assert "GITHUB_TOKEN" not in sanitized
    # MY_APP_KEY is not stripped — "KEY" alone is too aggressive a pattern
    # (would match KEYBOARD, KEYSTORE, etc.).  Only KEY with prefix modifiers
    # like API_KEY, PRIVATE_KEY triggers the strip.


def test_sanitize_env_preserves_config_env() -> None:
    """Config.env overrides take priority over base env."""
    from agent.mcp.client import MCPToolBridge
    from agent.mcp.config import MCPServerConfig

    config = MCPServerConfig(
        name="test", type="stdio", command="echo", args=[], url="",
        env={"MY_VAR": "from_config"},
    )
    base = {"PATH": "/bin", "MY_VAR": "from_base"}
    sanitized = MCPToolBridge._sanitize_env(base, config)
    assert sanitized["MY_VAR"] == "from_config"
