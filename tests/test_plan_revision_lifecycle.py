from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.session.models import SessionMode, SessionStatus
from agent.task import RunResult, RunStatus
from app.storage.sqlite import SqliteStorageBackend
from application.coordinators.run_coordinator import RunCoordinator
from application.events.schema_registry import SchemaRegistry
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.sqlite.run_uow import SqliteUnitOfWork
from runtime_core.ports import (
    RuntimePorts, ToolSuccess, HookGateResult,
)
from runtime_core.model_actions import ModelAction
from runtime_core.runtime import AgentRuntime
from server.routers.approvals import (
    _transition_plan_metadata,
    create_approvals_router,
)
from server.services.chat_pipeline import ChatPipeline, ChatPipelinePorts, ChatRequest
from server.services.event_outbox import OutboxStore
from server.services.plan_revision_service import PlanRevisionService
from server.services.session_service import SessionService


# ── Fake ports + real coordinator (mirrors tests/test_run_submission.py) ──────

class _FakeLLM:
    def invoke(self, messages, tools=None, tool_choice=None):
        return ModelAction.stop(reason="test")


class _FakeTools:
    def execute(self, tool_name, params, invocation_id=""):
        return ToolSuccess(tool_name=tool_name)


class _FakeHooks:
    def check(self, event_type, hook_input, tool_name=""):
        return HookGateResult(allowed=True)


class _FakeLiveEvents:
    def publish(self, event_type, payload, scope=None):
        pass


class _FakeClock:
    def now(self):
        import time
        return time.monotonic()

    def deadline(self, timeout_s):
        return self.now() + timeout_s


class _FakeTokenUsage:
    def record(self, run_id, input_tokens, output_tokens):
        pass


def _make_coordinator(db_path: str):
    """Create a real RunCoordinator backed by the same test DB as storage."""
    old_outbox = OutboxStore(db_path)
    old_outbox.install()
    conn = sqlite3.connect(db_path)
    SqliteOutboxStore.migrate_add_columns(conn)
    conn.commit()
    conn.close()

    registry = SchemaRegistry()
    outbox_store = SqliteOutboxStore(db_path, registry)
    ports = RuntimePorts(
        llm=_FakeLLM(), tools=_FakeTools(), hooks=_FakeHooks(),
        live_events=_FakeLiveEvents(), clock=_FakeClock(),
        token_usage=_FakeTokenUsage(),
    )
    runtime = AgentRuntime(ports)
    uow = SqliteUnitOfWork(db_path, outbox_store)
    return RunCoordinator(runtime, uow)


def _setup(tmp_path):
    storage = SqliteStorageBackend(str(tmp_path / "sessions.db"))
    session = storage.create_session(
        agent_name="plan",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="Plan",
    )
    revisions = PlanRevisionService(storage)
    service = SimpleNamespace(
        _storage=storage,
        _plan_revisions=revisions,
    )
    return storage, session, revisions, service


def test_save_transition_preserves_revision_and_contract(tmp_path):
    storage, session, revisions, service = _setup(tmp_path)
    revisions.append_revision(session.id, "Plan one")
    storage.update_metadata(session.id, {
        "plan_revision": 1,
        "plan_contract": {"goal": "Keep me"},
        "plan_approved_at": "stale",
    })

    _transition_plan_metadata(
        service,
        session.id,
        marker="plan_saved_at",
        revision=1,
        clear_contract=False,
    )

    updated = storage.get_session(session.id)
    assert updated.metadata["plan_revision"] == 1
    assert updated.metadata["plan_contract"] == {"goal": "Keep me"}
    assert "plan_saved_at" in updated.metadata
    assert "plan_approved_at" not in updated.metadata


def test_replanned_result_is_appended_after_rejection(tmp_path):
    storage, session, revisions, _ = _setup(tmp_path)
    revisions.append_revision(session.id, "Old plan")
    revisions.mark_status(session.id, 1, "rejected")
    storage.update_metadata(session.id, {"plan_revision": 1})
    session_service = SessionService(storage)
    ports = ChatPipelinePorts(
        runtime=MagicMock(),
        session_service=session_service,
        backend=MagicMock(),
        config=None,
        effective_llm_config={},
        repo_path=str(tmp_path),
        build_confirm_callback=MagicMock(),
        reload_rules=MagicMock(),
        loaded_rules=MagicMock(return_value=[]),
        accumulate_session_stats=MagicMock(),
        compact_session_async=MagicMock(),
        plan_revisions=revisions,
    )
    pipeline = ChatPipeline(ports)

    pipeline.finish(
        ChatRequest(
            session_id=session.id,
            prompt="Revise",
            agent_name="plan",
        ),
        RunResult(
            task_id=session.id,
            status=RunStatus.SUCCESS,
            summary="New revised plan",
            steps_taken=1,
        ),
    )

    stored = revisions.list_revisions(session.id)
    assert [(row["revision"], row["status"], row["content"]) for row in stored] == [
        (1, "rejected", "Old plan"),
        (2, "pending", "New revised plan"),
    ]
    assert storage.get_session(session.id).metadata["plan_revision"] == 2


def test_reject_endpoint_returns_typed_response_and_reuses_run(tmp_path):
    storage, session, revisions, _ = _setup(tmp_path)
    storage.set_summary(
        session.id,
        "Plan one",
        status=SessionStatus.COMPLETED,
    )
    revisions.append_revision(session.id, "Plan one")
    storage.update_metadata(session.id, {"plan_revision": 1})

    service = SimpleNamespace(
        _storage=storage,
        _plan_revisions=revisions,
        _event_bus=SimpleNamespace(create_session=AsyncMock()),
        session_service=SessionService(storage),
        run_chat_async=MagicMock(),
        repo_path=str(tmp_path),
        _native_components=SimpleNamespace(
            run_coordinator=_make_coordinator(str(tmp_path / "sessions.db")),
        ),
    )
    app = FastAPI()
    app.include_router(create_approvals_router(lambda: service))
    client = TestClient(app)

    first = client.post(
        f"/api/sessions/{session.id}/reject",
        json={"reason": "Add rollback steps"},
    )
    second = client.post(
        f"/api/sessions/{session.id}/reject",
        json={"reason": "Add rollback steps"},
    )

    assert first.status_code == 200
    payload = first.json()
    assert payload["approved"] is False
    assert payload["status"] == "running"
    assert payload["run_id"]
    assert payload["turn_id"]
    assert payload["turn_index"] == 1
    assert second.status_code == 200
    assert second.json()["run_id"] == payload["run_id"]
    service.run_chat_async.assert_called_once()
    context = service.run_chat_async.call_args.kwargs["run_context"]
    assert context.run_id == payload["run_id"]
    assert context.turn_id == payload["turn_id"]
    assert len(storage.list_runs(session.id)) == 1
    assert len(storage.list_messages(session.id)) == 1
