"""Plan mode tools — CC-aligned EnterPlanMode / ExitPlanMode.

These are "signal" tools: when invoked, they set a pending mode-switch
on the ToolRegistry. The main agent loop checks this flag after each
tool execution and triggers the actual mode switch.

Architecture:
  Tool.execute() → sets registry._pending_mode_switch
  main loop → checks registry._pending_mode_switch → switches agent mode

ExitPlanMode now accepts a structured ``contract`` JSON object that is
stored in the tool result metadata and consumed by the plan_ready event
without any regex parsing.

CC-aligned additions:
  - Subagent guard: subagents cannot enter plan mode (no user to approve)
  - Re-entry guidance: when a plan file already exists, injected into output
  - Unified handle_plan_mode_transition() for entry/exit
  - ExitPlanMode writes the plan file to disk automatically
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.base import BaseTool, ToolMetadata, ToolResult, ToolError, ToolErrorType

logger = logging.getLogger(__name__)


def _signal_mode_switch(registry: Any, new_mode: str, detail: str = "") -> str:
    """Set a pending mode switch on the registry for the main loop to pick up."""
    try:
        registry._pending_mode_switch = {"mode": new_mode, "detail": detail}
    except AttributeError:
        pass  # Registry not available; signal is best-effort
    return detail


def _build_plan_markdown(contract: dict[str, Any], summary: str = "") -> str:
    """Build YAML frontmatter + markdown body for plan file."""
    goal = contract.get("goal", "")
    steps = contract.get("steps", [])
    target_files = contract.get("target_files", [])
    verification = contract.get("verification", "")
    risks = contract.get("risks", [])

    yaml_lines = ["---"]
    if goal:
        yaml_lines.append(f"goal: {goal}")
    if steps:
        yaml_lines.append("steps:")
        for s in steps:
            yaml_lines.append(f"  - {s}")
    if target_files:
        yaml_lines.append("target_files:")
        for f in target_files:
            yaml_lines.append(f"  - {f}")
    if verification:
        yaml_lines.append(f"verification: {verification}")
    yaml_lines.append("---")
    yaml_lines.append("")
    if summary:
        yaml_lines.append(summary)
    return "\n".join(yaml_lines)


class EnterPlanModeTool(BaseTool):
    """Switch to plan mode to design an approach before coding.

    Sets the registry's _pending_mode_switch to 'plan', which the main
    agent loop detects and triggers:
      - Agent intent switch to ANALYSIS
      - Tool restrictions to read-only
      - Plan contract enforcement on FINISH
    """

    metadata = ToolMetadata(effects=frozenset())

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return True  # Signal tool, no I/O

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
        registry = getattr(self, "_registry", None)

        # SUBAGENT GUARD: Plan mode requires user interaction.
        # Subagents cannot enter plan mode — they lack a user to approve plans.
        if registry is not None:
            pipeline = getattr(registry, "_permission_pipeline", None)
            requesting_agent = getattr(pipeline, "_requesting_agent", "") if pipeline else ""
            if requesting_agent:
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        "Sub-agents cannot enter Plan Mode. "
                        "Plan Mode requires direct user interaction for approval. "
                        "Continue with your assigned task without entering plan mode."
                    ),
                    tool_error=ToolError(
                        error_type=ToolErrorType.PERMISSION_DENIED,
                        detail="Sub-agent cannot enter plan mode",
                    ),
                )

        # RE-ENTRY DETECTION: Check if a plan already exists for this session
        re_entry_guidance = ""
        if registry is not None:
            session_id = getattr(registry, "_session_id", "")
            repo_path = getattr(registry, "_repo_path", "")
            if session_id and repo_path:
                plan_file = Path(repo_path) / ".grace" / "plans" / f"{session_id}.md"
                if plan_file.is_file():
                    re_entry_guidance = (
                        "\n\n[RE-ENTRY] A plan file already exists for this session. "
                        "You are re-entering plan mode.\n"
                        "1. Read the existing plan file: Read .grace/plans/{slug}.md\n"
                        "2. Evaluate the user's request against the existing plan:\n"
                        "   - Different task? Overwrite the plan.\n"
                        "   - Same task, needs changes? Modify the plan.\n"
                        "   - Same task, no changes? Proceed to ExitPlanMode.\n"
                        "3. ALWAYS edit the plan file before calling ExitPlanMode.\n"
                        "4. Do NOT assume the old plan is still valid without reviewing it."
                    ).format(slug=session_id)

        # Signal the mode switch. The main loop picks up _pending_mode_switch
        # via check_pending_mode_switch() which calls _apply_mode_to_pipeline()
        # → save_pre_plan_mode() BEFORE set_permission_mode("plan").
        # Do NOT call handle_plan_mode_transition() here directly — it would
        # cause a double save (once here, once in check_pending_mode_switch)
        # and the second save would overwrite the saved prePlanMode.
        msg = _signal_mode_switch(
            registry, "plan",
            "[EnterPlanMode] Switched to plan mode. Analysis only. "
            "Produce a JSON contract plan before making changes."
            + re_entry_guidance
        )
        return ToolResult(success=True, output=msg or "Entered plan mode.")


class ExitPlanModeTool(BaseTool):
    """Submit a plan for approval and exit plan mode.

    Accepts a structured ``contract`` JSON object with fields like
    ``goal``, ``steps``, ``target_files``, ``verification``, ``risks``.
    The contract is stored in the tool result metadata and surfaced
    in the plan_ready WS event — no regex parsing needed.

    CC-aligned: Writes the plan file to .grace/plans/ automatically
    so the agent doesn't need Write access in plan mode.
    """

    metadata = ToolMetadata(effects=frozenset())

    @property
    def name(self) -> str:
        return "ExitPlanMode"

    @property
    def description(self) -> str:
        return (
            "Submit the current plan for user approval and exit plan mode. "
            "Provide a structured ``contract`` JSON object with: "
            "goal (string, required), steps (array of strings), "
            "target_files (array of file paths), verification (string), "
            "risks (array of strings, optional). "
            "Optionally include ``allowedPrompts`` to pre-approve tool calls "
            "during build execution."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "contract": {
                    "type": "object",
                    "description": (
                        "Structured plan contract. Required fields: "
                        "goal (string), steps (array of strings). "
                        "Optional: target_files, verification, risks, summary."
                    ),
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "One-sentence goal of the plan",
                        },
                        "steps": {
                            "type": "array",
                            "description": "Ordered implementation steps",
                            "items": {"type": "string"},
                        },
                        "target_files": {
                            "type": "array",
                            "description": "Files that will be created or modified",
                            "items": {"type": "string"},
                        },
                        "verification": {
                            "type": "string",
                            "description": "How to verify the plan was executed correctly",
                        },
                        "risks": {
                            "type": "array",
                            "description": "Potential risks or conflicts",
                            "items": {"type": "string"},
                        },
                        "summary": {
                            "type": "string",
                            "description": "Human-readable plan summary for the approval UI",
                        },
                    },
                    "required": ["goal", "steps"],
                },
                "allowedPrompts": {
                    "type": "array",
                    "description": (
                        "Optional tool-call patterns to pre-approve for the build "
                        "session. Each entry: {tool: 'Bash', prompt: 'run unit tests'}. "
                        "After plan approval, matching tool calls skip interactive confirm."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "description": "Tool name (Bash, Write, Edit, etc.)",
                            },
                            "prompt": {
                                "type": "string",
                                "description": "Natural-language description of intended use",
                            },
                        },
                        "required": ["tool", "prompt"],
                    },
                },
            },
            "required": ["contract"],
        }

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return True  # Signal tool, no I/O (plan file write is internal)

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # Restore permission mode after exiting plan (CC prePlanMode restore)
        registry = getattr(self, "_registry", None)
        if registry is not None:
            pipeline = getattr(registry, "_permission_pipeline", None)
            if pipeline is not None:
                pipeline.restore_pre_plan_mode()

        # CC-aligned prompt-based permissions: register pre-approved tool calls
        allowed_prompts = params.get("allowedPrompts", [])
        if allowed_prompts and registry is not None:
            pipeline = getattr(registry, "_permission_pipeline", None)
            if pipeline is not None:
                pipeline.add_approved_prompts(allowed_prompts)

        contract = params.get("contract", {})
        summary = contract.get("summary", "") or contract.get("goal", "")

        # CC-aligned: Write plan file to disk from within ExitPlanMode.
        # This eliminates the need for Write tool access in plan mode.
        if registry is not None:
            session_id = getattr(registry, "_session_id", "")
            repo_path = getattr(registry, "_repo_path", "")
            if session_id and repo_path:
                try:
                    plan_dir = Path(repo_path) / ".grace" / "plans"
                    plan_dir.mkdir(parents=True, exist_ok=True)
                    plan_file = plan_dir / f"{session_id}.md"
                    plan_content = _build_plan_markdown(contract, summary)
                    plan_file.write_text(plan_content, encoding="utf-8")
                    logger.info("Plan file written by ExitPlanMode: %s", plan_file)
                except Exception as exc:
                    logger.warning("ExitPlanMode could not write plan file: %s", exc)

        # Signal mode switch to build (exit plan)
        msg = _signal_mode_switch(
            registry, "build",
            f"[ExitPlanMode] Plan submitted for approval: {summary}"
        )
        return ToolResult(
            success=True,
            output=(
                f"Plan submitted for approval.\n\n"
                f"Goal: {contract.get('goal', '(not specified)')}\n"
                f"Steps: {len(contract.get('steps', []))} step(s)\n"
                f"Files: {', '.join(contract.get('target_files', [])) or '(none specified)'}\n\n"
                "Awaiting user review. The plan will be executed on approval."
            ),
            metadata={
                "plan_contract": dict(contract, **(
                    {"allowed_prompts": allowed_prompts} if allowed_prompts else {}
                )),
                "allowed_prompts": allowed_prompts,
            },
        )
