"""
P9: Hook event-specific input contracts — frozen, no Any/dict.

One input class per hook event.  Each class carries only the fields
relevant to that event.  No raw dict passthrough.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ── PreToolUse ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PreToolUseInput:
    tool_name: str
    tool_input: dict[str, Any]  # JSON-serializable tool params
    tool_use_id: str = ""
    session_id: str = ""


# ── PostToolUse ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PostToolUseInput:
    tool_name: str
    tool_input: dict[str, Any]
    tool_output: str = ""
    tool_error: str = ""
    tool_use_id: str = ""
    session_id: str = ""
    success: bool = True


# ── UserPromptSubmit ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class UserPromptSubmitInput:
    prompt: str
    session_id: str = ""


# ── Stop ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StopInput:
    session_id: str = ""
    steps_taken: int = 0
    tokens_used: int = 0


# ── SubagentStop ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SubagentStopInput:
    session_id: str = ""
    agent_name: str = ""
    steps_taken: int = 0


# ── PreCompact ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PreCompactInput:
    session_id: str = ""
    tokens_before: int = 0
    trigger: str = ""  # "auto" | "manual"
