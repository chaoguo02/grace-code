"""G36: Resource/Approval Authority — direct-call, no EventBus.

AC: Resource acquire succeeds when free, fails when held
AC: release_all cleans up all resources for a run
AC: Facts only on successful acquire/release
AC: Approval is direct-call, not EventBus event
AC: approve/deny returns typed ApprovalResult
AC: Pending requests tracked and cleared on decision
"""

import pytest

from application.resources.resource_coordinator import (
    ResourceCoordinator, ResourceLease,
)
from application.approvals.approval_coordinator import (
    ApprovalCoordinator, ApprovalResult, ApprovalDecision,
)


# ═══════════════════════════════════════════════════════════════════════════════
# G36.1 — Resource Coordinator
# ═══════════════════════════════════════════════════════════════════════════════

class TestResourceCoordinator:
    """G36: Resource acquire/release via direct-call Coordinator."""

    def test_acquire_succeeds_when_free(self):
        rc = ResourceCoordinator()
        lease = rc.acquire("gpu-0", "r1")
        assert lease is not None
        assert rc.is_acquired("gpu-0")

    def test_acquire_fails_when_held(self):
        rc = ResourceCoordinator()
        rc.acquire("gpu-0", "r1")
        assert rc.acquire("gpu-0", "r2") is None

    def test_release_by_owner_succeeds(self):
        rc = ResourceCoordinator()
        rc.acquire("gpu-0", "r1")
        assert rc.release("gpu-0", "r1") is True
        assert not rc.is_acquired("gpu-0")

    def test_release_by_non_owner_fails(self):
        rc = ResourceCoordinator()
        rc.acquire("gpu-0", "r1")
        assert rc.release("gpu-0", "r2") is False
        assert rc.is_acquired("gpu-0")  # still held by r1

    def test_release_all_cleans_up(self):
        rc = ResourceCoordinator()
        rc.acquire("a", "r1")
        rc.acquire("b", "r1")
        rc.acquire("c", "r2")

        count = rc.release_all("r1")
        assert count == 2
        assert rc.held_count == 1  # only r2's resource

    def test_facts_only_on_success(self):
        rc = ResourceCoordinator()
        rc.acquire("x", "r1")  # success → fact
        rc.acquire("x", "r2")  # fail → no fact
        rc.release("x", "r1")  # success → fact

        facts = rc.acquisition_facts
        assert len(facts) == 2, (
            f"G36: facts only on successful acquire/release, got {len(facts)}"
        )

    def test_resource_lease_is_frozen(self):
        lease = ResourceLease(resource_id="gpu-0", owner_run_id="r1")
        with pytest.raises(Exception):
            lease.resource_id = "gpu-1"  # type: ignore


# ═══════════════════════════════════════════════════════════════════════════════
# G36.2 — Approval Coordinator
# ═══════════════════════════════════════════════════════════════════════════════

class TestApprovalCoordinator:
    """G36: Approval is direct-call, not EventBus Command."""

    def test_approve_returns_typed_result(self):
        ac = ApprovalCoordinator()
        ac.request_approval("write", "t1", "needs user confirmation")
        result = ac.approve("t1", approved_by="user", reason="looks safe")

        assert isinstance(result, ApprovalResult)
        assert result.decision == ApprovalDecision.APPROVED
        assert result.approved_by == "user"

    def test_deny_returns_typed_result(self):
        ac = ApprovalCoordinator()
        ac.request_approval("delete", "t2")
        result = ac.deny("t2", reason="dangerous operation")

        assert result.decision == ApprovalDecision.DENIED
        assert "dangerous" in result.reason

    def test_pending_cleared_after_decision(self):
        ac = ApprovalCoordinator()
        ac.request_approval("write", "t1")
        assert ac.is_pending("t1")

        ac.approve("t1")
        assert not ac.is_pending("t1")

    def test_history_tracks_all_decisions(self):
        ac = ApprovalCoordinator()
        ac.request_approval("a", "t1")
        ac.request_approval("b", "t2")
        ac.approve("t1")
        ac.deny("t2")

        assert len(ac.history) == 2

    def test_pending_count(self):
        ac = ApprovalCoordinator()
        assert ac.pending_count == 0
        ac.request_approval("x", "t1")
        ac.request_approval("y", "t2")
        assert ac.pending_count == 2

    def test_approval_result_is_frozen(self):
        result = ApprovalResult(decision=ApprovalDecision.APPROVED, reason="ok")
        with pytest.raises(Exception):
            result.decision = ApprovalDecision.DENIED  # type: ignore
