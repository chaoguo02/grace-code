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
        run_id="root-run-123",
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
    assert persisted_run["parent_run_id"] == "root-run-123"
    assert persisted_run["status"] == "completed"
    assert {task["status"] for task in persisted_tasks} == {"completed"}
    assert {task["generation"] for task in persisted_tasks} == {3}
    assert all(task["report"]["session_id"].startswith("child-") for task in persisted_tasks)

    with store._connect() as conn:
        terminal_events = conn.execute(
            """
            SELECT event_json FROM session_trace_events
            WHERE session_id = ? AND event_type = 'delegation_completed'
            """,
            (parent.id,),
        ).fetchall()
    assert len(terminal_events) == 1
    assert run_id in str(terminal_events[0]["event_json"])


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


def test_agent_batch_required_partial_blocks_completion(tmp_path):
    from agent.session.models import AgentRunResult, AgentRunStatus

    tool, runtime, store, _parent = _bound_batch(tmp_path)

    def partial_worker(**kwargs):
        child = SimpleNamespace(id="child-partial", generation=1)
        kwargs["child_created_callback"](child)
        return AgentRunResult(
            agent_name=kwargs["request"].definition.name,
            session_id=child.id,
            status=AgentRunStatus.PARTIAL,
            summary="Only part of the required task completed",
            error="required evidence missing",
        )

    runtime.spawn_agent = partial_worker
    result = tool.execute({
        "description": "Required partial must fail the gate",
        "tasks": [
            {"id": "one", "agent": "explore", "goal": "One", "prompt": "One"},
            {"id": "two", "agent": "explore", "goal": "Two", "prompt": "Two"},
        ],
    })

    run = store.get_delegation_run(str(result.metadata["delegation_run_id"]))
    assert result.success is False
    assert "required" in result.error.lower()
    assert run["status"] == "partial"
    assert run["phase"] == "partial"


def test_agent_batch_optional_partial_does_not_block_completion(tmp_path):
    from agent.session.models import AgentRunResult, AgentRunStatus

    tool, runtime, store, _parent = _bound_batch(tmp_path)
    calls = 0

    def mixed_worker(**kwargs):
        nonlocal calls
        calls += 1
        child = SimpleNamespace(id=f"child-mixed-{calls}", generation=1)
        kwargs["child_created_callback"](child)
        status = (
            AgentRunStatus.PARTIAL
            if kwargs["request"].description == "Optional"
            else AgentRunStatus.COMPLETED
        )
        return AgentRunResult(
            agent_name=kwargs["request"].definition.name,
            session_id=child.id,
            status=status,
            summary=status.value,
            error="optional gap" if status is AgentRunStatus.PARTIAL else "",
        )

    runtime.spawn_agent = mixed_worker
    result = tool.execute({
        "description": "Optional partial is advisory",
        "topology": "chain",
        "tasks": [
            {
                "id": "optional",
                "agent": "explore",
                "goal": "Optional",
                "prompt": "Optional",
                "required": False,
                "isolation": "current",
            },
            {
                "id": "required",
                "agent": "explore",
                "goal": "Required",
                "prompt": "Required",
                "required": True,
                "isolation": "current",
            },
        ],
    })

    run = store.get_delegation_run(str(result.metadata["delegation_run_id"]))
    assert result.success is True
    assert run["status"] == "completed"


def test_agent_batch_preserved_worktree_waits_for_integration(tmp_path):
    from agent.session.models import (
        AgentRunResult,
        AgentRunStatus,
        WorktreeChange,
        WorktreeDisposition,
        WorktreeEvidence,
    )

    tool, runtime, store, _parent = _bound_batch(tmp_path)

    def worktree_worker(**kwargs):
        child = SimpleNamespace(id=f"child-wt-{len(runtime.calls) + 1}", generation=1)
        kwargs["child_created_callback"](child)
        runtime.calls.append({"child": child.id})
        return AgentRunResult(
            agent_name=kwargs["request"].definition.name,
            session_id=child.id,
            status=AgentRunStatus.COMPLETED,
            summary="Changes ready for review",
            worktree=WorktreeEvidence(
                change=WorktreeChange.UNCOMMITTED,
                path=str(tmp_path / child.id),
                branch=f"agent/{child.id}",
                base_branch="master",
                changed_files=("example.py",),
                revision="abc123",
            ),
            worktree_disposition=WorktreeDisposition.PRESERVED,
        )

    runtime.spawn_agent = worktree_worker
    result = tool.execute({
        "description": "Writing workers need integration",
        "tasks": [
            {"id": "one", "agent": "explore", "goal": "One", "prompt": "One"},
            {"id": "two", "agent": "explore", "goal": "Two", "prompt": "Two"},
        ],
    })

    run_id = str(result.metadata["delegation_run_id"])
    assert result.success is False
    assert result.metadata["phase"] == "awaiting_integration"
    assert store.get_delegation_run(run_id)["status"] == "running"
    assert {
        task["integration_status"]
        for task in store.list_delegation_tasks(run_id)
    } == {"pending"}


def test_session_store_marks_inflight_delegations_recovery_required(tmp_path):
    tool, _runtime, store, parent = _bound_batch(tmp_path)
    del tool
    store.create_delegation_run(
        run_id="interrupted-run",
        parent_session_id=parent.id,
        topology="fan_out_fan_in",
    )
    store.create_delegation_task(
        task_id="interrupted-run:queued",
        delegation_run_id="interrupted-run",
        agent_type="explore",
        goal="Queued",
    )
    store.create_delegation_task(
        task_id="interrupted-run:running",
        delegation_run_id="interrupted-run",
        agent_type="explore",
        goal="Running",
    )
    store.update_delegation_task("interrupted-run:running", status="running")

    assert store.reconcile_interrupted_delegations() == ["interrupted-run"]
    run = store.get_delegation_run("interrupted-run")
    tasks = store.list_delegation_tasks("interrupted-run")
    assert run["status"] == "partial"
    assert run["phase"] == "recovery_required"
    assert {task["status"] for task in tasks} == {"interrupted"}


def test_retry_task_supersedes_original_within_same_run(tmp_path):
    tool, _runtime, store, parent = _bound_batch(tmp_path)
    del tool
    run_id = "retry-run"
    original_id = f"{run_id}:original"
    replacement_id = f"{run_id}:retry-1"
    store.create_delegation_run(
        run_id=run_id,
        parent_session_id=parent.id,
        topology="one_to_one",
    )
    store.create_delegation_task(
        task_id=original_id,
        delegation_run_id=run_id,
        agent_type="explore",
        goal="Inspect",
    )
    store.update_delegation_task(original_id, status="failed", error="boom")
    store.complete_delegation_run(run_id, status="partial")

    store.create_delegation_task(
        task_id=replacement_id,
        delegation_run_id=run_id,
        agent_type="explore",
        goal="Inspect",
        retry_count=1,
        max_retries=1,
        supersedes_task_id=original_id,
    )

    assert store.get_delegation_task(original_id)["status"] == "superseded"
    assert store.get_delegation_run(run_id)["status"] == "running"
    replacement = store.get_delegation_task(replacement_id)
    assert replacement["retry_count"] == 1
    assert replacement["supersedes_task_id"] == original_id

    store.update_delegation_task(replacement_id, status="completed")
    converged = store.reconcile_delegation_run(run_id)
    assert converged["status"] == "completed"
    assert converged["phase"] == "completed"


def test_restart_reconciliation_preserves_integration_and_converges_stable_runs(tmp_path):
    tool, _runtime, store, parent = _bound_batch(tmp_path)
    del tool
    store.create_delegation_run(
        run_id="awaiting-integration",
        parent_session_id=parent.id,
        topology="one_to_one",
    )
    store.create_delegation_task(
        task_id="awaiting-integration:task",
        delegation_run_id="awaiting-integration",
        agent_type="general",
        goal="Edit",
    )
    store.update_delegation_task(
        "awaiting-integration:task",
        status="completed",
        integration_status="pending",
    )
    store.create_delegation_run(
        run_id="ready-to-complete",
        parent_session_id=parent.id,
        topology="one_to_one",
    )
    store.create_delegation_task(
        task_id="ready-to-complete:task",
        delegation_run_id="ready-to-complete",
        agent_type="explore",
        goal="Inspect",
    )
    store.update_delegation_task(
        "ready-to-complete:task",
        status="completed",
    )

    assert store.reconcile_interrupted_delegations() == []
    awaiting = store.get_delegation_run("awaiting-integration")
    completed = store.get_delegation_run("ready-to-complete")
    assert (awaiting["status"], awaiting["phase"]) == (
        "running", "awaiting_integration",
    )
    assert (completed["status"], completed["phase"]) == (
        "completed", "completed",
    )


def test_cancelled_task_rejects_late_worker_report(tmp_path):
    tool, _runtime, store, parent = _bound_batch(tmp_path)
    del tool
    store.create_delegation_run(
        run_id="cancel-race",
        parent_session_id=parent.id,
        topology="one_to_one",
    )
    store.create_delegation_task(
        task_id="cancel-race:task",
        delegation_run_id="cancel-race",
        agent_type="explore",
        goal="Inspect",
    )
    assert store.update_delegation_task(
        "cancel-race:task",
        status="cancelled",
        expected_statuses=("queued",),
    ) is True
    assert store.update_delegation_task(
        "cancel-race:task",
        status="completed",
        expected_statuses=("queued", "running"),
    ) is False
    assert store.get_delegation_task("cancel-race:task")["status"] == "cancelled"


def test_retry_supersede_can_only_be_claimed_once(tmp_path):
    import pytest

    tool, _runtime, store, parent = _bound_batch(tmp_path)
    del tool
    store.create_delegation_run(
        run_id="single-retry",
        parent_session_id=parent.id,
        topology="one_to_one",
    )
    store.create_delegation_task(
        task_id="single-retry:original",
        delegation_run_id="single-retry",
        agent_type="explore",
        goal="Inspect",
    )
    store.update_delegation_task("single-retry:original", status="failed")
    store.create_delegation_task(
        task_id="single-retry:first",
        delegation_run_id="single-retry",
        agent_type="explore",
        goal="Inspect",
        retry_count=1,
        supersedes_task_id="single-retry:original",
    )
    with pytest.raises(ValueError, match="already superseded"):
        store.create_delegation_task(
            task_id="single-retry:second",
            delegation_run_id="single-retry",
            agent_type="explore",
            goal="Inspect",
            retry_count=1,
            supersedes_task_id="single-retry:original",
        )


def test_session_store_migrates_legacy_delegation_schema(tmp_path):
    import sqlite3

    from agent.session.session_store import SessionStore

    db_path = tmp_path / "legacy-sessions.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE delegation_runs (
                id TEXT PRIMARY KEY, parent_session_id TEXT NOT NULL,
                parent_run_id TEXT NOT NULL DEFAULT '', topology TEXT NOT NULL,
                reason_code TEXT NOT NULL DEFAULT '', explanation TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL, budget_json TEXT NOT NULL DEFAULT '{}',
                downgraded_from TEXT NULL, is_team INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, completed_at TEXT NULL
            );
            CREATE TABLE delegation_tasks (
                id TEXT PRIMARY KEY, delegation_run_id TEXT NOT NULL,
                child_session_id TEXT NULL, generation INTEGER NOT NULL DEFAULT 0,
                agent_type TEXT NOT NULL, purpose TEXT NOT NULL DEFAULT 'analysis',
                goal TEXT NOT NULL, scope_json TEXT NOT NULL DEFAULT '[]',
                dependencies_json TEXT NOT NULL DEFAULT '[]',
                expected_files_json TEXT NOT NULL DEFAULT '[]',
                write_files_json TEXT NOT NULL DEFAULT '[]', required INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL, report_json TEXT NULL, error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, started_at TEXT NULL, completed_at TEXT NULL
            );
            """
        )

    store = SessionStore(db_path)
    with store._connect() as conn:
        run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(delegation_runs)")
        }
        task_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(delegation_tasks)")
        }
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(delegation_tasks)")
        }

    assert {"phase", "version", "synthesis_json", "verification_json"} <= run_columns
    assert {
        "prompt", "retry_count", "max_retries", "supersedes_task_id",
        "integration_status", "integration_error",
    } <= task_columns
    assert "idx_delegation_tasks_supersedes" in indexes


def test_agent_batch_feature_flag_fails_closed(tmp_path, monkeypatch):
    tool, runtime, store, parent = _bound_batch(tmp_path)
    monkeypatch.setenv("GRACE_MULTI_AGENT_MODE_ENABLED", "false")

    result = tool.execute({
        "description": "Disabled batch",
        "tasks": [
            {"id": "one", "agent": "explore", "goal": "One", "prompt": "One"},
            {"id": "two", "agent": "explore", "goal": "Two", "prompt": "Two"},
        ],
    })

    assert result.success is False
    assert "GRACE_MULTI_AGENT_MODE_ENABLED" in result.error
    assert runtime.calls == []
    assert store.list_delegation_runs(parent.id) == []


def test_agent_batch_large_dag_uses_independent_total_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("GRACE_MAX_MULTI_AGENT_TASKS", "8")
    monkeypatch.setenv("GRACE_MAX_FANOUT_PER_TURN", "2")
    monkeypatch.setenv("GRACE_MAX_CONCURRENT_SUBAGENTS", "2")
    tool, runtime, store, _parent = _bound_batch(tmp_path)
    tasks = [
        {
            "id": f"inspect-{index}",
            "agent": "explore",
            "goal": f"Inspect {index}",
            "prompt": f"Inspect module {index}",
        }
        for index in range(6)
    ]

    assert tool.parameters_schema["properties"]["tasks"]["maxItems"] == 8
    result = tool.execute({
        "description": "Six independent bounded tasks",
        "topology": "fan_out_fan_in",
        "tasks": tasks,
    })

    assert result.success is True
    assert len(runtime.calls) == 6
    run_id = str(result.metadata["delegation_run_id"])
    assert len(store.list_delegation_tasks(run_id)) == 6


def test_agent_batch_rejects_dag_over_total_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("GRACE_MAX_MULTI_AGENT_TASKS", "3")
    tool, runtime, store, parent = _bound_batch(tmp_path)

    result = tool.execute({
        "description": "Too many tasks",
        "tasks": [
            {"id": f"task-{index}", "agent": "explore", "goal": "Inspect", "prompt": "Inspect"}
            for index in range(4)
        ],
    })

    assert result.success is False
    assert "GRACE_MAX_MULTI_AGENT_TASKS" in result.error
    assert runtime.calls == []
    assert store.list_delegation_runs(parent.id) == []
