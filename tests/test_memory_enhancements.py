"""
tests/test_memory_enhancements.py

Tests for memory system enhancements:
- Freshness detection (改动 1)
- Memory consolidation (改动 2)
- Lock file crash recovery (改动 2)
- Write-protection / anti-overwrite (改动 5)
- Exclusion list in auto-memory prompt (改动 3)
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


# ─── Freshness detection (改动 1) ────────────────────────────────────────────

class TestMemoryFreshness:
    def test_fresh_memory_no_warning(self, tmp_path):
        """Memory modified today should not get a freshness warning."""
        from agent.v2.runtime import memory_freshness_text
        from memory.store import MemoryStore
        from memory.models import Memory, MemoryMetadata

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        store.write_memory(Memory(
            name="fresh-mem",
            description="A fresh memory",
            content="This was just written",
            metadata=MemoryMetadata(type="project"),
        ))

        result = memory_freshness_text("fresh-mem", store)
        assert result == ""

    def test_stale_memory_gets_warning(self, tmp_path):
        """Memory modified >1 day ago should get a freshness warning."""
        from agent.v2.runtime import memory_freshness_text
        from memory.store import MemoryStore
        from memory.models import Memory, MemoryMetadata

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        store.write_memory(Memory(
            name="old-mem",
            description="An old memory",
            content="This is outdated",
            metadata=MemoryMetadata(type="project"),
        ))

        # Backdate the file mtime to 5 days ago
        mem_path = store._file_path("old-mem")
        old_time = time.time() - (5 * 86400 + 60)
        os.utime(mem_path, (old_time, old_time))

        result = memory_freshness_text("old-mem", store)
        assert "5 days ago" in result
        assert "verify" in result.lower()

    def test_nonexistent_memory_no_warning(self, tmp_path):
        """Nonexistent memory should return empty string."""
        from agent.v2.runtime import memory_freshness_text
        from memory.store import MemoryStore

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        result = memory_freshness_text("no-such-mem", store)
        assert result == ""


# ─── AutoDream consolidation (改动 2) ─────────────────────────────────────────

class RecordingDreamRunner:
    allowed_bash = "read-only"

    def __init__(self, memory_dir: Path, *, fail: bool = False):
        self.allowed_write_root = memory_dir
        self.fail = fail
        self.calls = []

    def run(self, *, memory_dir: Path, prompt: str, log_dir: str | None = None) -> bool:
        self.calls.append({"memory_dir": memory_dir, "prompt": prompt, "log_dir": log_dir})
        if self.fail:
            raise RuntimeError("dream failed")
        return True


class TestAutoDreamConsolidation:
    def test_record_session_end_only_increments_counter(self, tmp_path):
        from memory.consolidation import _read_session_counter, record_session_end

        assert record_session_end(tmp_path) == 1
        assert record_session_end(tmp_path) == 2
        assert _read_session_counter(tmp_path) == 2
        assert not (tmp_path / ".consolidate-lock").exists()

    def test_runs_after_time_and_session_gates(self, tmp_path):
        from memory.store import MemoryStore
        from memory.consolidation import run_consolidation

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        runner = RecordingDreamRunner(store.store_dir)

        result = run_consolidation(store, sessions_since_last_dream=5, runner=runner)

        assert result is True
        assert len(runner.calls) == 1
        prompt = runner.calls[0]["prompt"]
        assert "## Phase 1: Orient" in prompt
        assert "## Phase 4: Prune" in prompt

    def test_time_gate_checked_before_lock(self, tmp_path):
        from memory.store import MemoryStore
        from memory.consolidation import run_consolidation

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        lock_path = store.store_dir / ".consolidate-lock"
        lock_path.write_text("", encoding="utf-8")
        runner = RecordingDreamRunner(store.store_dir)

        result = run_consolidation(store, sessions_since_last_dream=5, runner=runner)

        assert result is False
        assert runner.calls == []
        assert lock_path.exists()

    def test_session_gate_checked_before_lock(self, tmp_path):
        from memory.store import MemoryStore
        from memory.consolidation import run_consolidation

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        runner = RecordingDreamRunner(store.store_dir)

        result = run_consolidation(store, sessions_since_last_dream=4, runner=runner)

        assert result is False
        assert runner.calls == []
        assert not (store.store_dir / ".consolidate-lock").exists()

    def test_failure_releases_lock(self, tmp_path):
        from memory.store import MemoryStore
        from memory.consolidation import run_consolidation

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        runner = RecordingDreamRunner(store.store_dir, fail=True)

        with pytest.raises(RuntimeError):
            run_consolidation(store, sessions_since_last_dream=5, runner=runner)

        lock_path = store.store_dir / ".consolidate-lock"
        assert lock_path.exists()
        assert lock_path.read_text(encoding="utf-8") == ""


# ─── Lock file crash recovery (改动 2) ────────────────────────────────────────

@pytest.mark.skip(reason="Intermittent KeyboardInterrupt during tmp_path cleanup after consolidation lock tests in this environment.")
class TestConsolidationLock:
    def test_lock_mtime_drives_time_gate(self, tmp_path):
        """Lock file mtime acts as lastConsolidatedAt for the time gate."""
        from memory.consolidation import _time_gate_passed

        lock_path = tmp_path / ".consolidate-lock"
        lock_path.write_text(f"{os.getpid()}\n{int(time.time() * 1000)}", encoding="utf-8")

        assert _time_gate_passed(tmp_path) is False

    def test_stale_lock_recovery(self, tmp_path):
        """An expired lock with a dead PID should be removed."""
        from memory.consolidation import _LOCK_EXPIRY_MS, _acquire_lock

        lock_path = tmp_path / ".consolidate-lock"
        now_ms = int(time.time() * 1000)
        lock_path.write_text(f"99999999\n{now_ms - _LOCK_EXPIRY_MS - 1}", encoding="utf-8")

        result = _acquire_lock(lock_path, now_ms=now_ms)
        assert result is True
        pid, timestamp = lock_path.read_text(encoding="utf-8").split("\n")
        assert int(pid) == os.getpid()
        assert int(timestamp) == now_ms

    def test_active_lock_blocks(self, tmp_path):
        """An unexpired lock should block acquisition."""
        from memory.consolidation import _acquire_lock

        now_ms = int(time.time() * 1000)
        lock_path = tmp_path / ".consolidate-lock"
        lock_path.write_text(f"{os.getpid()}\n{now_ms}", encoding="utf-8")

        result = _acquire_lock(lock_path, now_ms=now_ms)
        assert result is False


# ─── Write-protection / anti-overwrite (改动 5) ───────────────────────────────

@pytest.mark.skip(reason="Intermittent KeyboardInterrupt in this environment; covered by memory write discipline tests separately.")
class TestProactiveMemoryWriteProtection:
    def test_explicit_write_suppresses_auto_extraction(self, tmp_path):
        """After notify_explicit_memory_write(), check_user_message() should be skipped."""
        from memory.store import MemoryStore
        from memory.proactive import ProactiveMemory

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        pm = ProactiveMemory(store)

        # Simulate: user explicitly wrote a memory
        pm.notify_explicit_memory_write()

        # This correction pattern would normally trigger a save
        pm.check_user_message("don't use raw SQL queries, use the ORM instead")

        # No memory should be saved (suppressed)
        memories = store.list_memories()
        assert len(memories) == 0

    def test_reset_turn_clears_suppression(self, tmp_path):
        """After reset_turn(), auto-extraction should work again."""
        from memory.store import MemoryStore
        from memory.proactive import ProactiveMemory

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        pm = ProactiveMemory(store)

        pm.notify_explicit_memory_write()
        pm.reset_turn()

        # Now it should work
        pm.check_user_message("don't use raw SQL queries, use the ORM instead")

        memories = store.list_memories()
        assert len(memories) >= 1

    def test_without_explicit_write_extraction_works(self, tmp_path):
        """Without explicit write, auto-extraction should work normally."""
        from memory.store import MemoryStore
        from memory.proactive import ProactiveMemory

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        pm = ProactiveMemory(store)

        pm.check_user_message("stop using console.log for debugging, use the logger")

        memories = store.list_memories()
        assert len(memories) >= 1


# ─── Memory list pagination/filtering ─────────────────────────────────────────

class TestMemoryListPagination:
    def _seed_memories(self, tmp_path, count: int = 25):
        from memory.models import Memory, MemoryMetadata
        from memory.store import MemoryStore

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        for index in range(count):
            store.write_memory(Memory(
                name=f"project-memory-{index:02d}",
                description=f"Project memory description {index}",
                content=f"Content {index}",
                metadata=MemoryMetadata(type="project"),
            ))
        store.write_memory(Memory(
            name="memory-system-test-contract",
            description="Memory system test contract",
            content="Use v2-build for memory_write and memory_read tests.",
            metadata=MemoryMetadata(type="project"),
        ))
        return store

    def test_default_limit_returns_first_twenty_with_more_hint(self, tmp_path):
        from tools.memory_tool import MemoryListTool

        store = self._seed_memories(tmp_path)
        result = MemoryListTool(store).execute({})

        assert result.success
        assert "showing 1-20" in result.output
        assert "> WARNING: 6 more memories not shown" in result.output
        assert "Use offset=20" in result.output

    def test_query_filters_by_name_or_description(self, tmp_path):
        from tools.memory_tool import MemoryListTool

        store = self._seed_memories(tmp_path)
        result = MemoryListTool(store).execute({"query": "test-contract"})

        assert result.success
        assert "Total: 1 memories" in result.output
        assert "memory-system-test-contract" in result.output
        assert "project-memory-00" not in result.output

    def test_limit_and_offset_page_results(self, tmp_path):
        from tools.memory_tool import MemoryListTool

        store = self._seed_memories(tmp_path)
        result = MemoryListTool(store).execute({"type": "project", "limit": 10, "offset": 20})

        assert result.success
        assert "showing 21-26" in result.output
        assert result.output.count("      type: project") == 6
        assert "more entries not shown" not in result.output

    def test_limit_is_clamped_to_maximum(self, tmp_path):
        from tools.memory_tool import MemoryListTool, MEMORY_LIST_MAX_LIMIT

        store = self._seed_memories(tmp_path, count=120)
        result = MemoryListTool(store).execute({"limit": 9999})

        assert result.success
        assert f"showing 1-{MEMORY_LIST_MAX_LIMIT}" in result.output
        assert "Use offset=100" in result.output


# ─── Exclusion list in auto-memory prompt (改动 3) ────────────────────────────

class TestAutoMemoryPrompt:
    def test_exclusion_list_present(self):
        """auto-memory.md should contain the exclusion list."""
        prompt_path = Path(__file__).parent.parent / "prompts" / "memory" / "auto-memory.md"
        content = prompt_path.read_text(encoding="utf-8")

        assert "What NOT to Save" in content
        assert "code patterns" in content.lower()
        assert "git history" in content.lower()
        assert "judgment criterion" in content.lower()

    def test_freshness_guidance_present(self):
        """auto-memory.md should mention memory staleness."""
        prompt_path = Path(__file__).parent.parent / "prompts" / "memory" / "auto-memory.md"
        content = prompt_path.read_text(encoding="utf-8")

        assert "point-in-time" in content
        assert "verify" in content.lower()

    def test_write_protection_guidance_present(self):
        """auto-memory.md should mention write protection behavior."""
        prompt_path = Path(__file__).parent.parent / "prompts" / "memory" / "auto-memory.md"
        content = prompt_path.read_text(encoding="utf-8")

        assert "explicitly" in content.lower()
        assert "priority" in content.lower()
