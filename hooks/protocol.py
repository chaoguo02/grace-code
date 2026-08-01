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
    NON_BLOCKING_ERROR = "non_blocking_error"
    """CC-aligned: hook failed but should not block the agent. Exit code != 0,2."""


class HookAttachmentKind(str, Enum):
    CONTEXT = "context"
    WARNING = "warning"


@dataclass(frozen=True)
class HookAttachment:
    """Typed model-context contribution kept separate from raw tool output."""

    kind: HookAttachmentKind
    text: str
    source: str


@dataclass
class HookOutput:
    """Parsed from hook script's stdout JSON."""

    decision: HookDecision | None = None
    reason: str | None = None
    additional_context: str | None = None
    updated_input: dict[str, Any] | None = None
    updated_output: dict[str, Any] | str | None = None

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
            updated_output=(
                data["updated_output"] if "updated_output" in data
                else data.get("updatedOutput")
            ),
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
        """Typed control instruction derived at the external protocol boundary.

        CC-aligned three-way classification:
          - Exit 0 + stdout JSON decision → BLOCK / APPROVE / CONTINUE
          - Exit 2 → BLOCK (blocking error)
          - Other non-zero → NON_BLOCKING_ERROR (logged warning, not fatal)
        """
        if self.exit_code == ExitCode.BLOCKING_ERROR:
            return HookControl.BLOCK
        if self.exit_code == ExitCode.SUCCESS and self.parsed is not None:
            if self.parsed.decision is HookDecision.BLOCK:
                return HookControl.BLOCK
            if self.parsed.decision is HookDecision.ALLOW:
                return HookControl.APPROVE
        if self.exit_code != ExitCode.SUCCESS:
            return HookControl.NON_BLOCKING_ERROR
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
    attachments: tuple[HookAttachment, ...] = ()
    updated_input: dict[str, Any] | None = None
    updated_output: dict[str, Any] | str | None = None
    warnings: list[str] = None  # type: ignore[assignment]
    """CC-aligned: non-blocking error messages accumulated during dispatch."""

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []
        if self.additional_context and not self.attachments:
            self.attachments = (
                HookAttachment(
                    kind=HookAttachmentKind.CONTEXT,
                    text=self.additional_context,
                    source="hook",
                ),
            )
