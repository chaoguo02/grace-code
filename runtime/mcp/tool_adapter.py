"""Adapters from MCP tools to runtime ConcreteTool objects."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Callable

from runtime.mcp.client import MCPToolBridge, MCPToolCallError
from runtime.mcp.types import MCPToolInfo, MCPToolProps
from runtime.tool import ToolResult, ToolUseContext, build_tool


def mcp_tool_to_runtime_tool(bridge: MCPToolBridge, tool_info: MCPToolInfo, always_load: bool = False):
    """Create a fail-closed runtime tool wrapper for one MCP tool."""

    async def call_fn(input: dict, _context: ToolUseContext) -> ToolResult[str]:
        try:
            result = await bridge.call_tool(tool_info.name, input)
        except MCPToolCallError as exc:
            return ToolResult(
                output="",
                metadata={
                    "mcp_server": tool_info.server_name,
                    "mcp_tool": tool_info.name,
                    "mcp_error": str(exc),
                },
            )

        output = _render_mcp_content(result.content)
        metadata = {
            "mcp_server": tool_info.server_name,
            "mcp_tool": tool_info.name,
            "mcp_is_error": result.is_error,
        }
        if result.is_error:
            metadata["mcp_error"] = output or f"MCP tool '{tool_info.name}' returned an error"
        return ToolResult(output=output, metadata=metadata)

    tool = build_tool(
        name=tool_info.runtime_name,
        input_schema=tool_info.input_schema,
        call_fn=call_fn,
        description_text=tool_info.description,
        is_concurrency_safe=lambda _input: False,
        is_read_only=lambda _input: False,
        is_destructive=lambda _input: False,
        mcp_props=MCPToolProps(
            server_name=tool_info.server_name,
            original_tool_name=tool_info.name,
            always_load=always_load,
            is_deferred=not always_load,  # MCP tools are deferred unless always_load
        ),
    )
    tool.metadata = dict(tool_info.metadata)  # keep for backward compat
    return tool


def deferred_mcp_tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    execute_fn: Callable[[dict[str, Any]], Any],
    connect_fn: Callable[[], None] | None = None,
    server_name: str = "",
    original_tool_name: str = "",
    metadata: dict[str, Any] | None = None,
):
    """Create a runtime MCP tool that connects on first use."""
    state: dict[str, Any] = {
        "connected": False,
        "connect_error": None,
    }
    lock = threading.Lock()

    def ensure_connected() -> None:
        if state["connected"]:
            return
        with lock:
            if state["connected"]:
                return
            if connect_fn is None:
                state["connected"] = True
                return
            try:
                connect_fn()
            except Exception as exc:
                state["connect_error"] = exc
                raise
            state["connected"] = True

    async def call_fn(input: dict, _context: ToolUseContext) -> ToolResult[str]:
        try:
            ensure_connected()
            result = execute_fn(input)
            return _coerce_execute_result(name, result)
        except Exception as exc:
            return ToolResult(
                output="",
                metadata={
                    "mcp_server": server_name,
                    "mcp_tool": original_tool_name or name,
                    "mcp_error": str(exc),
                },
            )

    tool = build_tool(
        name=name,
        input_schema=input_schema,
        call_fn=call_fn,
        description_text=description,
        is_concurrency_safe=lambda _input: False,
        is_read_only=lambda _input: False,
        is_destructive=lambda _input: False,
        mcp_props=MCPToolProps(
            server_name=server_name,
            original_tool_name=original_tool_name or name,
            is_deferred=True,
            always_load=False,
        ),
    )

    tool.metadata = dict(metadata or {})
    tool.ensure_connected = ensure_connected
    tool.execute = lambda arguments: _sync_execute(tool, arguments)
    tool.is_connected = lambda: bool(state["connected"])
    tool.connect_error = lambda: state["connect_error"]
    tool.to_api_schema = lambda: {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": input_schema,
        },
        "_meta": {
            "is_mcp": True,
            "is_deferred": True,
            "is_connected": bool(state["connected"]),
            "server_name": server_name,
        },
    }
    return tool


def adapt_mcp_tools(tool_infos: list[MCPToolInfo], *, manager: Any, defer: bool = False) -> list[Any]:
    """Adapt MCPToolInfo objects to manager-backed runtime tools."""
    tools: list[Any] = []
    for info in tool_infos:
        if defer:
            tools.append(deferred_mcp_tool(
                name=info.runtime_name,
                description=info.description,
                input_schema=info.input_schema,
                execute_fn=lambda args, runtime_name=info.runtime_name: manager.execute_tool(runtime_name, args),
                server_name=info.server_name,
                original_tool_name=info.name,
                metadata=info.metadata,
            ))
            continue
        bridge = getattr(manager, "bridges", {}).get(info.server_name)
        if bridge is None:
            tools.append(deferred_mcp_tool(
                name=info.runtime_name,
                description=info.description,
                input_schema=info.input_schema,
                execute_fn=lambda args, runtime_name=info.runtime_name: manager.execute_tool(runtime_name, args),
                server_name=info.server_name,
                original_tool_name=info.name,
                metadata=info.metadata,
            ))
        else:
            tools.append(mcp_tool_to_runtime_tool(bridge, info, always_load=True))
    return tools


def _sync_execute(tool: Any, arguments: dict[str, Any]) -> Any:
    result = asyncio.run(tool.call(arguments, ToolUseContext()))
    if result.metadata.get("mcp_error"):
        raise RuntimeError(result.metadata["mcp_error"])
    return result.output


def _coerce_execute_result(tool_name: str, result: Any) -> ToolResult[str]:
    if isinstance(result, ToolResult):
        return result
    if hasattr(result, "content") and hasattr(result, "is_error"):
        output = _render_mcp_content(list(getattr(result, "content", []) or []))
        metadata = dict(getattr(result, "metadata", None) or {})
        metadata.setdefault("mcp_tool", tool_name)
        metadata["mcp_is_error"] = bool(getattr(result, "is_error", False))
        if metadata["mcp_is_error"]:
            metadata["mcp_error"] = output or f"MCP tool '{tool_name}' returned an error"
        return ToolResult(output=output, metadata=metadata)
    return ToolResult(output=str(result or ""))


def _render_mcp_content(content: list[Any]) -> str:
    """Render MCP content blocks into text."""
    parts: list[str] = []
    for block in content:
        if hasattr(block, "text"):
            parts.append(str(block.text))
            continue
        if isinstance(block, dict):
            if "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
            continue
        parts.append(str(block))
    return "\n".join(part for part in parts if part).strip()
