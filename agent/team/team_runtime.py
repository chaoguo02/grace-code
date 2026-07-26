"""Live control plane for an explicitly approved experimental team.

SessionRuntime owns this object while the process is active and mirrors the
task board into durable delegation records.  The mailbox and leases are
deliberately process-local, so a restart exposes a recovery-required state
instead of pretending that live peers survived.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading

from agent.team.feature_flags import TeamFeatureConfig
from agent.team.lease_manager import LeaseManager
from agent.team.mailbox import Mailbox
from agent.team.state import MemberState, TeamState
from agent.team.task_board import BoardTaskState, TaskBoard


@dataclass(frozen=True)
class TeamMember:
    id: str
    role: str
    state: MemberState = MemberState.IDLE


class TeamRuntime:
    def __init__(
        self,
        *,
        team_id: str,
        lead_id: str,
        config: TeamFeatureConfig,
        user_approved: bool = False,
    ) -> None:
        if not config.enabled:
            raise RuntimeError("agent teams are disabled by feature flag")
        if not team_id or not lead_id:
            raise ValueError("team_id and lead_id are required")
        self.team_id = team_id
        self.lead_id = lead_id
        self.config = config
        self.state = (
            TeamState.AWAITING_APPROVAL
            if config.require_user_approval and not user_approved
            else TeamState.PROPOSED
        )
        self._members = {
            lead_id: TeamMember(id=lead_id, role="lead")
        }
        self.mailbox = Mailbox({lead_id})
        self.leases = LeaseManager()
        self.task_board = TaskBoard(
            self.leases,
            lease_ttl_seconds=config.lease_ttl_seconds,
            max_tasks=config.max_tasks,
        )
        self._lock = threading.RLock()

    @property
    def members(self) -> tuple[TeamMember, ...]:
        with self._lock:
            return tuple(self._members.values())

    def approve(self) -> None:
        with self._lock:
            if self.state is not TeamState.AWAITING_APPROVAL:
                raise RuntimeError("team is not awaiting approval")
            self.state = TeamState.PROPOSED

    def reject(self) -> None:
        """Reject a proposal before any teammate or task becomes active."""
        with self._lock:
            if self.state is not TeamState.AWAITING_APPROVAL:
                raise RuntimeError("team is not awaiting approval")
            self.state = TeamState.CANCELLED
            for member_id, member in tuple(self._members.items()):
                self._members[member_id] = TeamMember(
                    id=member.id,
                    role=member.role,
                    state=MemberState.STOPPED,
                )

    def add_member(self, member_id: str, role: str) -> TeamMember:
        with self._lock:
            if self.state is not TeamState.PROPOSED:
                raise RuntimeError("members can only be added before activation")
            if member_id in self._members:
                raise ValueError("team member already exists")
            if len(self._members) >= self.config.max_members:
                raise OverflowError("team member limit reached")
            member = TeamMember(id=member_id, role=role.strip())
            if not member.id or not member.role:
                raise ValueError("member id and role are required")
            self._members[member_id] = member
            self.mailbox.add_member(member_id)
            return member

    def activate(self) -> None:
        with self._lock:
            if self.state is TeamState.AWAITING_APPROVAL:
                raise PermissionError("explicit user approval is required")
            if self.state is not TeamState.PROPOSED:
                raise RuntimeError("team cannot be activated from its current state")
            if len(self._members) < 2:
                raise RuntimeError("team requires a lead and at least one teammate")
            self.state = TeamState.ACTIVE

    def set_member_state(self, member_id: str, state: MemberState) -> None:
        with self._lock:
            if self.state is not TeamState.ACTIVE:
                raise RuntimeError("team is not active")
            member = self._members[member_id]
            self._members[member_id] = TeamMember(
                id=member.id, role=member.role, state=MemberState(state)
            )

    def shutdown(self, *, cancel: bool = False) -> None:
        with self._lock:
            if self.state.terminal:
                return
            if self.state is not TeamState.ACTIVE:
                raise RuntimeError("only an active team can be shut down")
            self.state = TeamState.SHUTTING_DOWN
            unfinished = tuple(
                task for task in self.task_board.list() if not task.state.terminal
            )
            if unfinished and not cancel:
                self.state = TeamState.ACTIVE
                raise RuntimeError("cannot complete a team with unfinished tasks")
            self.state = TeamState.CANCELLED if cancel else TeamState.COMPLETED
            for member_id, member in tuple(self._members.items()):
                self._members[member_id] = TeamMember(
                    id=member.id, role=member.role, state=MemberState.STOPPED
                )
