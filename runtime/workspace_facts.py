"""Objective, side-effect-free workspace snapshots.

The Runtime captures a snapshot before and after an agent run.  Progress is a
change in the snapshot revision, never an inference from tool names or calls.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, order=True)
class FileFact:
    """Content fact for one absolute workspace path."""

    path: str
    digest: str


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Immutable Git worktree state at one point in time."""

    project_root: str
    is_git_repo: bool
    head_commit: str
    revision: str
    current_patch: str
    files: tuple[FileFact, ...]
    error: str = ""


@dataclass(frozen=True)
class WorkspaceDelta:
    """Difference between two snapshots of the same project."""

    before: WorkspaceSnapshot
    after: WorkspaceSnapshot
    has_changes: bool
    changed_paths: tuple[str, ...]

    @property
    def attributable_patch(self) -> str:
        """Return a patch only when the run started from a clean worktree.

        A current ``git diff HEAD`` cannot be attributed to this run when the
        baseline was already dirty.  In that case changed paths remain factual,
        but returning the whole patch would falsely claim pre-existing changes.
        """

        if self.before.files or self.before.current_patch:
            return ""
        return self.after.current_patch if self.has_changes else ""


def capture_workspace_snapshot(project_root: str | Path) -> WorkspaceSnapshot:
    """Capture Git-visible tracked and untracked state without mutating it."""

    root = Path(project_root).resolve()
    if not root.is_dir():
        return _empty_snapshot(root, error="project root does not exist or is not a directory")

    # Do not walk into a parent repository.  The target project root is the
    # isolation boundary; a parent checkout must not become its fact source.
    if not (root / ".git").exists():
        return _empty_snapshot(root)

    inside = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != b"true":
        return _empty_snapshot(root)

    head_result = _run_git(root, "rev-parse", "HEAD")
    head_commit = _decode(head_result.stdout).strip() if head_result.returncode == 0 else ""

    patch_result = _run_git(root, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    patch_bytes = patch_result.stdout if patch_result.returncode == 0 else b""

    tracked_result = _run_git(root, "diff", "--name-only", "-z", "HEAD", "--")
    tracked_names = _nul_paths(tracked_result.stdout) if tracked_result.returncode == 0 else ()

    untracked_result = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
    untracked_names = _nul_paths(untracked_result.stdout) if untracked_result.returncode == 0 else ()

    facts = tuple(sorted(
        (_file_fact(root, relative) for relative in set(tracked_names) | set(untracked_names)),
        key=lambda fact: fact.path,
    ))
    revision = _workspace_revision(head_commit, patch_bytes, facts)
    errors = [
        _decode(result.stderr).strip()
        for result in (patch_result, tracked_result, untracked_result)
        if result.returncode != 0 and result.stderr
    ]
    return WorkspaceSnapshot(
        project_root=str(root),
        is_git_repo=True,
        head_commit=head_commit,
        revision=revision,
        current_patch=_decode(patch_bytes),
        files=facts,
        error="; ".join(errors),
    )


def compare_workspace_snapshots(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> WorkspaceDelta:
    """Compare snapshots and report only paths whose facts changed this run."""

    if before.project_root != after.project_root:
        raise ValueError("cannot compare snapshots from different project roots")

    before_files = {fact.path: fact.digest for fact in before.files}
    after_files = {fact.path: fact.digest for fact in after.files}
    paths = sorted(set(before_files) | set(after_files))
    changed = tuple(path for path in paths if before_files.get(path) != after_files.get(path))
    has_changes = before.revision != after.revision

    return WorkspaceDelta(
        before=before,
        after=after,
        has_changes=has_changes,
        changed_paths=changed,
    )


def _empty_snapshot(root: Path, error: str = "") -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        project_root=str(root),
        is_git_repo=False,
        head_commit="",
        revision="",
        current_patch="",
        files=(),
        error=error,
    )


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(["git", *args], returncode=-1, stdout=b"", stderr=str(exc).encode("utf-8"))


def _nul_paths(raw: bytes) -> tuple[str, ...]:
    return tuple(os.fsdecode(item) for item in raw.split(b"\0") if item)


def _file_fact(root: Path, relative: str) -> FileFact:
    # Normalize the Git path without following a symlink outside the project.
    path = Path(os.path.abspath(root / relative))
    try:
        path.relative_to(root)
    except ValueError:
        return FileFact(path=str(path), digest="outside-project")

    digest = hashlib.sha256()
    try:
        stat = path.lstat()
        digest.update(str(stat.st_mode).encode("ascii"))
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif path.is_file():
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"non-file")
    except FileNotFoundError:
        digest.update(b"deleted")
    except OSError as exc:
        digest.update(f"unreadable:{exc.errno}".encode("ascii"))
    return FileFact(path=str(path), digest=digest.hexdigest())


def _workspace_revision(head_commit: str, patch: bytes, facts: tuple[FileFact, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(head_commit.encode("ascii", errors="replace"))
    digest.update(b"\0")
    digest.update(patch)
    for fact in facts:
        digest.update(b"\0")
        digest.update(fact.path.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(fact.digest.encode("ascii"))
    return digest.hexdigest()


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")
