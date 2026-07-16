from __future__ import annotations

import sys
from pathlib import Path

from agent.core import AgentConfig, ReActAgent
from agent.task import Observation, ObservationStatus, ToolCall, ToolOutcome
from llm.base import MockBackend
from runtime.project_environment import ExecutableKind
from tools.base import (
    NoopTool,
    ToolEffect,
    ToolMetadata,
    ToolRegistry,
    ToolRole,
)
from tools.memory_tool import MemoryWriteTool
from tools.runtime import RunResult, Runtime
from tools.test_tool import PytestTool


class _MissingTargetRuntime(Runtime):
    @property
    def name(self) -> str:
        return "missing-target"

    def resolve_executable(self, kind):
        return str(Path(sys.executable).resolve()) if kind is ExecutableKind.PYTHON else None

    def exec(self, cmd: str, cwd: str | None = None, timeout: int = 30) -> RunResult:
        raise AssertionError("parameterized execution required")

    def execute(self, command, args=None, cwd=None, timeout=30, env=None) -> RunResult:
        return RunResult(
            returncode=4,
            stdout="ERROR: file or directory not found: tests/missing_test.py",
            stderr="",
        )


def test_pytest_missing_target_has_typed_outcome(tmp_path):
    tool = PytestTool(runtime=_MissingTargetRuntime(), workspace_root=tmp_path)

    result = tool.execute({"path": "tests/missing_test.py"})

    assert result.outcome is ToolOutcome.TEST_TARGET_MISSING
    assert result.to_observation("arbitrary_test_runner").outcome is ToolOutcome.TEST_TARGET_MISSING


def test_missing_target_control_flow_does_not_parse_tool_name_or_text():
    agent = ReActAgent(MockBackend([]), ToolRegistry(), AgentConfig(stream=False))
    typed = Observation(
        status=ObservationStatus.ERROR,
        output="opaque",
        tool_name="renamed_test_runner",
        outcome=ToolOutcome.TEST_TARGET_MISSING,
    )
    textual_imitation = Observation(
        status=ObservationStatus.ERROR,
        output="pytest requested test target is missing",
        tool_name="test",
    )

    assert agent._is_missing_test_target_observation(typed) is True
    assert agent._is_missing_test_target_observation(textual_imitation) is False


def test_confirmation_search_is_selected_by_effect_not_name():
    registry = ToolRegistry()
    discovery = NoopTool("renamed_discovery")
    discovery.metadata = ToolMetadata(
        effects=frozenset({ToolEffect.DISCOVER_WORKSPACE})
    )
    registry.register(discovery)
    agent = ReActAgent(MockBackend([]), registry, AgentConfig(stream=False))

    assert agent._is_targeted_confirmation_call(
        ToolCall(name="renamed_discovery", params={})
    ) is True


def test_memory_persistence_role_is_declarative():
    assert ToolRole.PERSIST_MEMORY in MemoryWriteTool.metadata.roles
