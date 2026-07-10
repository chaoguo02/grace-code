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
from typing import Any


class ExitCode:
    SUCCESS = 0
    BLOCKING_ERROR = 2


@dataclass
class HookOutput:
    """Parsed from hook script's stdout JSON."""

    decision: str | None = None
    reason: str | None = None
    additional_context: str | None = None
    updated_input: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HookOutput":
        return cls(
            decision=data.get("decision"),
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
    def blocks(self) -> bool:
        """Exit code 2 = blocking error."""
        return self.exit_code == ExitCode.BLOCKING_ERROR

    @property
    def approves_explicitly(self) -> bool:
        """Exit 0 with decision=allow, or legacy exit 0 (approve)."""
        if self.parsed and self.parsed.decision == "allow":
            return True
        return False

    @property
    def has_context(self) -> bool:
        """Has additional context to inject into conversation."""
        if self.parsed and self.parsed.additional_context:
            return True
        return self.exit_code == ExitCode.SUCCESS and bool(self.stdout) and self.parsed is None


@dataclass
class DispatchResult:
    """Aggregated result from dispatching an event to all matching hooks."""

    blocked: bool = False
    reason: str = ""
    approved_explicitly: bool = False
    additional_context: str = ""
