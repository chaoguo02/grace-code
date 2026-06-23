"""Compatibility exports for task policy enforcement."""

from __future__ import annotations

from agent.policy import (
    COMMAND_TOOLS,
    DISCOVERY_TOOLS,
    GIT_TOOLS,
    MEMORY_TOOLS,
    READ_TOOLS,
    TEST_TOOLS,
    WEB_TOOLS,
    WRITE_TOOLS,
    TaskPolicy as TaskConstraints,
    normalize_repo_path,
)
from agent.policy_registry import PolicyAwareToolRegistry as ConstraintAwareRegistry


def parse_task_constraints(description: str, intent: str, repo_path: str) -> TaskConstraints:
    from agent.policy import build_task_policy
    from agent.task import Task

    return build_task_policy(Task(description=description, repo_path=repo_path, intent=intent))
