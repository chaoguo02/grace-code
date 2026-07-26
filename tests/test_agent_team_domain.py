import pytest

from agent.team import (
    BoardTask,
    BoardTaskState,
    LeaseManager,
    MemberState,
    TaskBoard,
    TeamFeatureConfig,
    TeamRuntime,
    TeamState,
)


def test_team_is_feature_gated_and_requires_approval():
    with pytest.raises(RuntimeError, match="disabled"):
        TeamRuntime(
            team_id="t",
            lead_id="lead",
            config=TeamFeatureConfig(),
        )

    runtime = TeamRuntime(
        team_id="t",
        lead_id="lead",
        config=TeamFeatureConfig(enabled=True),
    )
    assert runtime.state is TeamState.AWAITING_APPROVAL
    with pytest.raises(RuntimeError, match="before activation"):
        runtime.add_member("reviewer", "review")

    runtime.approve()
    runtime.add_member("reviewer", "review")
    runtime.activate()
    runtime.set_member_state("reviewer", MemberState.WORKING)
    assert runtime.state is TeamState.ACTIVE
    assert runtime.members[1].state is MemberState.WORKING


def test_task_board_dependencies_and_exclusive_leases():
    clock = [10.0]
    leases = LeaseManager(clock=lambda: clock[0])
    board = TaskBoard(leases, lease_ttl_seconds=5)
    board.add(BoardTask("inspect", "Inspect"))
    board.add(BoardTask("review", "Review", dependencies=("inspect",)))

    first = board.claim("inspect", "a")
    assert first is not None
    assert board.claim("inspect", "b") is None
    task, lease = first
    assert task.state is BoardTaskState.CLAIMED
    board.complete("inspect", "a", lease.token, "done")
    assert board.get("review").state is BoardTaskState.READY

    claimed = board.claim("review", "b")
    assert claimed is not None
    clock[0] = 16.0
    reclaimed = board.claim("review", "c")
    assert reclaimed is not None
    assert reclaimed[0].assignee_id == "c"


def test_mailbox_is_direct_bounded_and_members_only():
    runtime = TeamRuntime(
        team_id="t",
        lead_id="lead",
        config=TeamFeatureConfig(enabled=True),
        user_approved=True,
    )
    runtime.add_member("peer", "reviewer")
    runtime.activate()
    sent = runtime.mailbox.send("lead", "peer", "Check this evidence")
    assert runtime.mailbox.pending_count("peer") == 1
    assert runtime.mailbox.receive("peer") == (sent,)
    with pytest.raises(PermissionError):
        runtime.mailbox.send("outsider", "peer", "hello")


def test_team_refuses_clean_shutdown_with_unfinished_tasks():
    runtime = TeamRuntime(
        team_id="t",
        lead_id="lead",
        config=TeamFeatureConfig(enabled=True),
        user_approved=True,
    )
    runtime.add_member("peer", "reviewer")
    runtime.task_board.add(BoardTask("review", "Review"))
    runtime.activate()
    with pytest.raises(RuntimeError, match="unfinished"):
        runtime.shutdown()
    runtime.shutdown(cancel=True)
    assert runtime.state is TeamState.CANCELLED
    assert all(member.state is MemberState.STOPPED for member in runtime.members)


def test_pending_team_can_be_rejected_without_activation():
    runtime = TeamRuntime(
        team_id="t",
        lead_id="lead",
        config=TeamFeatureConfig(enabled=True),
    )

    runtime.reject()

    assert runtime.state is TeamState.CANCELLED
    assert runtime.members[0].state is MemberState.STOPPED
    with pytest.raises(RuntimeError, match="before activation"):
        runtime.add_member("peer", "reviewer")


def test_team_environment_limits_are_bounded_and_approval_stays_required():
    config = TeamFeatureConfig.from_environment({
        "GRACE_AGENT_TEAMS_ENABLED": "true",
        "GRACE_AGENT_TEAM_MAX_MEMBERS": "99",
        "GRACE_AGENT_TEAM_MAX_TASKS": "0",
        "GRACE_AGENT_TEAM_LEASE_TTL_SECONDS": "2",
    })

    assert config.enabled is True
    assert config.require_user_approval is True
    assert config.max_members == 8
    assert config.max_tasks == 1
    assert config.lease_ttl_seconds == 10.0
