from __future__ import annotations

from pathlib import Path

import pytest

from runtime.state_paths import (
    ProjectStatePaths,
    StateIsolationError,
    StateMigration,
    migrate_legacy_session_db,
)
from agent.event_log import EventLog
from agent.task import Task
from agent.v2.runtime import default_session_db_path


def test_project_state_is_deterministic_and_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    state_home = tmp_path / "state-home"
    project.mkdir()

    first = ProjectStatePaths.for_project(project, state_home=state_home)
    second = ProjectStatePaths.for_project(project, state_home=state_home)

    assert first == second
    assert first.sessions_db.is_absolute()
    with pytest.raises(ValueError):
        first.root.relative_to(project)


def test_different_projects_have_different_state_roots(tmp_path: Path) -> None:
    first_project = tmp_path / "first"
    second_project = tmp_path / "second"
    first_project.mkdir()
    second_project.mkdir()

    first = ProjectStatePaths.for_project(first_project, state_home=tmp_path / "state")
    second = ProjectStatePaths.for_project(second_project, state_home=tmp_path / "state")

    assert first.root != second.root


def test_state_home_inside_project_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(StateIsolationError):
        ProjectStatePaths.for_project(project, state_home=project / ".state")


def test_legacy_session_db_is_copied_once_without_source_mutation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    legacy = project / ".forge-agent" / "v2" / "sessions.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    target = tmp_path / "state" / "sessions.db"

    assert migrate_legacy_session_db(project, target) is StateMigration.COPIED
    assert target.read_bytes() == b"legacy"
    assert legacy.read_bytes() == b"legacy"

    target.write_bytes(b"current")
    assert migrate_legacy_session_db(project, target) is StateMigration.NOT_NEEDED
    assert target.read_bytes() == b"current"


def test_default_event_log_is_outside_project(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state_home = tmp_path / "state-home"
    monkeypatch.setenv("FORGE_AGENT_STATE_DIR", str(state_home))
    task = Task(description="read", repo_path=str(project), intent="analysis")

    with EventLog.create(task) as log:
        log_path = log.path.resolve()

    assert state_home.resolve() in log_path.parents
    with pytest.raises(ValueError):
        log_path.relative_to(project.resolve())


def test_default_session_db_uses_project_state_root(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state_home = tmp_path / "state-home"
    monkeypatch.setenv("FORGE_AGENT_STATE_DIR", str(state_home))

    db_path = Path(default_session_db_path(str(project))).resolve()

    assert state_home.resolve() in db_path.parents
    assert db_path.name == "sessions.db"
    with pytest.raises(ValueError):
        db_path.relative_to(project.resolve())
