from types import SimpleNamespace

from agent.session.models import (
    AgentCompletionNotification,
    AgentKind,
    AgentRunResult,
    AgentRunStatus,
    ContextOrigin,
    ExecutionPlacement,
    SessionMode,
    SessionStatus,
    WorkspaceMode,
    WorktreeDisposition,
)
from agent.session.session_store import SessionStore
from llm.base import LLMMessage
from server.services.multi_agent_service import MultiAgentService


def test_snapshot_projects_topology_delivery_and_context_without_claiming(tmp_path) -> None:
    store = SessionStore(str(tmp_path / "sessions.db"))
    root = store.create_session(
        agent_name="build",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="Root",
    )
    child = store.create_session(
        agent_name="reviewer",
        mode=SessionMode.SUBAGENT,
        agent_kind=AgentKind.NAMED_SUBAGENT,
        context_origin=ContextOrigin.FRESH,
        execution_placement=ExecutionPlacement.BACKGROUND,
        workspace_mode=WorkspaceMode.CURRENT,
        repo_path=str(tmp_path),
        title="Review changes",
        parent_id=root.id,
    )
    store.append_message(child.id, LLMMessage(role="user", content="Review it"))
    result = AgentRunResult(
        agent_name="reviewer",
        session_id=child.id,
        status=AgentRunStatus.COMPLETED,
        summary="No blocking findings",
        worktree_disposition=WorktreeDisposition.CLEANED,
    )
    store.set_agent_result(child.id, result)
    store.set_summary(child.id, result.summary, status=SessionStatus.COMPLETED)
    store.append_agent_notification(AgentCompletionNotification(
        parent_session_id=root.id,
        result=result,
    ))

    service = MultiAgentService(SimpleNamespace(_store=store))
    snapshot = service.get_snapshot(child.id)

    assert snapshot["root_session_id"] == root.id
    assert len(snapshot["nodes"]) == 2
    assert snapshot["scheduler"]["placement_counts"]["background"] == 1
    assert snapshot["communication_summary"]["pending_delivery"] == 1
    assert snapshot["communications"][-1]["source"] == "agent_notifications"
    assert snapshot["contexts"][1]["message_count"] == 1
    assert snapshot["consistency"]["state"] == "healthy"

    # The inspector is read-only: reading must not claim the pending result.
    assert store.list_agent_notifications(root.id)[0]["delivery_state"] == "pending"


def test_unknown_session_is_rejected(tmp_path) -> None:
    service = MultiAgentService(SimpleNamespace(
        _store=SessionStore(str(tmp_path / "sessions.db")),
    ))
    try:
        service.get_snapshot("missing")
    except ValueError as exc:
        assert "Unknown session" in str(exc)
    else:
        raise AssertionError("unknown session must be rejected")


def test_snapshot_projects_durable_delegation_contract(tmp_path) -> None:
    store = SessionStore(str(tmp_path / "delegations.db"))
    root = store.create_session(
        agent_name="research",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="Root",
    )
    store.create_delegation_run(
        run_id="delegation-1",
        parent_session_id=root.id,
        topology="one_to_one",
        reason_code="specialist_context",
        explanation="A bounded specialist task",
        budget={"available_tokens": 10_000},
    )
    store.create_delegation_task(
        task_id="delegation-1:inspect",
        delegation_run_id="delegation-1",
        agent_type="explore",
        purpose="exploration",
        goal="Inspect the router",
        prompt="Locate the routing boundary.",
        expected_files=("server/main.py",),
    )
    store.update_delegation_task(
        "delegation-1:inspect",
        status="completed",
        child_session_id="child-1",
        generation=2,
        report={
            "tokens_used": 321,
            "duration_ms": 45,
            "findings": [],
            "changed_files": [],
            "verification": [],
        },
    )
    store.complete_delegation_run("delegation-1", status="completed")

    service = MultiAgentService(SimpleNamespace(_store=store))
    snapshot = service.get_snapshot(root.id)

    assert snapshot["routing"]["reason_code"] == "specialist_context"
    assert snapshot["delegation_runs"][0]["required_count"] == 1
    assert snapshot["delegation_runs"][0]["completed_count"] == 1
    task = snapshot["delegation_tasks"][0]
    assert task["title"] == "Inspect the router"
    assert task["agent_name"] == "explore"
    assert task["tokens_used"] == 321
    assert task["run_id"] == "delegation-1"
