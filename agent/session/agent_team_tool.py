"""Approval-gated Agent Team proposal tool."""

from __future__ import annotations

from typing import Any

from core.base import (
    BaseTool,
    ToolConcurrency,
    ToolEffect,
    ToolMetadata,
    ToolResult,
    ToolRole,
)


class ProposeAgentTeamTool(BaseTool):
    """Let a root agent propose a team without granting activation authority."""

    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.DELEGATE_READ_ONLY}),
        roles=frozenset({ToolRole.DELEGATE}),
    )

    def __init__(
        self,
        runtime: Any,
        session_id: str,
        *,
        caller_agent_name: str,
    ) -> None:
        self._runtime = runtime
        self._session_id = session_id
        self._caller_agent_name = caller_agent_name

    @property
    def name(self) -> str:
        return "ProposeAgentTeam"

    @property
    def description(self) -> str:
        return (
            "Propose an approval-gated Agent Team for work that genuinely "
            "requires peer-to-peer messages or a shared task board. This call "
            "does not start teammates; the user must approve it in the "
            "Multi-Agent Control Plane."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        parent = self._runtime.agent_registry.get(self._caller_agent_name)
        roles = [
            child.name
            for child in self._runtime.agent_registry.delegatable_by(parent)
        ]
        return {
            "type": "object",
            "properties": {
                "members": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 7,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "role": {"type": "string", "enum": roles},
                        },
                        "required": ["id", "role"],
                    },
                },
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "goal": {"type": "string"},
                            "agent": {"type": "string", "enum": roles},
                            "purpose": {"type": "string"},
                            "dependencies": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "required": {"type": "boolean"},
                        },
                        "required": ["id", "goal"],
                    },
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why ordinary parent-mediated subagents are "
                        "insufficient for this task."
                    ),
                },
            },
            "required": ["members", "tasks", "reason"],
        }

    def concurrency_mode(self, params: dict[str, Any]) -> ToolConcurrency:
        return ToolConcurrency.SERIAL

    def execute(self, params: dict[str, Any]) -> ToolResult:
        try:
            proposal = self._runtime.propose_agent_team(
                session_id=self._session_id,
                members=list(params.get("members", [])),
                tasks=list(params.get("tasks", [])),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        return ToolResult(
            success=True,
            output=(
                "Agent Team proposal saved. No teammate has started. "
                "Tell the user why peer coordination is needed and ask them "
                "to approve or reject the proposal in the Multi-Agent "
                "Control Plane."
            ),
            metadata={
                "team_proposal": proposal,
                "reason": str(params.get("reason", "")),
                "requires_user_approval": True,
            },
        )
