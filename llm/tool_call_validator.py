"""ToolCallValidator — contract enforcement between LLM output and Tool execution.

Claude Code pattern: the LLM is an "action generator" operating within a strict
contract. Every tool call MUST pass validation against the registered tool
schemas BEFORE execution. Invalid calls are rejected at the control plane,
not leaked to the data plane (Runtime).

This module sits between core.py's Action parsing and ToolRegistry execution.
It does NOT modify the Action — it only returns a pass/fail result. On failure,
the main loop injects a structured error observation so the LLM can self-correct
on the next turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.task import ToolCall
    from llm.base import LLMToolSchema


@dataclass
class ValidationResult:
    """Result of tool call validation against registered schemas."""
    valid: bool
    error_type: str = ""         # "unknown_tool" | "missing_required" | "duplicate_call"
    error_message: str = ""
    offending_tool: str = ""     # which tool call failed


def validate_tool_calls(
    tool_calls: list,
    tool_schemas: list,
) -> ValidationResult:
    """Validate tool calls against the registered tool schemas.

    This is the CONTROL PLANE — it enforces the contract between "what the LLM
    asked for" and "what the system can do." Invalid tool calls are rejected
    BEFORE they reach the Runtime.

    Checks (in order):
        1. Tool name exists in schemas (→ "unknown_tool")
        2. Required params are present (→ "missing_required")
        3. No duplicate calls within the same action (→ "duplicate_call")

    Returns ValidationResult(valid=True) if all checks pass.
    """
    schema_map: dict[str, any] = {s.name: s for s in tool_schemas}

    for tc in tool_calls:
        name = getattr(tc, "name", "")
        params = getattr(tc, "params", {}) or {}

        # ── Check 1: Tool name exists ──
        if name not in schema_map:
            available = ", ".join(sorted(schema_map.keys()))
            return ValidationResult(
                valid=False,
                error_type="unknown_tool",
                error_message=(
                    f"Unknown tool '{name}'. Available tools: {available}"
                ),
                offending_tool=name,
            )

        # ── Check 2: Required params present ──
        schema = schema_map[name]
        required: list[str] = schema.parameters.get("required", []) if hasattr(schema, "parameters") else []
        for field in required:
            if field not in params:
                return ValidationResult(
                    valid=False,
                    error_type="missing_required",
                    error_message=(
                        f"Tool '{name}' requires parameter '{field}'. "
                        f"Your call was missing this field. Please retry with the required parameter."
                    ),
                    offending_tool=name,
                )

    # ── Check 3: Duplicate detection ──
    if len(tool_calls) > 1:
        seen: set[tuple] = set()
        for tc in tool_calls:
            name = getattr(tc, "name", "")
            params = getattr(tc, "params", {}) or {}
            try:
                key = (name, json.dumps(params, sort_keys=True, ensure_ascii=False))
            except (TypeError, ValueError):
                key = (name, str(params))
            if key in seen:
                return ValidationResult(
                    valid=False,
                    error_type="duplicate_call",
                    error_message=(
                        f"Duplicate tool call: '{name}' was called twice in the same "
                        f"response with identical parameters. Remove the duplicate and retry."
                    ),
                    offending_tool=name,
                )
            seen.add(key)

    return ValidationResult(valid=True)
