import pytest

from agent.session.subagent_router import RouterPolicy, SubagentRouter
from agent.session.task_shape import TaskPurpose, WorkItem
from agent.task import TaskIntent


def test_router_selects_specialists_and_auditable_fallback():
    router = SubagentRouter()
    policy = RouterPolicy(
        parent_intent=TaskIntent.ANALYSIS,
        allowed_agents=frozenset({"security-reviewer", "explore"}),
        available_agents=frozenset({"security-reviewer", "explore"}),
    )
    route = router.route(
        WorkItem("security", "Review auth", "server"),
        TaskPurpose.SECURITY,
        policy,
    )
    assert route.agent_name == "security-reviewer"
    assert route.reason_code == "purpose_match"

    fallback = router.route(
        WorkItem("review", "Review code", "server"),
        TaskPurpose.REVIEW,
        RouterPolicy(
            parent_intent=TaskIntent.ANALYSIS,
            allowed_agents=frozenset({"code-reviewer", "explore"}),
            available_agents=frozenset({"explore"}),
        ),
    )
    assert fallback.agent_name == "explore"
    assert fallback.reason_code == "specialist_unavailable_fallback"


def test_router_enforces_parent_authority_and_allowlist():
    router = SubagentRouter()
    item = WorkItem("edit", "Edit code", "server")
    with pytest.raises(PermissionError, match="analysis parent"):
        router.route(
            item,
            TaskPurpose.IMPLEMENTATION,
            RouterPolicy(
                parent_intent=TaskIntent.ANALYSIS,
                allowed_agents=frozenset({"general"}),
                available_agents=frozenset({"general"}),
            ),
        )

    with pytest.raises(PermissionError, match="allowlist"):
        router.route(
            WorkItem("debug", "Debug", "server"),
            TaskPurpose.DEBUGGING,
            RouterPolicy(
                parent_intent=TaskIntent.EDIT,
                allowed_agents=frozenset({"explore"}),
                available_agents=frozenset({"debugger", "explore"}),
            ),
        )

