"""Runtime-level coverage for persisted multi-agent delegation workflows."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


class _FakeRuntime:
    def __init__(self, *, store, registry) -> None:
        self._store = store
        self.agent_registry = registry
        self._event_callback = None
        self.calls: list[dict[str, object]] = []

    def spawn_agent(self, **kwargs):
        from agent.session.models import AgentRunResult, AgentRunStatus

        request = kwargs["request"]
        child_number = len(self.calls) + 1
        child = SimpleNamespace(id=f"child-{child_number}", generation=3)
        kwargs["child_created_callback"](child)
        self.calls.append({
            "agent": request.definition.name,
            "prompt": request.prompt,
            "metadata": kwargs["child_metadata"],
        })
        return AgentRunResult(
            agent_name=request.definition.name,
            session_id=child.id,
            status=AgentRunStatus.COMPLETED,
            summary=f"report-{child_number}",
            tokens_used=100 * child_number,
        )


def _bound_batch(tmp_path: Path):
    from agent.session.agent_batch_tool import AgentBatchTool
    from agent.session.agent_registry import AgentRegistryV2
    from agent.session.execution_budget import ExecutionBudget, ExecutionBudgetConfig
    from agent.session.models import SessionMode
    from agent.session.run_context import CancellationToken, RunContext
    from agent.session.session_store import SessionStore
    from core.base import ToolEffect
    from core.policy import PhasePolicy

    store = SessionStore(tmp_path / "sessions.db")
    parent = store.create_session(
        agent_name="research",
        mode=SessionMode.PRIMARY,
        repo_path=str(Path.cwd()),
        title="batch runtime test",
    )
    registry = AgentRegistryV2(Path.cwd())
    runtime = _FakeRuntime(store=store, registry=registry)
    budget = ExecutionBudget(ExecutionBudgetConfig(
        token_limit=20_000,
        step_limit=20,
        time_limit_seconds=30,
    ))
    budget.start()
    context = RunContext(
        budget=budget,
        cancellation=CancellationToken(),
        delegation_step_limit=8,
        phase_policy=PhasePolicy(),
        delegation_effects=frozenset({
            ToolEffect.DELEGATE_READ_ONLY,
            ToolEffect.DELEGATE_WRITE,
        }),
    )
    tool = AgentBatchTool(
        runtime,
        parent.id,
        caller_agent_name="research",
    ).with_run_context(context)
    return tool, runtime, store, parent


def test_agent_batch_persists_fan_out_and_worker_reports(tmp_path):
    tool, runtime, store, parent = _bound_batch(tmp_path)

    result = tool.execute({
        "description": "Inspect two independent modules",
        "topology": "fan_out_fan_in",
        "tasks": [
            {
                "id": "backend",
                "agent": "explore",
                "goal": "Inspect backend",
                "prompt": "Find the backend entry point.",
                "purpose": "exploration",
                "scope": ["server"],
                "expected_files": ["server/main.py"],
            },
            {
                "id": "frontend",
                "agent": "explore",
                "goal": "Inspect frontend",
                "prompt": "Find the frontend entry point.",
                "purpose": "exploration",
                "scope": ["web/src"],
                "expected_files": ["web/src/App.tsx"],
            },
        ],
    })

    assert result.success is True
    assert len(runtime.calls) == 2
    run_id = str(result.metadata["delegation_run_id"])
    persisted_run = store.get_delegation_run(run_id)
    persisted_tasks = store.list_delegation_tasks(run_id)
    assert persisted_run is not None
    assert persisted_run["parent_session_id"] == parent.id
    assert persisted_run["status"] == "completed"
    assert {task["status"] for task in persisted_tasks} == {"completed"}
    assert {task["generation"] for task in persisted_tasks} == {3}
    assert all(task["report"]["session_id"].startswith("child-") for task in persisted_tasks)


def test_agent_batch_chain_injects_dependency_summary(tmp_path):
    tool, runtime, store, _parent = _bound_batch(tmp_path)

    result = tool.execute({
        "description": "Inspect, then review",
        "topology": "chain",
        "tasks": [
            {
                "id": "inspect",
                "agent": "explore",
                "goal": "Inspect code",
                "prompt": "Collect evidence.",
                "purpose": "exploration",
            },
            {
                "id": "review",
                "agent": "security-reviewer",
                "goal": "Review evidence",
                "prompt": "Review the prior evidence.",
                "purpose": "security_review",
                "depends_on": ["inspect"],
            },
        ],
    })

    assert result.success is True
    assert [call["agent"] for call in runtime.calls] == [
        "explore",
        "security-reviewer",
    ]
    assert "- inspect: report-1" in str(runtime.calls[1]["prompt"])
    run_id = str(result.metadata["delegation_run_id"])
    assert store.get_delegation_run(run_id)["topology"] == "chain"


def test_agent_batch_rejects_overlapping_shared_workspace_writes(tmp_path):
    tool, runtime, store, parent = _bound_batch(tmp_path)

    result = tool.execute({
        "description": "Unsafe shared edits",
        "tasks": [
            {
                "id": "one",
                "agent": "code-reviewer",
                "goal": "Edit one",
                "prompt": "Edit the file.",
                "write_files": ["README.md"],
                "isolation": "current",
            },
            {
                "id": "two",
                "agent": "code-reviewer",
                "goal": "Edit two",
                "prompt": "Edit the same file.",
                "write_files": ["README.md"],
                "isolation": "current",
            },
        ],
    })

    assert result.success is False
    assert "write conflict" in result.error
    assert runtime.calls == []
    assert store.list_delegation_runs(parent.id) == []
