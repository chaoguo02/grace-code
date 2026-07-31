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
    """Serialize a dataclass to dict.

    Only skip None values and the 'type' discriminator is always included.
    Empty strings and empty containers are preserved — the frontend may
    depend on their presence (e.g. ev.error || 'default').
    """
    result = {}
    for k, v in asdict(obj).items():
        if v is None:
            continue
        result[k] = v
    return result


# ── Status events ─────────────────────────────────────────────────────


@dataclass
class WsStatus:
    type: Literal["status"] = "status"
    status: str = ""            # running | completed | failed | finish | gave_up | cancelled | compacted
    message: str = ""
    error: str = ""
    result: dict | None = None  # {summary, steps_taken, total_tokens}
    content: str = ""           # assistant 最终回答正文（由 task_complete.summary 填充）
    timestamp: str = ""
    # ── EventEnvelope fields ──
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""
    tool_call_id: str = ""

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
    # ── EventEnvelope ──
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""
    tool_call_id: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class WsThoughtDelta:
    """Streaming thought token — pushed in real-time during LLM generation."""
    type: Literal["thought_delta"] = "thought_delta"
    text: str = ""
    step: int = 0
    child_session_id: str = ""
    timestamp: str = ""
    # ── EventEnvelope ──
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""
    tool_call_id: str = ""

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
    # ── EventEnvelope ──
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""       # = tool_call_id
    tool_call_id: str = ""   # model-assigned tool use id

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
    evidence: dict | None = None
    """Evidence metadata: {evidence_id, kind, status, cached, source_fingerprint}"""
    child_session_id: str = ""
    timestamp: str = ""
    # ── EventEnvelope ──
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""       # NOT created — observation updates tool_use block
    tool_call_id: str = ""   # matches tool_call's id

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class WsEvidenceRecord:
    """One persisted evidence row projected to trace/WS after DB commit."""

    type: Literal["evidence_record"] = "evidence_record"
    evidence: dict = field(default_factory=dict)
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""
    tool_call_id: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Reflection ────────────────────────────────────────────────────────


@dataclass
class WsReflection:
    type: Literal["reflection"] = "reflection"
    content: str = ""
    timestamp: str = ""
    # ── EventEnvelope ──
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""
    tool_call_id: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Subagent events ───────────────────────────────────────────────────


@dataclass
class WsSubagentStart:
    type: Literal["subagent_start"] = "subagent_start"
    child_session_id: str = ""
    agent_name: str = ""
    timestamp: str = ""
    # ── EventEnvelope ──
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""
    tool_call_id: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class WsSubagentStop:
    type: Literal["subagent_stop"] = "subagent_stop"
    child_session_id: str = ""
    status: str = ""
    timestamp: str = ""
    # ── EventEnvelope ──
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""
    tool_call_id: str = ""

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


@dataclass
class WsApprovalResolved:
    """Persisted response paired with one approval_required event."""

    type: Literal["approval_resolved"] = "approval_resolved"
    request_id: str = ""
    tool_name: str = ""
    decision: str = ""
    note: str = ""
    updated_input: bool = False
    wait_ms: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Assistant text streaming ───────────────────────────────────────────


@dataclass
class WsAssistantTextStart:
    """Start of an assistant text block — creates a new text block with block_id."""
    type: Literal["assistant_text_start"] = "assistant_text_start"
    block_id: str = ""
    timestamp: str = ""
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    tool_call_id: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class WsAssistantTextDelta:
    """Streaming token of assistant text — appends to the block identified by block_id."""
    type: Literal["assistant_text_delta"] = "assistant_text_delta"
    text: str = ""
    block_id: str = ""
    timestamp: str = ""
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    tool_call_id: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class WsAssistantTextEnd:
    """End of an assistant text block — marks it as completed."""
    type: Literal["assistant_text_end"] = "assistant_text_end"
    block_id: str = ""
    timestamp: str = ""
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    tool_call_id: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class WsAssistantTextAborted:
    """Text block was aborted — stream error, cancel, max_tokens, etc."""
    type: Literal["assistant_text_aborted"] = "assistant_text_aborted"
    block_id: str = ""
    reason: str = ""
    timestamp: str = ""
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    tool_call_id: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Multi-agent delegation events ────────────────────────────────────


DelegationEventType = Literal[
    "delegation_planned",
    "delegation_task_queued",
    "delegation_task_started",
    "delegation_task_reported",
    "delegation_task_failed",
    "delegation_task_blocked",
    "delegation_task_retrying",
    "delegation_synthesis_started",
    "delegation_phase_changed",
    "delegation_integration_started",
    "delegation_integration_completed",
    "delegation_verification_started",
    "delegation_verification_completed",
    "delegation_completed",
    "delegation_budget_exhausted",
]


@dataclass
class WsDelegationEvent:
    """Typed, flattened delegation lifecycle event used by live WS and replay."""

    type: DelegationEventType = "delegation_phase_changed"
    delegation_run_id: str = ""
    task_id: str = ""
    generation: int = 0
    topology: str = ""
    task_count: int = 0
    phase: str = ""
    previous_phase: str = ""
    status: str = ""
    agent_type: str = ""
    child_session_id: str = ""
    report_count: int = 0
    tokens_used: int = 0
    duration_ms: int = 0
    reason: str = ""
    error: str = ""
    action: str = ""
    integration_status: str = ""
    verification: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    timestamp: str = ""
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""
    tool_call_id: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Run lifecycle events ───────────────────────────────────────────────


@dataclass
class WsRunStarted:
    """Run has transitioned QUEUED → RUNNING."""
    type: Literal["run_started"] = "run_started"
    run_id: str = ""
    turn_id: str = ""
    turn_index: int = 0
    timestamp: str = ""
    session_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""
    tool_call_id: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class WsRunTerminal:
    """Run has reached a terminal state (completed/failed/cancelled).

    Sent only AFTER the final assistant message and Run status have been
    committed to the database.  This is the signal that the frontend can
    safely refresh from the DB.
    """
    type: Literal["run_terminal"] = "run_terminal"
    run_id: str = ""
    turn_id: str = ""
    turn_index: int = 0
    status: str = ""          # completed | failed | cancelled
    summary: str = ""
    steps_taken: int = 0
    total_tokens: int = 0
    error: str = ""
    termination_reason: str = ""
    verification_status: str = ""
    verification_reason: str = ""
    verification: dict = field(default_factory=dict)
    workspace_delta: dict = field(default_factory=dict)
    evidence_summary: dict = field(default_factory=dict)
    timestamp: str = ""
    session_id: str = ""
    event_id: str = ""
    sequence: int = 0
    block_id: str = ""
    tool_call_id: str = ""

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


@dataclass
class WsReviewUpdated:
    """A durable multi-agent review job or task changed state."""

    type: Literal["review_updated"] = "review_updated"
    job_id: str = ""
    status: str = ""
    task_states: dict[str, str] = field(default_factory=dict)
    finding_count: int = 0
    workspace_revision: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Memory activity ───────────────────────────────────────────────────


@dataclass
class WsMemoryRecall:
    type: Literal["memory_recall"] = "memory_recall"
    injected_count: int = 0
    candidate_count: int = 0
    omitted_count: int = 0
    top_names: list[str] = field(default_factory=list)
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass
class WsMemoryWritten:
    type: Literal["memory_written"] = "memory_written"
    name: str = ""
    description: str = ""
    source: str = ""
    confidence: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


# ── Discriminated union ───────────────────────────────────────────────

WsEvent = (
    WsStatus | WsThought | WsThoughtDelta | WsToolCall | WsObservation | WsReflection
    | WsSubagentStart | WsSubagentStop
    | WsApprovalRequired | WsApprovalTimeout | WsApprovalResolved | WsPlanReady
    | WsWorktreeResolved | WsMemoryRecall | WsMemoryWritten
    | WsAssistantTextStart | WsAssistantTextDelta | WsAssistantTextEnd | WsAssistantTextAborted
    | WsRunStarted | WsRunTerminal
)
