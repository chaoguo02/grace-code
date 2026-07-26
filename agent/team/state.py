"""Agent-team and member lifecycle states."""

from enum import Enum


class TeamState(str, Enum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    ACTIVE = "active"
    SHUTTING_DOWN = "shutting_down"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.CANCELLED, self.FAILED}


class MemberState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    STOPPED = "stopped"
    FAILED = "failed"

