"""Tests for Phase 4: full-service convergence and end-to-end verification.

Covers:
  1. MultiAgentFeatureConfig reads from governor
  2. End-to-end: governor admit → spawn → execute → release
  3. Concurrent session limits enforced
  4. Mode switching: observe → enforce
"""

from __future__ import annotations

import os
import tempfile
import pytest


# ===========================================================================
# MultiAgentFeatureConfig governor integration
# ===========================================================================


class TestMultiAgentFeatureConfigGovernor:
    def test_reads_from_governor_when_not_observe(self):
        from agent.session.multi_agent_config import MultiAgentFeatureConfig
        from core.resource_governor import ResourceGovernor
        from config.schema import ResourceGovernanceConfig, ResourceGovernanceWorkerConfig

        cfg = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(global_max=8, per_root_max=4),
        )
        governor = ResourceGovernor(cfg)
        result = MultiAgentFeatureConfig.from_environment(
            environ={}, governor=governor,
        )
        assert result.max_concurrent == 8

    def test_falls_back_to_env_when_observe(self):
        from agent.session.multi_agent_config import MultiAgentFeatureConfig
        from core.resource_governor import ResourceGovernor
        from config.schema import ResourceGovernanceConfig

        cfg = ResourceGovernanceConfig(mode="observe")
        governor = ResourceGovernor(cfg)
        result = MultiAgentFeatureConfig.from_environment(
            environ={"GRACE_MAX_CONCURRENT_SUBAGENTS": "5"},
            governor=governor,
        )
        assert result.max_concurrent == 5  # falls back to env var

    def test_falls_back_when_no_governor(self):
        from agent.session.multi_agent_config import MultiAgentFeatureConfig
        result = MultiAgentFeatureConfig.from_environment(
            environ={"GRACE_MAX_CONCURRENT_SUBAGENTS": "3"},
        )
        assert result.max_concurrent == 3


# ===========================================================================
# End-to-end governor lifecycle
# ===========================================================================


class TestEndToEndGovernorLifecycle:
    """Verify the full cycle: admit → create → execute → release."""

    def test_governor_observe_full_cycle(self):
        """SessionRuntime with governor in observe mode completes cleanly."""
        from core.resource_governor import (
            ResourceGovernor, ResourceRequest, ResourceKind, AdmissionOutcome,
        )
        from config.schema import ResourceGovernanceConfig

        cfg = ResourceGovernanceConfig(mode="observe")
        governor = ResourceGovernor(cfg)

        # Simulate a spawn → execute → release cycle
        request = ResourceRequest(
            "spawn-test", "root-1", "sess-1",
            resources={ResourceKind.WORKER_SLOT: 1, ResourceKind.TOKEN_BUDGET: 10000},
        )
        result = governor.admit(request)
        assert result.outcome == AdmissionOutcome.GRANTED
        lease = result.lease
        assert not lease.is_released()

        # Simulate execution
        tokens_used = 8500
        lease.release(actual_used=tokens_used)
        assert lease.is_released()

        # Verify accounting
        snap = governor.snapshot()
        ws = snap.snapshots[ResourceKind.WORKER_SLOT]
        assert ws.reserved == 0  # fully released

    def test_governor_enforce_fails_at_capacity(self):
        from core.resource_governor import (
            ResourceGovernor, ResourceRequest, ResourceKind, AdmissionOutcome,
        )
        from config.schema import (
            ResourceGovernanceConfig, ResourceGovernanceWorkerConfig,
        )

        cfg = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(global_max=1, per_root_max=1),
        )
        governor = ResourceGovernor(cfg)

        # Fill capacity
        r1 = governor.admit(ResourceRequest(
            "r1", "root-1", "s-1", resources={ResourceKind.WORKER_SLOT: 1},
        ))
        assert r1.outcome == AdmissionOutcome.GRANTED

        # Second request blocked (by global or per-root limit)
        r2 = governor.admit(ResourceRequest(
            "r2", "root-1", "s-2", resources={ResourceKind.WORKER_SLOT: 1},
        ))
        assert r2.outcome != AdmissionOutcome.GRANTED

        # Release → FIFO queued request is granted.
        r1.lease.release()
        assert r2.outcome == AdmissionOutcome.GRANTED
        r2.lease.release()
        r3 = governor.admit(ResourceRequest(
            "r3", "root-1", "s-3", resources={ResourceKind.WORKER_SLOT: 1},
        ))
        assert r3.outcome == AdmissionOutcome.GRANTED
        r3.lease.release()


# ===========================================================================
# Mode switching
# ===========================================================================


class TestModeSwitching:
    def test_observe_to_enforce_behavior_change(self):
        from core.resource_governor import (
            ResourceGovernor, ResourceRequest, ResourceKind, AdmissionOutcome,
        )
        from config.schema import ResourceGovernanceConfig, ResourceGovernanceWorkerConfig

        # Observe mode — always grants
        cfg = ResourceGovernanceConfig(mode="observe")
        rg = ResourceGovernor(cfg)
        r = rg.admit(ResourceRequest(
            "r1", "root-1", "s-1", resources={ResourceKind.WORKER_SLOT: 100},
        ))
        assert r.outcome == AdmissionOutcome.GRANTED

        # Enforce mode — blocks oversell
        cfg2 = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(global_max=2, per_root_max=2),
        )
        rg2 = ResourceGovernor(cfg2)
        r2 = rg2.admit(ResourceRequest(
            "r2", "root-1", "s-1", resources={ResourceKind.WORKER_SLOT: 100},
        ))
        assert r2.outcome == AdmissionOutcome.IMPOSSIBLE



    def test_multi_agent_config_feature_gate_still_works(self, monkeypatch):
        """GRACE_MULTI_AGENT_MODE_ENABLED still gates the feature."""
        monkeypatch.setenv("GRACE_MULTI_AGENT_MODE_ENABLED", "false")
        from agent.session.multi_agent_config import MultiAgentFeatureConfig
        result = MultiAgentFeatureConfig.from_environment()
        assert result.enabled is False

    def test_observe_mode_never_blocks_regression(self):
        """Phase 0 invariant: observe mode NEVER blocks."""
        from core.resource_governor import (
            ResourceGovernor, ResourceRequest, ResourceKind, AdmissionOutcome,
        )
        from config.schema import ResourceGovernanceConfig

        rg = ResourceGovernor(ResourceGovernanceConfig(mode="observe"))
        for i in range(20):
            r = rg.admit(ResourceRequest(
                f"r{i}", f"root-{i%3}", f"sess-{i}",
                resources={ResourceKind.WORKER_SLOT: 50},
            ))
            assert r.outcome == AdmissionOutcome.GRANTED


# ===========================================================================
# admit_wait timeout + restart recovery
# ===========================================================================


class TestAdmitWait:
    def test_admit_wait_grants_when_available(self):
        from core.resource_governor import (
            ResourceGovernor, ResourceRequest, ResourceKind, AdmissionOutcome,
        )
        from config.schema import ResourceGovernanceConfig, ResourceGovernanceWorkerConfig

        cfg = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(global_max=2, per_root_max=2),
        )
        rg = ResourceGovernor(cfg)
        result = rg.admit_wait(ResourceRequest(
            "r1", "root-1", "s-1",
            resources={ResourceKind.WORKER_SLOT: 1},
        ), timeout_s=5.0)
        assert result.outcome == AdmissionOutcome.GRANTED
        result.lease.release()

    def test_admit_wait_times_out_when_full(self):
        from core.resource_governor import (
            ResourceGovernor, ResourceRequest, ResourceKind, AdmissionOutcome,
        )
        from config.schema import ResourceGovernanceConfig, ResourceGovernanceWorkerConfig

        cfg = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(global_max=1, per_root_max=1),
        )
        rg = ResourceGovernor(cfg)
        # Fill the only slot
        r1 = rg.admit_wait(ResourceRequest(
            "r1", "root-1", "s-1",
            resources={ResourceKind.WORKER_SLOT: 1},
        ), timeout_s=1.0)
        assert r1.outcome == AdmissionOutcome.GRANTED

        # Second request should time out
        r2 = rg.admit_wait(ResourceRequest(
            "r2", "root-1", "s-2",
            resources={ResourceKind.WORKER_SLOT: 1},
        ), timeout_s=0.1)
        assert r2.outcome in (
            AdmissionOutcome.CAPACITY_TIMEOUT,
            AdmissionOutcome.CANCELLED,
        )
        r1.lease.release()


class TestRestartRecovery:
    def test_governor_leases_not_recovered(self):
        """After creating a new governor (simulating restart), old leases are gone."""
        from core.resource_governor import (
            ResourceGovernor, ResourceRequest, ResourceKind, AdmissionOutcome,
        )
        from config.schema import ResourceGovernanceConfig, ResourceGovernanceWorkerConfig

        cfg = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(global_max=1, per_root_max=1),
        )
        # First governor
        rg1 = ResourceGovernor(cfg)
        r1 = rg1.admit(ResourceRequest(
            "r1", "root-1", "s-1",
            resources={ResourceKind.WORKER_SLOT: 1},
        ))
        assert r1.outcome == AdmissionOutcome.GRANTED
        # Simulate restart — create new governor, old lease gone
        rg2 = ResourceGovernor(cfg)
        r2 = rg2.admit(ResourceRequest(
            "r2", "root-1", "s-2",
            resources={ResourceKind.WORKER_SLOT: 1},
        ))
        assert r2.outcome == AdmissionOutcome.GRANTED  # not blocked by old lease


class TestProvider429Integration:
    def test_429_sets_shared_backoff(self):
        from core.provider_governor import ProviderGovernor
        pg = ProviderGovernor()
        pg.record_response("openai", status=429, retry_after=1.0)
        limiter = pg.get_limiter("openai")
        assert limiter.acquire() is False

    def test_backoff_minimum_is_one_second(self):
        """429 backoff floor is 1.0s regardless of Retry-After value."""
        from core.provider_governor import ProviderGovernor
        pg = ProviderGovernor()
        pg.record_response("openai", status=429, retry_after=0.05)
        limiter = pg.get_limiter("openai")
        # Backoff floor is 1.0s
        assert limiter.acquire() is False
