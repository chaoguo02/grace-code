"""MCP schema helpers.

Pool assembly lives in :mod:`tools.pool`; this module only owns MCP-specific
schema/deferred behavior.
"""

from __future__ import annotations

from typing import Any, Iterable

from tools.pool import assemble_tool_pool as _assemble_tool_pool

assemble_tool_pool = _assemble_tool_pool

def is_deferred_tool(tool: Any) -> bool:
    """Return whether a tool should be represented as deferred in API schemas.

    ``mcp_props`` is the single source of truth.
    """
    mcp_props = getattr(tool, "mcp_props", None)
    if mcp_props is not None:
        if mcp_props.always_load:
            return False
        return mcp_props.is_deferred
    return False


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
    return getattr(tool, "mcp_props", None) is not None


def filter_mcp_tools(tools: Iterable[Any]) -> list[Any]:
    """Return tools marked as MCP tools."""
    return [tool for tool in tools if _is_mcp_tool(tool)]


def filter_built_in_tools(tools: Iterable[Any]) -> list[Any]:
    """Return tools not marked as MCP tools."""
    return [tool for tool in tools if not _is_mcp_tool(tool)]


def _tool_value(tool: Any, key: str, default: Any = None) -> Any:
    if hasattr(tool, key):
        return getattr(tool, key)
    metadata = getattr(tool, "metadata", None)
    if isinstance(metadata, dict) and key in metadata:
        return metadata[key]
    return default
