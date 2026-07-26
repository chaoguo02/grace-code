from __future__ import annotations

import subprocess

from context.workspace_facts import capture_workspace_snapshot
from agent.session.runtime import SessionRuntime
from core.state_paths import ProjectStatePaths
from server.services.review_snapshot import ReviewSnapshotManager


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_materialized_review_snapshot_is_frozen_from_original_workspace(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "review@example.test")
    _git(repo, "config", "user.name", "Review Test")
    tracked = repo / "tracked.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "initial")

    tracked.write_text("value = 2\n", encoding="utf-8")
    untracked = repo / "new.py"
    untracked.write_text("new_value = 3\n", encoding="utf-8")
    captured = capture_workspace_snapshot(repo)
    manager = ReviewSnapshotManager(str(repo))

    materialized = manager.materialize("abcdef123456", captured)
    snapshot_path = __import__("pathlib").Path(materialized.path)
    try:
        assert (snapshot_path / "tracked.py").read_text(
            encoding="utf-8",
        ) == "value = 2\n"
        assert (snapshot_path / "new.py").read_text(
            encoding="utf-8",
        ) == "new_value = 3\n"

        tracked.write_text("value = 99\n", encoding="utf-8")
        untracked.write_text("new_value = 100\n", encoding="utf-8")

        assert (snapshot_path / "tracked.py").read_text(
            encoding="utf-8",
        ) == "value = 2\n"
        assert (snapshot_path / "new.py").read_text(
            encoding="utf-8",
        ) == "new_value = 3\n"
    finally:
        manager.discard(materialized.path)


def test_snapshot_manager_rejects_unmanaged_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = ReviewSnapshotManager(str(repo))

    try:
        manager.validate(str(tmp_path / "outside"))
    except ValueError as exc:
        assert "outside managed runtime state" in str(exc)
    else:
        raise AssertionError("unmanaged review paths must fail closed")


def test_runtime_scope_allows_only_direct_managed_review_snapshots(tmp_path):
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    managed = ProjectStatePaths.for_project(repo).review_snapshots / "abcdef"
    managed.mkdir(parents=True)
    runtime = object.__new__(SessionRuntime)
    runtime._agent_registry = type(
        "Registry",
        (),
        {"project_dir": str(repo)},
    )()

    assert runtime._require_review_snapshot_scope(
        str(repo),
        str(managed),
    ) == str(managed.resolve())

    try:
        runtime._require_review_snapshot_scope(
            str(repo),
            str(tmp_path / "outside"),
        )
    except ValueError as exc:
        assert "outside managed runtime state" in str(exc)
    else:
        raise AssertionError("runtime must reject unmanaged review roots")
