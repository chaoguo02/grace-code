"""Plan mode tools — CC-aligned EnterPlanMode / ExitPlanMode.

These are "signal" tools: when invoked, they set a pending mode-switch
on the ToolRegistry. The main agent loop checks this flag after each
tool execution and triggers the actual mode switch.

Architecture:
  Tool.execute() → sets registry._pending_mode_switch
  main loop → checks registry._pending_mode_switch → switches agent mode
"""

from __future__ import annotations

from typing import Any

from tools.base import BaseTool, ToolMetadata, ToolResult


def _signal_mode_switch(registry: Any, new_mode: str, detail: str = "") -> str:
    """Set a pending mode switch on the registry for the main loop to pick up."""
    try:
        registry._pending_mode_switch = {"mode": new_mode, "detail": detail}
    except AttributeError:
        pass  # Registry not available; signal is best-effort
    return detail


class EnterPlanModeTool(BaseTool):
    """Switch to plan mode to design an approach before coding.

    Sets the registry's _pending_mode_switch to 'plan', which the main
    agent loop detects and triggers:
      - Agent intent switch to ANALYSIS
      - Tool restrictions to read-only
      - Plan contract enforcement on FINISH
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
            "Use this before making large-scale changes to align on approach. "
            "The next response explores and plans — no edits are made."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        msg = _signal_mode_switch(
            getattr(self, "_registry", None), "plan",
            "[EnterPlanMode] Switched to plan mode. Analysis only. "
            "Produce a JSON contract plan before making changes."
        )
        return ToolResult(success=True, output=msg or "Entered plan mode.")


class ExitPlanModeTool(BaseTool):
    """Submit a plan for approval and exit plan mode.

    Sets the registry's _pending_mode_switch to 'build', which the main
    agent loop detects and triggers:
      - Plan contract validation
      - Mode switch back to build/edit
    """

    metadata = ToolMetadata(effects=frozenset())

    @property
    def name(self) -> str:
        return "ExitPlanMode"

    @property
    def description(self) -> str:
        return (
            "Submit the current plan for user approval and exit plan mode. "
            "The plan must include a valid JSON contract. On approval, "
            "resumes normal build/edit capabilities with Execute action."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "The plan description or contract to submit",
                },
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # Restore permission mode after exiting plan (CC prePlanMode restore)
        registry = getattr(self, "_registry", None)
        if registry is not None:
            pipeline = getattr(registry, "_permission_pipeline", None)
            if pipeline is not None:
                pipeline.restore_pre_plan_mode()
        plan_text = params.get("plan", "")
        msg = _signal_mode_switch(
            registry, "build",
            f"[ExitPlanMode] Plan submitted for approval.\n\n{plan_text}"
        )
        return ToolResult(
            success=True,
            output=(
                f"Plan submitted for approval.\n\n{plan_text}\n\n"
                "Awaiting user review. The plan will be executed on approval."
            ),
        )
