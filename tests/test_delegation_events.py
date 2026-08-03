"""Delegation event typing and exactly-once terminal persistence."""

from __future__ import annotations

import json
import sqlite3

from agent.session.models import SessionMode
from agent.session.session_store import SessionStore
from agent.task import Event, EventType
from server.services.event_bus import _translate_event


def _completed_run(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    parent = store.create_session(
        agent_name="research",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="delegation events",
    )
    store.create_delegation_run(
        run_id="delegation-1",
        parent_session_id=parent.id,
        parent_run_id="parent-run",
        topology="fan_out_fan_in",
    )
    store.create_delegation_task(
        task_id="delegation-1:inspect",
        delegation_run_id="delegation-1",
        agent_type="explore",
        goal="Inspect",
    )
    store.update_delegation_task(
        "delegation-1:inspect", status="completed",
        expected_statuses=("queued",),
    )
    return store, parent


def test_delegation_resource_facts_round_trip(tmp_path):
    store, _ = _completed_run(tmp_path)
    assert store.update_delegation_task_resource(
        "delegation-1:inspect",
        {
            "requested": {"worker_slot": 1},
            "granted": {"worker_slot": 1},
        },
    )
    # ExecutionBudget is the token authority and reports its lifecycle
    # independently; nested resource maps must merge rather than overwrite.
    assert store.update_delegation_task_resource(
        "delegation-1:inspect",
        {
            "requested": {"token_budget": 10_000},
            "granted": {"token_budget": 10_000},
        },
    )
    assert store.update_delegation_task_resource(
        "delegation-1:inspect",
        {
            "consumed": {"token_budget": 7_500},
            "refunded": {"token_budget": 2_500},
            "wait_time_s": 0.25,
        },
    )
    task = store.get_delegation_task("delegation-1:inspect")
    assert task["resource"] == {
        "requested": {"worker_slot": 1, "token_budget": 10_000},
        "granted": {"worker_slot": 1, "token_budget": 10_000},
        "consumed": {"token_budget": 7_500},
        "refunded": {"token_budget": 2_500},
        "wait_time_s": 0.25,
    }


def test_delegation_terminal_state_and_trace_are_committed_once(tmp_path):
    store, parent = _completed_run(tmp_path)

    first = store.reconcile_delegation_run("delegation-1")
    second = store.reconcile_delegation_run("delegation-1")

    assert first["status"] == "completed"
    assert first["_terminal_event"]["event_id"].startswith(
        "delegation-terminal:delegation-1:"
    )
    assert "_terminal_event" not in second
    from server.projections.trace_projection import TraceProjection
    from server.services.event_outbox import OutboxStore

    outbox = OutboxStore(store.db_path)
    batch = outbox.claim_batch("test-projection")
    terminal = [record for record in batch if record.event_type == "delegation.completed"]
    assert len(terminal) == 1
    assert TraceProjection(store.db_path).project(terminal[0]) is True
    assert TraceProjection(store.db_path).project(terminal[0]) is False

    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT event_json FROM session_trace_events "
            "WHERE session_id = ? AND event_type = 'delegation_completed'",
            (parent.id,),
        ).fetchall()
    assert len(rows) == 1
    event = json.loads(rows[0][0])
    assert event["delegation_run_id"] == "delegation-1"
    assert event["run_id"] == "parent-run"
    assert event["sequence"] == event["seq"]


def test_delegation_translation_is_flattened_and_typed():
    event = Event(
        event_type=EventType.DELEGATION_TASK_BLOCKED,
        task_id="delegation-1",
        session_id="parent-session",
        payload={
            "delegation_run_id": "delegation-1",
            "task_id": "task-2",
            "generation": 3,
            "phase": "executing",
            "status": "blocked",
            "reason": "Dependency incomplete",
            "dependencies": ["task-1"],
        },
    )

    translated = _translate_event(event)

    assert translated == [{
        "type": "delegation_task_blocked",
        "delegation_run_id": "delegation-1",
        "task_id": "task-2",
        "generation": 3,
        "topology": "",
        "task_count": 0,
        "phase": "executing",
        "previous_phase": "",
        "status": "blocked",
        "agent_type": "",
        "child_session_id": "",
        "report_count": 0,
        "tokens_used": 0,
        "duration_ms": 0,
        "reason": "Dependency incomplete",
        "error": "",
        "action": "",
        "integration_status": "",
        "verification": {},
        "budget": {},
        "changed_files": [],
        "dependencies": ["task-1"],
        "timestamp": event.timestamp,
        "session_id": "",
        "run_id": "",
        "turn_id": "",
        "event_id": event.event_id,
        "sequence": 0,
        "block_id": "",
        "tool_call_id": "",
    }]
