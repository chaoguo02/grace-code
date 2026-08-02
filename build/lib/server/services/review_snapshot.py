"""Materialize and validate immutable workspaces for multi-agent review."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from context.workspace_facts import WorkspaceSnapshot, capture_workspace_snapshot
from core.process import LocalRuntime
from core.state_paths import ProjectStatePaths


@dataclass(frozen=True)
class MaterializedReviewSnapshot:
    path: str
    workspace_revision: str
    head_commit: str


class ReviewSnapshotManager:
    """Own detached Git worktrees containing one captured dirty workspace."""

    def __init__(self, repo_path: str) -> None:
        self._repo_path = Path(repo_path).resolve()
        self._root = ProjectStatePaths.for_project(
            self._repo_path,
        ).review_snapshots.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def materialize(
        self,
        snapshot_id: str,
        captured: WorkspaceSnapshot,
    ) -> MaterializedReviewSnapshot:
        if not captured.is_git_repo or not captured.head_commit:
            raise ValueError("Review snapshots require a committed Git HEAD")
        if not snapshot_id or any(ch not in "0123456789abcdef-" for ch in snapshot_id):
            raise ValueError("Invalid review snapshot identifier")

        target = (self._root / snapshot_id).resolve()
        self._assert_managed(target)
        if target.exists():
            raise ValueError(f"Review snapshot already exists: {snapshot_id}")
        self._root.mkdir(parents=True, exist_ok=True)

        runtime = LocalRuntime(workspace_root=self._repo_path)
        created = runtime.execute(
            "git",
            args=[
                "worktree",
                "add",
                "--detach",
                str(target),
                captured.head_commit,
            ],
            cwd=str(self._repo_path),
            timeout=60,
        )
        if not created.success:
            if target.exists():
                try:
                    self.discard(str(target))
                except Exception:
                    pass
            raise ValueError(
                created.stderr.strip() or "Unable to materialize review snapshot"
            )

        try:
            self._overlay_workspace_changes(target, captured)
            current = capture_workspace_snapshot(self._repo_path)
            if current.revision != captured.revision:
                raise ValueError(
                    "Workspace changed while the review snapshot was being created"
                )
            return MaterializedReviewSnapshot(
                path=str(target),
                workspace_revision=captured.revision,
                head_commit=captured.head_commit,
            )
        except Exception:
            self.discard(str(target))
            raise

    def discard(self, snapshot_path: str) -> None:
        target = Path(snapshot_path).resolve()
        self._assert_managed(target)
        runtime = LocalRuntime(workspace_root=self._repo_path)
        removed = runtime.execute(
            "git",
            args=["worktree", "remove", "--force", str(target)],
            cwd=str(self._repo_path),
            timeout=60,
        )
        if not removed.success:
            raise ValueError(
                removed.stderr.strip() or "Unable to remove review snapshot"
            )
        if target.exists():
            shutil.rmtree(target)

    def validate(self, snapshot_path: str) -> str:
        target = Path(snapshot_path).resolve()
        self._assert_managed(target)
        if not target.is_dir():
            raise ValueError("Review snapshot is unavailable")
        return str(target)

    def _overlay_workspace_changes(
        self,
        target: Path,
        captured: WorkspaceSnapshot,
    ) -> None:
        for fact in captured.files:
            source = Path(fact.path).resolve()
            try:
                relative = source.relative_to(self._repo_path)
            except ValueError as exc:
                raise ValueError(
                    f"Captured file is outside the project: {source}"
                ) from exc
            destination = (target / relative).resolve()
            try:
                destination.relative_to(target)
            except ValueError as exc:
                raise ValueError(
                    f"Captured path escapes the review snapshot: {relative}"
                ) from exc

            if source.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    destination.unlink()
                os.symlink(os.readlink(source), destination)
            elif source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            elif destination.is_dir():
                shutil.rmtree(destination)
            elif destination.exists() or destination.is_symlink():
                destination.unlink()

    def _assert_managed(self, target: Path) -> None:
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(
                "Review snapshot path is outside managed runtime state"
            ) from exc
        if target == self._root:
            raise ValueError("Review snapshot root cannot be used as a snapshot")
