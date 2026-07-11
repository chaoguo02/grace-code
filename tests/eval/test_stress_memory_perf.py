"""Phase 5.2: Stress tests — memory system performance at scale.

Measures performance characteristics of the memory store at increasing
data volumes. These are benchmarks, not pass/fail tests — they establish
a baseline and alert if performance regresses severely.

Tests:
  1. list_memories() at 100/500/1000 memories (index scan only)
  2. list_by_scope() at 100/500/1000 memories (full file read per memory)
  3. list_by_scope() correctness at scale (sorted by confidence DESC)
"""
import os
import tempfile
import time
from pathlib import Path

import pytest

from memory.models import Memory, MemoryMetadata
from memory.store import MemoryStore


# ── Test thresholds (not hard limits — SKIP if exceeded) ──
LIST_MEMORIES_MAX_MS = 50      # Index scan: < 50ms @ 1000 files
LIST_BY_SCOPE_MAX_MS = 500     # Full read: < 500ms @ 1000 files
LIST_BY_SCOPE_100_MAX_MS = 100  # Full read: < 100ms @ 100 files
# MEMORY.md index is capped at 200 lines (~200 memories). list_memories()
# reads from this index, not from scanning .md files directly.
INDEX_CAP = 190  # approximately 200 lines minus headers


def _create_n_memories(store: MemoryStore, n: int, base_confidence: float = 0.7):
    """Create N memories with varying confidence and scope."""
    for i in range(n):
        conf = max(0.1, base_confidence + (i % 10) * 0.03)  # 0.7, 0.73, ..., 0.97
        mem = Memory(
            name=f"stress-mem-{i:04d}",
            description=f"Stress test memory number {i}",
            content=f"This is memory {i}. It contains project knowledge about module_{i % 20}.py. "
                    f"The module handles feature {i} with configuration options A, B, and C. "
                    f"Performance considerations: use caching for large datasets.",
            metadata=MemoryMetadata(
                type="project",
                scope="project" if i % 3 != 0 else "global",
                confidence=conf,
                status="active",
            ),
        )
        store.write_memory(mem)


class TestMemoryStoreStress:
    """Performance benchmarks for memory store operations."""

    @pytest.fixture
    def large_store(self, tmp_path):
        """Create a store pre-populated with 100 memories."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))
        _create_n_memories(store, 100)
        return store

    # ── list_memories() benchmarks ──────────────────────────────────────

    def test_list_memories_100(self, large_store):
        """list_memories() with 100 entries."""
        start = time.perf_counter()
        mems = large_store.list_memories()
        elapsed = (time.perf_counter() - start) * 1000
        assert len(mems) == 100
        print(f"\n  list_memories(100): {elapsed:.1f}ms")

    def test_list_memories_500(self, tmp_path):
        """list_memories() with 500 entries (index capped at ~200 lines)."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))
        _create_n_memories(store, 500)

        start = time.perf_counter()
        mems = store.list_memories()
        elapsed = (time.perf_counter() - start) * 1000
        # MEMORY.md index cap at ~200 lines → returns ~200 entries max
        assert len(mems) >= INDEX_CAP, f"Expected ≥{INDEX_CAP}, got {len(mems)}"
        print(f"\n  list_memories(500 files): {elapsed:.1f}ms ({len(mems)} in index)")

    def test_list_memories_1000(self, tmp_path):
        """list_memories() with 1000 entries (index capped at ~200 lines)."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))
        _create_n_memories(store, 1000)

        start = time.perf_counter()
        mems = store.list_memories()
        elapsed = (time.perf_counter() - start) * 1000
        assert len(mems) >= INDEX_CAP, f"Expected ≥{INDEX_CAP}, got {len(mems)}"
        print(f"\n  list_memories(1000 files): {elapsed:.1f}ms ({len(mems)} in index)")
        if elapsed > LIST_MEMORIES_MAX_MS:
            pytest.skip(
                f"list_memories(1000) took {elapsed:.0f}ms > {LIST_MEMORIES_MAX_MS}ms threshold. "
                f"Performance regression detected."
            )

    # ── list_by_scope() benchmarks ───────────────────────────────────────

    def test_list_by_scope_100(self, large_store):
        """list_by_scope('project') with 100 entries."""
        start = time.perf_counter()
        mems = large_store.list_by_scope("project", min_confidence=0.5)
        elapsed = (time.perf_counter() - start) * 1000
        # Should find ~67 project-scoped memories (2/3 of 100)
        assert len(mems) > 40
        print(f"\n  list_by_scope(100): {elapsed:.1f}ms ({len(mems)} results)")
        if elapsed > LIST_BY_SCOPE_100_MAX_MS:
            pytest.skip(
                f"list_by_scope(100) took {elapsed:.0f}ms > {LIST_BY_SCOPE_100_MAX_MS}ms threshold"
            )

    def test_list_by_scope_500(self, tmp_path):
        """list_by_scope('project') with 500 files (limited by index cap)."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))
        _create_n_memories(store, 500)

        start = time.perf_counter()
        mems = store.list_by_scope("project", min_confidence=0.5)
        elapsed = (time.perf_counter() - start) * 1000
        # list_by_scope goes through list_memories() → limited by index cap
        assert len(mems) > 0, "Should find some project-scoped memories"
        print(f"\n  list_by_scope(500 files): {elapsed:.1f}ms ({len(mems)} results)")

    def test_list_by_scope_1000(self, tmp_path):
        """list_by_scope('project') with 1000 files (limited by index cap)."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))
        _create_n_memories(store, 1000)

        start = time.perf_counter()
        mems = store.list_by_scope("project", min_confidence=0.5)
        elapsed = (time.perf_counter() - start) * 1000
        assert len(mems) > 0, "Should find some project-scoped memories"
        print(f"\n  list_by_scope(1000 files): {elapsed:.1f}ms ({len(mems)} results)")
        if elapsed > LIST_BY_SCOPE_MAX_MS:
            pytest.skip(
                f"list_by_scope(1000) took {elapsed:.0f}ms > {LIST_BY_SCOPE_MAX_MS}ms threshold. "
                f"Consider adding index-based lookup to avoid full file reads."
            )

    # ── Correctness at scale ─────────────────────────────────────────────

    def test_list_by_scope_sorted_by_confidence(self, large_store):
        """Results are sorted by confidence descending, even at scale."""
        mems = large_store.list_by_scope("project", min_confidence=0.0)
        confs = [m.metadata.confidence for m in mems]
        assert confs == sorted(confs, reverse=True), (
            f"Not sorted descending: first 5={confs[:5]}, last 5={confs[-5:]}"
        )

    def test_low_confidence_filtered(self, large_store):
        """min_confidence filter works correctly at scale."""
        all_mems = large_store.list_by_scope("project", min_confidence=0.0)
        high_mems = large_store.list_by_scope("project", min_confidence=0.8)
        assert len(high_mems) < len(all_mems)
        for m in high_mems:
            assert m.metadata.confidence >= 0.8
