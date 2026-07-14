from __future__ import annotations

from agent.policy import build_task_policy, extract_explicit_read_paths
from agent.task import Task, ToolCall
from llm.base import LLMToolSchema
from llm.tool_call_validator import validate_tool_calls
from tools.base import ToolEffect


def test_extract_explicit_read_paths_from_direct_file_mentions() -> None:
    paths = extract_explicit_read_paths(
        "只梳理 agent/core.py 里 broad analysis controller 的主要阶段切换逻辑，不要改代码。",
        repo_path=".",
    )

    assert paths == frozenset({"agent/core.py"})


def test_single_file_analysis_keeps_explicit_policy_scope() -> None:
    task = Task(
        description="只梳理 agent/core.py 里 broad analysis controller 的主要阶段切换逻辑，不要改代码。",
        repo_path=".",
        intent="analysis",
    )

    policy = build_task_policy(task)

    assert policy.execution.allowed_read_paths == frozenset({"agent/core.py"})


def test_single_file_analysis_policy_scopes_allowed_reads() -> None:
    task = Task(
        description="只梳理 agent/core.py 里 broad analysis controller 的主要阶段切换逻辑，不要改代码。",
        repo_path=".",
        intent="analysis",
    )

    policy = build_task_policy(task)

    assert policy.execution.allowed_read_paths == frozenset({"agent/core.py"})
    assert policy.execution.strict_file_scope is True
    assert policy.execution.allowed_effects == frozenset({
        ToolEffect.READ_WORKSPACE,
        ToolEffect.PRODUCE_DELIVERABLE,
    })
    assert ToolEffect.NETWORK in policy.execution.denied_effects
    assert ToolEffect.READ_AGENT_STATE in policy.execution.denied_effects


def test_user_tool_class_restrictions_are_typed_effects() -> None:
    task = Task(
        description=(
            "Do not run shell commands. Do not run tests. "
            "Do not use web. Do not use memory. Edit src/app.py."
        ),
        repo_path=".",
        intent="edit",
    )

    policy = build_task_policy(task)

    assert {
        ToolEffect.EXECUTE,
        ToolEffect.TEST,
        ToolEffect.NETWORK,
        ToolEffect.READ_AGENT_STATE,
        ToolEffect.WRITE_AGENT_STATE,
    }.issubset(policy.execution.denied_effects)
    assert policy.execution.denied_tools == frozenset()


def test_unregistered_finish_tool_is_rejected_by_control_plane() -> None:
    result = validate_tool_calls(
        [ToolCall(name="finish", params={"summary": "done"})],
        [LLMToolSchema(name="file_read", description="read", parameters={})],
    )

    assert result.valid is False
    assert result.error_type == "unknown_tool"
    assert result.offending_tool == "finish"
