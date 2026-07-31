"""End-to-end coverage for the deterministic weather MCP fixture."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

pytest.importorskip("mcp.server")

from agent.mcp.client import MCPToolBridge
from agent.mcp.types import MCPServerConfig
from agent.tool_availability_guard import ToolAvailabilityGuard
from agent.session.agent_registry import AgentRegistryV2
from agent.session.mcp_integration import MCPToolIntegration
from agent.session.runtime import SessionRuntime
from config.schema import load_config
from core.base import ExecutionContext, ToolRegistry
from core.policy import PhasePolicy
from core.policy_registry import PolicyAwareToolRegistry
from core.process import LocalRuntime
from examples.mcp_servers.weather_mock_server import (
    FIXTURE_ID,
    current_weather,
    weather_forecast,
)
from skills.buffer import SkillContextBuffer
from skills.registry import SkillRegistry
from skills.tool import SkillTool


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_weather_fixture_normalizes_chinese_and_english_city_names() -> None:
    assert current_weather({"city": "北京"})["city"] == "北京"
    result = weather_forecast({"city": "Shanghai", "days": 2})
    assert result["city"] == "上海"
    assert result["days"] == 2
    assert len(result["forecast"]) == 2
    assert result["fixture"] == FIXTURE_ID


@pytest.mark.parametrize("days", [0, 6, True, "invalid"])
def test_weather_fixture_rejects_invalid_forecast_days(days: object) -> None:
    with pytest.raises(ValueError, match="between 1 and 5"):
        weather_forecast({"city": "深圳", "days": days})


def test_weather_fixture_reports_supported_cities() -> None:
    with pytest.raises(ValueError, match="北京, 上海, 深圳"):
        current_weather({"city": "杭州"})


@pytest.mark.asyncio
async def test_weather_mcp_stdio_discovery_and_call() -> None:
    bridge = MCPToolBridge(MCPServerConfig(
        name="weather-mock",
        command=sys.executable,
        args=["-m", "examples.mcp_servers.weather_mock_server"],
        cwd=str(PROJECT_ROOT),
        timeout_seconds=10,
    ))
    try:
        tools = await bridge.connect()
        assert {tool.runtime_name for tool in tools} == {
            "mcp__weather_mock__weather_get_current",
            "mcp__weather_mock__weather_get_forecast",
        }

        result = await bridge.call_tool(
            "weather_get_forecast",
            {"city": "深圳", "days": 2},
        )
        assert result.is_error is False
        payload = json.loads(result.text)
        assert payload["fixture"] == FIXTURE_ID
        assert payload["city"] == "深圳"
        assert len(payload["forecast"]) == 2
    finally:
        await bridge.close()


def test_default_config_connects_weather_tools_to_runtime_integration() -> None:
    config = load_config()
    weather_config = config.mcp_servers["weather-mock"]
    integration = MCPToolIntegration({
        "mcp_servers": {"weather-mock": weather_config},
    })
    try:
        integration.initialize()
        assert {
            "mcp__weather_mock__weather_get_current",
            "mcp__weather_mock__weather_get_forecast",
        }.issubset({tool.name for tool in integration.tools})
        assert integration.connection_errors() == {}
    finally:
        integration.shutdown()


def test_city_weather_skill_activates_deferred_mcp_schemas() -> None:
    config = load_config()
    skill_registry = SkillRegistry.for_project(PROJECT_ROOT)
    skill_buffer = SkillContextBuffer()
    registry = ToolRegistry(
        skill_registry=skill_registry,
        skill_buffer=skill_buffer,
    )
    registry.register(SkillTool(
        skill_registry,
        buffer=skill_buffer,
        runtime=LocalRuntime(workspace_root=PROJECT_ROOT),
    ))
    integration = MCPToolIntegration({
        "mcp_servers": {
            "weather-mock": config.mcp_servers["weather-mock"],
        },
    })
    try:
        integration.initialize()
        integration.register_into(registry)
        registry.attach_mcp_integration(integration)
        session_registry = registry.scoped(ExecutionContext(
            workspace_root=str(PROJECT_ROOT),
            repo_path=str(PROJECT_ROOT),
        ))
        policy_registry = PolicyAwareToolRegistry(
            base=session_registry,
            phase_policy=PhasePolicy(),
            repo_path=str(PROJECT_ROOT),
            phase_name="test",
        )
        weather_names = {
            "mcp__weather_mock__weather_get_current",
            "mcp__weather_mock__weather_get_forecast",
        }
        assert weather_names.isdisjoint({
            schema.name for schema in policy_registry.get_schemas()
        })

        result = policy_registry.execute_tool(
            "Skill",
            {
                "skill_name": "city-weather",
                "arguments": "北京未来两天天气",
            },
        )

        assert result.success is True
        assert skill_registry.get_skill_meta(
            "city-weather",
        ).mcp_servers == frozenset({"weather-mock"})
        assert weather_names.issubset({
            schema.name for schema in policy_registry.get_schemas()
        })
        current = policy_registry.execute_tool(
            "mcp__weather_mock__weather_get_current",
            {"city": "北京"},
        )
        assert current.success is True
        assert json.loads(current.output)["city"] == "北京"
        assert current.metadata["mcp_server"] == "weather-mock"
    finally:
        integration.shutdown()
        skill_registry.close()


def test_plan_session_includes_skill_mcp_dependencies_as_deferred_tools() -> None:
    config = load_config()
    skill_registry = SkillRegistry.for_project(PROJECT_ROOT)
    registry = ToolRegistry(skill_registry=skill_registry)
    integration = MCPToolIntegration({
        "mcp_servers": {
            "weather-mock": config.mcp_servers["weather-mock"],
        },
    })
    try:
        integration.initialize()
        integration.register_into(registry)
        registry.attach_mcp_integration(integration)
        tool_availability_guard = ToolAvailabilityGuard()
        for name in integration.tool_names:
            tool_availability_guard.register(name)

        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime._mcp_integration = integration
        runtime._base_registry = registry
        runtime._tool_availability_guard = tool_availability_guard
        plan_spec = AgentRegistryV2(PROJECT_ROOT).get("plan")

        names = runtime._mcp_tool_names_for_spec(plan_spec)

        assert {
            "mcp__weather_mock__weather_get_current",
            "mcp__weather_mock__weather_get_forecast",
        }.issubset(names)
    finally:
        integration.shutdown()
        skill_registry.close()
