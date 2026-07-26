from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.task import (
    Event,
    EventType,
    RunResult,
    RunStatus,
    VerificationCheck,
    VerificationReason,
    VerificationStatus,
    WorkspaceDelta,
)
from agent.session.runtime import SessionRuntime
from agent.session.models import SessionMode
from app.storage.sqlite import SqliteStorageBackend
from llm.base import LLMMessage
from server.events import WsApprovalRequired
from server.services.event_bus import EventBus
from server.services.session_service import SessionService


def _create_storage(tmp_path):
    storage = SqliteStorageBackend(str(tmp_path / "sessions.db"))
    session = storage.create_session(
        agent_name="build",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="Trace session",
    )
    return storage, session


def test_sqlite_trace_events_are_sequenced_and_queryable(tmp_path):
    storage, session = _create_storage(tmp_path)

    first = storage.insert_trace_event(session.id, {"type": "thought", "content": "one"})
    second = storage.insert_trace_event(session.id, {"type": "tool_call", "name": "Read"})

    assert first["seq"] == 1
    assert second["seq"] == 2
    assert [event["type"] for event in storage.list_trace_events(session.id)] == ["thought", "tool_call"]
    assert [event["type"] for event in storage.list_trace_events(session.id, after_seq=1)] == ["tool_call"]


def test_event_bus_persists_translated_runtime_events_without_subscribers(tmp_path):
    storage, session = _create_storage(tmp_path)
    bus = EventBus(repo_path=str(tmp_path))
    bus.trace_store = storage

    bus.publish(Event(
        event_type=EventType.ACTION,
        task_id=session.id,
        session_id=session.id,
        payload={
            "step": 3,
            "action": {
                "thought": "Need to inspect the file",
                "tool_calls": [{"name": "Read", "params": {"file_path": "web/src/App.tsx"}, "id": "tc-1"}],
            },
        },
    ))

    events = storage.list_trace_events(session.id)
    assert [(event["type"], event["seq"]) for event in events] == [("thought", 1), ("tool_call", 2)]
    assert events[0]["content"] == "Need to inspect the file"
    assert events[1]["name"] == "Read"


def test_event_bus_fills_empty_typed_envelope_from_run_context(tmp_path):
    storage, session = _create_storage(tmp_path)
    bus = EventBus(repo_path=str(tmp_path))
    bus.trace_store = storage
    context = SimpleNamespace(
        run_id="run-1",
        turn_id="turn-1",
        turn_index=1,
    )

    bus.publish(Event(
        event_type=EventType.ACTION,
        task_id=session.id,
        session_id=session.id,
        payload={
            "step": 1,
            "action": {
                "thought": "Inspect first",
                "tool_calls": [],
            },
        },
    ), run_context=context)

    event = storage.list_trace_events(session.id)[0]
    assert event["session_id"] == session.id
    assert event["run_id"] == "run-1"
    assert event["turn_id"] == "turn-1"
    assert event["turn_index"] == 1


def test_event_bus_persists_direct_typed_events(tmp_path):
    storage, session = _create_storage(tmp_path)
    bus = EventBus(repo_path=str(tmp_path))
    bus.trace_store = storage

    bus.publish_typed(session.id, WsApprovalRequired(
        request_id="approval-1",
        tool_name="Write",
        params={"file_path": "web/src/App.tsx"},
    ))

    events = storage.list_trace_events(session.id)
    assert len(events) == 1
    assert events[0]["seq"] == 1
    assert events[0]["type"] == "approval_required"
    assert events[0]["request_id"] == "approval-1"


def test_turn_timeline_recovers_legacy_blank_event_envelopes(tmp_path):
    storage, session = _create_storage(tmp_path)
    service = SessionService(storage)
    run_id = "run-legacy"
    turn_id = "turn-legacy"
    storage.create_run(
        run_id=run_id,
        session_id=session.id,
        turn_id=turn_id,
        turn_index=1,
        prompt="Fix it",
    )
    storage.update_run(run_id, status="running", expect_status="queued")

    user = LLMMessage(role="user", content="Fix it")
    user.turn_id = turn_id
    storage.append_message(session.id, user)
    assistant = LLMMessage(role="assistant", content="Done")
    assistant.turn_id = turn_id
    storage.append_message(session.id, assistant)
    final_assistant = LLMMessage(role="assistant", content="Final durable answer")
    final_assistant.turn_id = turn_id
    storage.append_message(session.id, final_assistant)

    storage.insert_trace_event(session.id, {
        "type": "run_started",
        "run_id": run_id,
        "turn_id": turn_id,
        "turn_index": 1,
    })
    storage.insert_trace_event(session.id, {
        "type": "thought",
        "content": "Legacy thought",
        "run_id": "",
        "turn_id": "",
    })
    storage.insert_trace_event(session.id, {
        "type": "tool_call",
        "name": "Read",
        "run_id": "",
        "turn_id": "",
    })
    storage.insert_trace_event(session.id, {
        "type": "run_terminal",
        "run_id": run_id,
        "turn_id": turn_id,
        "status": "completed",
        "summary": "Done",
    })
    storage.update_run(
        run_id,
        status="completed",
        summary="Done",
        steps_taken=1,
        total_tokens=10,
        expect_status="running",
    )

    timeline = service.build_turn_timeline(session.id)

    assert len(timeline["turns"]) == 1
    turn = timeline["turns"][0]
    assert turn["run_id"] == run_id
    assert turn["turn_id"] == turn_id
    assert [event["type"] for event in turn["trace_events"]] == [
        "run_started",
        "thought",
        "tool_call",
        "run_terminal",
    ]
    assert turn["meta"]["status"] == "completed"
    assert turn["assistant_message"]["content"] == "Final durable answer"


def test_timeline_strips_legacy_unverified_prefix_from_assistant_message(tmp_path):
    storage, session = _create_storage(tmp_path)
    service = SessionService(storage)
    turn_id = "turn-unverified"

    assistant = LLMMessage(
        role="assistant",
        content=(
            "[UNVERIFIED — no test environment available. "
            "Code changes were made but NOT independently verified.]\n\n"
            "Actual answer"
        ),
    )
    assistant.turn_id = turn_id
    storage.append_message(session.id, assistant)

    timeline = service.build_turn_timeline(session.id)

    assert timeline["turns"][0]["assistant_message"]["content"] == "Actual answer"


def test_structured_run_outcome_is_persisted_and_projected(tmp_path):
    storage, session = _create_storage(tmp_path)
    service = SessionService(storage)
    run_id = "run-structured"
    turn_id = "turn-structured"
    storage.create_run(
        run_id=run_id,
        session_id=session.id,
        turn_id=turn_id,
        turn_index=1,
        prompt="Change it",
    )
    storage.update_run(run_id, status="running", expect_status="queued")
    storage.update_run(
        run_id,
        status="completed",
        summary="Clean answer",
        termination_reason="none",
        verification_status="unverified",
        verification_reason="not_run",
        verification_checks=[{
            "name": "tests", "status": "skipped", "detail": "not requested",
        }],
        workspace_delta={
            "has_changes": True,
            "changed_files": ["agent/task.py"],
            "patch": "",
            "source": "tool_journal",
            "is_run_scoped": True,
        },
        expect_status="running",
    )

    runs = service.list_runs(session.id)
    assert runs[0]["summary"] == "Clean answer"
    assert runs[0]["verification_status"] == "unverified"
    assert runs[0]["verification_checks"][0]["status"] == "skipped"
    assert runs[0]["workspace_delta"]["changed_files"] == ["agent/task.py"]

    # A run without messages/events is available from the run API, while turn
    # projection remains message/event-driven. Add one message to materialize it.
    user = LLMMessage(role="user", content="Change it")
    user.turn_id = turn_id
    storage.append_message(session.id, user)
    timeline = service.build_turn_timeline(session.id)
    meta = timeline["turns"][0]["meta"]
    assert meta["verification"]["reason"] == "not_run"
    assert meta["workspace_delta"]["is_run_scoped"] is True


def test_finalize_run_keeps_runtime_facts_out_of_summary():
    store = MagicMock()
    store.update_run.return_value = True
    published = []
    runtime = SimpleNamespace(
        _store=store,
        _publish_run_terminal=lambda session_id, event: published.append(
            (session_id, event)
        ),
    )
    result = RunResult(
        task_id="task-1",
        status=RunStatus.SUCCESS,
        summary="Clean final answer",
        steps_taken=2,
        total_tokens=50,
        verification_status=VerificationStatus.UNVERIFIED,
        verification_reason=VerificationReason.NOT_RUN,
        verification_checks=(
            VerificationCheck(name="tests", status="skipped"),
        ),
        workspace_delta=WorkspaceDelta(
            has_changes=True,
            changed_files=("agent/task.py",),
            patch="private diff",
            source="tool_journal",
            is_run_scoped=True,
        ),
    )

    SessionRuntime._finalize_run(
        runtime,
        SimpleNamespace(
            run_id="run-1", session_id="session-1",
            turn_id="turn-1", turn_index=1,
        ),
        result,
        "completed",
    )

    update_kwargs = store.update_run.call_args.kwargs
    assert update_kwargs["summary"] == "Clean final answer"
    assert update_kwargs["verification_reason"] == "not_run"
    assert update_kwargs["workspace_delta"]["patch"] == "private diff"
    event = published[0][1]
    assert event["summary"] == "Clean final answer"
    assert event["verification"]["checks"][0]["status"] == "skipped"
    assert event["workspace_delta"]["patch_available"] is True
    assert "patch" not in event["workspace_delta"]
