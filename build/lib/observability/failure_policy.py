"""observability/failure_policy.py

Failure taxonomy and recovery policy for harness replay verification.

Maps each TerminationReason to a stable harness behavior category that
defines whether the harness should:
- halt immediately (hard boundary)
- allow limited recovery attempts before halting (soft boundary)
- or never produce that termination under normal operation (internal)

The key invariant: after any terminal outcome, the harness MUST preserve its
boundary. No silent fallback into uncontrolled retry loops.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from agent.task import RunStatus, TerminationReason


class FailureCategory(str, Enum):
    """Stable harness vocabulary for failure classification."""

    RESOURCE_EXHAUSTED = "resource_exhausted"
    """Budget, steps, or turns limit reached. No recovery possible."""

    AUTHORITY_DENIED = "authority_denied"
    """Permission or guard rejected the action. Boundary was enforced."""

    TOOL_FAILURE = "tool_failure"
    """Repeated tool failures tripped the circuit breaker."""

    ENVIRONMENT_BLOCKED = "environment_blocked"
    """External environment unavailable (missing tooling, infra down)."""

    MODEL_ERROR = "model_error"
    """LLM provider returned an unrecoverable error."""

    AGENT_DECISION = "agent_decision"
    """The model itself decided to stop (gave up or mode-switch)."""

    USER_CANCELLED = "user_cancelled"
    """Explicit cancellation by the user or external signal."""

    INTERNAL_ERROR = "internal_error"
    """Unexpected harness-internal failure."""


class ResourceFailureCategory(str, Enum):
    """Stable, actionable classification for resource admission failures."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    CAPACITY_TIMEOUT = "capacity_timeout"
    PROVIDER_THROTTLED = "provider_throttled"
    HOST_MEMORY_PRESSURE = "host_memory_pressure"
    THREAD_CAPACITY = "thread_capacity"
    DISK_CAPACITY = "disk_capacity"
    EVENT_BACKPRESSURE = "event_backpressure"
    DB_BACKPRESSURE = "db_backpressure"


def classify_resource_failure(
    outcome: Any,
    kind: Any,
    reason: str = "",
) -> ResourceFailureCategory:
    """Classify a governor rejection without collapsing it into exhaustion."""
    from core.resource_governor import AdmissionOutcome, ResourceKind

    outcome = AdmissionOutcome(outcome)
    kind = ResourceKind(kind)
    if "memory" in reason.lower():
        return ResourceFailureCategory.HOST_MEMORY_PRESSURE
    if kind is ResourceKind.TOKEN_BUDGET:
        return ResourceFailureCategory.BUDGET_EXHAUSTED
    if kind in {ResourceKind.PROVIDER_RPM, ResourceKind.PROVIDER_TPM}:
        return ResourceFailureCategory.PROVIDER_THROTTLED
    if kind in {
        ResourceKind.WORKER_SLOT,
        ResourceKind.LLM_SLOT,
        ResourceKind.TOOL_SLOT,
    }:
        return ResourceFailureCategory.THREAD_CAPACITY
    if kind in {ResourceKind.WORKTREE_SLOT, ResourceKind.DISK_BYTES}:
        return ResourceFailureCategory.DISK_CAPACITY
    if kind is ResourceKind.EVENT_CAPACITY:
        return ResourceFailureCategory.EVENT_BACKPRESSURE
    if kind is ResourceKind.DB_WRITE_CAPACITY:
        return ResourceFailureCategory.DB_BACKPRESSURE
    if outcome is AdmissionOutcome.CAPACITY_TIMEOUT:
        return ResourceFailureCategory.CAPACITY_TIMEOUT
    return ResourceFailureCategory.CAPACITY_TIMEOUT


class BoundaryBehavior(str, Enum):
    """How the harness should behave at the boundary."""

    HALT_IMMEDIATELY = "halt_immediately"
    """No recovery attempts. Record and stop."""

    HALT_AFTER_INJECT = "halt_after_inject"
    """Inject a final message into history, then halt."""

    RECOVERABLE = "recoverable"
    """Limited recovery attempts allowed before escalating to halt."""


@dataclass(frozen=True)
class FailurePolicy:
    """Harness policy for a specific termination reason."""

    category: FailureCategory
    behavior: BoundaryBehavior
    max_recovery_attempts: int = 0
    preserves_history: bool = True
    expected_status: RunStatus | None = None


FAILURE_TAXONOMY: dict[TerminationReason, FailurePolicy] = {
    TerminationReason.NONE: FailurePolicy(
        category=FailureCategory.AGENT_DECISION,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.SUCCESS,
    ),
    TerminationReason.BUDGET_EXHAUSTED: FailurePolicy(
        category=FailureCategory.RESOURCE_EXHAUSTED,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.GAVE_UP,
    ),
    TerminationReason.MAX_STEPS: FailurePolicy(
        category=FailureCategory.RESOURCE_EXHAUSTED,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.MAX_STEPS,
    ),
    TerminationReason.MAX_TURNS: FailurePolicy(
        category=FailureCategory.RESOURCE_EXHAUSTED,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.MAX_STEPS,
    ),
    TerminationReason.CIRCUIT_BREAKER: FailurePolicy(
        category=FailureCategory.TOOL_FAILURE,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.GAVE_UP,
    ),
    TerminationReason.TOOL_FAILURE_LIMIT: FailurePolicy(
        category=FailureCategory.TOOL_FAILURE,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.GAVE_UP,
    ),
    TerminationReason.GUARD_REJECTED: FailurePolicy(
        category=FailureCategory.AUTHORITY_DENIED,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.GAVE_UP,
    ),
    TerminationReason.HOOK_STOPPED: FailurePolicy(
        category=FailureCategory.AUTHORITY_DENIED,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.GAVE_UP,
    ),
    TerminationReason.USER_CANCELLED: FailurePolicy(
        category=FailureCategory.USER_CANCELLED,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.CANCELLED,
    ),
    TerminationReason.AGENT_GAVE_UP: FailurePolicy(
        category=FailureCategory.AGENT_DECISION,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.GAVE_UP,
    ),
    TerminationReason.TOOL_USE_STOP: FailurePolicy(
        category=FailureCategory.AGENT_DECISION,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.SUCCESS,
    ),
    TerminationReason.MODEL_ERROR: FailurePolicy(
        category=FailureCategory.MODEL_ERROR,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.FAILED,
    ),
    TerminationReason.PROMPT_TOO_LONG: FailurePolicy(
        category=FailureCategory.MODEL_ERROR,
        behavior=BoundaryBehavior.RECOVERABLE,
        max_recovery_attempts=1,
        expected_status=RunStatus.FAILED,
    ),
    TerminationReason.ENVIRONMENT_UNAVAILABLE: FailurePolicy(
        category=FailureCategory.ENVIRONMENT_BLOCKED,
        behavior=BoundaryBehavior.HALT_AFTER_INJECT,
        expected_status=RunStatus.BLOCKED,
    ),
    TerminationReason.ABORTED_TOOLS: FailurePolicy(
        category=FailureCategory.USER_CANCELLED,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.CANCELLED,
    ),
    TerminationReason.INTERNAL_ERROR: FailurePolicy(
        category=FailureCategory.INTERNAL_ERROR,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.FAILED,
    ),
}


def classify_termination(reason: TerminationReason) -> FailurePolicy:
    """Look up the harness policy for a termination reason.

    Returns INTERNAL_ERROR policy for unknown reasons (fail-closed).
    """
    return FAILURE_TAXONOMY.get(reason, FailurePolicy(
        category=FailureCategory.INTERNAL_ERROR,
        behavior=BoundaryBehavior.HALT_IMMEDIATELY,
        expected_status=RunStatus.FAILED,
    ))


def verify_boundary_preserved(
    termination_reason: TerminationReason,
    actual_status: RunStatus,
) -> tuple[bool, str]:
    """Verify that the run status is consistent with the termination policy.

    Returns (passed, reason) tuple. Used by harness gates to reject runs
    where the boundary was not preserved.
    """
    policy = classify_termination(termination_reason)
    if policy.expected_status is None:
        return True, "no expected status constraint"
    if actual_status == policy.expected_status:
        return True, "status matches policy"
    return False, (
        f"boundary violation: {termination_reason.value} should produce "
        f"{policy.expected_status.value} but got {actual_status.value}"
    )


def termination_to_harness_label(reason: TerminationReason) -> str:
    """Map a termination reason to a short stable label for harness output."""
    policy = classify_termination(reason)
    return f"{policy.category.value}:{reason.value}"
