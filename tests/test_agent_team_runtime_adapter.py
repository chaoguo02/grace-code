"""SessionRuntime adapter coverage for approval-gated Agent Teams."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _runtime_adapter(tmp_path: Path):
    from agent.session.agent_registry import AgentRegistryV2
    from agent.session.models import SessionMode
    from agent.session.session_store import SessionStore

    store = SessionStore(tmp_path / "teams.db")
    root = store.create_session(
        agent_name="research",
        mode=SessionMode.PRIMARY,
        repo_path=str(Path.cwd()),
        title="team adapter test",
    )
    runtime = SimpleNamespace(
        _store=store,
        _agent_registry=AgentRegistryV2(Path.cwd()),
        _teams={},
        _team_proposals={},
    )
    return runtime, store, root


def test_team_proposal_requires_approval_and_orders_dependency_dag(
    tmp_path, monkeypatch,
):
    from agent.session.runtime import SessionRuntime

    monkeypatch.setenv("GRACE_AGENT_TEAMS_ENABLED", "1")
    runtime, store, root = _runtime_adapter(tmp_path)

    proposal = SessionRuntime.propose_agent_team(
        runtime,
        session_id=root.id,
        members=[
            {"id": "investigator", "role": "explore"},
            {"id": "reviewer", "role": "code-reviewer"},
        ],
        # Deliberately reverse dependency order at the API boundary.
        tasks=[
            {
                "id": "review",
                "goal": "Review the evidence",
                "dependencies": ["inspect"],
                "agent": "code-reviewer",
            },
            {
                "id": "inspect",
                "goal": "Collect evidence",
                "agent": "explore",
            },
        ],
    )

    assert proposal["state"] == "awaiting_approval"
    assert store.list_delegation_runs(root.id) == []
    assert [
        task["id"] for task in runtime._team_proposals[root.id]["tasks"]
    ] == ["inspect", "review"]

    activated = SessionRuntime.approve_agent_team(
        runtime,
        session_id=root.id,
    )
    assert activated["state"] == "active"
    runs = store.list_delegation_runs(root.id)
    assert len(runs) == 1
    assert runs[0]["is_team"] is True
    tasks = store.list_delegation_tasks(str(runs[0]["id"]))
    assert len(tasks) == 2
    assert tasks[1]["dependencies"] == [tasks[0]["id"]]


def test_team_proposal_rejects_invalid_role_and_dependency_cycle(
    tmp_path, monkeypatch,
):
    from agent.session.runtime import SessionRuntime

    monkeypatch.setenv("GRACE_AGENT_TEAMS_ENABLED", "1")
    runtime, _store, root = _runtime_adapter(tmp_path)

    try:
        SessionRuntime.propose_agent_team(
            runtime,
            session_id=root.id,
            members=[{"id": "worker", "role": "general"}],
            tasks=[{"id": "one", "goal": "Do work"}],
        )
    except ValueError as exc:
        assert "delegatable agent definitions" in str(exc)
    else:
        raise AssertionError("invalid teammate role should be rejected")

    try:
        SessionRuntime.propose_agent_team(
            runtime,
            session_id=root.id,
            members=[{"id": "worker", "role": "explore"}],
            tasks=[
                {"id": "one", "goal": "One", "dependencies": ["two"]},
                {"id": "two", "goal": "Two", "dependencies": ["one"]},
            ],
        )
    except ValueError as exc:
        assert "dependency cycle" in str(exc)
    else:
        raise AssertionError("cyclic team task graph should be rejected")


def test_team_proposal_can_be_rejected_before_any_run_is_created(
    tmp_path, monkeypatch,
):
    from agent.session.runtime import SessionRuntime

    monkeypatch.setenv("GRACE_AGENT_TEAMS_ENABLED", "1")
    runtime, store, root = _runtime_adapter(tmp_path)
    SessionRuntime.propose_agent_team(
        runtime,
        session_id=root.id,
        members=[{"id": "worker", "role": "explore"}],
        tasks=[{"id": "inspect", "goal": "Inspect"}],
    )

    rejected = SessionRuntime.reject_agent_team(
        runtime,
        session_id=root.id,
    )

    assert rejected["state"] == "cancelled"
    assert root.id not in runtime._team_proposals
    assert store.list_delegation_runs(root.id) == []


def test_approved_teammates_can_use_direct_mailbox_and_shared_board(
    tmp_path, monkeypatch,
):
    from agent.session.models import SessionMode
    from agent.session.runtime import SessionRuntime

    monkeypatch.setenv("GRACE_AGENT_TEAMS_ENABLED", "1")
    runtime, store, root = _runtime_adapter(tmp_path)
    SessionRuntime.propose_agent_team(
        runtime,
        session_id=root.id,
        members=[
            {"id": "investigator", "role": "explore"},
            {"id": "reviewer", "role": "code-reviewer"},
        ],
        tasks=[{"id": "inspect", "goal": "Inspect"}],
    )
    activated = SessionRuntime.approve_agent_team(
        runtime,
        session_id=root.id,
    )
    team_id = str(activated["team_id"])
    investigator = store.create_session(
        agent_name="explore",
        mode=SessionMode.SUBAGENT,
        repo_path=str(Path.cwd()),
        title="investigator",
        parent_id=root.id,
        metadata={
            "team_id": team_id,
            "team_member_id": "investigator",
        },
    )
    reviewer = store.create_session(
        agent_name="code-reviewer",
        mode=SessionMode.SUBAGENT,
        repo_path=str(Path.cwd()),
        title="reviewer",
        parent_id=root.id,
        metadata={
            "team_id": team_id,
            "team_member_id": "reviewer",
        },
    )

    sent = SessionRuntime.coordinate_agent_team(
        runtime,
        session_id=investigator.id,
        action="send",
        recipient_id="reviewer",
        message="Please challenge this finding.",
    )
    inbox = SessionRuntime.coordinate_agent_team(
        runtime,
        session_id=reviewer.id,
        action="inbox",
    )
    board = SessionRuntime.coordinate_agent_team(
        runtime,
        session_id=reviewer.id,
        action="board",
    )

    assert sent["action"] == "sent"
    assert inbox["messages"][0]["sender_id"] == "investigator"
    assert inbox["messages"][0]["body"] == "Please challenge this finding."
    assert board["tasks"][0]["id"] == "inspect"
