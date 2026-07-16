from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.workspace_facts import capture_workspace_snapshot, compare_workspace_snapshots
from tools.runtime import LocalRuntime


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Forge Tests")
    tracked = repo / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "baseline")
    return repo


def test_unchanged_preexisting_dirty_tree_is_not_agent_progress(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tracked = repo / "tracked.txt"
    tracked.write_text("dirty before run\n", encoding="utf-8")

    before = capture_workspace_snapshot(repo)
    after = capture_workspace_snapshot(repo)

    delta = compare_workspace_snapshots(before, after)
    assert not delta.has_changes
    assert delta.changed_paths == ()


def test_change_to_already_dirty_file_is_detected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tracked = repo / "tracked.txt"
    tracked.write_text("dirty before run\n", encoding="utf-8")
    before = capture_workspace_snapshot(repo)

    tracked.write_text("changed by agent\n", encoding="utf-8")
    after = capture_workspace_snapshot(repo)

    delta = compare_workspace_snapshots(before, after)
    assert delta.has_changes
    assert delta.changed_paths == (str(tracked.resolve()),)


def test_untracked_file_is_part_of_workspace_revision(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = capture_workspace_snapshot(repo)

    created = repo / "created.txt"
    created.write_text("new\n", encoding="utf-8")
    after = capture_workspace_snapshot(repo)

    delta = compare_workspace_snapshots(before, after)
    assert delta.has_changes
    assert delta.changed_paths == (str(created.resolve()),)


def test_reverting_to_starting_state_has_no_net_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = capture_workspace_snapshot(repo)
    tracked = repo / "tracked.txt"
    tracked.write_text("temporary\n", encoding="utf-8")
    tracked.write_text("base\n", encoding="utf-8")
    after = capture_workspace_snapshot(repo)

    assert not compare_workspace_snapshots(before, after).has_changes


def test_setup_workspace_never_runs_git_commands(tmp_path: Path, monkeypatch) -> None:
    runtime = LocalRuntime()

    def unexpected_execute(*args, **kwargs):
        raise AssertionError("setup_workspace must not mutate Git state")

    monkeypatch.setattr(runtime, "execute", unexpected_execute)
    assert runtime.setup_workspace(str(tmp_path))
