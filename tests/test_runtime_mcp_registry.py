from __future__ import annotations

import asyncio

from runtime.mcp import (
    MCPToolProps,
    assemble_tool_pool,
    filter_built_in_tools,
    filter_mcp_tools,
    find_tool,
    is_deferred_tool,
    tools_to_api_schemas,
)
from runtime.tool import ToolResult, ToolUseContext, build_tool


def _tool(name: str, *, is_mcp: bool = False, always_load: bool = False, should_defer: bool = False):
    async def call_fn(input: dict, _context: ToolUseContext) -> ToolResult[str]:
        return ToolResult(output="ok")

    # MCP tools are deferred unless always_load; non-MCP use explicit should_defer
    effective_defer = (is_mcp and not always_load) or should_defer
    mcp_props = MCPToolProps(
        is_deferred=effective_defer, always_load=always_load,
    ) if is_mcp else None
    tool = build_tool(
        name=name,
        input_schema={"type": "object", "properties": {}},
        call_fn=call_fn,
        description_text=f"{name} description",
        mcp_props=mcp_props,
    )
    # Keep metadata dict for backward compat with _tool_value() fallback
    tool.metadata = {
        "is_mcp": is_mcp,
        "always_load": always_load,
        "should_defer": should_defer,
    }
    return tool


def test_tool_pool_merge_ordering_and_duplicate_precedence():
    built_in_b = _tool("b")
    built_in_a = _tool("a")
    mcp_a = _tool("a", is_mcp=True)
    mcp_c = _tool("c", is_mcp=True)

    pool = assemble_tool_pool([built_in_b, built_in_a], [mcp_c, mcp_a])

    assert [tool.name for tool in pool] == ["a", "b", "c"]
    assert pool[0] is built_in_a


def test_tool_pool_filters_denied_mcp_globs():
    pool = assemble_tool_pool(
        [_tool("read")],
        [_tool("mcp__server__read", is_mcp=True), _tool("mcp__server__write", is_mcp=True)],
        deny_rules=["mcp__*__write"],
    )

    assert [tool.name for tool in pool] == ["read", "mcp__server__read"]


def test_deferred_tool_semantics():
    assert is_deferred_tool(_tool("mcp", is_mcp=True)) is True
    assert is_deferred_tool(_tool("mcp", is_mcp=True, always_load=True)) is False
    assert is_deferred_tool(_tool("builtin", should_defer=True)) is True
    assert is_deferred_tool(_tool("builtin")) is False


def test_tools_to_api_schemas_marks_only_deferred_tools():
    schemas = tools_to_api_schemas([
        _tool("builtin"),
        _tool("deferred_builtin", should_defer=True),
        _tool("mcp", is_mcp=True),
        _tool("loaded_mcp", is_mcp=True, always_load=True),
    ])
    by_name = {schema["name"]: schema for schema in schemas}

    assert "defer_loading" not in by_name["builtin"]
    assert by_name["deferred_builtin"]["defer_loading"] is True
    assert by_name["mcp"]["defer_loading"] is True
    assert "defer_loading" not in by_name["loaded_mcp"]


def test_find_and_filter_helpers():
    built_in = _tool("builtin")
    mcp = _tool("mcp", is_mcp=True)
    tools = [built_in, mcp]

    assert find_tool(tools, "mcp") is mcp
    assert find_tool(tools, "missing") is None
    assert filter_mcp_tools(tools) == [mcp]
    assert filter_built_in_tools(tools) == [built_in]


def test_metadata_fallback_for_deferred_semantics():
    """Legacy metadata dict is still consulted when mcp_props is None."""
    tool = _tool("metadata_only")  # mcp_props=None
    tool.metadata = {"is_mcp": True, "always_load": False}
    assert is_deferred_tool(tool) is True
