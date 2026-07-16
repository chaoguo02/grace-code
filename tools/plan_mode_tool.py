"""Plan mode tools — CC-aligned EnterPlanMode / ExitPlanMode.

Wraps the PlanApprovalService and approval interaction infrastructure
as BaseTool subclasses so the LLM can enter/exit plan mode at runtime.
"""

from __future__ import annotations

from typing import Any

from tools.base import BaseTool, ToolMetadata, ToolResult


class EnterPlanModeTool(BaseTool):
    """Switch to plan mode to design an approach before coding.

    When invoked, subsequent turns use the plan agent (read-only, structured
    output) instead of the build agent. The LLM explores code, produces a
    plan contract, and presents it for approval.
    """

    metadata = ToolMetadata(effects=frozenset())

    @property
    def name(self) -> str:
        return "EnterPlanMode"

    @property
    def description(self) -> str:
        return (
            "Switch to plan mode. The agent becomes read-only and will "
            "explore the codebase to produce a structured implementation plan. "
            "Use this before making large-scale changes to align on approach."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # The actual mode switch is handled by a PostToolUse hook or
        # runtime interceptor that detects this tool call and switches
        # the agent's intent to ANALYSIS with plan contract enforcement.
        return ToolResult(
            success=True,
            output=(
                "Entered plan mode. The next response will explore the codebase "
                "and produce a structured implementation plan. No edits will be "
                "made in plan mode."
            ),
        )


class ExitPlanModeTool(BaseTool):
    """Present a plan for approval and exit plan mode.

    When invoked after producing a plan, the plan is submitted for user
    approval. If approved, the agent switches back to build mode.
    """

    metadata = ToolMetadata(effects=frozenset())

    @property
    def name(self) -> str:
        return "ExitPlanMode"

    @property
    def description(self) -> str:
        return (
            "Submit the current plan for user approval and exit plan mode. "
            "The plan must include a valid JSON contract. If approved, "
            "the agent resumes normal build/edit capabilities."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "The plan description or implementation contract to submit for approval",
                },
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        plan_text = params.get("plan", "")
        return ToolResult(
            success=True,
            output=(
                f"Plan submitted for approval.\n\n{plan_text}\n\n"
                "Awaiting user review. The plan will be executed on approval."
            ),
        )
