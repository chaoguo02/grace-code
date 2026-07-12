"""Worktree service — manages Git worktree lifecycle for subagent isolation.

Extracted from fork_subagent().
Constitution: subagent.py should "run subagents", not "manage Git isolation".
Worktree create/merge/discard belongs here.

All git operations go through Runtime.execute() — NEVER raw subprocess.
This ensures Docker sandbox compatibility.
"""

from __future__ import annotations

import logging
import tempfile as _tf
from typing import Any

logger = logging.getLogger(__name__)


def _get_runtime(repo_path: str) -> "Any":
    """Get a LocalRuntime for worktree git operations."""
    from tools.runtime import LocalRuntime
    return LocalRuntime()


def create_worktree(
    repo_path: str,
    definition_name: str,
    agent_id: str,
    *,
    isolation: str = "fork",
    runtime: "Any | None" = None,
) -> tuple[Any | None, str]:
    """Create a Git worktree for subagent isolation.

    Returns (worktree, effective_repo_path). If worktree creation fails or
    isolation is not "worktree", returns (None, repo_path).

    Never raises — failures are logged and fall back to fork mode.
    """
    if isolation != "worktree":
        return None, repo_path

    try:
        rt = runtime or _get_runtime(repo_path)
        from tools.snapshot import WorktreeManager
        wt_manager = WorktreeManager(repo_path, runtime=rt)
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
    runtime: "Any | None" = None,
) -> tuple[bool, str]:
    """Merge worktree changes back to the main repo.

    Auto-commits all changes in the worktree before merging (subagents
    don't commit on their own).

    Returns (merged: bool, error: str). Never raises.
    """
    if worktree is None:
        return False, ""
    try:
        rt = runtime or _get_runtime(repo_path)
        # Stage all changes via Runtime
        rt.execute("git", args=["add", "-A"], cwd=worktree.path, timeout=30)
        # Use -F (file) to avoid shell special-character injection in -m
        _msg = f"Subagent {definition_name}: {prompt[:200]}"
        with _tf.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as _f:
            _f.write(_msg)
            _msg_path = _f.name
        try:
            rt.execute("git", args=["commit", "-F", _msg_path], cwd=worktree.path, timeout=30)
        finally:
            import os as _os
            try:
                _os.unlink(_msg_path)
            except OSError:
                pass
        from tools.snapshot import WorktreeManager
        wt_manager = WorktreeManager(repo_path, runtime=rt)
        wt_manager.merge(worktree)
        logger.info("Worktree merged: %s → %s", worktree.branch, repo_path)
        return True, ""
    except Exception as exc:
        logger.warning("Worktree merge failed: %s", exc)
        return False, str(exc)


def has_changes(worktree: Any, runtime: "Any | None" = None) -> bool:
    """Check if the worktree has real uncommitted file changes (git diff).

    Physical diff is the ground truth — not logical status. A subagent
    that hit MAX_STEPS may have already written valuable code.
    """
    if worktree is None:
        return False
    try:
        rt = runtime or _get_runtime(str(worktree.path))
        result = rt.execute("git", args=["diff", "--stat"], cwd=worktree.path, timeout=30)
        return bool(result.stdout.strip())
    except Exception:
        return False


def discard_worktree(worktree: Any, repo_path: str, runtime: "Any | None" = None) -> None:
    """Clean up a worktree. Never raises."""
    if worktree is None:
        return
    try:
        rt = runtime or _get_runtime(repo_path)
        from tools.snapshot import WorktreeManager
        wt_manager = WorktreeManager(repo_path, runtime=rt)
        wt_manager.discard(worktree)
    except Exception as exc:
        logger.debug("Worktree discard failed (non-critical): %s", exc)
