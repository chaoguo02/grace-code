"""
hooks/protocol.py

Exit code protocol and structured output for hook communication.

Aligned with Claude Code's hook system:
- Exit 0 = success/allow
- Exit 2 = blocking error (only for blockable events)
- Other = non-blocking error, logged but not fatal
- stdout JSON with optional decision/additional_context fields
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any


class ExitCode(IntEnum):
    SUCCESS = 0
    BLOCKING_ERROR = 2


class HookDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


class HookControl(str, Enum):
    CONTINUE = "continue"
    BLOCK = "block"
    APPROVE = "approve"


@dataclass
class HookOutput:
    """Parsed from hook script's stdout JSON."""

    decision: HookDecision | None = None
    reason: str | None = None
    additional_context: str | None = None
    updated_input: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HookOutput":
        raw_decision = data.get("decision")
        try:
            decision = HookDecision(raw_decision) if raw_decision is not None else None
        except ValueError:
            decision = None
        return cls(
            decision=decision,
            reason=data.get("reason"),
            additional_context=data.get("additional_context") or data.get("additionalContext"),
            updated_input=data.get("updated_input") or data.get("updatedInput"),
        )


@dataclass
class HookResult:
    """Result from executing a single hook."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    parsed: HookOutput | None = None

    @property
    def control(self) -> HookControl:
        """Typed control instruction derived at the external protocol boundary."""
        if self.exit_code == ExitCode.BLOCKING_ERROR:
            return HookControl.BLOCK
        if self.exit_code == ExitCode.SUCCESS and self.parsed is not None:
            if self.parsed.decision is HookDecision.BLOCK:
                return HookControl.BLOCK
            if self.parsed.decision is HookDecision.ALLOW:
                return HookControl.APPROVE
        return HookControl.CONTINUE

    @property
    def context(self) -> str:
        """Additional context emitted by a successful hook, if any."""
        if self.parsed and self.parsed.additional_context:
            return self.parsed.additional_context
        if self.exit_code == ExitCode.SUCCESS and self.parsed is None:
            return self.stdout
        return ""


@dataclass
class DispatchResult:
    """Aggregated result from dispatching an event to all matching hooks."""

    control: HookControl = HookControl.CONTINUE
    reason: str = ""
    additional_context: str = ""
