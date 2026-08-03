"""
G36: Approval Coordinator — direct command, not EventBus Command.

- Approval is a synchronous decision gate, NOT an EventBus event.
- PermissionRequest Hook is a sync gate checked by HookDispatcher.
- ApprovalCoordinator handles the decision: approve/deny with reason.
- No Command Events published — only direct-call decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    decision: ApprovalDecision
    reason: str = ""
    approved_by: str = ""  # hook name or "user"
    tool_name: str = ""
    tool_use_id: str = ""


class ApprovalCoordinator:
    """Direct-call approval authority.  Not EventBus-driven.

    G36: Approval is a synchronous decision.  No Command Events.
         PermissionRequest Hook gate runs first; this handles the result.
    """

    def __init__(self) -> None:
        self._pending: dict[str, object] = {}  # tool_use_id → pending request
        self._history: list[ApprovalResult] = []

    def request_approval(self, tool_name: str, tool_use_id: str,
                         reason: str = "") -> None:
        """Register a pending approval request (called by Hook gate)."""
        self._pending[tool_use_id] = {
            "tool_name": tool_name, "reason": reason,
        }

    def approve(self, tool_use_id: str, approved_by: str = "user",
                reason: str = "") -> ApprovalResult:
        """Approve a pending request.  Direct call, not an EventBus event."""
        self._pending.pop(tool_use_id, None)
        result = ApprovalResult(
            decision=ApprovalDecision.APPROVED,
            reason=reason, approved_by=approved_by,
            tool_use_id=tool_use_id,
        )
        self._history.append(result)
        return result

    def deny(self, tool_use_id: str, reason: str = "",
             approved_by: str = "user") -> ApprovalResult:
        """Deny a pending request."""
        self._pending.pop(tool_use_id, None)
        result = ApprovalResult(
            decision=ApprovalDecision.DENIED,
            reason=reason, approved_by=approved_by,
            tool_use_id=tool_use_id,
        )
        self._history.append(result)
        return result

    def is_pending(self, tool_use_id: str) -> bool:
        return tool_use_id in self._pending

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def history(self) -> list[ApprovalResult]:
        return list(self._history)
