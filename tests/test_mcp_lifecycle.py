"""MCP lifecycle boundary tests."""

from dataclasses import dataclass
from types import SimpleNamespace

@dataclass
class FakeToolInfo:
    server_name: str
    name: str
    description: str = "fake"
    input_schema: dict = None
    metadata: dict = None

    def __post_init__(self):
        if self.input_schema is None:
            self.input_schema = {"type": "object", "properties": {}}
        if self.metadata is None:
            self.metadata = {}

    @property
    def runtime_name(self):
        return f"mcp__{self.server_name}__{self.name}"


class FakeBridge:
    def __init__(self, config):
        self.config = config
        self.closed = False
        self._connected = False
        self._fingerprint = "fake-v1"

    @property
    def is_connected(self):
        return self._connected

    @property
    def fingerprint(self):
        return self._fingerprint

    def mark_disconnected(self):
        self._connected = False

    def launch_fingerprint_changed(self):
        return False

    def set_tools_changed_callback(self, callback):
        self._tools_changed_callback = callback

    async def discover_tools(self):
        return [FakeToolInfo(server_name=self.config.name, name="tool")]

    async def connect(self):
        self._connected = True
        return [FakeToolInfo(server_name=self.config.name, name="tool")]

    async def close(self):
        self.closed = True
        self._connected = False

    async def call_tool(self, tool_name, arguments):
        from agent.mcp.client import MCPCallResult
        return MCPCallResult(content=[{"text": "ok"}], is_error=False)

    async def list_resources(self):
        return []

    async def read_resource(self, uri):
        return {"contents": []}


class FailingBridge(FakeBridge):
    async def connect(self):
        raise RuntimeError("boom")


class TestMCPLifecycle:
    def test_deferred_schema_is_hidden_until_tool_search_activates_it(self):
        from core.base import BaseTool, ToolRegistry, ToolResult
        from tools.workflow_tool import ToolSearchTool

        class DeferredTool(BaseTool):
            name = "mcp__docs__lookup"
            description = "Look up documentation"
            parameters_schema = {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            }
            from agent.mcp.types import MCPToolProps
            mcp_props = MCPToolProps(
                server_name="docs",
                original_tool_name="lookup",
                is_deferred=True,
            )

            @property
            def should_defer(self):
                return self.mcp_props.is_deferred

            @should_defer.setter
            def should_defer(self, value):
                self.mcp_props.is_deferred = value

            @property
            def always_load(self):
                return self.mcp_props.always_load

            @always_load.setter
            def always_load(self, value):
                self.mcp_props.always_load = value

            def execute(self, params):
                return ToolResult(success=True, output="ok")

        deferred = DeferredTool()
        search = ToolSearchTool()
        integration = SimpleNamespace(
            deferred_tool_descriptors=lambda: [{
                "name": deferred.name,
                "description": deferred.description,
                "server": "docs",
            }],
            activate_tools=lambda names: [
                name for name in names
                if not setattr(deferred, "always_load", True)
                and not setattr(deferred, "should_defer", False)
            ],
            connection_errors=lambda: {},
        )
        registry = ToolRegistry().register(search).register(deferred)
        search.set_mcp_context(registry, integration)

        assert [schema.name for schema in registry.get_schemas()] == ["ToolSearch"]

        result = search.execute({"query": "documentation"})

        assert result.success is True
        assert deferred.should_defer is False
        assert [schema.name for schema in registry.get_schemas()] == [
            "ToolSearch",
            "mcp__docs__lookup",
        ]

    def test_shared_entrypoint_initializes_and_registers_mcp(self, monkeypatch):
        import agent.session
        from entry.bootstrap.registry_factory import initialize_mcp_integration

        calls = []

        class FakeIntegration:
            def __init__(self, raw_config):
                calls.append(("construct", raw_config))

            def initialize(self):
                calls.append(("initialize",))

            def register_into(self, registry):
                calls.append(("register", registry))

        monkeypatch.setattr(agent.session, "MCPToolIntegration", FakeIntegration)
        from core.base import ToolRegistry
        registry = ToolRegistry()
        config = SimpleNamespace(mcp_servers={"docs": {"command": "fake"}})

        integration = initialize_mcp_integration(config, registry)

        assert isinstance(integration, FakeIntegration)
        assert calls == [
            ("construct", {"mcp_servers": config.mcp_servers}),
            ("initialize",),
            ("register", registry),
        ]

    def test_shared_entrypoint_skips_empty_mcp_config(self):
        from entry.bootstrap.registry_factory import initialize_mcp_integration

        assert initialize_mcp_integration(
            SimpleNamespace(mcp_servers={}),
            SimpleNamespace(_tools={}),
        ) is None

    def test_manager_tracks_server_ownership_and_closes_one_server(self, monkeypatch):
        from agent.mcp import sync_bridge
        from agent.mcp.sync_bridge import SyncMCPToolManager
        from agent.mcp.types import MCPServerConfig

        bridges = {}

        def fake_create(config):
            bridge = FakeBridge(config)
            bridges[config.name] = bridge
            return bridge

        monkeypatch.setattr(sync_bridge, "create_mcp_bridge", fake_create)
        manager = SyncMCPToolManager()
        try:
            tools = manager.load_and_discover([
                MCPServerConfig(name="alpha"),
                MCPServerConfig(name="beta"),
            ])

            assert {tool.name for tool in tools} >= {"mcp__alpha__tool", "mcp__beta__tool"}
            assert "mcp__alpha__tool" in manager.server_tools["alpha"]
            assert "mcp__beta__tool" in manager.server_tools["beta"]

            manager.close_server("alpha")

            assert bridges["alpha"].closed is True
            assert bridges["beta"].closed is False
            assert "alpha" not in manager.server_tools
            assert "beta" in manager.server_tools
        finally:
            manager.close_all()

    def test_manager_records_failed_server_and_shutdown_clears_state(self, monkeypatch):
        from agent.mcp import sync_bridge
        from agent.mcp.sync_bridge import SyncMCPToolManager
        from agent.mcp.types import MCPServerConfig

        monkeypatch.setattr(sync_bridge, "create_mcp_bridge", lambda config: FailingBridge(config))
        manager = SyncMCPToolManager()
        try:
            tools = manager.load_and_discover([MCPServerConfig(name="bad")])
            assert tools == []
            assert manager.failed_servers["bad"] == "boom"
        finally:
            manager.close_all()

        assert manager.server_tools == {}
        assert manager.failed_servers == {}

    def test_deferred_sync_execute_works_inside_running_event_loop(self):
        from agent.mcp.tool_adapter import deferred_mcp_tool

        tool = deferred_mcp_tool(
            name="mcp__fake__tool",
            description="fake",
            input_schema={"type": "object", "properties": {}},
            execute_fn=lambda _args: "ok",
            server_name="fake",
            original_tool_name="tool",
        )

        async def run_inside_loop():
            return tool.execute({})

        import asyncio
        assert asyncio.run(run_inside_loop()).output == "ok"

    def test_restart_sliding_window_is_bounded(self):
        from agent.mcp.sync_bridge import SyncMCPToolManager

        manager = SyncMCPToolManager(
            health_interval_seconds=60,
            restart_limit=2,
            restart_window_seconds=60,
        )
        try:
            assert manager._restart_allowed("docs") is True
            manager._record_restart("docs")
            manager._record_restart("docs")
            assert manager._restart_allowed("docs") is False
        finally:
            manager.close_all()

    def test_stdio_close_uses_terminate_then_force_kill(self, monkeypatch):
        import asyncio
        from agent.mcp import client
        from agent.mcp.client import MCPToolBridge
        from agent.mcp.types import MCPServerConfig

        monkeypatch.setattr(client, "MCP_CLOSE_GRACE_SECONDS", 0.01)
        monkeypatch.setattr(client, "MCP_FORCE_KILL_WAIT_SECONDS", 0.01)

        class HungTransport:
            terminated = False
            killed = False

            async def __aexit__(self, *_args):
                raise RuntimeError("transport stuck")

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            async def wait(self):
                if not self.killed:
                    await asyncio.Event().wait()

        bridge = MCPToolBridge(MCPServerConfig(name="stdio"))
        transport = HungTransport()
        bridge._transport_cm = transport

        asyncio.run(bridge.close())

        assert transport.terminated is True
        assert transport.killed is True
