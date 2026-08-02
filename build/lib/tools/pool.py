"""Deterministic tool-pool assembly with cache-stable partitions."""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatch
from typing import Any


def is_mcp_tool(tool: Any) -> bool:
    return getattr(tool, "mcp_props", None) is not None


def assemble_tool_pool(
    built_in_tools: Iterable[Any],
    mcp_tools: Iterable[Any] = (),
    *,
    deny_rules: Iterable[str] = (),
) -> list[Any]:
    """Return built-ins first and MCP tools second, sorted within partitions.

    Duplicate names are rejected instead of silently changing which tool wins.
    This makes the built-in schema prefix stable for prompt caching and keeps
    registration failures deterministic.
    """
    denied = tuple(deny_rules)
    partitions = (
        sorted(built_in_tools, key=lambda tool: tool.name),
        sorted(
            (
                tool for tool in mcp_tools
                if not any(fnmatch(tool.name, rule) for rule in denied)
            ),
            key=lambda tool: tool.name,
        ),
    )
    result: list[Any] = []
    seen: set[str] = set()
    for partition in partitions:
        for tool in partition:
            if tool.name in seen:
                raise ValueError(
                    f"Duplicate tool name during pool assembly: {tool.name}",
                )
            seen.add(tool.name)
            result.append(tool)
    return result


assembleToolPool = assemble_tool_pool
