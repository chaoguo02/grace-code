from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from agent.v2.models import (
    ForkResult,
    ForkStatus,
    SessionMode,
    WorktreeDisposition,
)
from agent.v2.session_store import SessionStore
from agent.v2.worktree_service import inspect_worktree
from entry.cli import cli
from runtime.state_paths import ProjectStatePaths, STATE_HOME_ENV
from tools.snapshot import WorktreeManager


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True, text=True,
    )


def _retained_worktree(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Forge Tests")
    (repo / "tracked.txt").write_text("parent\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")

    state_home = tmp_path / "state"
    monkeypatch.setenv(STATE_HOME_ENV, str(state_home))
    paths = ProjectStatePaths.for_project(repo)
    worktree = WorktreeManager(repo).create("retained-child")
    (Path(worktree.path) / "child.txt").write_text("child\n", encoding="utf-8")
    evidence = inspect_worktree(worktree)

    store = SessionStore(str(paths.sessions_db))
    parent = store.create_session(
        agent_name="build", mode=SessionMode.PRIMARY,
        repo_path=str(repo), title="parent",
    )
    child = store.create_session(
        agent_name="general", mode=SessionMode.SUBAGENT,
        repo_path=str(repo), title="child",
        parent_id=parent.id, root_id=parent.root_id,
    )
    store.set_fork_result(child.id, ForkResult(
        agent_name="general",
        session_id=child.id,
        status=ForkStatus.COMPLETED,
        summary="retained child changes",
        worktree=evidence,
        worktree_disposition=WorktreeDisposition.RETAINED,
    ))
    return repo, store, child, evidence


def test_worktree_cli_lists_inspects_and_discards_exact_revision(
    tmp_path, monkeypatch,
):
    repo, store, child, evidence = _retained_worktree(tmp_path, monkeypatch)
    runner = CliRunner()

    listed = runner.invoke(cli, [
        "worktree", "list", "--repo", str(repo), "--json",
    ])
    assert listed.exit_code == 0, listed.output
    records = json.loads(listed.output)
    assert len(records) == 1
    assert records[0]["child_session_id"] == child.id
    assert records[0]["disposition"] == "retained"
    assert records[0]["availability"] == "available"
    assert records[0]["evidence"]["revision"] == evidence.revision

    inspected = runner.invoke(cli, [
        "worktree", "inspect", child.id,
        "--repo", str(repo), "--json",
    ])
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.output)["evidence"]["changed_files"] == ["child.txt"]

    stale = runner.invoke(cli, [
        "worktree", "discard", child.id,
        "--repo", str(repo), "--revision", "stale", "--yes", "--json",
    ])
    assert stale.exit_code != 0
    assert json.loads(stale.output)["status"] == "stale"
    assert Path(evidence.path).is_dir()

    discarded = runner.invoke(cli, [
        "worktree", "discard", child.id,
        "--repo", str(repo), "--revision", evidence.revision, "--yes", "--json",
    ])
    assert discarded.exit_code == 0, discarded.output
    assert json.loads(discarded.output)["status"] == "discarded"
    assert not Path(evidence.path).exists()
    resolved = store.get_session(child.id).fork_result
    assert resolved.worktree_disposition is WorktreeDisposition.DISCARDED
    assert resolved.worktree is None


def test_worktree_cli_applies_retained_result(tmp_path, monkeypatch):
    repo, store, child, evidence = _retained_worktree(tmp_path, monkeypatch)

    applied = CliRunner().invoke(cli, [
        "worktree", "apply", child.id,
        "--repo", str(repo), "--revision", evidence.revision, "--yes", "--json",
    ])

    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["status"] == "applied"
    assert (repo / "child.txt").read_text(encoding="utf-8") == "child\n"
    assert not Path(evidence.path).exists()
    resolved = store.get_session(child.id).fork_result
    assert resolved.worktree_disposition is WorktreeDisposition.APPLIED
    assert resolved.worktree is None


def test_worktree_cli_list_does_not_create_empty_state(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))
    sessions_db = ProjectStatePaths.for_project(repo).sessions_db

    result = CliRunner().invoke(cli, [
        "worktree", "list", "--repo", str(repo), "--json",
    ])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
    assert not sessions_db.exists()
