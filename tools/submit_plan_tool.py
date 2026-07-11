"""Submit read plan tool for phased analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from context.read_plan import ReadPlan, ReadPlanItem
from tools.base import BaseTool, ToolResult


@dataclass
class SubmitReadPlanRef:
    """Mutable ref binding the submit_read_plan tool to the agent core."""

    pending_plan: ReadPlan | None = None
    last_error: str | None = None
    repo_path: str = ""
    task_id: str = ""


class SubmitReadPlanTool(BaseTool):
    is_read_only = True
    """Agent calls this tool to submit a structured read plan during plan_reads phase."""

    def __init__(self, ref: SubmitReadPlanRef) -> None:
        self._ref = ref

    @property
    def name(self) -> str:
        return "submit_read_plan"

    @property
    def description(self) -> str:
        return (
            "Submit a structured read plan for the analysis task. "
            "Call this after exploring the codebase with discovery tools "
            "(find_files, search_text, find_symbol) to decide which files to read. "
            "The plan specifies which files to read, why, and when to stop."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "subsystem": {
                    "type": "string",
                    "description": "Name of the subsystem being analyzed (e.g. 'auth-middleware', 'evidence-lifecycle')",
                },
                "stop_condition": {
                    "type": "string",
                    "description": "When to stop reading and begin synthesis (e.g. 'after reading all 4 core files')",
                },
                "items": {
                    "type": "array",
                    "description": "Files to read, with reasons and priorities",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "File path relative to repo root",
                            },
                            "reason": {
                                "type": "string",
                                "description": "Why this file needs to be read",
                            },
                            "closes_gap": {
                                "type": "string",
                                "description": "What knowledge gap reading this file closes",
                            },
                            "priority": {
                                "type": "integer",
                                "description": "Read priority (1=highest)",
                                "default": 1,
                            },
                            "max_ranges": {
                                "type": "integer",
                                "description": "Max number of range reads allowed for this file",
                                "default": 1,
                            },
                        },
                        "required": ["path", "reason", "closes_gap"],
                    },
                    "minItems": 1,
                },
            },
            "required": ["subsystem", "stop_condition", "items"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        try:
            plan = self._validate_and_build_plan(params)
            self._ref.pending_plan = plan
            self._ref.last_error = None
            item_summary = ", ".join(item.path for item in plan.items[:5])
            if len(plan.items) > 5:
                item_summary += f", ... ({len(plan.items)} total)"
            return ToolResult(
                success=True,
                output=(
                    f"Read plan accepted: {len(plan.items)} files to read.\n"
                    f"Subsystem: {plan.subsystem}\n"
                    f"Files: {item_summary}\n"
                    f"Stop condition: {plan.stop_condition}\n"
                    "Proceeding to inspect phase — you can now read the planned files."
                ),
            )
        except (ValueError, KeyError, TypeError) as exc:
            self._ref.pending_plan = None
            self._ref.last_error = str(exc)
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid read plan: {exc}",
            )

    def _validate_and_build_plan(self, params: dict[str, Any]) -> ReadPlan:
        subsystem = str(params.get("subsystem", "")).strip()
        if not subsystem:
            raise ValueError("subsystem is required")

        stop_condition = str(params.get("stop_condition", "")).strip()
        if not stop_condition:
            raise ValueError("stop_condition is required")

        raw_items = params.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("items must be a non-empty array")

        items: list[ReadPlanItem] = []
        for index, item in enumerate(raw_items, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"item {index} must be an object")
            path = str(item.get("path", "")).strip()
            reason = str(item.get("reason", "")).strip()
            closes_gap = str(item.get("closes_gap", "")).strip()
            if not path:
                raise ValueError(f"item {index} is missing path")
            if not reason:
                raise ValueError(f"item {index} is missing reason")
            if not closes_gap:
                raise ValueError(f"item {index} is missing closes_gap")
            priority = int(item.get("priority", index))
            max_ranges = max(1, int(item.get("max_ranges", 1)))
            items.append(ReadPlanItem(
                path=path,
                reason=reason,
                closes_gap=closes_gap,
                priority=priority,
                max_ranges=max_ranges,
            ))

        task_id = self._ref.task_id or "unknown"
        return ReadPlan(
            task_id=task_id,
            subsystem=subsystem,
            items=sorted(items, key=lambda item: (item.priority, item.path)),
            stop_condition=stop_condition,
            approved=True,
        )
