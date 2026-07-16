"""Tool pool helpers for runtime MCP tools."""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any, Iterable


def assemble_tool_pool(built_in_tools: Iterable[Any], mcp_tools: Iterable[Any], deny_rules: Iterable[str] | None = None) -> list[Any]:
    """Merge built-in and MCP tools into a deterministic tool pool."""
    denied = list(deny_rules or [])
    built_ins = sorted(list(built_in_tools), key=lambda tool: tool.name)
    mcps = sorted(
        [tool for tool in mcp_tools if not _is_denied(tool.name, denied)],
        key=lambda tool: tool.name,
    )

    merged: list[Any] = []
    seen: set[str] = set()
    for tool in [*built_ins, *mcps]:
        if tool.name in seen:
            continue
        seen.add(tool.name)
        merged.append(tool)
    return merged


def is_deferred_tool(tool: Any) -> bool:
    """Return whether a tool should be represented as deferred in API schemas.

    Prefers declarative ``mcp_props``; falls back to legacy dynamic attributes.
    """
    mcp_props = getattr(tool, "mcp_props", None)
    if mcp_props is not None:
        if mcp_props.always_load:
            return False
        return mcp_props.is_deferred
    # Legacy fallback for tools without mcp_props
    if bool(_tool_value(tool, "always_load", False)):
        return False
    if bool(_tool_value(tool, "is_mcp", False)):
        return True
    return bool(_tool_value(tool, "should_defer", False))


def tools_to_api_schemas(tools: Iterable[Any]) -> list[dict[str, Any]]:
    """Serialize tools to Anthropic-style API definitions."""
    schemas: list[dict[str, Any]] = []
    for tool in tools:
        if hasattr(tool, "to_api_definition"):
            schema = dict(tool.to_api_definition())
        else:
            schema = {
                "name": tool.name,
                "description": "",
                "input_schema": getattr(tool, "input_schema", {"type": "object", "properties": {}}),
            }
        if is_deferred_tool(tool):
            schema["defer_loading"] = True
        schemas.append(schema)
    return schemas


def find_tool(tools: Iterable[Any], name: str) -> Any | None:
    """Find a tool by name."""
    return next((tool for tool in tools if tool.name == name), None)


def _is_mcp_tool(tool: Any) -> bool:
    """Check whether a tool is an MCP tool using declarative mcp_props."""
    mcp_props = getattr(tool, "mcp_props", None)
    if mcp_props is not None:
        return True
    return bool(_tool_value(tool, "is_mcp", False))


def filter_mcp_tools(tools: Iterable[Any]) -> list[Any]:
    """Return tools marked as MCP tools."""
    return [tool for tool in tools if _is_mcp_tool(tool)]


def filter_built_in_tools(tools: Iterable[Any]) -> list[Any]:
    """Return tools not marked as MCP tools."""
    return [tool for tool in tools if not _is_mcp_tool(tool)]


def _is_denied(tool_name: str, deny_rules: Iterable[str] | None) -> bool:
    """Return whether a tool name matches any deny glob."""
    return any(fnmatch(tool_name, rule) for rule in deny_rules or [])


def _tool_value(tool: Any, key: str, default: Any = None) -> Any:
    if hasattr(tool, key):
        return getattr(tool, key)
    metadata = getattr(tool, "metadata", None)
    if isinstance(metadata, dict) and key in metadata:
        return metadata[key]
    return default
