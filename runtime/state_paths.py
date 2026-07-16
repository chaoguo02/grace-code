"""Physically isolated paths for Forge Agent runtime state."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


STATE_HOME_ENV = "FORGE_AGENT_STATE_DIR"


class StateIsolationError(ValueError):
    """Raised when runtime state would be placed inside the target project."""


class StateMigration(Enum):
    NOT_NEEDED = "not_needed"
    COPIED = "copied"


@dataclass(frozen=True)
class ProjectStatePaths:
    """All private runtime state for one canonical project root."""

    project_root: Path
    root: Path

    @classmethod
    def for_project(
        cls,
        project_root: str | Path,
        *,
        state_home: str | Path | None = None,
    ) -> "ProjectStatePaths":
        project = Path(project_root).resolve()
        home = _state_home(state_home).resolve()
        project_key = _project_key(project)
        root = (home / "projects" / project_key).resolve()
        try:
            root.relative_to(project)
        except ValueError:
            pass
        else:
            raise StateIsolationError(
                f"runtime state root must be outside project: {root}"
            )
        return cls(project_root=project, root=root)

    @property
    def sessions_db(self) -> Path:
        return self.root / "sessions" / "sessions.db"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def goals(self) -> Path:
        return self.root / "goals" / "goal.json"

    @property
    def datasets(self) -> Path:
        return self.root / "datasets" / "forge-agent-failures.jsonl"

    @property
    def plans(self) -> Path:
        return self.root / "plans"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def experiments(self) -> Path:
        return self.root / "experiments"

    @property
    def worktrees(self) -> Path:
        return self.root / "worktrees"


def migrate_legacy_session_db(
    project_root: str | Path,
    target: str | Path,
) -> StateMigration:
    """Copy the legacy project-local DB once, without altering the source."""

    project = Path(project_root).resolve()
    source = project / ".forge-agent" / "v2" / "sessions.db"
    destination = Path(target).resolve()
    if destination.exists() or not source.is_file():
        return StateMigration.NOT_NEEDED
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return StateMigration.COPIED


def _state_home(explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    configured = os.environ.get(STATE_HOME_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".forge-agent" / "state"


def _project_key(project: Path) -> str:
    canonical = os.path.normcase(str(project))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", project.name).strip("-._")
    return f"{slug or 'project'}-{digest}"
