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
    submitted = submit_run_turn(
        storage,
        session_id=session.id,
        prompt="implement",
        idempotency_key="chain-1",
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
    submitted = submit_run_turn(
        storage, session_id=session.id, prompt="x", idempotency_key="cas-1",
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
