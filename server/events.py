"""
server/events.py — typed WS event dataclasses.

Single source of truth for all WebSocket message shapes.
Replace the ad-hoc dict construction in _translate_event()
with these structured types.

The frontend mirrors these types in web/src/types/events.ts.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal


def _to_dict(obj) -> dict:
    """Serialize a dataclass to dict, skipping None/empty values."""
    result = {}
    for k, v in asdict(obj).items():
        if v is not None and v != "" and v != {} and v != []:
            result[k] = v
    return result


# ── Status events ─────────────────────────────────────────────────────


@dataclass
class WsStatus:
    type: Literal["status"] = "status"
    status: str = ""            # running | completed | failed | finish | gave_up | compacted
    message: str = ""
    error: str = ""
    result: dict | None = None  # {summary, steps_taken, total_tokens}
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Thought ───────────────────────────────────────────────────────────


@dataclass
class WsThought:
    type: Literal["thought"] = "thought"
    content: str = ""
    step: int = 0
    child_session_id: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Tool call ─────────────────────────────────────────────────────────


@dataclass
class WsToolCall:
    type: Literal["tool_call"] = "tool_call"
    name: str = ""
    params: dict = field(default_factory=dict)
    step: int = 0
    id: str = ""
    child_session_id: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Observation ───────────────────────────────────────────────────────


@dataclass
class WsObservation:
    type: Literal["observation"] = "observation"
    tool_name: str = ""
    output: str = ""
    error: str = ""
    status: str = ""
    step: int = 0
    id: str = ""
    diff: str = ""
    child_session_id: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Reflection ────────────────────────────────────────────────────────


@dataclass
class WsReflection:
    type: Literal["reflection"] = "reflection"
    content: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Subagent events ───────────────────────────────────────────────────


@dataclass
class WsSubagentStart:
    type: Literal["subagent_start"] = "subagent_start"
    child_session_id: str = ""
    agent_name: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class WsSubagentStop:
    type: Literal["subagent_stop"] = "subagent_stop"
    child_session_id: str = ""
    status: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Approval events ───────────────────────────────────────────────────


@dataclass
class WsApprovalRequired:
    type: Literal["approval_required"] = "approval_required"
    request_id: str = ""
    tool_name: str = ""
    params: dict = field(default_factory=dict)
    thought: str = ""
    decision_reason: str = ""
    tool_use_id: str = ""
    permission_mode: str = ""
    risk_level: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class WsApprovalTimeout:
    type: Literal["approval_timeout"] = "approval_timeout"
    request_id: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Plan ready ────────────────────────────────────────────────────────


@dataclass
class WsPlanReady:
    type: Literal["plan_ready"] = "plan_ready"
    plan_text: str = ""
    contract: dict | None = None
    revision: int = 0
    max_revisions: int = 5
    result: dict | None = None
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Worktree resolved ─────────────────────────────────────────────────


@dataclass
class WsWorktreeResolved:
    type: Literal["worktree_resolved"] = "worktree_resolved"
    child_session_id: str = ""
    action: str = ""
    status: str = ""
    message: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Discriminated union ───────────────────────────────────────────────

WsEvent = (
    WsStatus | WsThought | WsToolCall | WsObservation | WsReflection
    | WsSubagentStart | WsSubagentStop
    | WsApprovalRequired | WsApprovalTimeout | WsPlanReady
    | WsWorktreeResolved
)
