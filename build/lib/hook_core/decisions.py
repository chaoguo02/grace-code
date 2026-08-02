"""
CC-aligned hook decision types — frozen, per-event, explicit semantics.

Each hook event has a specific decision shape.
PreToolUse uses a four-way permission model: deny > defer > ask > allow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class PermissionDecision(StrEnum):
    """CC-aligned four-way permission with defined precedence.

    Precedence (highest to lowest): deny > defer > ask > allow
    """
    DENY = "deny"
    DEFER = "defer"
    ASK = "ask"
    ALLOW = "allow"

    @staticmethod
    def precedence() -> list["PermissionDecision"]:
        return [
            PermissionDecision.DENY,
            PermissionDecision.DEFER,
            PermissionDecision.ASK,
            PermissionDecision.ALLOW,
        ]


class StopVerdict(StrEnum):
    CONTINUE = "continue"
    BLOCK = "block"


# ── PreToolUse ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PreToolUseDecision:
    permission: PermissionDecision = PermissionDecision.ALLOW
    updated_input: dict | None = None
    reason: str = ""


# ── PostToolUse ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PostToolUseDecision:
    additional_context: str = ""
    replace_output: str | None = None
    decision: str = ""  # "block" to feed back to model (tool already ran)


# ── PostToolUseFailure ──────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PostToolUseFailureDecision:
    additional_context: str = ""
    decision: str = ""


# ── UserPromptSubmit ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class UserPromptSubmitDecision:
    block: bool = False
    reason: str = ""
    updated_input: dict | None = None


# ── Stop ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StopDecision:
    decision: StopVerdict = StopVerdict.CONTINUE
    reason: str = ""


# ── SessionStart ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SessionStartDecision:
    additional_context: str = ""


# ── PreCompact ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PreCompactDecision:
    block: bool = False
    reason: str = ""


# ── Default (for events without a specific decision shape) ──────────────────

@dataclass(frozen=True, slots=True)
class ObserveDecision:
    """Used for notification-only events (SubagentStart, SessionEnd, etc.)."""
    pass
