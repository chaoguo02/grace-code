"""Experimental, opt-in agent-team domain primitives."""

from agent.team.feature_flags import TeamFeatureConfig
from agent.team.lease_manager import Lease, LeaseManager
from agent.team.mailbox import MailMessage, Mailbox
from agent.team.state import MemberState, TeamState
from agent.team.task_board import BoardTask, BoardTaskState, TaskBoard
from agent.team.team_runtime import TeamRuntime

__all__ = [
    "BoardTask",
    "BoardTaskState",
    "Lease",
    "LeaseManager",
    "MailMessage",
    "Mailbox",
    "MemberState",
    "TaskBoard",
    "TeamFeatureConfig",
    "TeamRuntime",
    "TeamState",
]

