from __future__ import annotations

from queue import Queue
from unittest.mock import Mock

from agent.session.models import WorktreeChange, WorktreeEvidence
from agent.session.runtime import SessionRuntime
from agent.session.worktree_service import (
    WorktreeOperationResult,
    WorktreeOperationStatus,
)


def _evidence(revision: str = "reviewed-revision") -> WorktreeEvidence:
    return WorktreeEvidence(
        change=WorktreeChange.UNCOMMITTED,
        path="D:/state/worktree",
        branch="multi-agent/child",
        base_branch="main",
        revision=revision,
    )


def test_resolution_dispatches_reviewed_revision_to_canonical_operation():
    runtime = object.__new__(SessionRuntime)
    runtime.apply_subagent_worktree = Mock(
        return_value=WorktreeOperationResult(
            WorktreeOperationStatus.APPLIED,
            _evidence(),
        ),
    )

    result = runtime._resolve_worktree_sync(
        "parent",
        "child",
        "apply",
        expected_revision="reviewed-revision",
    )

    runtime.apply_subagent_worktree.assert_called_once_with(
        "parent",
        "child",
        expected_revision="reviewed-revision",
    )
    assert result == {
        "resolved": True,
        "action": "apply",
        "child_session_id": "child",
        "status": "applied",
        "message": "applied",
        "expected_revision": "reviewed-revision",
        "current_revision": "reviewed-revision",
    }


def test_enqueue_is_idempotent_for_same_action_and_revision():
    runtime = object.__new__(SessionRuntime)
    runtime._worktree_worker_started = True
    runtime._worktree_queue = Queue()
    runtime._worktree_results = {}

    first = runtime.enqueue_worktree_command(
        "parent",
        "child",
        "retain",
        expected_revision="reviewed-revision",
    )
    second = runtime.enqueue_worktree_command(
        "parent",
        "child",
        "retain",
        expected_revision="reviewed-revision",
    )

    assert first == second == "child_retain"
    assert runtime._worktree_queue.qsize() == 1
    assert runtime._worktree_queue.get_nowait() == (
        "parent",
        "child",
        "retain",
        "reviewed-revision",
    )


def test_enqueue_requires_an_expected_revision():
    runtime = object.__new__(SessionRuntime)
    runtime._worktree_worker_started = True
    runtime._worktree_queue = Queue()
    runtime._worktree_results = {}

    try:
        runtime.enqueue_worktree_command(
            "parent",
            "child",
            "discard",
            expected_revision="",
        )
    except ValueError as exc:
        assert str(exc) == "expected_revision is required"
    else:
        raise AssertionError("empty revisions must fail closed")
