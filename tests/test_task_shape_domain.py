import pytest

from agent.session.task_shape import (
    DependencyEdge,
    TaskPurpose,
    TaskShape,
    WorkItem,
)
from agent.task import TaskIntent


def test_task_shape_normalizes_dependencies_and_reports_conflicts():
    first = WorkItem(
        id="api",
        goal="Change API",
        domain="server",
        write_files=("server/api.py",),
    )
    second = WorkItem(
        id="tests",
        goal="Update tests",
        domain="tests",
        depends_on=("api",),
        write_files=("server/api.py",),
    )
    shape = TaskShape(
        intent=TaskIntent.EDIT,
        purpose=TaskPurpose.IMPLEMENTATION,
        domains=("server", "tests"),
        work_items=(first, second),
    )

    assert shape.dependency_edges == (DependencyEdge("api", "tests"),)
    assert shape.has_dependencies
    assert shape.has_write_conflicts


def test_task_shape_rejects_cycles_unknown_dependencies_and_unsafe_paths():
    with pytest.raises(ValueError, match="acyclic"):
        TaskShape(
            intent=TaskIntent.ANALYSIS,
            purpose=TaskPurpose.EXPLORATION,
            domains=("repo",),
            work_items=(
                WorkItem("a", "A", "repo", depends_on=("b",)),
                WorkItem("b", "B", "repo", depends_on=("a",)),
            ),
        )

    with pytest.raises(ValueError, match="unknown dependencies"):
        TaskShape(
            intent=TaskIntent.ANALYSIS,
            purpose=TaskPurpose.EXPLORATION,
            domains=("repo",),
            work_items=(WorkItem("a", "A", "repo", depends_on=("missing",)),),
        )

    with pytest.raises(ValueError, match="repository-relative"):
        WorkItem("a", "A", "repo", expected_files=("../secret",))

