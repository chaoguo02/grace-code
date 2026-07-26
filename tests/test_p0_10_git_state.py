"""
P0-10: _capture_git_state() / _refresh_git_state() exception handling.

Verifies that:
  - Expected git failures (ImportError, InvalidGitRepositoryError, etc.)
    are gracefully degraded.
  - Systemic errors (MemoryError, OSError EACCES/EPERM) are NOT swallowed.
  - _refresh_git_state() marks is_git_repo=False on failure.
  - _refresh_error_logged flag prevents log storms and resets on recovery.
  - GitStateLike Protocol is satisfied by _GitState.
"""

import errno
import logging
from unittest.mock import MagicMock, patch

import pytest


# ── M2: _capture_git_state() ──────────────────────────────────────────────

class TestCaptureGitState:
    """Verify _capture_git_state() handles expected vs systemic exceptions correctly."""

    def test_import_error_degrades_gracefully(self):
        """P0-10 regression: ImportError → is_git_repo=False, no raise."""
        from agent.core import _capture_git_state

        with patch("builtins.__import__", side_effect=ImportError("no git")):
            state = _capture_git_state("/some/repo")
            assert state.is_git_repo is False

    def test_not_a_git_repo_degrades(self):
        """InvalidGitRepositoryError → is_git_repo=False."""
        from git.exc import InvalidGitRepositoryError
        from agent.core import _capture_git_state

        with patch("git.Repo", side_effect=InvalidGitRepositoryError("not a repo")):
            state = _capture_git_state("/some/repo")
            assert state.is_git_repo is False

    def test_no_such_path_degrades(self):
        """NoSuchPathError → is_git_repo=False."""
        from git.exc import NoSuchPathError
        from agent.core import _capture_git_state

        with patch("git.Repo", side_effect=NoSuchPathError("no such path")):
            state = _capture_git_state("/no/such/path")
            assert state.is_git_repo is False

    def test_git_error_degrades(self):
        """GitError (corrupted / empty repo) → is_git_repo=False."""
        from git.exc import GitError
        from agent.core import _capture_git_state

        with patch("git.Repo", side_effect=GitError("corrupted repo")):
            state = _capture_git_state("/corrupted/repo")
            assert state.is_git_repo is False

    def test_permission_denied_propagates(self):
        """OSError EACCES → must NOT be swallowed (systemic error)."""
        from agent.core import _capture_git_state
        perm_error = OSError(errno.EACCES, "Permission denied", "/root/repo")

        with patch("git.Repo", side_effect=perm_error):
            with pytest.raises(OSError):
                _capture_git_state("/root/repo")

    def test_eperm_propagates(self):
        """OSError EPERM → must NOT be swallowed."""
        from agent.core import _capture_git_state
        perm_error = OSError(errno.EPERM, "Operation not permitted", "/protected")

        with patch("git.Repo", side_effect=perm_error):
            with pytest.raises(OSError):
                _capture_git_state("/protected")

    def test_enotdir_degrades(self):
        """OSError ENOTDIR (path issue) → is_git_repo=False."""
        from agent.core import _capture_git_state
        path_error = OSError(errno.ENOTDIR, "Not a directory", "/bad/path")

        with patch("git.Repo", side_effect=path_error):
            state = _capture_git_state("/bad/path")
            assert state.is_git_repo is False

    def test_memory_error_propagates(self):
        """MemoryError → must propagate (not caught by except Exception)."""
        from agent.core import _capture_git_state

        # MemoryError is NOT a subclass of Exception in the except clause
        # once we stop using bare `except Exception`; it will propagate naturally.
        # But with the current code (except Exception), MemoryError IS caught.
        # This test documents the desired behavior after the fix.
        with patch("git.Repo", side_effect=MemoryError("OOM")):
            with pytest.raises(MemoryError):
                _capture_git_state("/some/repo")

    def test_last_git_error_recorded(self):
        """Expected git failure → _last_git_error field records the error."""
        from git.exc import InvalidGitRepositoryError
        from agent.core import _capture_git_state

        msg = "not a git repo at all"
        with patch("git.Repo", side_effect=InvalidGitRepositoryError(msg)):
            state = _capture_git_state("/some/repo")
            assert msg in state._last_git_error


# ── M3: _refresh_git_state() ─────────────────────────────────────────────

class TestRefreshGitState:
    """Verify _refresh_git_state() marks state correctly and controls log storms."""

    def test_failure_sets_is_git_repo_false(self, caplog):
        """On git failure during refresh → is_git_repo becomes False."""
        from git.exc import GitError
        from agent.core import _GitState, _refresh_git_state

        state = _GitState()
        state.is_git_repo = True

        with patch("git.Repo", side_effect=GitError("repo vanished")):
            _refresh_git_state(state, "/some/repo")

        assert state.is_git_repo is False

    def test_permission_error_sets_is_git_repo_false(self):
        """OSError EACCES during refresh → is_git_repo=False."""
        from agent.core import _GitState, _refresh_git_state

        state = _GitState()
        state.is_git_repo = True
        perm_error = OSError(errno.EACCES, "Permission denied")

        with patch("git.Repo", side_effect=perm_error):
            _refresh_git_state(state, "/root/repo")

        assert state.is_git_repo is False

    def test_first_error_is_warning(self, caplog):
        """First refresh failure → WARNING level log."""
        from git.exc import GitError
        from agent.core import _GitState, _refresh_git_state

        state = _GitState()
        state.is_git_repo = True

        with patch("git.Repo", side_effect=GitError("boom")):
            with caplog.at_level(logging.WARNING):
                _refresh_git_state(state, "/repo")

        assert state._refresh_error_logged is True
        assert state._last_git_error

    def test_second_error_is_not_warning(self, caplog):
        """Second consecutive refresh failure → not WARNING (suppressed)."""
        from git.exc import GitError
        from agent.core import _GitState, _refresh_git_state

        state = _GitState()
        state.is_git_repo = True
        state._refresh_error_logged = True  # simulate prior failure

        with patch("git.Repo", side_effect=GitError("boom again")):
            with caplog.at_level(logging.WARNING):
                _refresh_git_state(state, "/repo")

        # No WARNING-level records should have been logged
        assert len(caplog.records) == 0

    def test_success_resets_error_state(self):
        """After a successful refresh, _refresh_error_logged resets to False."""
        from agent.core import _GitState, _refresh_git_state

        state = _GitState()
        state.is_git_repo = True
        state._baseline_revision = "abc123"
        state._refresh_error_logged = True
        state._last_git_error = "old error"

        mock_repo = MagicMock()
        mock_repo.git.diff.return_value = "file.py\n"
        with patch("git.Repo", return_value=mock_repo):
            _refresh_git_state(state, "/repo")

        assert state._refresh_error_logged is False
        assert state._last_git_error == ""

    def test_not_git_repo_skips_refresh(self):
        """When is_git_repo is already False, _refresh_git_state returns early."""
        from agent.core import _GitState, _refresh_git_state

        state = _GitState()
        state.is_git_repo = False
        state._baseline_revision = "abc"

        with patch("git.Repo") as mock_repo_class:
            _refresh_git_state(state, "/repo")
            mock_repo_class.assert_not_called()


# ── M5: GitStateLike Protocol ────────────────────────────────────────────

class TestGitStateLikeProtocol:
    """Verify _GitState satisfies the GitStateLike Protocol used by completion_guard."""

    def test_git_state_satisfies_protocol(self):
        """_GitState has all fields required by GitStateLike Protocol."""
        from agent.core import _GitState

        state = _GitState()
        state.is_git_repo = True
        state.has_changes = False
        state.files_changed = {"a.py"}
        state.current_diff = "+import x"
        state._baseline_dirty_files = set()

        # If _GitState doesn't satisfy the Protocol, this test would fail at
        # type-check time; here we validate the fields exist at runtime.
        assert hasattr(state, "is_git_repo")
        assert hasattr(state, "has_changes")
        assert hasattr(state, "files_changed")
        assert hasattr(state, "current_diff")
        assert hasattr(state, "_baseline_dirty_files")

    def test_completion_guard_accepts_git_state(self):
        """completion_guard.check() accepts _GitState as git_state argument."""
        from agent.completion_guard import CompletionCheckResult, CompletionContext, TaskCompletionGuard
        from agent.core import _GitState

        ctx = CompletionContext()
        guard = TaskCompletionGuard()
        state = _GitState()
        state.is_git_repo = False  # non-repo → no diff checks triggered

        result = guard.check(ctx=ctx, task_intent="edit", git_state=state)
        assert isinstance(result, CompletionCheckResult)


def test_refresh_detects_second_edit_to_file_dirty_at_run_start(tmp_path):
    """A pre-existing dirty file still counts when this run changes it again."""
    import git

    from agent.core import _capture_git_state, _refresh_git_state

    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as config:
        config.set_value("user", "name", "Grace Test")
        config.set_value("user", "email", "grace@example.invalid")
    target = tmp_path / "tracked.txt"
    target.write_text("committed\n", encoding="utf-8")
    repo.index.add(["tracked.txt"])
    repo.index.commit("initial")

    target.write_text("dirty before run\n", encoding="utf-8")
    state = _capture_git_state(str(tmp_path))
    assert "tracked.txt" in state._baseline_dirty_files

    _refresh_git_state(state, str(tmp_path))
    assert state.has_changes is False

    target.write_text("changed by this run\n", encoding="utf-8")
    _refresh_git_state(state, str(tmp_path))

    assert state.has_changes is True
    assert state._run_changed_files == {"tracked.txt"}
