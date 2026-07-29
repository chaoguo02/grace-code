"""Integration coordinator contract tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent.session.integration_coordinator import (
    DelegationIntegrationCoordinator,
    IntegrationDecision,
)
from agent.session.models import SessionMode, WorktreeChange, WorktreeEvidence
from agent.session.session_store import SessionStore


class _Status:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeRuntime:
    def __init__(self, store: SessionStore, repo_path: Path) -> None:
        self.store = store
        self.repo_path = str(repo_path)
        self.evidence: dict[str, WorktreeEvidence] = {}
        self.applied: list[str] = []

    def get_session_repo_path(self, _session_id: str) -> str:
        return self.repo_path

    def inspect_subagent_worktree(
        self, _parent_session_id: str, child_session_id: str,
    ) -> WorktreeEvidence:
        return self.evidence[child_session_id]

    def apply_subagent_worktree(
        self, _parent_session_id: str, child_session_id: str, *, expected_revision: str,
    ):
        evidence = self.evidence[child_session_id]
        assert expected_revision == evidence.revision
        self.applied.append(child_session_id)
        task = self.store.get_delegation_task_for_child(child_session_id)
        self.store.update_delegation_task(
            str(task["id"]),
            status=str(task["status"]),
            integration_status="applied",
        )
        self.store.reconcile_delegation_run(str(task["delegation_run_id"]))
        return SimpleNamespace(status=_Status("applied"), error="", is_success=True)

    def discard_subagent_worktree(self, *args, **kwargs):
        raise AssertionError("discard was not expected")

    def retain_subagent_worktree(self, *args, **kwargs):
        raise AssertionError("retain was not expected")


def _setup(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    parent = store.create_session(
        agent_name="orchestrator",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="integration",
    )
    store.create_delegation_run(
        run_id="run-1",
        parent_session_id=parent.id,
        topology="chain",
    )
    runtime = _FakeRuntime(store, tmp_path)
    return store, parent, runtime


def _task(
    store: SessionStore,
    *,
    task_id: str,
    child_id: str,
    write_file: str,
    dependencies: tuple[str, ...] = (),
) -> None:
    store.create_delegation_task(
        task_id=task_id,
        delegation_run_id="run-1",
        agent_type="general",
        goal=task_id,
        dependencies=dependencies,
        write_files=(write_file,),
    )
    store.update_delegation_task(
        task_id,
        status="completed",
        child_session_id=child_id,
        integration_status="pending",
    )


def _evidence(tmp_path: Path, child_id: str, path: str, revision: str):
    return WorktreeEvidence(
        change=WorktreeChange.UNCOMMITTED,
        path=str(tmp_path / child_id),
        branch=f"agent/{child_id}",
        base_branch="master",
        changed_files=(path,),
        revision=revision,
    )


def test_integrates_in_dependency_order_and_verifies_parent(tmp_path, monkeypatch):
    store, parent, runtime = _setup(tmp_path)
    _task(store, task_id="run-1:backend", child_id="child-backend", write_file="server/api.py")
    _task(
        store,
        task_id="run-1:frontend",
        child_id="child-frontend",
        write_file="web/src/api.ts",
        dependencies=("run-1:backend",),
    )
    runtime.evidence = {
        "child-backend": _evidence(tmp_path, "child-backend", "server/api.py", "rev-1"),
        "child-frontend": _evidence(tmp_path, "child-frontend", "web/src/api.ts", "rev-2"),
    }
    monkeypatch.setenv(
        "GRACE_MULTI_AGENT_VERIFICATION_COMMANDS",
        '[["python", "-c", "import sys; sys.exit(0)"]]'
    )

    result = DelegationIntegrationCoordinator(runtime, store).integrate(
        parent_session_id=parent.id,
        run_id="run-1",
        decisions=[
            IntegrationDecision("run-1:frontend", "apply", "rev-2"),
            IntegrationDecision("run-1:backend", "apply", "rev-1"),
        ],
    )

    assert runtime.applied == ["child-backend", "child-frontend"]
    assert result["run"]["status"] == "completed"
    assert result["run"]["verification"]["status"] == "passed"


def test_rejects_actual_changes_outside_declared_write_set(tmp_path):
    store, parent, runtime = _setup(tmp_path)
    _task(store, task_id="run-1:write", child_id="child-write", write_file="safe.py")
    runtime.evidence["child-write"] = _evidence(
        tmp_path, "child-write", "unexpected.py", "rev-1",
    )

    result = DelegationIntegrationCoordinator(runtime, store).integrate(
        parent_session_id=parent.id,
        run_id="run-1",
        decisions=[IntegrationDecision("run-1:write", "apply", "rev-1")],
    )

    assert runtime.applied == []
    assert result["run"]["status"] == "partial"
    assert result["run"]["phase"] == "integration_failed"
    assert result["tasks"][0]["integration_status"] == "contract_violation"


def test_requires_configured_parent_verification_after_apply(tmp_path, monkeypatch):
    store, parent, runtime = _setup(tmp_path)
    _task(store, task_id="run-1:write", child_id="child-write", write_file="safe.py")
    runtime.evidence["child-write"] = _evidence(
        tmp_path, "child-write", "safe.py", "rev-1",
    )
    monkeypatch.delenv("GRACE_MULTI_AGENT_VERIFICATION_COMMANDS", raising=False)

    result = DelegationIntegrationCoordinator(runtime, store).integrate(
        parent_session_id=parent.id,
        run_id="run-1",
        decisions=[IntegrationDecision("run-1:write", "apply", "rev-1")],
    )

    assert result["run"]["status"] == "running"
    assert result["run"]["phase"] == "awaiting_verification"
    assert result["run"]["verification"]["status"] == "not_configured"
