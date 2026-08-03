from __future__ import annotations

import sqlite3
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

from application.coordinators.run_coordinator import RunCoordinator
from application.events.schema_registry import SchemaRegistry
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.sqlite.run_uow import SqliteUnitOfWork
from runtime_core.ports import (
    RuntimePorts, LLMPort, ToolPort, HookGatePort,
    LiveEventPort, ClockPort, TokenUsagePort,
    HookGateResult, ToolSuccess,
)
from runtime_core.model_actions import ModelAction
from runtime_core.runtime import AgentRuntime


# ── Fake Ports ────────────────────────────────────────────────────────────────

class FakeLLM:
    def invoke(self, messages, tools=None, tool_choice=None):
        return ModelAction.stop(reason="test")
    def stream(self, messages, tools=None, tool_choice=None):
        async def _stream():
            return ModelAction.stop(reason="test")
        return _stream()


class FakeTools:
    def execute(self, tool_name, params, invocation_id=""):
        return ToolSuccess(tool_name=tool_name)


class FakeHooks:
    def check(self, event_type, hook_input, tool_name=""):
        return HookGateResult(allowed=True)


class FakeLiveEvents:
    def publish(self, event_type, payload, scope=None):
        pass


class FakeClock:
    def now(self):
        import time
        return time.monotonic()
    def deadline(self, timeout_s):
        return self.now() + timeout_s


class FakeTokenUsage:
    def record(self, run_id, input_tokens, output_tokens):
        pass


def _make_coordinator(db_path: str):
    """Create a RunCoordinator backed by the test DB."""
    from server.services.event_outbox import OutboxStore
    old_outbox = OutboxStore(db_path)
    old_outbox.install()
    conn = sqlite3.connect(db_path)
    SqliteOutboxStore.migrate_add_columns(conn)
    conn.commit()
    conn.close()

    ports = RuntimePorts(
        llm=FakeLLM(), tools=FakeTools(), hooks=FakeHooks(),
        live_events=FakeLiveEvents(), clock=FakeClock(),
        token_usage=FakeTokenUsage(),
    )
    runtime = AgentRuntime(ports)
    registry = SchemaRegistry()
    outbox_store = SqliteOutboxStore(db_path, registry)
    uow = SqliteUnitOfWork(db_path, outbox_store)
    return RunCoordinator(runtime, uow)


def _storage(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    storage = SqliteStorageBackend(db_path)
    session = storage.create_session(
        agent_name="plan",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="Plan",
    )
    coordinator = _make_coordinator(db_path)
    return storage, session, coordinator


def test_submit_run_turn_is_atomic_and_idempotent(tmp_path):
    storage, session, coordinator = _storage(tmp_path)

    first = submit_run_turn(
        storage,
        session_id=session.id,
        prompt="Revise this plan",
        idempotency_key="plan:reject:one",
        coordinator=coordinator,
    )
    second = submit_run_turn(
        storage,
        session_id=session.id,
        prompt="Revise this plan",
        idempotency_key="plan:reject:one",
        coordinator=coordinator,
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
    storage, session, coordinator = _storage(tmp_path)
    submit_run_turn(
        storage,
        session_id=session.id,
        prompt="First",
        idempotency_key="same-key",
        coordinator=coordinator,
    )

    with pytest.raises(IdempotencyConflictError):
        submit_run_turn(
            storage,
            session_id=session.id,
            prompt="Different",
            idempotency_key="same-key",
            coordinator=coordinator,
        )
    with pytest.raises(RunAlreadyActiveError):
        submit_run_turn(
            storage,
            session_id=session.id,
            prompt="Parallel",
            idempotency_key="different-key",
            coordinator=coordinator,
        )


def test_startup_recovery_atomically_fails_orphaned_run_and_session(tmp_path):
    storage, session, coordinator = _storage(tmp_path)
    submitted = submit_run_turn(
        storage,
        session_id=session.id,
        prompt="Hello",
        idempotency_key="orphan",
        coordinator=coordinator,
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
    storage, session, coordinator = _storage(tmp_path)
    submitted = submit_run_turn(
        storage,
        session_id=session.id,
        prompt="Hello",
        idempotency_key="cancelled-orphan",
        coordinator=coordinator,
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


def test_cancel_active_run_defers_terminal_commit_to_runtime_finalizer() -> None:
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
    assert updates == []
