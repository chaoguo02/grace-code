"""
G20: Runtime outcome — frozen, deterministic digest, per-status types.

- Each outcome status has explicit factory method.
- digest() returns deterministic hash of semantic fields only
  (excludes wall clock, random IDs — same input → same digest).
- All nested collections are tuples/frozen values.
- Evidence is a value object, not a repository row.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from core.eventing.identifiers import RunId


class RunStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    GAVE_UP = "gave_up"


class CancellationReason(StrEnum):
    USER_REQUESTED = "user_requested"
    TIMEOUT = "timeout"
    HOOK_BLOCKED = "hook_blocked"
    CIRCUIT_BREAKER = "circuit_breaker"


# ── Evidence (immutable value object) ──────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ToolEvidence:
    """Immutable record of one tool execution."""
    tool_name: str = ""
    success: bool = True
    duration_ms: float = 0.0
    tool_use_id: str = ""  # T20: CC tool_use_id for traceability


@dataclass(frozen=True, slots=True)
class RunEvidence:
    """Immutable evidence collected during a run."""
    tool_calls: tuple[ToolEvidence, ...] = ()
    files_touched: tuple[str, ...] = ()
    hook_blocks: tuple[str, ...] = ()


# ── RuntimeOutcome ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RuntimeOutcome:
    """Frozen result of a run execution.  Deterministic digest."""

    run_id: RunId
    status: RunStatus
    steps_taken: int = 0
    tokens_used: int = 0
    input_tokens: int = 0   # H3: separated from tokens_used for accurate recording
    output_tokens: int = 0  # H3: separated from tokens_used for accurate recording
    summary: str = ""
    error: str = ""
    cancellation_reason: CancellationReason | None = None
    blocked_by: str = ""
    evidence: RunEvidence | None = None
    messages: tuple = ()
    """Phase 5: 本次 run 产生的 assistant/tool 消息（规范 dict：
    {"role","content","tool_calls"|"tool_call_id","is_error"}），
    供跨轮持久化写入 session_messages。digest() 排除此字段（对话数据）。"""

    # ── Factory methods ────────────────────────────────────────────────

    @classmethod
    def completed(cls, run_id: RunId, steps: int = 0, tokens: int = 0,
                  summary: str = "",
                  evidence: RunEvidence | None = None,
                  messages: tuple = ()) -> RuntimeOutcome:
        return cls(run_id=run_id, status=RunStatus.COMPLETED,
                   steps_taken=steps, tokens_used=tokens,
                   summary=summary, evidence=evidence, messages=messages)

    @classmethod
    def failed(cls, run_id: RunId, error: str = "",
               steps: int = 0, tokens: int = 0,
               evidence: RunEvidence | None = None,
               messages: tuple = ()) -> RuntimeOutcome:
        return cls(run_id=run_id, status=RunStatus.FAILED,
                   error=error, steps_taken=steps, tokens_used=tokens,
                   evidence=evidence, messages=messages)

    @classmethod
    def cancelled(cls, run_id: RunId,
                  reason: CancellationReason = CancellationReason.USER_REQUESTED,
                  steps: int = 0, tokens: int = 0,
                  evidence: RunEvidence | None = None,
                  messages: tuple = ()) -> RuntimeOutcome:
        return cls(run_id=run_id, status=RunStatus.CANCELLED,
                   cancellation_reason=reason, steps_taken=steps,
                   tokens_used=tokens, evidence=evidence, messages=messages)

    @classmethod
    def blocked(cls, run_id: RunId, blocked_by: str = "",
                detail: str = "", steps: int = 0, tokens: int = 0,
                evidence: RunEvidence | None = None,
                messages: tuple = ()) -> RuntimeOutcome:
        return cls(run_id=run_id, status=RunStatus.BLOCKED,
                   steps_taken=steps, tokens_used=tokens,
                   summary=f"blocked by {blocked_by}", error=detail,
                   blocked_by=blocked_by, evidence=evidence, messages=messages)

    # ── G20: Deterministic digest ──────────────────────────────────────

    def digest(self) -> str:
        """SHA-256 digest of semantic fields only.

        Excludes: wall clock timestamps, random event IDs.
        Includes: run_id, status, steps, tokens, summary, error, evidence.
        Same input → same digest across 100+ runs.
        """
        fields = (
            str(self.run_id),
            self.status.value,
            str(self.steps_taken),
            str(self.tokens_used),
            self.summary,
            self.error,
            self.cancellation_reason.value if self.cancellation_reason else "",
            self.blocked_by,
        )
        if self.evidence:
            evidence_str = "|".join(
                f"{e.tool_name}:{1 if e.success else 0}"
                for e in self.evidence.tool_calls
            )
            fields += (evidence_str,)
        payload = "|".join(fields)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def is_terminal(self) -> bool:
        return self.status in (RunStatus.COMPLETED, RunStatus.FAILED,
                                RunStatus.CANCELLED)

    @property
    def is_success(self) -> bool:
        return self.status == RunStatus.COMPLETED
