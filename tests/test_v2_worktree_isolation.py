from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent.v2.worktree_service import (
    WorktreeChange,
    WorktreeIsolationError,
    create_worktree,
    discard_worktree,
    finalize_worktree,
    inspect_changes,
    inspect_worktree,
)
from agent.v2.models import AgentIsolation
from runtime.state_paths import STATE_HOME_ENV, ProjectStatePaths
from tools.base import ExecutionContext, ToolRegistry
from tools.file_tool import FileReadTool
from tools.runtime import LocalRuntime, ProcessTermination
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


def test_declared_worktree_isolation_fails_closed(tmp_path, monkeypatch):
    project = tmp_path / "not-a-git-repo"
    project.mkdir()
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))

    with pytest.raises(WorktreeIsolationError):
        create_worktree(
            str(project), "general", "child2",
            isolation=AgentIsolation.WORKTREE,
        )
