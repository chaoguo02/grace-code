from __future__ import annotations

import pytest

from agent.session.models import SessionMode
from app.storage.sqlite import SqliteStorageBackend
from server.services.run_submission import (
    IdempotencyConflictError,
    RunAlreadyActiveError,
    submit_run_turn,
)


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
