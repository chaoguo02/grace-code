from __future__ import annotations

import asyncio

import pytest

from agent.core import AgentConfig
from agent.session.agent_registry import AgentRegistryV2
from agent.session.mcp_integration import MCPRuntimeToolProxy, MCPToolIntegration
from agent.session.runtime import SessionRuntime
from agent.session.session_store import SessionStore
from llm.base import MockBackend
from agent.mcp import MCPServerConfig
from agent.mcp.types import MCPToolProps
from executor.tool import ToolResult as RuntimeToolResult, ToolUseContext, build_tool
from core.base import (
    NoopTool,
    ToolEffect,
    ToolMetadata,
    ToolRegistry,
    ToolRole,
)


def _runtime_tool(name: str, output: str = "ok", *, is_error: bool = False):
    async def call_fn(input: dict, _context: ToolUseContext) -> RuntimeToolResult[str]:
        metadata = {"mcp_is_error": is_error}
        if is_error:
            metadata["mcp_error"] = output
        return RuntimeToolResult(output=output, metadata=metadata)

    tool = build_tool(
        name=name,
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        call_fn=call_fn,
        description_text=f"{name} description",
        mcp_props=MCPToolProps(server_name="test"),
    )
    tool.metadata = {"is_mcp": True}
    return tool


def test_raw_mcp_servers_config_parses_stdio_servers():
    integration = MCPToolIntegration({
        "mcp_servers": {
            "fs": {"command": "npx", "args": ["-y", "server-fs"], "timeout": 3},
            "remote": {"type": "sse", "url": "https://example.com/sse"},
        }
    })

    assert len(integration._server_configs) == 2
    config = integration._server_configs[0]
    assert isinstance(config, MCPServerConfig)
    assert config.name == "fs"
    assert config.command == "npx"
    assert config.args == ["-y", "server-fs"]
    assert config.timeout_seconds == 3.0
    # SSE server now also parsed (CC-aligned: all 4 transports supported)
    sse = integration._server_configs[1]
    assert sse.name == "remote"
    assert sse.type == "sse"
    assert sse.url == "https://example.com/sse"


def test_initialize_without_servers_is_noop():
    integration = MCPToolIntegration({})

    integration.initialize()

    assert integration.is_initialized is True
    assert integration.manager is None
    assert integration.tools == []


def test_get_tool_pool_requires_initialization():
    integration = MCPToolIntegration({})

    with pytest.raises(RuntimeError, match="not initialized"):
        integration.get_tool_pool([])


def test_get_tool_pool_filters_denied_mcp_and_keeps_builtin_duplicate():
    integration = MCPToolIntegration({}, deny_tools=["mcp__server__delete_*"])
    integration._initialized = True
    builtin = NoopTool("mcp__server__echo", output="builtin")
    mcp_echo = MCPRuntimeToolProxy(_runtime_tool("mcp__server__echo"))
    mcp_delete = MCPRuntimeToolProxy(_runtime_tool("mcp__server__delete_file"))
    integration._tools = [mcp_delete, mcp_echo]

    pool = integration.get_tool_pool([builtin])

    assert [tool.name for tool in pool] == ["mcp__server__echo"]
    assert pool[0] is builtin


def test_runtime_tool_proxy_converts_success_and_error_results():
    success = MCPRuntimeToolProxy(_runtime_tool("mcp__server__ok", "done"))
    failure = MCPRuntimeToolProxy(_runtime_tool("mcp__server__fail", "remote failed", is_error=True))

    ok_result = success.execute({"value": "x"})
    fail_result = failure.execute({"value": "x"})

    assert ok_result.success is True
    assert ok_result.output == "done"
    assert fail_result.success is False
    assert fail_result.error == "remote failed"


def test_register_into_skips_duplicate_tools():
    registry = ToolRegistry().register(NoopTool("mcp__server__echo"))
    integration = MCPToolIntegration({})
    integration._initialized = True
    integration._tools = [
        MCPRuntimeToolProxy(_runtime_tool("mcp__server__echo")),
        MCPRuntimeToolProxy(_runtime_tool("mcp__server__add")),
    ]

    integration.register_into(registry)

    assert "mcp__server__echo" in registry
    assert "mcp__server__add" in registry


def test_disconnect_agent_servers_keeps_session_scoped_tools_without_server_name():
    integration = MCPToolIntegration({})
    integration._initialized = True
    session_tool = MCPRuntimeToolProxy(_runtime_tool("mcp__session__echo"))
    agent_tool = MCPRuntimeToolProxy(_runtime_tool("mcp__agent__echo"))
    agent_tool.server_name = "agent-server"
    integration._tools = [session_tool, agent_tool]
    integration._runtime_tools = []

    class Spec:
        mcp_servers = [{"agent-server": {"command": "demo"}}]

    integration.disconnect_agent_servers(Spec())

    assert [tool.name for tool in integration._tools] == ["mcp__session__echo"]


def test_session_runtime_exposes_mcp_tools_to_build_and_general_only(tmp_path):
    agent_registry = AgentRegistryV2(project_dir=tmp_path)
    base_registry = ToolRegistry()
    for tool_name in sorted(agent_registry.tool_names_for("build")):
        tool = NoopTool(tool_name)
        if tool_name == "task":
            tool.metadata = ToolMetadata(
                effects=frozenset({ToolEffect.DELEGATE_WRITE}),
                roles=frozenset({ToolRole.DELEGATE}),
            )
        base_registry.register(tool)
    mcp_tool = MCPRuntimeToolProxy(_runtime_tool("mcp__server__echo"))
    base_registry.register(mcp_tool)

    class FakeIntegration:
        tool_names = frozenset({"mcp__server__echo"})

    runtime = SessionRuntime(
        store=SessionStore(str(tmp_path / "sessions.db")),
        backend=MockBackend([]),
        base_registry=base_registry,
        agent_registry=agent_registry,
        root_agent_config=AgentConfig(stream=False),
        log_dir=str(tmp_path / "logs"),
        mcp_integration=FakeIntegration(),
    )
    session = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    build_registry = runtime._build_registry_for_session(agent_registry.get("build"), session)
    general_registry = runtime._build_registry_for_session(agent_registry.get("general"), session)
    plan_registry = runtime._build_registry_for_session(agent_registry.get("plan"), session)
    explore_registry = runtime._build_registry_for_session(agent_registry.get("explore"), session)

    assert "mcp__server__echo" in build_registry
    assert "mcp__server__echo" in general_registry
    assert "mcp__server__echo" not in plan_registry
    assert "mcp__server__echo" not in explore_registry


# ═══════════════════════════════════════════════════════════════════════════
# Batch M3: SSE notification dispatch + response routing
# ═══════════════════════════════════════════════════════════════════════════


class TestSseNotificationDispatch:
    """SSE bridge properly dispatches MCP notifications and routes responses."""

    @staticmethod
    def _make_sse_bridge():
        """Create a minimal SSE bridge for testing dispatch methods."""
        from agent.mcp.client import SseMCPBridge
        from agent.mcp.types import MCPServerConfig

        config = MCPServerConfig(
            name="test-sse-server",
            type="sse",
            url="http://localhost:9999",
            idle_timeout_seconds=30,
        )
        return SseMCPBridge(config)

    @staticmethod
    def _run(coro):
        """Run a coroutine synchronously (methods under test don't actually await)."""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        # In an async context, create a new loop (test purposes only)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=5)

    def test_dispatch_tools_list_changed_notification(self):
        """notifications/tools/list_changed is dispatched to _on_list_changed."""
        bridge = self._make_sse_bridge()

        called = []
        async def fake_on_list_changed(msg):
            called.append(msg)

        bridge._on_list_changed = fake_on_list_changed
        self._run(bridge._dispatch_notification(
            "notifications/tools/list_changed",
            {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
        ))
        assert len(called) == 1

    def test_dispatch_other_notification_to_handler(self):
        """Other notifications/* are forwarded to _on_tools_changed callback."""
        bridge = self._make_sse_bridge()

        handler_calls = []
        def handler(msg):
            handler_calls.append(msg)

        bridge._on_tools_changed = handler
        self._run(bridge._dispatch_notification(
            "notifications/resources/updated",
            {"jsonrpc": "2.0", "method": "notifications/resources/updated"},
        ))
        assert len(handler_calls) == 1

    def test_dispatch_unknown_notification_without_handler_is_silent(self):
        """Notifications without a handler are logged, not raised."""
        bridge = self._make_sse_bridge()
        bridge._on_tools_changed = None

        # Should not raise
        self._run(bridge._dispatch_notification(
            "notifications/resources/updated",
            {"jsonrpc": "2.0", "method": "notifications/resources/updated"},
        ))

    def test_dispatch_non_notification_method_is_logged(self):
        """Non-notification methods are logged at debug level."""
        bridge = self._make_sse_bridge()

        # Should not raise
        self._run(bridge._dispatch_notification(
            "tools/call",
            {"jsonrpc": "2.0", "method": "tools/call"},
        ))

    def test_route_sse_response_stores_by_id(self):
        """JSON-RPC responses from SSE are stored in _sse_responses dict."""
        bridge = self._make_sse_bridge()
        self._run(bridge._route_sse_response(42, {"jsonrpc": "2.0", "id": 42, "result": "ok"}))

        assert hasattr(bridge, "_sse_responses")
        assert 42 in bridge._sse_responses
        assert bridge._sse_responses[42]["result"] == "ok"

    def test_route_sse_response_supports_string_ids(self):
        """SSE response routing handles string RPC ids."""
        bridge = self._make_sse_bridge()
        self._run(bridge._route_sse_response("req-1", {"jsonrpc": "2.0", "id": "req-1", "result": "done"}))

        assert "req-1" in bridge._sse_responses
        assert bridge._sse_responses["req-1"]["result"] == "done"

    def test_dispatch_notification_with_handler_error_is_silent(self):
        """If the notification handler raises, the SSE stream should not break."""
        bridge = self._make_sse_bridge()

        def failing_handler(msg):
            raise RuntimeError("handler bug")

        bridge._on_tools_changed = failing_handler
        # Should not raise
        self._run(bridge._dispatch_notification(
            "notifications/resources/updated",
            {"jsonrpc": "2.0", "method": "notifications/resources/updated"},
        ))
