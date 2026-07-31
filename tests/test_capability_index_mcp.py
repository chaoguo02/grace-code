from __future__ import annotations

from types import SimpleNamespace

from capabilities import (
    CapabilityIndex,
    CapabilityKind,
    CapabilityQuery,
    CapabilityStatus,
)
from capabilities.providers.mcp_provider import McpCapabilityProvider
from capabilities.render import CapabilityPromptRenderer
from capabilities.sanitize import sanitize_error
from agent.mcp.types import MCPToolProps


class _McpTool:
    def __init__(
        self,
        *,
        name: str,
        description: str,
        server_name: str,
        should_defer: bool = False,
        always_load: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.mcp_props = MCPToolProps(
            server_name=server_name,
            original_tool_name=name.rsplit("__", 1)[-1],
            is_deferred=should_defer,
            always_load=always_load,
        )
        self.should_defer = should_defer
        self.always_load = always_load


def _query(*, visible_to_model=True) -> CapabilityQuery:
    return CapabilityQuery(
        kinds=frozenset({CapabilityKind.MCP_SERVER, CapabilityKind.MCP_TOOL}),
        visible_to_model=visible_to_model,
    )


def test_mcp_provider_renders_loaded_and_deferred_tools() -> None:
    integration = SimpleNamespace(
        server_tools={
            "docs": ["mcp__docs__lookup", "mcp__docs__search"],
        },
        failed_servers={},
        tools=[
            _McpTool(
                name="mcp__docs__lookup",
                description="Look up documentation",
                server_name="docs",
                should_defer=True,
            ),
            _McpTool(
                name="mcp__docs__search",
                description="Search documentation",
                server_name="docs",
                should_defer=False,
                always_load=True,
            ),
        ],
        tool_names=frozenset({"mcp__docs__lookup", "mcp__docs__search"}),
        deferred_tool_descriptors=lambda: [{
            "name": "mcp__docs__lookup",
            "description": "Look up documentation",
            "server": "docs",
        }],
    )
    query = _query(visible_to_model=None)

    snapshot = CapabilityIndex([McpCapabilityProvider(integration)]).snapshot(query)
    sections = CapabilityPromptRenderer().render(snapshot, query)
    content = "\n\n".join(section.content for section in sections)

    assert "Use `ToolSearch`" in content
    assert "Connected MCP servers:" in content
    assert "MCP server 'docs' providing 2 tools" in content
    assert "Loaded MCP tools:" in content
    assert "mcp__docs__search — Search documentation" in content
    assert "Deferred MCP tools:" in content
    assert "mcp__docs__lookup — Look up documentation" in content
    lookup = [
        descriptor for descriptor in snapshot.descriptors
        if descriptor.metadata.name == "mcp__docs__lookup"
    ][0]
    assert lookup.runtime.status is CapabilityStatus.DEFERRED


def test_mcp_provider_renders_failed_servers_with_sanitized_errors() -> None:
    integration = SimpleNamespace(
        server_tools={"bad": []},
        failed_servers={
            "bad": "Authorization: Bearer abc.def token=super-secret command python server.py --api-key hidden",
        },
        tools=[],
        tool_names=frozenset(),
        deferred_tool_descriptors=lambda: [],
    )
    query = _query(visible_to_model=None)

    snapshot = CapabilityIndex([McpCapabilityProvider(integration)]).snapshot(query)
    sections = CapabilityPromptRenderer().render(snapshot, query)
    content = "\n\n".join(section.content for section in sections)

    assert "Failed MCP servers:" in content
    assert "bad" in content
    assert "[REDACTED]" in content
    assert "abc.def" not in content
    assert "super-secret" not in content
    failed = snapshot.by_kind(CapabilityKind.MCP_SERVER)[0]
    assert failed.runtime.status is CapabilityStatus.FAILED


def test_mcp_server_fallback_description_is_never_empty() -> None:
    integration = SimpleNamespace(
        server_tools={"empty": []},
        failed_servers={},
        tools=[],
        tool_names=frozenset(),
        deferred_tool_descriptors=lambda: [],
    )

    snapshot = CapabilityIndex([McpCapabilityProvider(integration)]).snapshot(_query())

    assert snapshot.descriptors[0].metadata.description == "MCP server 'empty' providing 0 tools"


def test_mcp_snapshot_fingerprint_is_deterministic() -> None:
    integration = SimpleNamespace(
        server_tools={"docs": ["mcp__docs__lookup"]},
        failed_servers={},
        tools=[
            _McpTool(
                name="mcp__docs__lookup",
                description="Look up documentation",
                server_name="docs",
                should_defer=True,
            ),
        ],
        tool_names=frozenset({"mcp__docs__lookup"}),
        deferred_tool_descriptors=lambda: [],
    )
    query = _query(visible_to_model=None)
    provider = McpCapabilityProvider(integration)

    first = CapabilityIndex([provider]).snapshot(query)
    second = CapabilityIndex([provider]).snapshot(query)

    assert first.fingerprint == second.fingerprint


def test_sanitize_error_redacts_common_secret_shapes() -> None:
    text = sanitize_error(
        "Bearer abc.def api_key=secret-token https://example.com?token=raw-secret",
    )

    assert "[REDACTED]" in text
    assert "abc.def" not in text
    assert "secret-token" not in text
    assert "raw-secret" not in text
