"""Worktree service — manages Git worktree lifecycle for subagent isolation.

Extracted from fork_subagent().
Constitution: subagent.py should "run subagents", not "manage Git isolation".
Worktree create/merge/discard belongs here.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def create_worktree(
    repo_path: str,
    definition_name: str,
    agent_id: str,
    *,
    isolation: str = "fork",
) -> tuple[Any | None, str]:
    """Create a Git worktree for subagent isolation.

    Returns (worktree, effective_repo_path). If worktree creation fails or
    isolation is not "worktree", returns (None, repo_path).

    Never raises — failures are logged and fall back to fork mode.
    """
    if isolation != "worktree":
        return None, repo_path

    try:
        from tools.snapshot import WorktreeManager
        wt_manager = WorktreeManager(repo_path)
        wt_name = f"agent-{definition_name}-{agent_id}"
        worktree = wt_manager.create(wt_name)
        logger.info(
            "Worktree created for '%s': %s (branch: %s)",
            definition_name, worktree.path, worktree.branch,
        )
        return worktree, worktree.path
    except Exception as exc:
        logger.warning(
            "Worktree creation failed for '%s': %s — falling back to fork mode",
            definition_name, exc,
        )
        return None, repo_path


def merge_worktree(
    worktree: Any,
    repo_path: str,
    definition_name: str,
    prompt: str = "",
) -> tuple[bool, str]:
    """Merge worktree changes back to the main repo.

    Auto-commits all changes in the worktree before merging (subagents
    don't commit on their own).

    Returns (merged: bool, error: str). Never raises.
    """
    if worktree is None:
        return False, ""
    try:
        import subprocess as _sp
        import tempfile as _tf
        # Commit all changes in the worktree
        _sp.run(
            ["git", "add", "-A"],
            cwd=worktree.path, capture_output=True, check=True,
        )
        # Use -F (file) to avoid shell special-character injection in -m
        _msg = f"Subagent {definition_name}: {prompt[:200]}"
        with _tf.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as _f:
            _f.write(_msg)
            _msg_path = _f.name
        try:
            _sp.run(
                ["git", "commit", "-F", _msg_path],
                cwd=worktree.path, capture_output=True,
            )  # May fail (nothing to commit) — that's OK
        finally:
            import os as _os
            try:
                _os.unlink(_msg_path)
            except OSError:
                pass
        from tools.snapshot import WorktreeManager
        wt_manager = WorktreeManager(repo_path)
        wt_manager.merge(worktree)
        logger.info("Worktree merged: %s → %s", worktree.branch, repo_path)
        return True, ""
    except Exception as exc:
        logger.warning("Worktree merge failed: %s", exc)
        return False, str(exc)


def has_changes(worktree: Any) -> bool:
    """Check if the worktree has real uncommitted file changes (git diff).

    Physical diff is the ground truth — not logical status. A subagent
    that hit MAX_STEPS may have already written valuable code.
    """
    if worktree is None:
        return False
    try:
        import subprocess as _sp
        result = _sp.run(
            ["git", "diff", "--stat"],
            cwd=worktree.path, capture_output=True, text=True,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def discard_worktree(worktree: Any, repo_path: str) -> None:
    """Clean up a worktree. Never raises."""
    if worktree is None:
        return
    try:
        from tools.snapshot import WorktreeManager
        wt_manager = WorktreeManager(repo_path)
        wt_manager.discard(worktree)
    except Exception as exc:
        logger.debug("Worktree discard failed (non-critical): %s", exc)
