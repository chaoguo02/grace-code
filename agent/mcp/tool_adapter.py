"""Adapters from MCP metadata to canonical ``BaseTool`` objects."""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from agent.mcp.effect_inference import infer_mcp_is_read_only
from agent.mcp.types import MCPToolInfo, MCPToolProps
from core.base import RiskLevel, ToolResult
from tools.factory import build_tool

MCP_OUTPUT_WARN_CHARS = 10_000
MCP_OUTPUT_MAX_CHARS = 25_000


def mcp_tool_to_runtime_tool(
    manager: Any,
    tool_info: MCPToolInfo,
    always_load: bool = False,
):
    """Create a fail-closed runtime tool wrapper for one MCP tool."""

    def execute(input: dict[str, Any]) -> ToolResult:
        try:
            result = manager.execute_tool(tool_info.runtime_name, input)
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                metadata={
                    "mcp_server": tool_info.server_name,
                    "mcp_tool": tool_info.name,
                    "mcp_error": str(exc),
                },
            )

        output = _bounded_mcp_output(
            tool_info.runtime_name,
            _render_mcp_content(result.content),
        )
        metadata = {
            "mcp_server": tool_info.server_name,
            "mcp_tool": tool_info.name,
            "mcp_is_error": result.is_error,
        }
        if result.is_error:
            metadata["mcp_error"] = output or f"MCP tool '{tool_info.name}' returned an error"
        return ToolResult(
            success=not result.is_error,
            output=output,
            error=(output if result.is_error else ""),
            metadata=metadata,
        )

    tool = build_tool(
        name=tool_info.runtime_name,
        parameters_schema=tool_info.input_schema,
        execute=execute,
        description=tool_info.description,
        is_read_only=lambda _input: infer_mcp_is_read_only(
            tool_info.name, tool_info.description,
            metadata=getattr(tool_info, "metadata", None),
        ),
        risk=lambda _input: RiskLevel.MEDIUM,
        mcp_props=MCPToolProps(
            server_name=tool_info.server_name,
            original_tool_name=tool_info.name,
            always_load=always_load,
            is_deferred=not always_load,  # MCP tools are deferred unless always_load
        ),
    )
    return tool


def deferred_mcp_tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    execute_fn: Callable[[dict[str, Any]], Any],
    aexecute_fn: Callable[[dict[str, Any]], Any] | None = None,
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

    def call_fn(input: dict[str, Any]) -> ToolResult:
        try:
            ensure_connected()
            result = execute_fn(input)
            return _coerce_execute_result(name, result)
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                metadata={
                    "mcp_server": server_name,
                    "mcp_tool": original_tool_name or name,
                    "mcp_error": str(exc),
                },
            )

    async def acall_fn(input: dict[str, Any]) -> ToolResult:
        """CC tool.call() 等价 — async MCP 执行。

        优先用 aexecute_fn (aexecute_tool 真 async)；缺失则 to_thread 包
        sync execute_fn（过渡）。
        """
        import asyncio
        try:
            ensure_connected()
            if aexecute_fn is not None:
                result = await aexecute_fn(input)
            else:
                result = await asyncio.to_thread(execute_fn, input)
            return _coerce_execute_result(name, result)
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                metadata={
                    "mcp_server": server_name,
                    "mcp_tool": original_tool_name or name,
                    "mcp_error": str(exc),
                },
            )

    tool = build_tool(
        name=name,
        parameters_schema=input_schema,
        execute=call_fn,
        aexecute=acall_fn,
        description=description,
        is_read_only=lambda _input: infer_mcp_is_read_only(
            original_tool_name or name, description,
            metadata=metadata,
        ),
        risk=lambda _input: RiskLevel.MEDIUM,
        mcp_props=MCPToolProps(
            server_name=server_name,
            original_tool_name=original_tool_name or name,
            is_deferred=True,
            always_load=False,
        ),
    )

    tool.source_metadata = dict(metadata or {})
    tool.ensure_connected = ensure_connected
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
                aexecute_fn=lambda args, runtime_name=info.runtime_name: manager.aexecute_tool(runtime_name, args),
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
                aexecute_fn=lambda args, runtime_name=info.runtime_name: manager.aexecute_tool(runtime_name, args),
                server_name=info.server_name,
                original_tool_name=info.name,
                metadata=info.metadata,
            ))
        else:
            tools.append(mcp_tool_to_runtime_tool(manager, info, always_load=True))
    return tools


# ── MCP Resource tools (ListMcpResourcesTool, ReadMcpResourceTool) ──

def create_resource_list_tool(manager: Any, server_name: str):
    """Create a ListMcpResourcesTool wrapper for a connected MCP bridge."""

    def call_fn(_input: dict[str, Any]) -> ToolResult:
        try:
            resources = manager.list_resources(server_name)
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                metadata={"mcp_error": str(exc)},
            )
        if not resources:
            return ToolResult(
                success=True,
                output=f"No resources from MCP server '{server_name}'",
            )
        lines = [f"MCP resources from '{server_name}':"]
        for r in resources:
            lines.append(f"  {r['uri']} — {r['name']} ({r.get('mimeType', '')})")
        return ToolResult(success=True, output="\n".join(lines))

    return build_tool(
        name=f"mcp__{server_name}__list_resources",
        description=f"List resources from MCP server '{server_name}'",
        parameters_schema={
            "type": "object", "properties": {}, "required": [],
        },
        execute=call_fn,
        is_read_only=lambda _input: True,
        mcp_props=MCPToolProps(
            server_name=server_name,
            original_tool_name="list_resources",
            always_load=True,
        ),
    )


def create_resource_read_tool(manager: Any, server_name: str):
    """Create a ReadMcpResourceTool wrapper for a connected MCP bridge."""

    def call_fn(input: dict[str, Any]) -> ToolResult:
        uri = input.get("uri", "")
        if not uri:
            return ToolResult(
                success=False,
                output="",
                error="uri is required",
                metadata={"mcp_error": "uri is required"},
            )
        try:
            result = manager.read_resource(server_name, uri)
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                metadata={"mcp_error": str(exc)},
            )
        contents = result.get("contents", [])
        if not contents:
            return ToolResult(
                success=True,
                output=f"Resource '{uri}' returned empty content",
            )
        text = "\n".join(c.get("text", "") for c in contents)
        return ToolResult(success=True, output=text)

    return build_tool(
        name=f"mcp__{server_name}__read_resource",
        description=f"Read an MCP resource from '{server_name}' by URI",
        parameters_schema={
            "type": "object",
            "properties": {"uri": {"type": "string", "description": "Resource URI to read"}},
            "required": ["uri"],
        },
        execute=call_fn,
        is_read_only=lambda _input: True,
        mcp_props=MCPToolProps(
            server_name=server_name,
            original_tool_name="read_resource",
            always_load=True,
        ),
    )


def _coerce_execute_result(tool_name: str, result: Any) -> ToolResult:
    if isinstance(result, ToolResult):
        return result
    if hasattr(result, "content") and hasattr(result, "is_error"):
        output = _render_mcp_content(list(getattr(result, "content", []) or []))
        metadata = dict(getattr(result, "metadata", None) or {})
        metadata.setdefault("mcp_tool", tool_name)
        metadata["mcp_is_error"] = bool(getattr(result, "is_error", False))
        if metadata["mcp_is_error"]:
            metadata["mcp_error"] = output or f"MCP tool '{tool_name}' returned an error"
        return ToolResult(
            success=not metadata["mcp_is_error"],
            output=output,
            error=(output if metadata["mcp_is_error"] else ""),
            metadata=metadata,
        )
    return ToolResult(success=True, output=str(result or ""))


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


def _bounded_mcp_output(tool_name: str, output: str) -> str:
    size = len(output)
    if size > MCP_OUTPUT_MAX_CHARS:
        return (
            output[:MCP_OUTPUT_MAX_CHARS]
            + f"\n\n[MCP output truncated: {size} chars -> "
            + f"{MCP_OUTPUT_MAX_CHARS} chars]"
        )
    if size > MCP_OUTPUT_WARN_CHARS:
        return (
            output
            + f"\n\n[Note: MCP tool '{tool_name}' returned {size} "
            + "chars. Consider narrowing the request.]"
        )
    return output
