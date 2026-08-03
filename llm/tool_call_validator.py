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
    error_type: str = ""         # "unknown_tool" | "invalid_params" | "duplicate_call"
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
        2. Params validate against the tool's JSON Schema via SchemaValidator
           (jsonschema — includes required-field enforcement, → "invalid_params")
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

        schema = schema_map[name]

        # ── Check 2: Parameter validation (P1-2: jsonschema) ──
        # Required-field presence is enforced by SchemaValidator (jsonschema
        # "required" keyword); the LLM-friendly message is emitted through
        # format_errors_for_llm() so self-correction feedback is preserved.
        if not isinstance(params, dict):
            return _invalid_params(
                name,
                "parameters must be an object",
            )
        root_schema = schema.parameters if hasattr(schema, "parameters") else {}
        if root_schema:
            from core.schema_validator import SchemaValidator
            validator = SchemaValidator(root_schema)
            result = validator.safe_parse(params)
            if not result.valid:
                feedback = validator.format_errors_for_llm(result.errors)
                legacy_paths = [
                    "params" + "".join(
                        f"[{part}]" if part.isdigit() else f".{part}"
                        for part in error.path.strip("/").split("/")
                        if part
                    )
                    for error in result.errors
                    if error.path and error.path != "/"
                ]
                if legacy_paths:
                    feedback += "\n  Parameter paths: " + ", ".join(legacy_paths)
                return _invalid_params(name, feedback)

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


def _invalid_params(tool_name: str, detail: str) -> ValidationResult:
    return ValidationResult(
        valid=False,
        error_type="invalid_params",
        error_message=f"Tool '{tool_name}' has invalid parameters: {detail}.",
        offending_tool=tool_name,
    )
