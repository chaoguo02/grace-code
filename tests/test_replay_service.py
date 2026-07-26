from __future__ import annotations

from types import SimpleNamespace

from core.base import Action, ActionType, ToolRegistry
from llm.base import MockBackend
from server.services.replay_service import ReplayService


def _step(step: int = 1) -> dict:
    return {
        "step": step,
        "runtime_decision": {
            "action": "continue",
            "reason": "",
            "strip_tools": False,
            "terminate_reason": "none",
            "terminate_status": "",
        },
        "visible_tools": [
            {"name": "Read", "visible": True, "source": "registry"},
        ],
        "model_action": {
            "action_type": "tool_call",
            "tool_calls": [{"id": "call-1", "name": "Read", "params": {}}],
        },
        "tool_executions": [
            {
                "tool_name": "Read",
                "tool_call_id": "call-1",
                "success": True,
                "outcome": "none",
                "duration_ms": 3,
            },
        ],
        "outcome": "continue",
        "termination_reason": "none",
        "termination_status": "",
    }


class _SessionService:
    def get_session(self, session_id):
        if session_id != "session-1":
            return None
        return SimpleNamespace(agent_name="build")


class _Storage:
    def __init__(self, events):
        self.events = events

    def list_trace_events(self, session_id, *, limit):
        assert limit == 5000
        return self.events


def _service(events):
    return ReplayService(SimpleNamespace(
        session_service=_SessionService(),
        _storage=_Storage(events),
    ))


def test_persisted_run_contract_is_validated_against_step_events() -> None:
    step = _step()
    record = {
        "version": 1,
        "run_id": "run-1",
        "task_id": "session-1",
        "session_id": "session-1",
        "steps": [step],
        "termination_reason": "budget_exhausted",
        "termination_status": "gave_up",
        "provenance": {"model": "fixture"},
        "permission_snapshot": {},
        "runtime_snapshot": {},
        "visible_tools": [],
        "summary": "budget ended",
    }
    result = _service([
        {"type": "run_started", "run_id": "run-1", "sequence": 1},
        {
            "type": "replay_step",
            "run_id": "run-1",
            "sequence": 2,
            "payload": step,
        },
        {
            "type": "replay_run",
            "run_id": "run-1",
            "sequence": 3,
            "payload": record,
        },
    ]).get_session_replay("session-1")

    run = result["runs"][0]
    assert run["contract_source"] == "persisted_replay_run"
    assert run["evidence_complete"] is True
    assert run["validation"]["valid"] is True
    assert run["validation"]["boundary_preserved"] is True
    assert run["metrics"]["tool_executions"] == 1


def test_historical_steps_are_reconstructed_without_inventing_provenance() -> None:
    result = _service([
        {
            "type": "replay_step",
            "run_id": "run-old",
            "sequence": 4,
            "payload": _step(),
        },
        {
            "type": "run_terminal",
            "run_id": "run-old",
            "sequence": 5,
            "termination_reason": "max_steps",
            "status": "failed",
            "summary": "limit",
        },
    ]).get_session_replay("session-1")

    run = result["runs"][0]
    assert run["contract_source"] == "reconstructed_from_steps"
    assert run["evidence_complete"] is False
    assert run["record"]["provenance"] == {}
    assert run["record"]["termination_status"] == "max_steps"
    assert run["validation"]["boundary_preserved"] is True


def test_unknown_session_is_rejected() -> None:
    service = _service([])

    try:
        service.get_session_replay("missing")
    except ValueError as exc:
        assert "Unknown session" in str(exc)
    else:
        raise AssertionError("missing session must be rejected")


def test_runtime_emits_run_level_replay_contract(tmp_path) -> None:
    from agent.agent_config import AgentConfig
    from agent.session.agent_registry import AgentRegistryV2
    from agent.session.models import SessionMode
    from agent.session.runtime import SessionRuntime
    from agent.session.session_store import SessionStore

    store = SessionStore(str(tmp_path / "sessions.db"))
    session = store.create_session(
        agent_name="build",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="Replay contract",
    )
    emitted = []

    def capture(event, *, run_context=None):
        emitted.append(event)

    runtime = SessionRuntime(
        store=store,
        backend=MockBackend([
            Action(ActionType.FINISH, thought="done", message="done"),
        ]),
        base_registry=ToolRegistry(),
        agent_registry=AgentRegistryV2(project_dir=tmp_path),
        root_agent_config=AgentConfig(max_steps=2, stream=False),
        log_dir=str(tmp_path),
        event_callback=capture,
    )
    result = runtime.run_session(
        session.id,
        agent_name="build",
        task_description="finish once",
    )

    replay_runs = [
        event for event in emitted
        if event.event_type.value == "replay_run"
    ]
    assert result.status.value == "success"
    assert len(replay_runs) == 1
    assert replay_runs[0].payload["run_id"] == session.id
    assert replay_runs[0].payload["session_id"] == session.id
    assert replay_runs[0].payload["termination_status"] == "success"
