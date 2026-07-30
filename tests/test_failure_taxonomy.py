"""Tests for failure taxonomy and recovery policy (Phase 15 Issue 03)."""

import pytest

from agent.task import RunStatus, TerminationReason
from core.resource_governor import AdmissionOutcome, ResourceKind
from observability.failure_policy import (
    BoundaryBehavior,
    FailureCategory,
    FailurePolicy,
    ResourceFailureCategory,
    FAILURE_TAXONOMY,
    classify_resource_failure,
    classify_termination,
    termination_to_harness_label,
    verify_boundary_preserved,
)


@pytest.mark.parametrize("kind,expected", [
    (ResourceKind.TOKEN_BUDGET, ResourceFailureCategory.BUDGET_EXHAUSTED),
    (ResourceKind.PROVIDER_RPM, ResourceFailureCategory.PROVIDER_THROTTLED),
    (ResourceKind.WORKER_SLOT, ResourceFailureCategory.THREAD_CAPACITY),
    (ResourceKind.TOOL_SLOT, ResourceFailureCategory.THREAD_CAPACITY),
    (ResourceKind.DISK_BYTES, ResourceFailureCategory.DISK_CAPACITY),
    (ResourceKind.EVENT_CAPACITY, ResourceFailureCategory.EVENT_BACKPRESSURE),
    (ResourceKind.DB_WRITE_CAPACITY, ResourceFailureCategory.DB_BACKPRESSURE),
])
def test_resource_admission_failures_have_actionable_categories(kind, expected):
    assert classify_resource_failure(
        AdmissionOutcome.CAPACITY_TIMEOUT, kind
    ) is expected


class TestFailureTaxonomyCompleteness:
    """Every TerminationReason must map to a policy."""

    def test_all_reasons_have_a_policy(self):
        for reason in TerminationReason:
            policy = classify_termination(reason)
            assert isinstance(policy, FailurePolicy), f"no policy for {reason.value}"
            assert isinstance(policy.category, FailureCategory)
            assert isinstance(policy.behavior, BoundaryBehavior)

    def test_taxonomy_covers_all_enum_members(self):
        covered = set(FAILURE_TAXONOMY.keys())
        expected = set(TerminationReason)
        assert covered == expected, f"missing: {expected - covered}, extra: {covered - expected}"


class TestBoundaryPreservation:
    """After failure, the harness MUST produce the expected status."""

    @pytest.mark.parametrize("reason,expected_status", [
        (TerminationReason.BUDGET_EXHAUSTED, RunStatus.GAVE_UP),
        (TerminationReason.MAX_STEPS, RunStatus.MAX_STEPS),
        (TerminationReason.CIRCUIT_BREAKER, RunStatus.GAVE_UP),
        (TerminationReason.TOOL_FAILURE_LIMIT, RunStatus.GAVE_UP),
        (TerminationReason.GUARD_REJECTED, RunStatus.GAVE_UP),
        (TerminationReason.HOOK_STOPPED, RunStatus.GAVE_UP),
        (TerminationReason.USER_CANCELLED, RunStatus.CANCELLED),
        (TerminationReason.MODEL_ERROR, RunStatus.FAILED),
        (TerminationReason.ENVIRONMENT_UNAVAILABLE, RunStatus.BLOCKED),
    ])
    def test_boundary_preserved_correct_status(self, reason, expected_status):
        passed, msg = verify_boundary_preserved(reason, expected_status)
        assert passed, msg

    @pytest.mark.parametrize("reason,wrong_status", [
        (TerminationReason.BUDGET_EXHAUSTED, RunStatus.SUCCESS),
        (TerminationReason.MAX_STEPS, RunStatus.SUCCESS),
        (TerminationReason.CIRCUIT_BREAKER, RunStatus.SUCCESS),
        (TerminationReason.USER_CANCELLED, RunStatus.SUCCESS),
        (TerminationReason.MODEL_ERROR, RunStatus.SUCCESS),
    ])
    def test_boundary_violation_detected(self, reason, wrong_status):
        passed, msg = verify_boundary_preserved(reason, wrong_status)
        assert not passed
        assert "boundary violation" in msg


class TestRecoveryPolicy:
    """Verify that recovery attempts are bounded."""

    def test_resource_exhausted_no_recovery(self):
        policy = classify_termination(TerminationReason.BUDGET_EXHAUSTED)
        assert policy.behavior == BoundaryBehavior.HALT_IMMEDIATELY
        assert policy.max_recovery_attempts == 0

    def test_prompt_too_long_allows_one_recovery(self):
        policy = classify_termination(TerminationReason.PROMPT_TOO_LONG)
        assert policy.behavior == BoundaryBehavior.RECOVERABLE
        assert policy.max_recovery_attempts == 1

    def test_environment_blocked_injects_then_halts(self):
        policy = classify_termination(TerminationReason.ENVIRONMENT_UNAVAILABLE)
        assert policy.behavior == BoundaryBehavior.HALT_AFTER_INJECT

    def test_all_halt_immediately_have_zero_recovery(self):
        for reason, policy in FAILURE_TAXONOMY.items():
            if policy.behavior == BoundaryBehavior.HALT_IMMEDIATELY:
                assert policy.max_recovery_attempts == 0, (
                    f"{reason.value} has HALT_IMMEDIATELY but max_recovery_attempts={policy.max_recovery_attempts}"
                )


class TestHarnessLabels:
    def test_label_format(self):
        label = termination_to_harness_label(TerminationReason.BUDGET_EXHAUSTED)
        assert label == "resource_exhausted:budget_exhausted"

    def test_label_for_authority_denied(self):
        label = termination_to_harness_label(TerminationReason.GUARD_REJECTED)
        assert label == "authority_denied:guard_rejected"

    def test_label_for_agent_decision(self):
        label = termination_to_harness_label(TerminationReason.AGENT_GAVE_UP)
        assert label == "agent_decision:agent_gave_up"
