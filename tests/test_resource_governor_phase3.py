"""Tests for Phase 3: provider, event, DB, and worktree governance."""

from __future__ import annotations

import time as _time
import pytest


# ===========================================================================
# Area A: ProviderGovernor Tests
# ===========================================================================


class TestProviderGovernor:
    def test_limiter_acquire_no_limits(self):
        from core.provider_governor import ProviderRateLimiter
        limiter = ProviderRateLimiter()  # all 0 = unlimited
        assert limiter.acquire() is True
        limiter.release()

    def test_limiter_concurrent_limit(self):
        from core.provider_governor import ProviderRateLimiter
        limiter = ProviderRateLimiter(max_concurrent=1)
        assert limiter.acquire() is True
        assert limiter.acquire() is False  # second acquire fails
        limiter.release()
        assert limiter.acquire() is True   # now available

    def test_limiter_rpm_limit(self):
        from core.provider_governor import ProviderRateLimiter
        limiter = ProviderRateLimiter(rpm_limit=2)
        assert limiter.acquire() is True
        assert limiter.acquire() is True
        assert limiter.acquire() is False  # RPM exceeded

    def test_shared_backoff_after_429(self):
        from core.provider_governor import ProviderRateLimiter
        limiter = ProviderRateLimiter(max_concurrent=10)
        limiter.record_429(retry_after_s=0.5)
        assert limiter.acquire() is False  # blocked by backoff

    def test_provider_governor_get_limiter(self):
        from core.provider_governor import ProviderGovernor
        pg = ProviderGovernor()
        limiter = pg.get_limiter("deepseek")
        assert limiter is not None
        # Same provider returns same limiter
        assert pg.get_limiter("deepseek") is limiter
        # Different provider returns different limiter
        assert pg.get_limiter("openai") is not limiter

    def test_record_429_triggers_backoff(self):
        from core.provider_governor import ProviderGovernor
        pg = ProviderGovernor()
        pg.record_response("openai", status=429, retry_after=0.5)
        limiter = pg.get_limiter("openai")
        assert limiter.acquire() is False


# ===========================================================================
# Area B: EventBus Bounded Queue Tests
# ===========================================================================


# ===========================================================================
# Area D: Worktree Governance Tests
# ===========================================================================


class TestWorktreeGovernance:
    def test_disk_space_check_passes_when_free(self, tmp_path):
        from agent.session.worktree_service import _check_disk_space
        # tmp_path is on a filesystem with plenty of space
        _check_disk_space(str(tmp_path))  # should not raise

    def test_disk_space_check_includes_estimated_checkout_size(
        self, tmp_path, monkeypatch,
    ):
        import shutil
        import agent.session.worktree_service as service

        monkeypatch.setattr(
            service, "_estimate_checkout_bytes", lambda _: 200 * 1024 * 1024,
        )
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _: shutil._ntuple_diskusage(
                1_000 * 1024 * 1024,
                800 * 1024 * 1024,
                220 * 1024 * 1024,
            ),
        )
        with pytest.raises(
            service.WorktreeIsolationError,
            match="estimated checkout 200 MB",
        ):
            service._check_disk_space(str(tmp_path))

    def test_quota_check_allows_when_under_limit(self):
        from agent.session.worktree_service import _check_worktree_quota
        from core.resource_governor import ResourceGovernor
        from config.schema import ResourceGovernanceConfig

        cfg = ResourceGovernanceConfig(mode="observe")
        governor = ResourceGovernor(cfg)
        _check_worktree_quota(".", governor)  # should not raise

    def test_quota_check_blocks_when_at_limit(self):
        from agent.session.worktree_service import (
            _check_worktree_quota, WorktreeIsolationError,
        )
        from core.resource_governor import ResourceGovernor, ResourceRequest, ResourceKind
        from config.schema import ResourceGovernanceConfig

        cfg = ResourceGovernanceConfig(mode="enforce")
        governor = ResourceGovernor(cfg)
        # Fill all worktree slots using different roots to avoid per_root_max
        limit = cfg.worktree.global_max
        for i in range(limit):
            governor.admit(ResourceRequest(
                f"wt{i}", f"root-{i}", f"sess-{i}",
                resources={ResourceKind.WORKTREE_SLOT: 1},
            ))
        with pytest.raises(WorktreeIsolationError, match="quota"):
            _check_worktree_quota(".", governor)


class TestWorktreeJanitor:
    def test_cleanup_orphans_no_worktrees(self, tmp_path):
        from agent.session.worktree_manager import WorktreeManager
        mgr = WorktreeManager(str(tmp_path), worktree_root=tmp_path / "wts")
        removed = mgr.cleanup_orphans()
        assert removed == 0


# ===========================================================================
# Backward Compatibility
# ===========================================================================


class TestPhase3BackwardCompat:
    def test_governor_observe_still_always_grants(self):
        from core.resource_governor import (
            ResourceGovernor, ResourceRequest, ResourceKind, AdmissionOutcome,
        )
        from config.schema import ResourceGovernanceConfig
        cfg = ResourceGovernanceConfig(mode="observe")
        rg = ResourceGovernor(cfg)
        result = rg.admit(ResourceRequest(
            "test", "root-1", "sess-1",
            resources={ResourceKind.WORKER_SLOT: 10},
        ))
        assert result.outcome == AdmissionOutcome.GRANTED
        result.lease.release()

    def test_enforce_still_works(self):
        from core.resource_governor import (
            ResourceGovernor, ResourceRequest, ResourceKind, AdmissionOutcome,
        )
        from config.schema import ResourceGovernanceConfig, ResourceGovernanceWorkerConfig
        cfg = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(global_max=1, per_root_max=1),
        )
        rg = ResourceGovernor(cfg)
        r1 = rg.admit(ResourceRequest("r1", "root-1", "s-1", resources={ResourceKind.WORKER_SLOT: 1}))
        assert r1.outcome == AdmissionOutcome.GRANTED
        r2 = rg.admit(ResourceRequest("r2", "root-1", "s-2", resources={ResourceKind.WORKER_SLOT: 1}))
        assert r2.outcome != AdmissionOutcome.GRANTED
        r1.lease.release()
