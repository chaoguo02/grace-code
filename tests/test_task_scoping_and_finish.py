from __future__ import annotations

from agent.policy import build_task_policy
from agent.task import Task, ToolCall
from llm.base import LLMToolSchema
from llm.tool_call_validator import validate_tool_calls
from tools.base import ToolEffect


def test_explicit_read_paths_flow_through_policy() -> None:
    """Paths passed explicitly via Task.explicit_read_paths flow into policy."""
    task = Task(
        description="Analyze auth module",
        repo_path=".",
        intent="analysis",
        explicit_read_paths=frozenset({"agent/core.py"}),
    )

    policy = build_task_policy(task)

    assert policy.execution.allowed_read_paths == frozenset({"agent/core.py"})
    assert policy.execution.strict_file_scope is True


def test_no_implicit_path_extraction_from_description() -> None:
    """Paths are NOT inferred from natural language descriptions."""
    task = Task(
        description="只梳理 agent/core.py 里 broad analysis controller 的主要阶段切换逻辑，不要改代码。",
        repo_path=".",
        intent="analysis",
    )

    policy = build_task_policy(task)

    # No explicit read paths → None (not NLP-inferred)
    assert policy.execution.allowed_read_paths is None


def test_single_file_analysis_policy_scopes_allowed_reads() -> None:
    """Explicit read paths produce a strict file-scoped analysis policy."""
    task = Task(
        description="Analyze auth module",
        repo_path=".",
        intent="analysis",
        explicit_read_paths=frozenset({"agent/core.py"}),
    )

    policy = build_task_policy(task)

    assert policy.execution.allowed_read_paths == frozenset({"agent/core.py"})
    assert policy.execution.strict_file_scope is True
    assert policy.execution.allowed_effects == frozenset({
        ToolEffect.READ_WORKSPACE,
        ToolEffect.PRODUCE_DELIVERABLE,
    })


def test_user_tool_class_restrictions_not_inferred_from_description() -> None:
    """Blocked effects are NOT inferred from natural language anymore.

    They must be set explicitly via Task fields or CLI flags.
    """
    task = Task(
        description=(
            "Do not run shell commands. Do not run tests. "
            "Do not use web. Do not use memory. Edit src/app.py."
        ),
        repo_path=".",
        intent="edit",
    )

    policy = build_task_policy(task)

    # No blocked effects from NLP — denied_effects is empty
    assert policy.execution.denied_effects == frozenset()
    assert policy.execution.denied_tools == frozenset()


def test_unregistered_finish_tool_is_rejected_by_control_plane() -> None:
    result = validate_tool_calls(
        [ToolCall(name="finish", params={"summary": "done"})],
        [LLMToolSchema(name="file_read", description="read", parameters={})],
    )

    assert result.valid is False
    assert result.error_type == "unknown_tool"
    assert result.offending_tool == "finish"
