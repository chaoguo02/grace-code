"""Fail-closed Git worktree isolation for child agents."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from agent.session.models import WorkspaceMode, WorktreeChange, WorktreeEvidence

logger = logging.getLogger(__name__)


class WorktreeIsolationError(RuntimeError):
    """Raised when declared worktree isolation cannot be provisioned."""


class WorktreeOperationStatus(str, Enum):
    """Typed outcome for an explicit parent worktree operation."""

    APPLIED = "applied"
    DISCARDED = "discarded"
    NO_CHANGES = "no_changes"
    RETAINED = "retained"
    STALE = "stale"
    PARENT_DIRTY = "parent_dirty"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class WorktreeOperationResult:
    status: WorktreeOperationStatus
    evidence: WorktreeEvidence
    error: str = ""

    @property
    def is_success(self) -> bool:
        return self.status in {
            WorktreeOperationStatus.APPLIED,
            WorktreeOperationStatus.DISCARDED,
            WorktreeOperationStatus.NO_CHANGES,
            WorktreeOperationStatus.RETAINED,
        }


def _get_runtime(repo_path: str) -> Any:
    from core.process import LocalRuntime
    return LocalRuntime(workspace_root=repo_path)


def _worktree_root(repo_path: str) -> str:
    from core.state_paths import ProjectStatePaths
    return str(ProjectStatePaths.for_project(repo_path).worktrees)


def create_worktree(
    repo_path: str,
    definition_name: str,
    agent_id: str,
    *,
    isolation: WorkspaceMode = WorkspaceMode.CURRENT,
    runtime: Any | None = None,
    governor: Any | None = None,
    root_session_id: str = "",
    session_id: str = "",
    cancel_token: Any | None = None,
) -> tuple[Any | None, str]:
    """Provision declared isolation and return its effective project root.

    Phase 3: checks worktree quota and disk space before creation.
    """
    if isolation is not WorkspaceMode.WORKTREE:
        return None, repo_path
    worktree_lease = None
    if governor is not None:
        from core.resource_governor import (
            AdmissionOutcome,
            ResourceKind,
            ResourceRequest,
        )
        result = governor.admit_wait(ResourceRequest(
            request_id=f"worktree-{agent_id}",
            root_session_id=root_session_id or session_id or agent_id,
            session_id=session_id or agent_id,
            resources={ResourceKind.WORKTREE_SLOT: 1},
            timeout_s=float(getattr(
                getattr(governor._config, "queue", None),
                "timeout_seconds",
                120.0,
            )),
            cancel_token=cancel_token,
        ))
        if result.outcome is not AdmissionOutcome.GRANTED:
            raise WorktreeIsolationError(
                f"Worktree capacity unavailable: {result.outcome.value}"
                + (f" — {result.reason}" if result.reason else "")
            )
        worktree_lease = result.lease
    disk_limit_mb = int(getattr(
        getattr(getattr(governor, "_config", None), "worktree", None),
        "disk_limit_mb",
        0,
    ))
    _check_disk_space(repo_path, minimum_free_mb=disk_limit_mb)
    try:
        from agent.session.worktree_manager import WorktreeManager
        manager = WorktreeManager(
            repo_path,
            runtime=runtime or _get_runtime(repo_path),
            worktree_root=_worktree_root(repo_path),
        )
        worktree = manager.create(f"agent-{definition_name}-{agent_id}")
        worktree.resource_lease = worktree_lease
        logger.info(
            "Worktree created for '%s': %s (branch: %s)",
            definition_name, worktree.path, worktree.branch,
        )
        return worktree, worktree.path
    except Exception as exc:
        if worktree_lease is not None:
            worktree_lease.release()
        raise WorktreeIsolationError(
            f"Worktree isolation failed for {definition_name!r}: {exc}"
        ) from exc


def _check_worktree_quota(repo_path: str, governor: Any | None) -> None:
    """Phase 3: check worktree quota before creation."""
    if governor is None:
        return
    from core.resource_governor import ResourceKind
    # Check global worktree limit
    snap = governor.snapshot()
    ws = snap.snapshots.get(ResourceKind.WORKTREE_SLOT)
    if ws is not None and ws.limit > 0 and ws.reserved >= ws.limit:
        raise WorktreeIsolationError(
            f"Worktree quota exhausted: {ws.reserved}/{ws.limit} active"
        )


def _estimate_checkout_bytes(repo_path: str) -> int:
    """Estimate bytes needed for a checkout from Git's tracked file set."""
    import subprocess

    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo_path,
            capture_output=True,
            check=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    total = 0
    root = Path(repo_path)
    for raw_path in completed.stdout.split(b"\0"):
        if not raw_path:
            continue
        try:
            total += (root / raw_path.decode("utf-8", "surrogateescape")).stat().st_size
        except (OSError, UnicodeError):
            continue
    return total


def _check_disk_space(repo_path: str, *, minimum_free_mb: int = 0) -> None:
    """Phase 3: check available disk space before creating a worktree.

    Requires at least 100 MB free on the filesystem containing the worktree
    root. On Windows this checks the drive containing *repo_path*.
    """
    import shutil
    try:
        usage = shutil.disk_usage(_worktree_root(repo_path))
        configured_floor = max(100, minimum_free_mb) * 1024 * 1024
        # A worktree shares Git objects but materializes tracked files. Keep
        # 20% headroom for filesystem allocation and checkout metadata.
        checkout_bytes = _estimate_checkout_bytes(repo_path)
        required_bytes = max(
            configured_floor,
            int(checkout_bytes * 1.20),
        )
        if usage.free < required_bytes:
            free_mb = usage.free / (1024 * 1024)
            required_mb = required_bytes / (1024 * 1024)
            raise WorktreeIsolationError(
                f"Insufficient disk space: {free_mb:.0f} MB free "
                f"(need at least {required_mb:.0f} MB; "
                f"estimated checkout {checkout_bytes / (1024 * 1024):.0f} MB)"
            )
    except OSError:
        # disk_usage can fail on some filesystems — allow through
        pass


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
        from context.workspace_facts import capture_workspace_snapshot
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
        if Path(worktree.path).exists():
            return WorktreeEvidence(
                change=WorktreeChange.UNKNOWN,
                path=evidence.path,
                branch=evidence.branch,
                base_branch=evidence.base_branch,
                base_commit=evidence.base_commit,
                changed_files=evidence.changed_files,
                revision=evidence.revision,
                error="Clean child worktree could not be removed",
            )
    return evidence


def apply_worktree(
    worktree: Any,
    repo_path: str,
    *,
    expected_revision: str,
    runtime: Any | None = None,
) -> WorktreeOperationResult:
    """Explicitly merge a reviewed child into the current parent branch."""
    evidence = inspect_worktree(worktree)
    if evidence.change is WorktreeChange.UNKNOWN:
        return WorktreeOperationResult(
            WorktreeOperationStatus.FAILED, evidence,
            evidence.error or "Unable to inspect child worktree",
        )
    if evidence.revision != expected_revision:
        return WorktreeOperationResult(
            WorktreeOperationStatus.STALE, evidence,
            "Child worktree changed after the reviewed revision",
        )
    if evidence.change is WorktreeChange.NONE:
        discard_worktree(worktree, repo_path, runtime)
        if Path(worktree.path).exists():
            return WorktreeOperationResult(
                WorktreeOperationStatus.FAILED, evidence,
                "Clean child worktree could not be removed",
            )
        return WorktreeOperationResult(WorktreeOperationStatus.NO_CHANGES, evidence)

    from context.workspace_facts import capture_workspace_snapshot
    parent_before = capture_workspace_snapshot(repo_path)
    if not parent_before.is_git_repo:
        return WorktreeOperationResult(
            WorktreeOperationStatus.FAILED, evidence,
            parent_before.error or "Parent project is not a Git worktree",
        )
    if parent_before.files or parent_before.current_patch:
        return WorktreeOperationResult(
            WorktreeOperationStatus.PARENT_DIRTY, evidence,
            "Parent worktree has tracked or untracked changes",
        )

    child_runtime = _get_runtime(str(worktree.path))
    if evidence.change in {WorktreeChange.UNCOMMITTED, WorktreeChange.BOTH}:
        staged = child_runtime.execute(
            "git", args=["add", "-A"], cwd=worktree.path, timeout=30,
        )
        if not staged.success:
            return WorktreeOperationResult(
                WorktreeOperationStatus.FAILED, evidence,
                staged.stderr or "Unable to stage child changes",
            )
        committed = child_runtime.execute(
            "git",
            args=["commit", "-m", f"Apply isolated subagent {worktree.name}"],
            cwd=worktree.path,
            timeout=30,
        )
        if not committed.success:
            return WorktreeOperationResult(
                WorktreeOperationStatus.FAILED, inspect_worktree(worktree),
                committed.stderr or "Unable to commit child changes",
            )
        evidence = inspect_worktree(worktree)

    # Refuse a TOCTOU-visible parent change between validation and merge.
    parent_now = capture_workspace_snapshot(repo_path)
    if parent_now.revision != parent_before.revision:
        return WorktreeOperationResult(
            WorktreeOperationStatus.PARENT_DIRTY, evidence,
            "Parent worktree changed while preparing the child result",
        )

    parent_runtime = runtime or _get_runtime(repo_path)
    merged = parent_runtime.execute(
        "git",
        args=[
            "merge", "--no-ff", worktree.branch,
            "-m", f"Merge isolated subagent {worktree.name}",
        ],
        cwd=repo_path,
        timeout=60,
    )
    if not merged.success:
        conflicts = parent_runtime.execute(
            "git", args=["diff", "--name-only", "--diff-filter=U", "-z"],
            cwd=repo_path, timeout=30,
        )
        aborted = parent_runtime.execute(
            "git", args=["merge", "--abort"], cwd=repo_path, timeout=30,
        )
        conflict_paths = _nul_paths(conflicts.stdout) if conflicts.success else set()
        status = (
            WorktreeOperationStatus.CONFLICT
            if conflict_paths
            else WorktreeOperationStatus.FAILED
        )
        error = merged.stderr or "Git merge failed"
        if not aborted.success:
            error = f"{error}; merge abort failed: {aborted.stderr}"
        return WorktreeOperationResult(status, evidence, error)

    discard_worktree(worktree, repo_path, parent_runtime)
    if Path(worktree.path).exists():
        return WorktreeOperationResult(
            WorktreeOperationStatus.FAILED, evidence,
            "Changes were merged but the child worktree could not be removed",
        )
    return WorktreeOperationResult(WorktreeOperationStatus.APPLIED, evidence)


def discard_reviewed_worktree(
    worktree: Any,
    repo_path: str,
    *,
    expected_revision: str,
    runtime: Any | None = None,
) -> WorktreeOperationResult:
    """Discard exactly the child revision the parent reviewed."""
    evidence = inspect_worktree(worktree)
    if evidence.change is WorktreeChange.UNKNOWN:
        return WorktreeOperationResult(
            WorktreeOperationStatus.FAILED, evidence,
            evidence.error or "Unable to inspect child worktree",
        )
    if evidence.revision != expected_revision:
        return WorktreeOperationResult(
            WorktreeOperationStatus.STALE, evidence,
            "Child worktree changed after the reviewed revision",
        )
    discard_worktree(worktree, repo_path, runtime)
    if Path(worktree.path).exists():
        return WorktreeOperationResult(
            WorktreeOperationStatus.FAILED, evidence,
            "Child worktree could not be removed",
        )
    return WorktreeOperationResult(WorktreeOperationStatus.DISCARDED, evidence)


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
        from agent.session.worktree_manager import WorktreeManager
        manager = WorktreeManager(
            repo_path,
            runtime=runtime or _get_runtime(repo_path),
            worktree_root=_worktree_root(repo_path),
        )
        manager.discard(worktree)
        if not Path(worktree.path).exists():
            lease = getattr(worktree, "resource_lease", None)
            if lease is not None:
                lease.release()
                worktree.resource_lease = None
    except Exception as exc:
        logger.debug("Worktree discard failed (non-critical): %s", exc)
