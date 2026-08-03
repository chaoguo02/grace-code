"""Production lifecycle chain acceptance tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from agent.session.models import SessionMode
from app.storage.sqlite import SqliteStorageBackend
from server.projections.projection_runner import ProjectionRunner
from server.projections.trace_projection import TraceProjection
from server.services.event_outbox import OutboxRelay, OutboxStore
from server.services.run_submission import submit_run_turn

from application.coordinators.run_coordinator import RunCoordinator
from application.events.schema_registry import SchemaRegistry
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.sqlite.run_uow import SqliteUnitOfWork
from runtime_core.outcome import RunStatus
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
    """Create a real RunCoordinator backed by the test DB.

    Must create a unified event_outbox schema compatible with both
    old SessionStore (needs event_version) and new SqliteOutboxStore
    (needs payload_digest).
    """
    # Use old OutboxStore first (has event_version column)
    from server.services.event_outbox import OutboxStore
    old_outbox = OutboxStore(db_path)
    old_outbox.install()
    # Migrate to add new SqliteOutboxStore columns (payload_digest)
    conn = sqlite3.connect(db_path)
    SqliteOutboxStore.migrate_add_columns(conn)
    # Also install the owner_lease table for OutboxRelay
    from infrastructure.outbox.owner_lease import OwnerLease
    OwnerLease.install(conn)
    conn.commit()
    conn.close()

    registry = SchemaRegistry()
    outbox_store = SqliteOutboxStore(db_path, registry)

    ports = RuntimePorts(
        llm=FakeLLM(),
        tools=FakeTools(),
        hooks=FakeHooks(),
        live_events=FakeLiveEvents(),
        clock=FakeClock(),
        token_usage=FakeTokenUsage(),
    )
    runtime = AgentRuntime(ports)
    uow = SqliteUnitOfWork(db_path, outbox_store)
    return RunCoordinator(runtime, uow)


@pytest.mark.asyncio
async def test_run_lifecycle_state_outbox_trace_and_live_are_one_chain(tmp_path):
    db_path = str(tmp_path / "chain.db")
    storage = SqliteStorageBackend(db_path)
    session = storage.create_session(
        agent_name="build",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="outbox chain",
    )
    coordinator = _make_coordinator(db_path)
    submitted = submit_run_turn(
        storage,
        session_id=session.id,
        prompt="implement",
        idempotency_key="chain-1",
        coordinator=coordinator,
    )

    store = storage._store
    assert store.start_run_with_event(
        submitted.run_id,
        session.id,
        turn_id=submitted.turn_id,
        turn_index=submitted.turn_index,
    )
    assert store.finalize_run_with_event(
        submitted.run_id,
        session.id,
        status="completed",
        summary="done",
        steps_taken=2,
        total_tokens=123,
        event_payload={
            "turn_id": submitted.turn_id,
            "turn_index": submitted.turn_index,
            "termination_reason": "finish",
        },
    )

    live: list[dict] = []
    outbox = OutboxStore(db_path)
    runner = ProjectionRunner(
        TraceProjection(db_path),
        lambda _session_id, message: live.append(message),
    )
    relay = OutboxRelay(outbox, runner.deliver, worker_id="test-relay")
    relay.POLL_INTERVAL_S = 0.01
    await relay.start()
    await asyncio.sleep(0.1)
    assert await relay.stop() == 0

    with sqlite3.connect(db_path) as conn:
        statuses = conn.execute(
            "SELECT status, COUNT(*) FROM event_outbox GROUP BY status"
        ).fetchall()
        traces = conn.execute(
            "SELECT event_json FROM session_trace_events WHERE session_id=? ORDER BY seq",
            (session.id,),
        ).fetchall()
        run_status = conn.execute(
            "SELECT status FROM runs WHERE id=?", (submitted.run_id,),
        ).fetchone()[0]

    assert statuses == [("delivered", 3)]
    assert run_status == "completed"
    trace_events = [json.loads(row[0]) for row in traces]
    assert [event["type"] for event in trace_events] == [
        "run.submitted", "run_started", "run_terminal",
    ]
    assert [message["type"] for message in live] == [
        "run_started", "run_terminal",
    ]
    assert live[-1]["summary"] == "done"
    assert live[-1]["total_tokens"] == 123


def test_terminal_cas_emits_exactly_one_fact(tmp_path):
    db_path = str(tmp_path / "cas.db")
    storage = SqliteStorageBackend(db_path)
    session = storage.create_session(
        agent_name="build", mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path), title="cas",
    )
    coordinator = _make_coordinator(db_path)
    submitted = submit_run_turn(
        storage, session_id=session.id, prompt="x", idempotency_key="cas-1",
        coordinator=coordinator,
    )
    assert storage._store.start_run_with_event(submitted.run_id, session.id)
    assert storage._store.finalize_run_with_event(
        submitted.run_id, session.id, status="completed",
    )
    assert not storage._store.finalize_run_with_event(
        submitted.run_id, session.id, status="failed", error="late failure",
    )

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE aggregate_id=? AND event_type NOT IN "
            "('run.submitted', 'run.started')",
            (submitted.run_id,),
        ).fetchone()[0]
    assert count == 1
