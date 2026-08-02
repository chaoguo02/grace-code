"""Durable retry/resume scheduler tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent.session.agent_registry import AgentRegistryV2
from agent.session.delegation_scheduler import DelegationRunScheduler
from agent.session.models import AgentRunResult, AgentRunStatus, SessionMode
from agent.session.session_store import SessionStore


class _Runtime:
    def __init__(self, store: SessionStore, project: Path) -> None:
        self._store = store
        self.agent_registry = AgentRegistryV2(project)
        self._root_agent_config = SimpleNamespace(
            max_steps=20,
            budget_tokens=20_000,
        )
        self.executed: list[str] = []

    @property
    def session_store(self):
        return self._store

    @property
    def root_agent_config(self):
        return self._root_agent_config

    def run_explicit_delegation(self, parent_session_id: str, **kwargs):
        del parent_session_id
        task_id = str(kwargs["child_metadata"]["delegation_task_id"])
        self.executed.append(task_id)
        child = SimpleNamespace(id=f"child-{len(self.executed)}", generation=1)
        kwargs["child_created_callback"](child)
        return AgentRunResult(
            agent_name=kwargs["request"].agent_name,
            session_id=child.id,
            status=AgentRunStatus.COMPLETED,
            summary=f"completed {task_id}",
        )


def _chain(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    parent = store.create_session(
        agent_name="research",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="retry chain",
    )
    store.create_delegation_run(
        run_id="chain",
        parent_session_id=parent.id,
        topology="chain",
    )
    previous = None
    for task_id in ("inspect", "implement", "verify"):
        full_id = f"chain:{task_id}"
        store.create_delegation_task(
            task_id=full_id,
            delegation_run_id="chain",
            agent_type="explore",
            goal=task_id,
            prompt=task_id,
            dependencies=(previous,) if previous else (),
            max_retries=2,
        )
        store.update_delegation_task(
            full_id,
            status="failed" if task_id == "inspect" else "failed",
            error="Dependency incomplete" if previous else "root failed",
        )
        previous = full_id
    store.complete_delegation_run("chain", status="partial")
    return store, parent


def test_non_leaf_retry_replaces_and_rewires_entire_downstream(tmp_path):
    store, _parent = _chain(tmp_path)

    replacements = store.prepare_delegation_retry("chain:inspect")

    assert len(replacements) == 3
    assert {
        task["status"] for task in store.list_delegation_tasks("chain")
        if task["id"] in {"chain:inspect", "chain:implement", "chain:verify"}
    } == {"superseded"}
    by_old = {str(task["supersedes_task_id"]): task for task in replacements}
    assert by_old["chain:implement"]["dependencies"] == [
        by_old["chain:inspect"]["id"]
    ]
    assert by_old["chain:verify"]["dependencies"] == [
        by_old["chain:implement"]["id"]
    ]
    assert store.get_delegation_run("chain")["status"] == "running"


def test_scheduler_executes_replacement_chain_in_ready_order(tmp_path):
    store, parent = _chain(tmp_path)
    replacements = store.prepare_delegation_retry("chain:inspect")
    by_old = {str(task["supersedes_task_id"]): task for task in replacements}
    runtime = _Runtime(store, Path.cwd())

    run = DelegationRunScheduler(runtime, store).execute(
        parent_session_id=parent.id,
        run_id="chain",
    )

    assert runtime.executed == [
        by_old["chain:inspect"]["id"],
        by_old["chain:implement"]["id"],
        by_old["chain:verify"]["id"],
    ]
    assert run["status"] == "completed"


def test_resume_replaces_interrupted_subgraph(tmp_path):
    store, parent = _chain(tmp_path)
    # Simulate restart facts: only the root was active; downstream was blocked.
    store.update_delegation_task("chain:inspect", status="interrupted")
    store.transition_delegation_run(
        "chain", status="partial", phase="recovery_required",
    )

    replacements = store.prepare_delegation_resume("chain")
    runtime = _Runtime(store, Path.cwd())
    run = DelegationRunScheduler(runtime, store).execute(
        parent_session_id=parent.id,
        run_id="chain",
    )

    assert len(replacements) == 3
    assert run["status"] == "completed"
    assert len(runtime.executed) == 3
