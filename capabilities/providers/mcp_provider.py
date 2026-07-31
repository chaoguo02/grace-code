"""MCP integration adapter for the capability index."""

from __future__ import annotations

from typing import Any

from capabilities.models import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityMetadata,
    CapabilityQuery,
    CapabilityRuntimeState,
    CapabilityStatus,
)


class McpCapabilityProvider:
    def __init__(self, mcp_integration: Any) -> None:
        self._mcp_integration = mcp_integration

    def list_descriptors(self, query: CapabilityQuery) -> tuple[CapabilityDescriptor, ...]:
        if (
            CapabilityKind.MCP_SERVER not in query.kinds
            and CapabilityKind.MCP_TOOL not in query.kinds
        ):
            return ()

        descriptors: list[CapabilityDescriptor] = []
        server_tools = dict(getattr(self._mcp_integration, "server_tools", {}) or {})
        failed_servers = dict(getattr(self._mcp_integration, "failed_servers", {}) or {})
        tools = list(getattr(self._mcp_integration, "tools", []) or [])
        server_names = sorted(set(server_tools) | set(failed_servers) | {_server_name(tool) for tool in tools if _server_name(tool)})

        if CapabilityKind.MCP_SERVER in query.kinds:
            for server_name in server_names:
                tool_count = len(server_tools.get(server_name, ()))
                error = str(failed_servers.get(server_name, "") or "")
                status = CapabilityStatus.FAILED if server_name in failed_servers else CapabilityStatus.AVAILABLE
                descriptor = CapabilityDescriptor(
                    metadata=CapabilityMetadata(
                        kind=CapabilityKind.MCP_SERVER,
                        name=server_name,
                        description=f"MCP server '{server_name}' providing {tool_count} tools",
                        source="mcp",
                        namespace="mcp",
                        invocation="ToolSearch" if status is not CapabilityStatus.FAILED else "unavailable",
                        server_name=server_name,
                    ),
                    runtime=CapabilityRuntimeState(
                        status=status,
                        visible_to_model=status is not CapabilityStatus.FAILED,
                        activation="ToolSearch" if status is not CapabilityStatus.FAILED else "",
                        error=error,
                    ),
                )
                if query.matches(descriptor):
                    descriptors.append(descriptor)

        if CapabilityKind.MCP_TOOL in query.kinds:
            for tool in tools:
                server_name = _server_name(tool)
                failed = bool(server_name and server_name in failed_servers)
                deferred = bool(getattr(tool, "should_defer", False)) and not bool(getattr(tool, "always_load", False))
                if failed:
                    status = CapabilityStatus.FAILED
                    visible = False
                    activation = ""
                    error = str(failed_servers.get(server_name, "") or "")
                elif deferred:
                    status = CapabilityStatus.DEFERRED
                    visible = False
                    activation = "ToolSearch"
                    error = ""
                else:
                    status = CapabilityStatus.AVAILABLE
                    visible = True
                    activation = "direct"
                    error = ""
                descriptor = CapabilityDescriptor(
                    metadata=CapabilityMetadata(
                        kind=CapabilityKind.MCP_TOOL,
                        name=str(getattr(tool, "name", "") or ""),
                        description=str(getattr(tool, "description", "") or "(no description)"),
                        source="mcp",
                        namespace="mcp",
                        invocation=activation,
                        server_name=server_name,
                    ),
                    runtime=CapabilityRuntimeState(
                        status=status,
                        visible_to_model=visible,
                        activation=activation,
                        error=error,
                    ),
                )
                if query.matches(descriptor):
                    descriptors.append(descriptor)

        return tuple(descriptors)


def _server_name(tool: Any) -> str:
    return str(
        getattr(
            getattr(tool, "mcp_props", None),
            "server_name",
            "",
        ) or ""
    )
