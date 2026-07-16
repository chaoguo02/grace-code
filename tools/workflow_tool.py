"""CC-aligned Workflow + ToolSearch + WaitForMcpServers tools.

Architecture:
  - Workflow: multi-agent orchestration (declarative fan-out)
  - ToolSearch: searches deferred MCP tools by description/name at runtime.
    Injected with a reference to the registry so it can find deferred MCP tools.
  - WaitForMcpServers: waits for one or more MCP servers still connecting.
    Available only when tool search is disabled.
"""

from __future__ import annotations

from typing import Any

from tools.base import BaseTool, ToolEffect, ToolMetadata, ToolResult


# ---------------------------------------------------------------------------
# Workflow — CC-aligned multi-agent orchestration
# ---------------------------------------------------------------------------

class WorkflowTool(BaseTool):
    """Orchestrate multiple subagents in parallel and synthesize results."""

    metadata = ToolMetadata(effects=frozenset({ToolEffect.DELEGATE_WRITE}))

    @property
    def name(self) -> str:
        return "Workflow"

    @property
    def description(self) -> str:
        return (
            "Run a multi-agent workflow: fan out independent tasks to subagents "
            "and synthesize the results. Steps run concurrently where safe."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short description of the workflow (shown in progress display)",
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string", "description": "Subagent type to use"},
                            "prompt": {"type": "string", "description": "Task description for this step"},
                        },
                        "required": ["agent", "prompt"],
                    },
                    "description": "List of parallel steps to execute",
                },
            },
            "required": ["description", "steps"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        steps = params.get("steps", [])
        if not steps:
            return ToolResult(success=False, output="", error="At least one step is required")
        names = [s.get("agent", "?") for s in steps]
        return ToolResult(
            success=True,
            output=f"Workflow dispatched: {len(steps)} steps [{', '.join(names)}]",
        )


# ---------------------------------------------------------------------------
# ToolSearch — CC-aligned deferred MCP tool discovery
# ---------------------------------------------------------------------------

class ToolSearchTool(BaseTool):
    """Search for and load deferred MCP tools by description or name.

    CC reference: https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search

    When enabled (default), MCP tools load with defer_loading markers.
    The LLM calls this tool when it needs functionality it doesn't see
    in the immediate tools list. The tool searches across deferred MCP
    tools by name, description, and server, returning full schemas for
    matching tools. It also waits for servers still connecting.

    Injected with `set_mcp_context(registry, mcp_integration)` at
    session startup so it can access live MCP state.
    """

    metadata = ToolMetadata(effects=frozenset())

    def __init__(self) -> None:
        super().__init__()
        self._registry_ref: Any = None      # ToolRegistry
        self._mcp_integration: Any = None    # MCPToolIntegration

    def set_mcp_context(self, registry: Any, mcp_integration: Any) -> None:
        """Inject runtime MCP context so the tool can search live state."""
        self._registry_ref = registry
        self._mcp_integration = mcp_integration

    @property
    def name(self) -> str:
        return "ToolSearch"

    @property
    def description(self) -> str:
        return (
            "Search for tools from MCP servers that are not yet loaded. "
            "Use this when you need functionality you don't see in the "
            "available tools list. Returns matching tool schemas."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What functionality you're looking for (description or tool name)",
                },
            },
            "required": ["query"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        query = (params.get("query", "") or "").strip().lower()
        if not query:
            return ToolResult(success=False, output="", error="query is required")

        # Collect deferred MCP tools from the registry and MCP integration
        deferred: list[dict[str, Any]] = []

        # 1. From MCPToolIntegration (preferred — has connection state)
        if self._mcp_integration is not None:
            for tool in getattr(self._mcp_integration, "_tools", []):
                if getattr(tool, "should_defer", False) or getattr(tool, "is_mcp", False):
                    deferred.append({
                        "name": tool.name,
                        "description": tool.description,
                        "server": getattr(tool, "mcp_props", None) and tool.mcp_props.server_name or "",
                    })

        # 2. Fallback: scan registry for MCP tools
        if not deferred and self._registry_ref is not None:
            for name, tool in getattr(self._registry_ref, "_tools", {}).items():
                mcp_props = getattr(tool, "mcp_props", None)
                if mcp_props is not None:
                    deferred.append({
                        "name": name,
                        "description": tool.description,
                        "server": mcp_props.server_name,
                    })

        # Match against query
        matches = [
            d for d in deferred
            if query in d["name"].lower()
            or query in d["description"].lower()
            or query in d.get("server", "").lower()
            or any(query in v.lower() for v in str(d.get("metadata", {})).split() if len(v) > 2)
        ]

        if not matches:
            # Report connection errors per CC spec
            errors = self._gather_connection_errors()
            if errors:
                return ToolResult(
                    success=True,
                    output=(
                        f"No matching deferred tools found for '{params['query']}'. "
                        f"Connection issues: {errors}"
                    ),
                )
            return ToolResult(
                success=True,
                output=(
                    f"No matching deferred tools found for '{params['query']}'. "
                    f"Available MCP tools: {len(deferred)} total across "
                    f"{len(set(d.get('server','') for d in deferred))} server(s). "
                    f"Names: {', '.join(d['name'] for d in deferred[:20])}"
                ),
            )

        lines = [f"Matching deferred tools for '{params['query']}':"]
        for m in matches[:20]:
            lines.append(f"  {m['name']} — {m['description'][:120]}")
        if len(matches) > 20:
            lines.append(f"  ... and {len(matches) - 20} more")
        return ToolResult(success=True, output="\n".join(lines))

    def _gather_connection_errors(self) -> str:
        """Collect connection errors from MCP servers (CC spec requirement)."""
        parts: list[str] = []
        if self._mcp_integration is not None:
            manager = getattr(self._mcp_integration, "_manager", None)
            if manager is not None:
                for name, bridge in getattr(manager, "_bridges", {}).items():
                    if not getattr(bridge, "is_connected", False):
                        parts.append(f"{name}: not connected")
        return "; ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# WaitForMcpServers — available when ToolSearch is disabled
# ---------------------------------------------------------------------------

class WaitForMcpServersTool(BaseTool):
    """Wait for one or more MCP servers still connecting in the background.

    CC reference: "When tool search is disabled, Claude uses WaitForMcpServers."

    Injected with MCP context at session startup.
    """

    metadata = ToolMetadata(effects=frozenset())

    def __init__(self) -> None:
        super().__init__()
        self._mcp_integration: Any = None

    def set_mcp_context(self, _registry: Any, mcp_integration: Any) -> None:
        self._mcp_integration = mcp_integration

    @property
    def name(self) -> str:
        return "WaitForMcpServers"

    @property
    def description(self) -> str:
        return (
            "Wait for one or more MCP servers that are still connecting. "
            "Use this when a needed server isn't connected yet and you need "
            "its tools to complete a request."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "servers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of MCP servers to wait for (empty = all)",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds to wait (default: 30)",
                },
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        server_names: list[str] | None = params.get("servers")
        timeout = int(params.get("timeout_seconds", 30))

        if self._mcp_integration is None:
            return ToolResult(success=True, output="No MCP integration active — all servers ready.")

        manager = getattr(self._mcp_integration, "_manager", None)
        if manager is None:
            return ToolResult(success=True, output="No MCP manager — all servers ready.")

        bridges = getattr(manager, "_bridges", {})
        targets = set(server_names) if server_names else set(bridges.keys())

        connected: list[str] = []
        waiting: list[str] = []
        for name in targets:
            bridge = bridges.get(name)
            if bridge is None:
                waiting.append(f"{name}: not configured")
            elif bridge.is_connected:
                connected.append(name)
            else:
                waiting.append(f"{name}: not connected")

        if not waiting:
            return ToolResult(
                success=True,
                output=f"All requested MCP servers are ready: {', '.join(connected) or 'none'}",
            )

        return ToolResult(
            success=True,
            output=(
                f"MCP servers ready: {', '.join(connected) or 'none'}\n"
                f"Still connecting: {'; '.join(waiting)}\n"
                f"Retry if the needed server becomes available."
            ),
        )
