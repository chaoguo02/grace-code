"""Tests for Phase 0 resource governance: config loading, observe mode, invariants.

Covers:
  1. Config loading — defaults, YAML parsing, compat mapping
  2. Observe mode — always grants, records would-block facts
  3. Core invariants — idempotent release, conservation, shutdown
  4. ResourceMetricsCollector — snapshot collection and export
  5. Edge cases — zero limits, oversized requests, context manager
"""

from __future__ import annotations

import logging
import pytest
from config.schema import (
    AppConfig,
    ResourceGovernanceConfig,
    ResourceGovernanceWorkerConfig,
    ResourceGovernanceQueueConfig,
    load_config,
    _parse,
)
from core.resource_governor import (
    AdmissionOutcome,
    ResourceGovernor,
    ResourceGovernorSnapshot,
    ResourceKind,
    ResourceLease,
    ResourcePressure,
    ResourceRequest,
    ResourceSnapshot,
)
from core.resource_metrics import ResourceMetricsCollector


# ===========================================================================
# Config Loading Tests
# ===========================================================================


class TestResourceGovernanceConfig:
    """Default and parsed config loading."""

    def test_default_config_enforces_resource_limits(self):
        cfg = ResourceGovernanceConfig()
        assert cfg.mode == "enforce"
        assert cfg.worker.global_max == 2
        assert cfg.worker.per_root_max == 2
        assert cfg.queue.max_size == 64
        assert cfg.queue.timeout_seconds == 120.0
        assert cfg.provider.rate_limit_enabled is False
        assert cfg.worktree.global_max == 10
        assert cfg.worktree.per_root_max == 3
        assert cfg.worktree.disk_limit_mb == 0
        assert cfg.shutdown.drain_timeout_seconds == 30.0
        assert cfg.shutdown.force_kill_seconds == 5.0

    def test_app_config_includes_resource_governance(self):
        cfg = AppConfig()
        assert cfg.resource_governance is not None
        assert cfg.resource_governance.mode == "enforce"

    def test_parse_from_yaml_dict_minimal(self):
        """_parse() should handle missing resource_governance section gracefully."""
        cfg = _parse({
            "llm": {}, "agent": {}, "tools": {}, "memory": {},
            "plan": {}, "multi_agent": {}, "context": {}, "hitl": {},
            "observability": {}, "prompts": {},
        })
        rg = cfg.resource_governance
        assert rg.mode == "enforce"
        assert rg.worker.global_max == 2

    def test_parse_from_yaml_dict_full(self):
        """_parse() correctly populates all resource_governance fields from dict."""
        cfg = _parse({
            "resource_governance": {
                "mode": "enforce",
                "worker": {"global_max": 4, "per_root_max": 3},
                "queue": {"max_size": 32, "timeout_seconds": 60.0},
                "provider": {"rate_limit_enabled": True, "rpm": 100, "tpm": 50000},
                "event": {"queue_max_size": 2048},
                "worktree": {"global_max": 5, "per_root_max": 2, "disk_limit_mb": 500},
                "shutdown": {"drain_timeout_seconds": 10.0, "force_kill_seconds": 2.0},
            },
            "llm": {}, "agent": {}, "tools": {}, "memory": {},
            "plan": {}, "multi_agent": {}, "context": {}, "hitl": {},
            "observability": {}, "prompts": {},
        })
        rg = cfg.resource_governance
        assert rg.mode == "enforce"
        assert rg.worker.global_max == 4
        assert rg.worker.per_root_max == 3
        assert rg.queue.max_size == 32
        assert rg.queue.timeout_seconds == 60.0
        assert rg.provider.rate_limit_enabled is True
        assert rg.provider.rpm == 100
        assert rg.provider.tpm == 50000
        assert rg.event.queue_max_size == 2048
        assert rg.worktree.global_max == 5
        assert rg.worktree.disk_limit_mb == 500
        assert rg.shutdown.drain_timeout_seconds == 10.0


# ===========================================================================
# Observe Mode Tests
# ===========================================================================


class TestResourceGovernorObserveMode:
    """ResourceGovernor in observe mode — the Phase 0 default."""

    def _governor(self, **overrides) -> ResourceGovernor:
        cfg = ResourceGovernanceConfig(mode="observe", **overrides)
        return ResourceGovernor(cfg)

    def test_always_grants_in_observe_mode(self):
        rg = self._governor()
        request = ResourceRequest(
            request_id="test-1",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 10},  # far exceeds limit of 2
        )
        result = rg.admit(request)
        assert result.outcome == AdmissionOutcome.GRANTED
        assert result.lease is not None
        assert not result.lease.is_released()

    def test_records_would_block_in_observe_mode(self):
        rg = self._governor()
        request = ResourceRequest(
            request_id="test-2",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 10},
        )
        rg.admit(request)
        blocked = rg.blocked_would_be_counts()
        assert blocked.get("WORKER_SLOT", 0) == 1

    def test_multiple_oversell_requests_all_recorded(self):
        rg = self._governor()
        for i in range(5):
            rg.admit(ResourceRequest(
                request_id=f"test-{i}",
                root_session_id="root-1",
                session_id=f"sess-{i}",
                resources={ResourceKind.WORKER_SLOT: 10},
            ))
        blocked = rg.blocked_would_be_counts()
        assert blocked.get("WORKER_SLOT", 0) == 5

    def test_shutdown_rejects_new_requests(self):
        rg = self._governor()
        rg.shutdown()

        request = ResourceRequest(
            request_id="test-3",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 1},
        )
        result = rg.admit(request)
        assert result.outcome == AdmissionOutcome.SHUTDOWN
        assert result.pressure == ResourcePressure.DRAINING

    def test_shutdown_is_idempotent(self):
        rg = self._governor()
        rg.shutdown()
        rg.shutdown()  # no-op
        assert rg.is_shutting_down


# ===========================================================================
# Lease Invariant Tests
# ===========================================================================


class TestResourceLeaseInvariants:
    """Core invariants for ResourceLease lifecycle."""

    def _governor(self) -> ResourceGovernor:
        cfg = ResourceGovernanceConfig(mode="observe")
        return ResourceGovernor(cfg)

    def test_lease_release_is_idempotent(self):
        rg = self._governor()
        request = ResourceRequest(
            request_id="test-4",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 1},
        )
        result = rg.admit(request)
        lease = result.lease
        assert lease is not None
        assert not lease.is_released()

        lease.release()
        assert lease.is_released()

        # Second release is a no-op
        lease.release()
        assert lease.is_released()

    def test_lease_context_manager(self):
        rg = self._governor()
        request = ResourceRequest(
            request_id="test-5",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 1},
        )
        result = rg.admit(request)
        lease = result.lease
        assert not lease.is_released()
        with lease:
            pass  # simulated work
        assert lease.is_released()

    def test_conservation_invariant(self):
        """reserved + consumed <= limit after release cycle."""
        rg = self._governor()
        r1 = rg.admit(ResourceRequest(
            request_id="test-6",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 1},
        ))
        r2 = rg.admit(ResourceRequest(
            request_id="test-7",
            root_session_id="root-1",
            session_id="sess-2",
            resources={ResourceKind.WORKER_SLOT: 1},
        ))
        snap = rg.snapshot()
        ws = snap.snapshots[ResourceKind.WORKER_SLOT]
        assert ws.reserved == 2
        assert ws.consumed == 0
        assert ws.reserved + ws.consumed <= ws.limit

        # Release one lease
        r1.lease.release()
        snap2 = rg.snapshot()
        ws2 = snap2.snapshots[ResourceKind.WORKER_SLOT]
        assert ws2.reserved + ws2.consumed <= ws2.limit

    def test_release_with_actual_usage(self):
        """Renewable worker capacity is fully returned on release."""
        rg = self._governor()
        result = rg.admit(ResourceRequest(
            request_id="test-8",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 5},
        ))
        # Use only 2 out of 5 reserved
        result.lease.release(actual_used=2)
        snap = rg.snapshot()
        ws = snap.snapshots[ResourceKind.WORKER_SLOT]
        # Worker slots are renewable, not cumulative quota.
        assert ws.reserved == 0
        assert ws.consumed == 0
        assert ws.available == ws.limit

    def test_multiple_distinct_resource_kinds(self):
        """Multiple resource kinds in one request."""
        rg = self._governor()
        result = rg.admit(ResourceRequest(
            request_id="test-9",
            root_session_id="root-1",
            session_id="sess-1",
            resources={
                ResourceKind.WORKER_SLOT: 1,
                ResourceKind.TOKEN_BUDGET: 10000,
            },
        ))
        assert result.outcome == AdmissionOutcome.GRANTED
        snap = rg.snapshot()
        # Both kinds should be tracked
        assert ResourceKind.WORKER_SLOT in snap.snapshots
        assert ResourceKind.TOKEN_BUDGET in snap.snapshots


# ===========================================================================
# Snapshot Tests
# ===========================================================================


class TestResourceGovernorSnapshot:
    """Snapshot accuracy."""

    def _governor(self) -> ResourceGovernor:
        cfg = ResourceGovernanceConfig(mode="observe")
        return ResourceGovernor(cfg)

    def test_empty_snapshot_reflects_defaults(self):
        rg = self._governor()
        snap = rg.snapshot()
        assert snap.mode == "observe"
        assert snap.total_grants == 0
        assert snap.total_rejections == 0
        assert snap.active_leases == 0

        ws = snap.snapshots[ResourceKind.WORKER_SLOT]
        assert ws.limit == 2
        assert ws.reserved == 0
        assert ws.consumed == 0
        assert ws.available == 2

    def test_snapshot_after_admit(self):
        rg = self._governor()
        rg.admit(ResourceRequest(
            request_id="test-snap-1",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 1},
        ))
        snap = rg.snapshot()
        assert snap.total_grants == 1
        assert snap.active_leases == 1

    def test_pressure_is_computed(self):
        rg = self._governor()
        # Request > 60% of limit (limit=2, request=2 → 100%)
        rg.admit(ResourceRequest(
            request_id="test-pressure-1",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 2},
        ))
        snap = rg.snapshot()
        ws = snap.snapshots[ResourceKind.WORKER_SLOT]
        # reserved=2, limit=2 → 100% → CRITICAL
        assert ws.pressure == ResourcePressure.CRITICAL


# ===========================================================================
# Edge Case Tests
# ===========================================================================


class TestEdgeCases:
    def _governor(self) -> ResourceGovernor:
        cfg = ResourceGovernanceConfig(mode="observe")
        return ResourceGovernor(cfg)

    def test_zero_amount_request(self):
        """Request with zero resource amount."""
        rg = self._governor()
        result = rg.admit(ResourceRequest(
            request_id="test-zero",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 0},
        ))
        assert result.outcome == AdmissionOutcome.GRANTED

    def test_empty_resources_request(self):
        """Request with no resources specified."""
        rg = self._governor()
        result = rg.admit(ResourceRequest(
            request_id="test-empty",
            root_session_id="root-1",
            session_id="sess-1",
            resources={},
        ))
        assert result.outcome == AdmissionOutcome.GRANTED
        assert result.lease is not None

    def test_unknown_mode_falls_back_to_observe(self, caplog):
        """Invalid mode string falls back to observe with warning."""
        cfg = ResourceGovernanceConfig(mode="invalid_mode")
        with caplog.at_level(logging.WARNING):
            rg = ResourceGovernor(cfg)
        assert rg.mode == "observe"
        fallback_msgs = [
            r.message for r in caplog.records
            if "Unknown resource_governance mode" in r.message
        ]
        assert len(fallback_msgs) == 1

    def test_high_resource_count_does_not_crash(self):
        """Many resource kinds in one request."""
        rg = self._governor()
        result = rg.admit(ResourceRequest(
            request_id="test-many",
            root_session_id="root-1",
            session_id="sess-1",
            resources={
                kind: 1 for kind in ResourceKind
            },
        ))
        assert result.outcome == AdmissionOutcome.GRANTED


# ===========================================================================
# ResourceMetricsCollector Tests
# ===========================================================================


class TestResourceMetricsCollector:
    def _governor(self) -> ResourceGovernor:
        cfg = ResourceGovernanceConfig(mode="observe")
        return ResourceGovernor(cfg)

    def test_collect_produces_valid_snapshot(self):
        rg = self._governor()
        collector = ResourceMetricsCollector(rg)

        snap = collector.collect()
        assert snap.mode == "observe"
        assert ResourceKind.WORKER_SLOT in snap.snapshots

    def test_collect_with_activity(self):
        rg = self._governor()
        collector = ResourceMetricsCollector(rg)

        rg.admit(ResourceRequest(
            request_id="test-metrics-1",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 2},
        ))
        collector.collect()
        metrics = collector.export()

        assert metrics["mode"] == "observe"
        assert metrics["active_leases"] == 1
        assert "blocked_would_be" in metrics
        assert len(metrics["snapshots"]) > 0

    def test_export_format_keys(self):
        rg = self._governor()
        collector = ResourceMetricsCollector(rg)
        collector.collect()
        metrics = collector.export()

        # Verify all expected top-level keys
        for key in ("mode", "timestamp_s", "total_grants", "total_rejections",
                     "total_timeouts", "active_leases", "snapshots",
                     "blocked_would_be"):
            assert key in metrics

        # Verify snapshot entries have expected keys
        for entry in metrics["snapshots"]:
            for key in ("kind", "limit", "consumed", "reserved",
                         "available", "queued", "pressure", "utilization_pct"):
                assert key in entry

    def test_pressure_summary(self):
        rg = self._governor()
        collector = ResourceMetricsCollector(rg)

        # Normal state
        collector.collect()
        summary = collector.pressure_summary()
        assert summary["overall"] == "NORMAL"
        assert summary["critical_resources"] == []

        # Create pressure by reserving workers
        rg.admit(ResourceRequest(
            request_id="test-pressure-2",
            root_session_id="root-1",
            session_id="sess-1",
            resources={ResourceKind.WORKER_SLOT: 2},
        ))
        collector.collect()
        summary2 = collector.pressure_summary()
        assert summary2["overall"] == "CRITICAL"
        assert "WORKER_SLOT" in summary2["critical_resources"]

    def test_snapshot_history_capped(self):
        rg = self._governor()
        collector = ResourceMetricsCollector(rg, max_history=5)

        for i in range(10):
            collector.collect()
        assert len(collector.snapshot_history) == 5

    def test_observe_samples_produce_capacity_recommendations(self):
        rg = self._governor()
        collector = ResourceMetricsCollector(rg)
        rg.admit(ResourceRequest(
            request_id="observed-load",
            root_session_id="root",
            session_id="session",
            resources={ResourceKind.WORKER_SLOT: 3},
        ))
        collector.collect()

        worker = collector.capacity_recommendations()["worker_slot"]
        assert worker["observed_peak"] == 3
        assert worker["sample_count"] == 1

    def test_latest_returns_none_when_empty(self):
        rg = self._governor()
        collector = ResourceMetricsCollector(rg)
        assert collector.latest() is None
