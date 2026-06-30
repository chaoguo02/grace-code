from __future__ import annotations

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
            "Each child session is stateless — put ALL context in the prompt. "
            f"Available subagent types: {subagents}."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "subagent_type": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["subagent_type", "prompt"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        subagent_type = str(params.get("subagent_type") or "").strip()
        prompt = str(params.get("prompt") or "").strip()
        if not subagent_type or not prompt:
            return ToolResult(
                success=False,
                output="",
                error="task requires subagent_type and prompt",
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
            description=prompt[:60],
            prompt=prompt,
        )
        is_failure = child_result.status == "failed" and bool(child_result.error)
        output = child_result.summary
        if child_result.missing_info:
            output += f"\n\n[Note: {child_result.missing_info}]"
        return ToolResult(
            success=not is_failure,
            output=output,
            error=child_result.error if is_failure else None,
        )
