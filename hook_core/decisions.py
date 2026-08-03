"""
G11: Hook typed decisions — FrozenJsonObject, typed unions, per-event types.

- updated_input uses FrozenJsonObject, not untyped mappings
- HookContractViolation for invalid/unknown inputs
- Each lifecycle point has an independent decision type
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.json_values import FrozenJsonObject, freeze_json


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


# ── Contract violation ──────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class HookContractViolation:
    """Returned when hook input/decision types mismatch the contract.

    E.g. raw dict passed where FrozenJsonObject expected, or unknown
    decision type for a given lifecycle point.
    """
    reason: str = ""
    detail: str = ""


# ── PreToolUse ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PreToolUseDecision:
    permission: PermissionDecision = PermissionDecision.ALLOW
    updated_input: FrozenJsonObject | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.updated_input is not None and isinstance(self.updated_input, dict):
            object.__setattr__(self, "updated_input", freeze_json(self.updated_input))


# ── PostToolUse ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PostToolUseDecision:
    additional_context: str = ""
    replace_output: str | None = None
    decision: str = ""


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
    updated_input: FrozenJsonObject | None = None  # G11: was dict | None


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


# ── PermissionRequest ───────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PermissionRequestDecision:
    permission: PermissionDecision = PermissionDecision.ALLOW
    reason: str = ""


# ── G11: Per-event decision registry ────────────────────────────────────────
# Maps lifecycle event names to their expected decision types.
# Used by HookDispatcher to validate decision contract.

# ── Backward compat (used by dispatcher, bridge — removed in G14/G39) ──────

@dataclass(frozen=True, slots=True)
class ObserveDecision:
    """Deprecated: each event should use its specific decision type."""
    pass


EVENT_DECISION_MAP: dict[str, type] = {
    "PreToolUse": PreToolUseDecision,
    "PostToolUse": PostToolUseDecision,
    "PostToolUseFailure": PostToolUseFailureDecision,
    "PostToolBatch": ObserveDecision,  # T7: batch-completion hook
    "UserPromptSubmit": UserPromptSubmitDecision,
    "Stop": StopDecision,
    "StopFailure": ObserveDecision,
    "SessionStart": SessionStartDecision,
    "SessionEnd": ObserveDecision,
    "SubagentStart": ObserveDecision,
    "SubagentStop": ObserveDecision,
    "PreCompact": PreCompactDecision,
    "PostCompact": ObserveDecision,
    "PermissionRequest": PermissionRequestDecision,
    "PermissionDenied": ObserveDecision,
    "Notification": ObserveDecision,
}
