"""Scenario-to-specialist routing without an extra model call."""

from __future__ import annotations

from dataclasses import dataclass

from agent.session.task_shape import TaskPurpose, WorkItem
from agent.task import TaskIntent


_EDIT_AGENTS = frozenset({"general"})


@dataclass(frozen=True)
class AgentRoute:
    work_item_id: str
    agent_name: str
    reason_code: str
    explanation: str


@dataclass(frozen=True)
class RouterPolicy:
    parent_intent: TaskIntent
    allowed_agents: frozenset[str]
    available_agents: frozenset[str]
    edit_agents: frozenset[str] = _EDIT_AGENTS

    def __post_init__(self) -> None:
        object.__setattr__(self, "parent_intent", TaskIntent(self.parent_intent))


_PURPOSE_AGENTS: dict[TaskPurpose, str] = {
    TaskPurpose.EXPLORATION: "explore",
    TaskPurpose.PLANNING: "plan-researcher",
    TaskPurpose.DEBUGGING: "debugger",
    TaskPurpose.IMPLEMENTATION: "general",
    TaskPurpose.REVIEW: "code-reviewer",
    TaskPurpose.VERIFICATION: "test-runner",
    TaskPurpose.SECURITY: "security-reviewer",
    TaskPurpose.GENERAL: "general",
}

class SubagentRouter:
    def route(
        self,
        item: WorkItem,
        purpose: TaskPurpose,
        policy: RouterPolicy,
    ) -> AgentRoute:
        purpose = TaskPurpose(purpose)
        preferred = item.candidate_agent or _PURPOSE_AGENTS[purpose]
        reason = "candidate_agent" if item.candidate_agent else "purpose_match"
        if preferred in policy.edit_agents and policy.parent_intent is TaskIntent.ANALYSIS:
            raise PermissionError("analysis parent cannot route work to an edit agent")
        if preferred not in policy.allowed_agents:
            raise PermissionError(f"agent {preferred!r} is not in the parent allowlist")
        if preferred not in policy.available_agents:
            fallback = self._fallback(purpose, policy)
            if fallback is None:
                raise LookupError(f"agent {preferred!r} is not available")
            preferred = fallback
            reason = "specialist_unavailable_fallback"
        return AgentRoute(
            work_item_id=item.id,
            agent_name=preferred,
            reason_code=reason,
            explanation=f"Route {item.id!r} to {preferred!r} for {purpose.value}.",
        )

    @staticmethod
    def _fallback(purpose: TaskPurpose, policy: RouterPolicy) -> str | None:
        candidates = (
            ("explore", "code-reviewer")
            if purpose is not TaskPurpose.IMPLEMENTATION
            else ("general",)
        )
        return next(
            (
                name
                for name in candidates
                if name in policy.allowed_agents and name in policy.available_agents
            ),
            None,
        )
