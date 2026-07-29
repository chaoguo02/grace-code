from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.session.models import SessionMode, SessionStatus
from app.storage.sqlite import SqliteStorageBackend
from server.services.run_submission import (
    IdempotencyConflictError,
    RunAlreadyActiveError,
    submit_run_turn,
)
from server.services.agent_service import AgentService


def _storage(tmp_path):
    storage = SqliteStorageBackend(str(tmp_path / "sessions.db"))
    session = storage.create_session(
        agent_name="plan",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="Plan",
    )
    return storage, session


def test_submit_run_turn_is_atomic_and_idempotent(tmp_path):
    storage, session = _storage(tmp_path)

    first = submit_run_turn(
        storage,
        session_id=session.id,
        prompt="Revise this plan",
        idempotency_key="plan:reject:one",
    )
    second = submit_run_turn(
        storage,
        session_id=session.id,
        prompt="Revise this plan",
        idempotency_key="plan:reject:one",
    )

    assert first.created is True
    assert second.created is False
    assert second.run_id == first.run_id
    assert second.turn_id == first.turn_id
    assert len(storage.list_runs(session.id)) == 1
    messages = storage.list_messages(session.id)
    assert [(message.role, message.content) for message in messages] == [
        ("user", "Revise this plan"),
    ]
    assert messages[0].turn_id == first.turn_id


def test_submit_run_turn_rejects_conflicts(tmp_path):
    storage, session = _storage(tmp_path)
    submit_run_turn(
        storage,
        session_id=session.id,
        prompt="First",
        idempotency_key="same-key",
    )

    with pytest.raises(IdempotencyConflictError):
        submit_run_turn(
            storage,
            session_id=session.id,
            prompt="Different",
            idempotency_key="same-key",
        )
    with pytest.raises(RunAlreadyActiveError):
        submit_run_turn(
            storage,
            session_id=session.id,
            prompt="Parallel",
            idempotency_key="different-key",
        )


def test_startup_recovery_atomically_fails_orphaned_run_and_session(tmp_path):
    storage, session = _storage(tmp_path)
    submitted = submit_run_turn(
        storage,
        session_id=session.id,
        prompt="Hello",
        idempotency_key="orphan",
    )
    storage.update_run(
        submitted.run_id,
        status="running",
        expect_status="queued",
    )
    storage.update_status(session.id, SessionStatus.RUNNING)

    recovered = storage.recover_orphaned_runs()

    assert recovered == [{
        "run_id": submitted.run_id,
        "session_id": session.id,
    }]
    run = storage.get_run(submitted.run_id)
    assert run["status"] == "failed"
    assert run["termination_reason"] == "internal_error"
    assert run["completed_at"]
    recovered_session = storage.get_session(session.id)
    assert recovered_session.status is SessionStatus.FAILED
    assert recovered_session.error == "Interrupted by server restart"
    assert storage.recover_orphaned_runs() == []


def test_startup_recovery_reconciles_running_session_to_cancelled_run(tmp_path):
    storage, session = _storage(tmp_path)
    submitted = submit_run_turn(
        storage,
        session_id=session.id,
        prompt="Hello",
        idempotency_key="cancelled-orphan",
    )
    storage.update_run(
        submitted.run_id,
        status="running",
        expect_status="queued",
    )
    storage.update_status(session.id, SessionStatus.RUNNING)
    storage.update_run(
        submitted.run_id,
        status="cancelled",
        error="User cancelled",
        expect_status="running",
    )

    recovered = storage.recover_orphaned_runs()

    assert recovered == [{
        "run_id": submitted.run_id,
        "session_id": session.id,
    }]
    recovered_session = storage.get_session(session.id)
    assert recovered_session.status is SessionStatus.CANCELLED
    assert recovered_session.error == "User cancelled"


def test_cancel_immediately_reconciles_session_status() -> None:
    updates = []
    storage = SimpleNamespace(
        get_active_run=lambda session_id: {"id": "run-1"},
        update_run=lambda run_id, **kwargs: updates.append(
            ("run", run_id, kwargs)
        ),
        update_status=lambda session_id, status, error="": updates.append(
            ("session", session_id, status, error)
        ),
    )
    runtime = SimpleNamespace(
        get_approval_broker=lambda session_id: None,
        cancel_session=lambda session_id, detail="": True,
    )
    event_bus = SimpleNamespace(publish_typed=lambda *args, **kwargs: None)
    service = AgentService.__new__(AgentService)
    service._storage = storage
    service._runtime = runtime
    service._event_bus = event_bus

    assert service.cancel_run("session-1", "stop now") is True
    assert updates[0][0:2] == ("run", "run-1")
    assert updates[1] == (
        "session", "session-1", SessionStatus.CANCELLED, "stop now",
    )
