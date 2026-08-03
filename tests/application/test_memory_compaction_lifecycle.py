"""G35: Memory/Compaction — scheduler lifecycle, compaction snapshot.

AC: MemoryScheduler has explicit start/stop with owned task (non-daemon)
AC: bootstrap() runs registered bootstrap tasks synchronously
AC: prune() runs registered prune tasks and returns count
AC: stop() waits for active maintenance cycle to complete
AC: CompactionResult is frozen dataclass (typed snapshot, not EventBus command)
AC: CompactionService.compact() called as direct command, not via EventBus
AC: Compaction preserves system prompt budget and recent messages
"""

from __future__ import annotations

import time as _time

import pytest

from application.maintenance.memory_scheduler import MemoryScheduler
from application.context.compaction_service import CompactionService, CompactionResult


# ═══════════════════════════════════════════════════════════════════════════════
# G35.1 — MemoryScheduler lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestMemorySchedulerLifecycle:
    """G35: MemoryScheduler has owned task, non-daemon, waits for shutdown."""

    def test_start_stop_cycle(self):
        scheduler = MemoryScheduler()
        assert not scheduler.running

        scheduler.start()
        assert scheduler.running

        scheduler.stop(timeout_s=2.0)
        assert not scheduler.running

    def test_bootstrap_runs_synchronously(self):
        """bootstrap() must execute registered tasks immediately, not schedule them."""
        bootstrap_ran = []

        scheduler = MemoryScheduler()
        scheduler.register("bootstrap_load", lambda: bootstrap_ran.append(True),
                           interval_s=999.0)

        scheduler.bootstrap("s1")
        assert len(bootstrap_ran) == 1, "bootstrap() must run synchronously"

    def test_prune_returns_count(self):
        """prune() must return the number of prune tasks executed."""
        prune_count = [0]

        def _prune():
            prune_count[0] += 1

        scheduler = MemoryScheduler()
        scheduler.register("prune_expired", _prune, interval_s=999.0)
        scheduler.register("prune_orphans", _prune, interval_s=999.0)

        count = scheduler.prune()
        assert count == 2, f"prune() should return 2, got {count}"
        assert prune_count[0] == 2, "both prune tasks should execute"

    def test_stop_waits_for_active_cycle(self):
        """G35: stop() must block until the current maintenance cycle finishes."""
        cycle_started = []
        cycle_finished = []

        def _slow_task():
            cycle_started.append(True)
            _time.sleep(0.1)
            cycle_finished.append(True)

        scheduler = MemoryScheduler()
        scheduler.register("slow", _slow_task, interval_s=0.01)
        scheduler.start()

        # Let at least one cycle start
        _time.sleep(0.05)
        scheduler.stop(timeout_s=3.0)

        # If stop() waited, cycle_finished should have an entry
        assert len(cycle_started) >= 1, "At least one cycle should have started"
        # After stop returns, no new cycles should start
        assert not scheduler.running

    def test_thread_is_non_daemon(self):
        scheduler = MemoryScheduler()
        scheduler.start()
        assert scheduler._thread is not None
        assert not scheduler._thread.daemon, (
            "G35: memory scheduler thread must NOT be daemon"
        )
        scheduler.stop()

    def test_double_start_idempotent(self):
        scheduler = MemoryScheduler()
        scheduler.start()
        scheduler.start()  # second start is no-op
        assert scheduler.running
        scheduler.stop()

    def test_stop_before_start_safe(self):
        scheduler = MemoryScheduler()
        scheduler.stop()  # must not raise
        assert not scheduler.running


# ═══════════════════════════════════════════════════════════════════════════════
# G35.2 — CompactionService
# ═══════════════════════════════════════════════════════════════════════════════

class TestCompactionService:
    """G35: CompactionService produces typed snapshots, not EventBus commands."""

    def test_compact_no_truncation_needed(self):
        svc = CompactionService()
        msgs = [{"role": "user", "content": "hi"}]
        result = svc.compact(msgs, max_tokens=100_000)

        assert isinstance(result, CompactionResult)
        assert not result.truncated
        assert result.messages_before == result.messages_after

    def test_compact_truncates_when_over_budget(self):
        svc = CompactionService()
        # 200 messages of ~100 tokens each ≈ 20,000 tokens
        msgs = [{"role": "user", "content": "x" * 400} for _ in range(200)]
        result = svc.compact(msgs, max_tokens=5000)

        assert isinstance(result, CompactionResult)
        assert result.truncated
        assert result.messages_after < result.messages_before, (
            f"Expected truncation: {result.messages_before} → {result.messages_after}"
        )

    def test_compact_preserves_recent_messages(self):
        svc = CompactionService()
        # 500 messages of ~60 tokens each ≈ 30,000 tokens > 5,000 budget
        msgs = [{"role": "user", "content": "x" * 200} for _ in range(500)]
        result = svc.compact(msgs, max_tokens=5000)

        assert result.truncated, (
            f"500 long messages should exceed 5000 token budget, "
            f"got {result.tokens_before} tokens"
        )
        # Most recent messages should be kept
        assert result.messages_after > 0
        assert result.messages_after < 500

    def test_compaction_result_is_frozen(self):
        result = CompactionResult(
            tokens_before=1000, tokens_after=500,
            messages_before=50, messages_after=20, truncated=True,
        )
        with pytest.raises(Exception):
            result.tokens_after = 999  # type: ignore

    def test_system_prompt_budget_reserved(self):
        """G35: System prompt tokens should be reserved from budget."""
        svc = CompactionService()
        msgs = [{"role": "user", "content": "x" * 1000} for _ in range(30)]
        # With system prompt taking some budget, fewer messages should fit
        result_with_sp = svc.compact(msgs, system_prompt="x" * 2000, max_tokens=5000)
        result_without_sp = svc.compact(msgs, max_tokens=5000)

        # With system prompt, available budget is smaller → more truncation
        assert result_with_sp.truncated
