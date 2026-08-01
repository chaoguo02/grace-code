"""Regression gates for the intentionally removed Agent Team product."""

from agent.session.runtime import SessionRuntime
from server.routers.multi_agent import create_multi_agent_router


REMOVED_RUNTIME_METHODS = {
    "propose_agent_team",
    "approve_agent_team",
    "reject_agent_team",
    "coordinate_agent_team",
    "send_team_message",
    "claim_team_task",
    "complete_team_task",
    "execute_team_task",
    "resolve_team_task_review",
    "shutdown_agent_team",
}


def test_agent_team_runtime_surface_is_absent() -> None:
    assert all(
        not hasattr(SessionRuntime, method)
        for method in REMOVED_RUNTIME_METHODS
    )


def test_agent_team_http_surface_is_absent() -> None:
    router = create_multi_agent_router(lambda: None)
    paths = {route.path for route in router.routes}
    assert not any("/team" in path for path in paths)

