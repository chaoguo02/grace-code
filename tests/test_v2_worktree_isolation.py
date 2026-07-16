from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent.v2.worktree_service import (
    WorktreeChange,
    WorktreeIsolationError,
    WorktreeOperationStatus,
    apply_worktree,
    create_worktree,
    discard_worktree,
    discard_reviewed_worktree,
    finalize_worktree,
    inspect_changes,
    inspect_worktree,
)
from agent.v2.models import AgentIsolation
from runtime.state_paths import STATE_HOME_ENV, ProjectStatePaths
from core.base import ExecutionContext, ToolRegistry
from tools.file_tool import FileReadTool
from runtime.process import LocalRuntime, ProcessTermination
from tools.search_tool import FindFilesTool, SearchTextTool


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Forge Tests"],
        cwd=path, capture_output=True, check=True,
    )
    (path / "tracked.txt").write_text("main\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path, capture_output=True, check=True,
    )
    return path


def test_local_runtime_rejects_process_cwd_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    runtime = LocalRuntime(workspace_root=workspace)

    result = runtime.execute("git", args=["status"], cwd=str(outside))

    assert result.success is False
    assert result.termination is ProcessTermination.START_FAILED
    assert "outside workspace" in result.stderr


def test_registry_scoping_clones_tools_without_mutating_parent(tmp_path):
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    parent.mkdir()
    child.mkdir()
    (parent / "value.txt").write_text("parent", encoding="utf-8")
    (child / "value.txt").write_text("child", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(FileReadTool(workspace_root=str(parent)))
    registry.register(FindFilesTool(workspace_root=str(parent)))
    registry.register(SearchTextTool(workspace_root=str(parent)))

    scoped = registry.scoped(ExecutionContext(
        workspace_root=str(child), repo_path=str(child),
    ))

    assert "parent" in registry.execute_tool("file_read", {"path": "value.txt"}).output
    assert "child" in scoped.execute_tool("file_read", {"path": "value.txt"}).output
    assert str(parent / "value.txt") in registry.execute_tool(
        "find_files", {"pattern": "value.txt"},
    ).output
    assert str(child / "value.txt") in scoped.execute_tool(
        "find_files", {"pattern": "value.txt"},
    ).output
    assert "parent" in registry.execute_tool(
        "search_text", {"pattern": "parent"},
    ).output
    assert "child" in scoped.execute_tool(
        "search_text", {"pattern": "child"},
    ).output
    denied = scoped.execute_tool("file_read", {"path": str(parent / "value.txt")})
    assert denied.success is False
    assert "workspace" in (denied.error or "").lower()
    assert str(parent / "value.txt").lower() in (denied.error or "").lower()


def test_worktree_state_is_external_and_changes_are_preserved(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    state_home = tmp_path / "agent-state"
    monkeypatch.setenv(STATE_HOME_ENV, str(state_home))

    worktree, effective_path = create_worktree(
        str(repo), "general", "child1", isolation=AgentIsolation.WORKTREE,
    )
    assert worktree is not None
    effective = Path(effective_path).resolve()
    with pytest.raises(ValueError):
        effective.relative_to(repo.resolve())
    assert effective.is_relative_to(ProjectStatePaths.for_project(repo).worktrees)

    (effective / "new-file.txt").write_text("child output\n", encoding="utf-8")
    assert inspect_changes(worktree) is WorktreeChange.UNCOMMITTED

    evidence = finalize_worktree(worktree, str(repo))
    assert evidence.change is WorktreeChange.UNCOMMITTED
    assert evidence.changed_files == ("new-file.txt",)
    assert evidence.base_commit
    assert evidence.revision
    assert not (repo / "new-file.txt").exists()
    assert effective.exists()
    discard_worktree(worktree, str(repo))
    assert not effective.exists()


def test_unchanged_worktree_is_removed_during_finalization(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))
    worktree, effective_path = create_worktree(
        str(repo), "general", "clean-child", isolation=AgentIsolation.WORKTREE,
    )

    evidence = finalize_worktree(worktree, str(repo))

    assert evidence.change is WorktreeChange.NONE
    assert not Path(effective_path).exists()


def test_failed_clean_worktree_removal_is_preserved_as_unknown(tmp_path, monkeypatch):
    import agent.v2.worktree_service as service

    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))
    worktree, effective_path = create_worktree(
        str(repo), "general", "cleanup-failed", isolation=AgentIsolation.WORKTREE,
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(service, "discard_worktree", lambda *args, **kwargs: None)
        evidence = service.finalize_worktree(worktree, str(repo))

    assert evidence.change is WorktreeChange.UNKNOWN
    assert "could not be removed" in evidence.error
    assert Path(effective_path).exists()
    discard_worktree(worktree, str(repo))


def test_head_change_is_preserved_even_without_file_diff(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))
    worktree, effective_path = create_worktree(
        str(repo), "general", "empty-commit", isolation=AgentIsolation.WORKTREE,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "child checkpoint"],
        cwd=effective_path, capture_output=True, check=True,
    )

    evidence = finalize_worktree(worktree, str(repo))

    assert evidence.change is WorktreeChange.COMMITTED
    assert evidence.changed_files == ()
    assert Path(effective_path).exists()
    discard_worktree(worktree, str(repo))


def test_worktree_evidence_uses_immutable_creation_commit(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))
    worktree, effective_path = create_worktree(
        str(repo), "general", "fixed-base", isolation=AgentIsolation.WORKTREE,
    )
    original_base = worktree.base_commit
    (repo / "parent-only.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "parent moved"],
        cwd=repo, capture_output=True, check=True,
    )
    (Path(effective_path) / "child-only.txt").write_text("child\n", encoding="utf-8")

    evidence = inspect_worktree(worktree)

    assert evidence.base_commit == original_base
    assert evidence.changed_files == ("child-only.txt",)
    discard_worktree(worktree, str(repo))


def test_apply_refuses_revision_that_changed_after_review(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))
    worktree, effective_path = create_worktree(
        str(repo), "general", "stale-apply", isolation=AgentIsolation.WORKTREE,
    )
    child_file = Path(effective_path) / "child.txt"
    child_file.write_text("reviewed\n", encoding="utf-8")
    reviewed = inspect_worktree(worktree)
    child_file.write_text("changed later\n", encoding="utf-8")

    result = apply_worktree(
        worktree, str(repo), expected_revision=reviewed.revision,
    )

    assert result.status is WorktreeOperationStatus.STALE
    assert not (repo / "child.txt").exists()
    assert Path(effective_path).exists()
    discard_worktree(worktree, str(repo))


def test_apply_refuses_dirty_parent_worktree(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))
    worktree, effective_path = create_worktree(
        str(repo), "general", "dirty-parent", isolation=AgentIsolation.WORKTREE,
    )
    (Path(effective_path) / "child.txt").write_text("child\n", encoding="utf-8")
    reviewed = inspect_worktree(worktree)
    parent_file = repo / "parent-untracked.txt"
    parent_file.write_text("parent\n", encoding="utf-8")

    result = apply_worktree(
        worktree, str(repo), expected_revision=reviewed.revision,
    )

    assert result.status is WorktreeOperationStatus.PARENT_DIRTY
    assert not (repo / "child.txt").exists()
    parent_file.unlink()
    discard_worktree(worktree, str(repo))


def test_apply_reports_conflict_and_restores_clean_parent(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))
    worktree, effective_path = create_worktree(
        str(repo), "general", "conflict", isolation=AgentIsolation.WORKTREE,
    )
    (Path(effective_path) / "tracked.txt").write_text("child\n", encoding="utf-8")
    reviewed = inspect_worktree(worktree)
    (repo / "tracked.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "parent edit"],
        cwd=repo, capture_output=True, check=True,
    )

    result = apply_worktree(
        worktree, str(repo), expected_revision=reviewed.revision,
    )

    assert result.status is WorktreeOperationStatus.CONFLICT
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "parent\n"
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert status.stdout == ""
    assert Path(effective_path).exists()
    discard_worktree(worktree, str(repo))


def test_discard_requires_exact_reviewed_revision(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))
    worktree, effective_path = create_worktree(
        str(repo), "general", "reviewed-discard", isolation=AgentIsolation.WORKTREE,
    )
    child_file = Path(effective_path) / "child.txt"
    child_file.write_text("first\n", encoding="utf-8")
    first = inspect_worktree(worktree)
    child_file.write_text("second\n", encoding="utf-8")

    stale = discard_reviewed_worktree(
        worktree, str(repo), expected_revision=first.revision,
    )
    current = inspect_worktree(worktree)
    discarded = discard_reviewed_worktree(
        worktree, str(repo), expected_revision=current.revision,
    )

    assert stale.status is WorktreeOperationStatus.STALE
    assert discarded.status is WorktreeOperationStatus.DISCARDED
    assert not Path(effective_path).exists()


def test_declared_worktree_isolation_fails_closed(tmp_path, monkeypatch):
    project = tmp_path / "not-a-git-repo"
    project.mkdir()
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))

    with pytest.raises(WorktreeIsolationError):
        create_worktree(
            str(project), "general", "child2",
            isolation=AgentIsolation.WORKTREE,
        )


def test_worktree_manager_refuses_discard_outside_managed_root(tmp_path, monkeypatch):
    from runtime.snapshot import Worktree, WorktreeError, WorktreeManager

    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))
    manager = WorktreeManager(str(repo))
    forged = Worktree(
        name="forged",
        path=str(repo),
        branch="multi-agent/forged",
        base_branch="main",
        base_commit="deadbeef",
    )

    with pytest.raises(WorktreeError, match="outside managed root"):
        manager.discard(forged)
