"""Fail-closed Git worktree isolation for forked agents."""

from __future__ import annotations

import logging
from typing import Any

from agent.v2.models import AgentIsolation, WorktreeChange, WorktreeEvidence

logger = logging.getLogger(__name__)


class WorktreeIsolationError(RuntimeError):
    """Raised when declared worktree isolation cannot be provisioned."""


def _get_runtime(repo_path: str) -> Any:
    from tools.runtime import LocalRuntime
    return LocalRuntime(workspace_root=repo_path)


def _worktree_root(repo_path: str) -> str:
    from runtime.state_paths import ProjectStatePaths
    return str(ProjectStatePaths.for_project(repo_path).worktrees)


def create_worktree(
    repo_path: str,
    definition_name: str,
    agent_id: str,
    *,
    isolation: AgentIsolation = AgentIsolation.FORK,
    runtime: Any | None = None,
) -> tuple[Any | None, str]:
    """Provision declared isolation and return its effective project root."""
    if isolation is not AgentIsolation.WORKTREE:
        return None, repo_path
    try:
        from tools.snapshot import WorktreeManager
        manager = WorktreeManager(
            repo_path,
            runtime=runtime or _get_runtime(repo_path),
            worktree_root=_worktree_root(repo_path),
        )
        worktree = manager.create(f"agent-{definition_name}-{agent_id}")
        logger.info(
            "Worktree created for '%s': %s (branch: %s)",
            definition_name, worktree.path, worktree.branch,
        )
        return worktree, worktree.path
    except Exception as exc:
        raise WorktreeIsolationError(
            f"Worktree isolation failed for {definition_name!r}: {exc}"
        ) from exc


def inspect_worktree(worktree: Any, runtime: Any | None = None) -> WorktreeEvidence:
    """Capture immutable Git facts without mutating either checkout."""
    if worktree is None:
        return WorktreeEvidence(
            change=WorktreeChange.NONE,
            path="",
            branch="",
            base_branch="",
            base_commit="",
        )
    try:
        child_runtime = runtime or _get_runtime(str(worktree.path))
        status = child_runtime.execute(
            "git", args=["status", "--porcelain", "--untracked-files=all"],
            cwd=worktree.path, timeout=30,
        )
        head = child_runtime.execute(
            "git", args=["rev-parse", "HEAD"],
            cwd=worktree.path, timeout=30,
        )
        tracked = child_runtime.execute(
            "git", args=["diff", "--name-only", "-z", worktree.base_commit, "--"],
            cwd=worktree.path, timeout=30,
        )
        untracked = child_runtime.execute(
            "git", args=["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=worktree.path, timeout=30,
        )
        results = (status, head, tracked, untracked)
        failed = [result for result in results if not result.success]
        if failed:
            return WorktreeEvidence(
                change=WorktreeChange.UNKNOWN,
                path=str(worktree.path),
                branch=str(worktree.branch),
                base_branch=str(worktree.base_branch),
                base_commit=str(worktree.base_commit),
                error="; ".join(
                    result.stderr.strip() or "git inspection failed"
                    for result in failed
                ),
            )
        has_uncommitted = bool(status.stdout.strip())
        has_committed = head.stdout.strip() != worktree.base_commit
        if has_uncommitted and has_committed:
            change = WorktreeChange.BOTH
        elif has_uncommitted:
            change = WorktreeChange.UNCOMMITTED
        elif has_committed:
            change = WorktreeChange.COMMITTED
        else:
            change = WorktreeChange.NONE
        from runtime.workspace_facts import capture_workspace_snapshot
        snapshot = capture_workspace_snapshot(worktree.path)
        changed_files = tuple(sorted(set(
            _nul_paths(tracked.stdout) | _nul_paths(untracked.stdout)
        )))
        return WorktreeEvidence(
            change=change,
            path=str(worktree.path),
            branch=str(worktree.branch),
            base_branch=str(worktree.base_branch),
            base_commit=str(worktree.base_commit),
            changed_files=changed_files,
            revision=snapshot.revision,
            error=snapshot.error,
        )
    except (OSError, TypeError, ValueError) as exc:
        return WorktreeEvidence(
            change=WorktreeChange.UNKNOWN,
            path=str(worktree.path),
            branch=str(worktree.branch),
            base_branch=str(worktree.base_branch),
            base_commit=str(worktree.base_commit),
            error=str(exc),
        )


def _nul_paths(raw: str) -> set[str]:
    return {item for item in raw.split("\0") if item}


def inspect_changes(worktree: Any, runtime: Any | None = None) -> WorktreeChange:
    """Compatibility view over the typed worktree evidence."""
    return inspect_worktree(worktree, runtime).change


def finalize_worktree(
    worktree: Any, repo_path: str, runtime: Any | None = None,
) -> WorktreeEvidence:
    """Clean an unchanged child or preserve its changes for explicit review."""
    evidence = inspect_worktree(worktree, runtime)
    if evidence.change is WorktreeChange.NONE:
        discard_worktree(worktree, repo_path)
    return evidence


def has_changes(worktree: Any, runtime: Any | None = None) -> bool:
    """Compatibility predicate backed by the typed Git fact state."""
    return inspect_changes(worktree, runtime) in {
        WorktreeChange.UNCOMMITTED,
        WorktreeChange.COMMITTED,
        WorktreeChange.BOTH,
    }


def discard_worktree(
    worktree: Any, repo_path: str, runtime: Any | None = None,
) -> None:
    if worktree is None:
        return
    try:
        from tools.snapshot import WorktreeManager
        manager = WorktreeManager(
            repo_path,
            runtime=runtime or _get_runtime(repo_path),
            worktree_root=_worktree_root(repo_path),
        )
        manager.discard(worktree)
    except Exception as exc:
        logger.debug("Worktree discard failed (non-critical): %s", exc)
