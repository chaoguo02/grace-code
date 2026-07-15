from __future__ import annotations

import asyncio

import pytest

from agent.core import AgentConfig
from agent.v2.agent_registry import AgentRegistryV2
from agent.v2.mcp_integration import MCPRuntimeToolProxy, MCPToolIntegration
from agent.v2.runtime import SessionRuntime
from agent.v2.session_store import SessionStore
from llm.base import MockBackend
from runtime.mcp import MCPServerConfig
from runtime.mcp.types import MCPToolProps
from runtime.tool import ToolResult as RuntimeToolResult, ToolUseContext, build_tool
from tools.base import (
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

    assert len(integration._server_configs) == 1
    config = integration._server_configs[0]
    assert isinstance(config, MCPServerConfig)
    assert config.name == "fs"
    assert config.command == "npx"
    assert config.args == ["-y", "server-fs"]
    assert config.timeout_seconds == 3.0


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
