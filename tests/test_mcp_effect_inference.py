"""1. MCP effect inference: heuristic + explicit metadata."""

from __future__ import annotations

from agent.mcp.effect_inference import infer_mcp_effects, infer_mcp_is_read_only
from core.types import ToolEffect


def test_search_tool_is_read_only() -> None:
    """search_* prefix + 'search' keyword → DISCOVER_WORKSPACE."""
    assert infer_mcp_is_read_only("search_issues", "Search GitHub issues")


def test_get_tool_is_read_only() -> None:
    """get_* prefix + 'fetch' keyword → READ_WORKSPACE."""
    assert infer_mcp_is_read_only("get_user", "Fetch user profile")


def test_delete_tool_is_not_read_only() -> None:
    """delete_* prefix → WRITE_WORKSPACE."""
    assert not infer_mcp_is_read_only("delete_file", "Delete a file permanently")


def test_create_tool_is_not_read_only() -> None:
    """create_* + 'deploy' → WRITE_WORKSPACE."""
    assert not infer_mcp_is_read_only("create_deployment", "Deploy to production")


def test_explicit_read_only_hint_overrides() -> None:
    """Server-declared read_only_hint wins over heuristic."""
    assert infer_mcp_is_read_only(
        "run_pipeline", "Execute CI pipeline",
        metadata={"read_only_hint": True},
    )


def test_explicit_effects_list_overrides_all() -> None:
    """Server-declared effects list wins over everything."""
    effects = infer_mcp_effects(
        "search_issues", "Search GitHub issues",
        metadata={"effects": ["read_workspace", "network"]},
    )
    assert effects == frozenset({ToolEffect.READ_WORKSPACE, ToolEffect.NETWORK})


def test_no_metadata_falls_back_to_heuristic() -> None:
    """No metadata → heuristic based on name+description."""
    effects = infer_mcp_effects("list_users", "List all users in workspace")
    assert effects == frozenset({ToolEffect.DISCOVER_WORKSPACE})


def test_unknown_tool_logs_warning() -> None:
    """Completely ambiguous name → UNKNOWN with logged warning."""
    effects = infer_mcp_effects("xyzzy", "does abcd efgh")
    assert ToolEffect.UNKNOWN in effects


def test_resource_tools_are_read_only() -> None:
    """list_resources and read_resource are always read-only."""
    assert infer_mcp_is_read_only("list_resources", "List MCP resources")
    assert infer_mcp_is_read_only("read_resource", "Read MCP resource by URI")
