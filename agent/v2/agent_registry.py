from __future__ import annotations

from agent.core import _READONLY_TOOLS
from agent.v2.models import AgentSpec


_BUILD_ALLOWED = frozenset({
    "shell",
    "file_read",
    "file_view",
    "file_write",
    "file_edit",
    "edit",
    "search_text",
    "find_files",
    "find_symbol",
    "pytest",
    "test",
    "git_status",
    "git_diff",
    "git_add",
    "git_commit",
    "web_search",
    "web_fetch",
    "artifact_list",
    "artifact_read",
    "artifact_search",
    "evidence_list",
    "evidence_get",
    "memory_read",
    "memory_list",
    "memory_write",
    "memory_delete",
    "memory_search",
    "submit_read_plan",
})

_PLAN_ALLOWED = frozenset(_READONLY_TOOLS)
_EXPLORE_ALLOWED = frozenset(_READONLY_TOOLS)
_GENERAL_ALLOWED = _BUILD_ALLOWED


class AgentRegistryV2:
    def __init__(self) -> None:
        self._agents: dict[str, AgentSpec] = {
            "build": AgentSpec(
                name="build",
                mode="primary",
                allowed_tools=_BUILD_ALLOWED,
                allow_task_tool=True,
                description="Primary coding agent with broad tool access.",
            ),
            "plan": AgentSpec(
                name="plan",
                mode="primary",
                allowed_tools=_PLAN_ALLOWED,
                allow_task_tool=True,
                description="Read-only primary planning agent.",
            ),
            "explore": AgentSpec(
                name="explore",
                mode="subagent",
                allowed_tools=_EXPLORE_ALLOWED,
                allow_task_tool=False,
                description="Read-only child agent for exploration.",
            ),
            "general": AgentSpec(
                name="general",
                mode="subagent",
                allowed_tools=_GENERAL_ALLOWED,
                allow_task_tool=False,
                description="General child coding agent.",
            ),
        }

    def get(self, name: str) -> AgentSpec:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise KeyError(f"Unknown v2 agent: {name}") from exc

    def has(self, name: str) -> bool:
        return name in self._agents

    def list_subagents(self) -> list[AgentSpec]:
        return [
            spec for spec in self._agents.values()
            if spec.mode != "primary" and not spec.hidden
        ]

    def list_primary_agents(self) -> list[AgentSpec]:
        return [
            spec for spec in self._agents.values()
            if spec.mode == "primary" and not spec.hidden
        ]
