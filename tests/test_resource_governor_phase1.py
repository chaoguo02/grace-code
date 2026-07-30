"""Tests for Phase 1 resource governance: enforce mode, per-root limits, cancel, queue drain.

Covers:
  1. Enforce mode — global + per-root limits
  2. Queue drain — release triggers grants
  3. Cancel — removes from queue
  4. Impossible detection
  5. Token estimator accuracy
  6. Regression — observe mode unchanged
"""

from __future__ import annotations

import pytest
from core.resource_governor import (
    AdmissionOutcome,
    ResourceGovernor,
    ResourceKind,
    ResourceLease,
    ResourcePressure,
    ResourceRequest,
)
from core.token_estimator import estimate_tokens
from config.schema import (
    ResourceGovernanceConfig,
    ResourceGovernanceWorkerConfig,
)


# ===========================================================================
# Enforce Mode Tests
# ===========================================================================


class TestEnforceMode:
    """Global and per-root capacity enforcement."""

    def _governor(self, global_max=2, per_root_max=2):
        cfg = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(
                global_max=global_max, per_root_max=per_root_max,
            ),
        )
        return ResourceGovernor(cfg)

    def test_grants_when_under_limit(self):
        rg = self._governor(global_max=2)
        result = rg.admit(ResourceRequest(
            request_id="r1", root_session_id="root-1", session_id="s-1",
            resources={ResourceKind.WORKER_SLOT: 1},
        ))
        assert result.outcome == AdmissionOutcome.GRANTED
        assert result.lease is not None

    def test_blocks_when_over_global_limit(self):
        rg = self._governor(global_max=2)
        rg.admit(ResourceRequest("r1", "root-1", "s-1", resources={ResourceKind.WORKER_SLOT: 1}))
        rg.admit(ResourceRequest("r2", "root-1", "s-2", resources={ResourceKind.WORKER_SLOT: 1}))
        # Third request should not grant
        result = rg.admit(ResourceRequest("r3", "root-1", "s-3", resources={ResourceKind.WORKER_SLOT: 1}))
        assert result.outcome != AdmissionOutcome.GRANTED

    def test_blocks_when_over_per_root_limit(self):
        rg = self._governor(global_max=10, per_root_max=1)
        rg.admit(ResourceRequest("r1", "root-1", "s-1", resources={ResourceKind.WORKER_SLOT: 1}))
        result = rg.admit(ResourceRequest("r2", "root-1", "s-2", resources={ResourceKind.WORKER_SLOT: 1}))
        assert result.outcome != AdmissionOutcome.GRANTED

    def test_different_roots_independent(self):
        rg = self._governor(global_max=10, per_root_max=1)
        rg.admit(ResourceRequest("r1", "root-A", "s-1", resources={ResourceKind.WORKER_SLOT: 1}))
        # Different root should be fine
        result = rg.admit(ResourceRequest("r2", "root-B", "s-2", resources={ResourceKind.WORKER_SLOT: 1}))
        assert result.outcome == AdmissionOutcome.GRANTED

    def test_per_root_usage_tracking(self):
        rg = self._governor(per_root_max=2)
        rg.admit(ResourceRequest("r1", "root-A", "s-1", resources={ResourceKind.WORKER_SLOT: 1}))
        assert rg.get_per_root_usage("root-A", ResourceKind.WORKER_SLOT) == 1


class TestImpossibleDetection:
    def _governor(self, global_max=2):
        cfg = ResourceGovernanceConfig(mode="enforce")
        return ResourceGovernor(cfg)

    def test_impossible_when_exceeds_total(self):
        rg = self._governor(global_max=2)
        result = rg.admit(ResourceRequest(
            "r1", "root-1", "s-1",
            resources={ResourceKind.WORKER_SLOT: 10},
        ))
        assert result.outcome == AdmissionOutcome.IMPOSSIBLE


class TestQueueDrain:
    def _governor(self, global_max=1, per_root_max=1):
        cfg = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(
                global_max=global_max, per_root_max=per_root_max,
            ),
        )
        return ResourceGovernor(cfg)

    def test_capacity_freed_after_release(self):
        """Release grants the FIFO head instead of letting newcomers bypass."""
        rg = self._governor(global_max=1, per_root_max=1)
        r1 = rg.admit(ResourceRequest("r1", "root-1", "s-1", resources={ResourceKind.WORKER_SLOT: 1}))
        assert r1.outcome == AdmissionOutcome.GRANTED

        # Capacity full — queue
        r2 = rg.admit(ResourceRequest("r2", "root-1", "s-2", resources={ResourceKind.WORKER_SLOT: 1}))
        assert r2.outcome == AdmissionOutcome.QUEUED

        # Release first lease — queued request receives the freed slot.
        r1.lease.release()
        snap = rg.snapshot()
        ws = snap.snapshots[ResourceKind.WORKER_SLOT]
        assert ws.reserved == 1
        assert ws.available == 0
        assert ws.queued == 0
        assert r2.outcome == AdmissionOutcome.GRANTED
        assert r2.lease is not None

        r2.lease.release()
        r3 = rg.admit(ResourceRequest("r3", "root-1", "s-3", resources={ResourceKind.WORKER_SLOT: 1}))
        assert r3.outcome == AdmissionOutcome.GRANTED


class TestCancelRequest:
    def _governor(self):
        cfg = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(
                global_max=1, per_root_max=1,
            ),
        )
        return ResourceGovernor(cfg)

    def test_cancel_removes_from_queue(self):
        rg = self._governor()
        rg.admit(ResourceRequest("r1", "root-1", "s-1", resources={ResourceKind.WORKER_SLOT: 1}))
        # Queue a request
        r2 = rg.admit(ResourceRequest("r2", "root-1", "s-2", resources={ResourceKind.WORKER_SLOT: 1}))
        assert r2.outcome == AdmissionOutcome.QUEUED

        # Cancel it
        assert rg.cancel_request("r2") is True
        # Second cancel is no-op
        assert rg.cancel_request("r2") is False

    def test_cancel_nonexistent(self):
        rg = self._governor()
        assert rg.cancel_request("nonexistent") is False


class TestShutdown:
    def _governor(self):
        return ResourceGovernor(ResourceGovernanceConfig(mode="enforce"))

    def test_shutdown_rejects_new(self):
        rg = self._governor()
        rg.shutdown()
        result = rg.admit(ResourceRequest("r1", "root-1", "s-1", resources={ResourceKind.WORKER_SLOT: 1}))
        assert result.outcome == AdmissionOutcome.SHUTDOWN


# ===========================================================================
# Token Estimator Test
# ===========================================================================


class TestTokenEstimator:
    def test_estimate_text(self):
        result = estimate_tokens("hello world")
        assert result == max(1, int(len("hello world") / 3.5))


# ===========================================================================
# Observe Mode Regression
# ===========================================================================


class TestObserveModeRegression:
    """Phase 0 behavior preserved when mode=observe."""

    def _governor(self):
        return ResourceGovernor(ResourceGovernanceConfig(mode="observe"))

    def test_always_grants(self):
        rg = self._governor()
        for i in range(10):
            result = rg.admit(ResourceRequest(
                f"r{i}", "root-1", f"s-{i}",
                resources={ResourceKind.WORKER_SLOT: 10},
            ))
            assert result.outcome == AdmissionOutcome.GRANTED

    def test_blocked_would_be_updated(self):
        rg = self._governor()
        rg.admit(ResourceRequest("r1", "root-1", "s-1", resources={ResourceKind.WORKER_SLOT: 10}))
        blocked = rg.blocked_would_be_counts()
        assert blocked.get("WORKER_SLOT", 0) > 0
