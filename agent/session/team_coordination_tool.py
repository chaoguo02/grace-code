"""Runtime-bound peer coordination for approved Agent Team members."""

from __future__ import annotations

import json
from typing import Any

from core.base import (
    BaseTool,
    ToolConcurrency,
    ToolEffect,
    ToolMetadata,
    ToolResult,
)


class TeamCoordinateTool(BaseTool):
    """Expose only the mailbox and task board of the caller's approved team."""

    metadata = ToolMetadata(effects=frozenset({
        ToolEffect.READ_AGENT_STATE,
        ToolEffect.WRITE_AGENT_STATE,
    }))

    def __init__(self, runtime: Any, session_id: str) -> None:
        self._runtime = runtime
        self._session_id = session_id

    @property
    def name(self) -> str:
        return "TeamCoordinate"

    @property
    def description(self) -> str:
        return (
            "Coordinate within your approved Agent Team. Send a bounded peer "
            "message, receive your pending mailbox, or inspect the shared task "
            "board. This tool is unavailable to ordinary subagents."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["send", "inbox", "board"],
                },
                "recipient_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["action"],
        }

    def concurrency_mode(self, params: dict[str, Any]) -> ToolConcurrency:
        return ToolConcurrency.SERIAL

    def execute(self, params: dict[str, Any]) -> ToolResult:
        try:
            result = self._runtime.coordinate_agent_team(
                session_id=self._session_id,
                action=str(params.get("action", "")),
                recipient_id=str(params.get("recipient_id", "")),
                message=str(params.get("message", "")),
            )
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        return ToolResult(
            success=True,
            output=json.dumps(result, ensure_ascii=False, sort_keys=True),
            data=result,
        )
