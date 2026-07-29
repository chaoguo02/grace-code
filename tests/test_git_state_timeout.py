from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from agent.core import _capture_git_state, _dirty_git_files


def test_dirty_git_scan_is_bounded_and_prunes_runtime_trees(tmp_path):
    completed = SimpleNamespace(
        returncode=0,
        stdout=" M tracked.py\0?? new.py\0",
        stderr="",
    )
    repo = SimpleNamespace(working_tree_dir=str(tmp_path))

    with patch("agent.core.subprocess.run", return_value=completed) as run:
        assert _dirty_git_files(repo) == {"tracked.py", "new.py"}

    command = run.call_args.args[0]
    assert run.call_args.kwargs["timeout"] == 10
    assert ":(exclude).scratch/**" in command
    assert ":(exclude)node_modules/**" in command


def test_git_status_timeout_degrades_instead_of_blocking_run():
    fake_repo = SimpleNamespace(
        working_tree_dir=".",
        head=SimpleNamespace(commit=SimpleNamespace(hexsha="abc")),
    )
    timeout = subprocess.TimeoutExpired(["git", "status"], timeout=10)

    with (
        patch("git.Repo", return_value=fake_repo),
        patch("agent.core.subprocess.run", side_effect=timeout),
    ):
        state = _capture_git_state(".")

    assert state.is_git_repo is False
    assert "timed out" in state._last_git_error
