"""
P9: Hook decision types — frozen, explicit semantics.

No Any/dict raw transform.  Each hook event has a specific decision shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class HookDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class StopDecision(StrEnum):
    CONTINUE = "continue"
    BLOCK = "block"


# ── PreToolUse ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PreToolUseDecision:
    permission: HookDecision = HookDecision.ALLOW
    updated_input: dict | None = None
    reason: str = ""


# ── PostToolUse ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PostToolUseDecision:
    additional_context: str = ""
    replace_output: str | None = None


# ── UserPromptSubmit ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class UserPromptSubmitDecision:
    block: bool = False
    reason: str = ""


# ── Stop ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StopDecision:
    decision: StopDecision = StopDecision.CONTINUE
    reason: str = ""


# ── PreCompact ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PreCompactDecision:
    block: bool = False
    reason: str = ""
