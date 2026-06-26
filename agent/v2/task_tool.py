from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from tools.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from agent.v2.runtime import SessionRuntime


class TaskToolV2(BaseTool):
    def __init__(self, runtime: "SessionRuntime", parent_session_id: str) -> None:
        self._runtime = runtime
        self._parent_session_id = parent_session_id

    @property
    def name(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        subagents = ", ".join(spec.name for spec in self._runtime.agent_registry.list_subagents())
        return (
            "Create a child session and delegate a subtask to a subagent. "
            f"Available subagent types: {subagents}."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "subagent_type": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["description", "subagent_type", "prompt"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        description = str(params.get("description") or "").strip()
        subagent_type = str(params.get("subagent_type") or "").strip()
        prompt = str(params.get("prompt") or "").strip()
        if not description or not subagent_type or not prompt:
            return ToolResult(
                success=False,
                output="",
                error="task requires description, subagent_type, and prompt",
            )
        if not self._runtime.agent_registry.has(subagent_type):
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown v2 subagent_type '{subagent_type}'",
            )

        child_result = self._runtime.run_child_session(
            parent_session_id=self._parent_session_id,
            subagent_type=subagent_type,
            description=description,
            prompt=prompt,
        )
        self._runtime.apply_child_result_policy(self._parent_session_id, child_result)
        output = (
            "Structured child session result. Treat this JSON object as the authoritative child output.\n"
            f"{json.dumps(child_result.to_dict(), ensure_ascii=False, indent=2)}"
        )
        return ToolResult(
            success=child_result.status != "failed",
            output=output,
            error=child_result.error or None,
        )
