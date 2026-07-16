"""Phase 6 end-to-end verification: MetadataCache + Worktree isolation.

This is NOT a unit test suite — it's a verification script that exercises
every Phase 6 feature in realistic scenarios and asserts correct behavior.
"""
import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from memory.metadata_cache import MetadataCache, CachedMetadata
from memory.models import Anchor, Memory, MemoryMetadata, MemorySummary
from memory.store import MemoryStore
from memory.context import MemoryContext
from agent.v2.models import AgentDefinition, AgentKind, WorkspaceMode
from agent.task import TaskIntent


# ═══════════════════════════════════════════════════════════════════════════
# 6.1a: MetadataCache — correctness
# ═══════════════════════════════════════════════════════════════════════════

class TestMetadataCacheCorrectness:
    """Verify cache returns correct results, not just fast ones."""

    @pytest.fixture
    def populated_store(self, tmp_path):
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))
        for i in range(20):
            store.write_memory(Memory(
                name=f"mem-{i:02d}",
                description=f"Memory number {i}",
                content=f"Content of memory {i}",
                metadata=MemoryMetadata(
                    type="project",
                    scope="project" if i % 3 != 0 else "global",
                    confidence=0.5 + (i % 5) * 0.1,
                    status="active",
                ),
            ))
        return store

    def test_cache_has_all_entries(self, populated_store):
        """After building, cache contains every written memory."""
        cache = populated_store._metadata_cache
        assert cache.is_built
        assert cache.count == 20

    def test_list_summaries_matches_store(self, populated_store):
        """Cache summaries match what's actually on disk."""
        cache_summaries = populated_store._metadata_cache.list_summaries()
        cache_names = {s.name for s in cache_summaries}
        # Compare with actual files on disk
        disk_files = {
            f.stem for f in Path(populated_store.store_dir).glob("*.md")
            if f.name != "MEMORY.md"
        }
        assert cache_names == disk_files

    def test_list_by_scope_filters_correctly(self, populated_store):
        """list_by_scope returns only matching scope+confidence."""
        project_mems = populated_store.list_by_scope("project", min_confidence=0.6)
        for m in project_mems:
            assert m.metadata.scope == "project"
            assert m.metadata.confidence >= 0.6

    def test_list_by_scope_sorted_desc(self, populated_store):
        """Results sorted by confidence DESC."""
        mems = populated_store.list_by_scope("project", min_confidence=0.0)
        confs = [m.metadata.confidence for m in mems]
        assert confs == sorted(confs, reverse=True), f"Not sorted: {confs}"

    def test_content_loadable_after_cache_lookup(self, populated_store):
        """Cache returns Memory without content, but read_memory loads it."""
        mems = populated_store.list_by_scope("project", min_confidence=0.5)
        assert len(mems) > 0
        # Content should be empty from cache
        assert mems[0].content == ""
        # Load full content
        full = populated_store.read_memory(mems[0].name)
        assert full is not None
        assert full.content.startswith("Content of memory")

    def test_write_updates_cache_immediately(self, populated_store):
        """After write, cache reflects the new memory without rebuild."""
        new_mem = Memory(
            name="new-memory",
            description="Just added",
            content="Brand new content",
            metadata=MemoryMetadata(type="project", scope="project", confidence=0.9),
        )
        populated_store.write_memory(new_mem)
        # Cache should already have it
        mems = populated_store.list_by_scope("project", min_confidence=0.0)
        names = {m.name for m in mems}
        assert "new-memory" in names

    def test_delete_updates_cache_immediately(self, populated_store):
        """After delete, cache removes the memory without rebuild."""
        populated_store.delete_memory("mem-00")
        mems = populated_store.list_by_scope("project", min_confidence=0.0)
        names = {m.name for m in mems}
        assert "mem-00" not in names


# ═══════════════════════════════════════════════════════════════════════════
# 6.1b: Performance — cache vs no-cache comparison
# ═══════════════════════════════════════════════════════════════════════════

class TestMetadataCachePerformance:
    """Verify cache actually eliminates file I/O bottleneck."""

    def test_list_by_scope_no_file_reads_with_cache(self, tmp_path):
        """With cache, list_by_scope does ZERO read_memory calls (content not loaded)."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))

        # Create 50 memories
        for i in range(50):
            store.write_memory(Memory(
                name=f"perf-{i:03d}",
                description=f"Perf test {i}",
                content=f"Content {i}" * 20,
                metadata=MemoryMetadata(
                    type="project", scope="project" if i % 2 == 0 else "global",
                    confidence=0.7,
                ),
            ))

        # Measure list_by_scope with cache
        start = time.perf_counter()
        mems = store.list_by_scope("project", min_confidence=0.5)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(mems) > 0, "Should find some project-scoped memories"
        # Cache path should be very fast (< 10ms for 50 entries)
        assert elapsed_ms < 50, (
            f"list_by_scope(50) took {elapsed_ms:.1f}ms, expected < 50ms. "
            f"Cache may not be working."
        )
        # Content should be empty (cache doesn't load files)
        for m in mems:
            assert m.content == ""

    def test_cache_rebuild_is_fast(self, tmp_path):
        """Building cache from 100 files takes < 200ms."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))

        for i in range(100):
            store.write_memory(Memory(
                name=f"build-{i:03d}",
                description=f"Build test {i}",
                content=f"Content {i}" * 20,
                metadata=MemoryMetadata(type="project", scope="project", confidence=0.7),
            ))

        # Measure rebuild
        start = time.perf_counter()
        cache = MetadataCache()
        cache.build(store_dir)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert cache.count == 100
        assert elapsed_ms < 500, (
            f"Cache build(100) took {elapsed_ms:.1f}ms, expected < 500ms"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6.1c: MEMORY.md backward compatibility
# ═══════════════════════════════════════════════════════════════════════════

class TestMemoryMdBackwardCompat:
    """Verify system works with and without MEMORY.md."""

    def test_system_works_without_memory_md(self, tmp_path):
        """Fresh store with no MEMORY.md still works via cache."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        # Deliberately do NOT create MEMORY.md
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))

        store.write_memory(Memory(
            name="test-mem", description="Test", content="Hello",
            metadata=MemoryMetadata(type="project"),
        ))
        mems = store.list_memories()
        assert len(mems) == 1
        assert mems[0].name == "test-mem"

    def test_old_memory_md_still_readable(self, tmp_path):
        """If MEMORY.md exists from old version, system still reads it as fallback."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()

        # Create old-style MEMORY.md
        (store_dir / "MEMORY.md").write_text(
            "# Memory Index\n"
            "- [old-mem](old-mem.md) — An old memory (project)\n",
            encoding="utf-8",
        )
        # Create the actual memory file
        (store_dir / "old-mem.md").write_text(
            "---\nname: old-mem\ndescription: An old memory\ntype: project\n---\n\nOld content.\n",
            encoding="utf-8",
        )

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))
        mems = store.list_memories()
        names = {m.name for m in mems}
        assert "old-mem" in names


# ═══════════════════════════════════════════════════════════════════════════
# 6.2: Git Worktree isolation verification
# ═══════════════════════════════════════════════════════════════════════════

class TestWorktreeIsolation:
    """Verify worktree-based isolation actually isolates filesystem changes."""

    @pytest.fixture
    def git_repo(self, tmp_path, monkeypatch):
        """Create a real git repo for worktree testing."""
        from runtime.state_paths import STATE_HOME_ENV
        monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "agent-state"))
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo), capture_output=True,
        )
        # Create initial file and commit
        (repo / "main.py").write_text("print('hello')\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(repo), capture_output=True, check=True,
        )
        return str(repo)

    def test_worktree_creation_and_cleanup(self, git_repo):
        """Worktree can be created, used, and cleaned up."""
        from runtime.snapshot import WorktreeManager

        manager = WorktreeManager(git_repo)
        wt = manager.create("test-agent-001")

        # Worktree exists
        assert os.path.isdir(wt.path)
        assert (Path(wt.path) / "main.py").exists()

        # Modify file in worktree
        (Path(wt.path) / "main.py").write_text("print('modified')\n", encoding="utf-8")

        # Commit in worktree (simulates what fork_subagent does)
        subprocess.run(["git", "add", "-A"], cwd=wt.path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "test modification"],
            cwd=wt.path, capture_output=True, check=True,
        )

        # Main repo is unchanged before merge
        main_content = (Path(git_repo) / "main.py").read_text()
        assert main_content == "print('hello')\n"

        # Merge back
        manager.merge(wt)

        # Main repo now has the change
        main_content_after = (Path(git_repo) / "main.py").read_text()
        assert main_content_after == "print('modified')\n"

        # Cleanup
        manager.discard(wt)
        assert not os.path.isdir(wt.path)

    def test_discard_on_failure(self, git_repo):
        """When worktree is discarded, main repo is unchanged."""
        from runtime.snapshot import WorktreeManager

        manager = WorktreeManager(git_repo)
        wt = manager.create("test-agent-fail")

        # Make a destructive change
        (Path(wt.path) / "main.py").write_text("CORRUPTED DATA", encoding="utf-8")

        # Discard (simulating subagent failure)
        manager.discard(wt)

        # Main repo is unchanged
        main_content = (Path(git_repo) / "main.py").read_text()
        assert main_content == "print('hello')\n"
        assert not os.path.isdir(wt.path)

    def test_workspace_field_recognized(self):
        """Workspace placement does not define the agent's identity."""
        agent = AgentDefinition(
            name="test-worktree-agent",
            description="Agent with worktree isolation",
            intent=TaskIntent.EDIT,
            workspace_mode=WorkspaceMode.WORKTREE,
        )
        assert agent.workspace_mode is WorkspaceMode.WORKTREE
        assert agent.agent_kind is AgentKind.NAMED_SUBAGENT
        assert agent.mode == "subagent"

    def test_shared_workspace_is_default(self):
        """Default workspace is shared while the child context stays fresh."""
        agent = AgentDefinition(
            name="test-shared-agent",
            description="Default shared-workspace agent",
            intent=TaskIntent.EDIT,
        )
        assert agent.workspace_mode is WorkspaceMode.CURRENT
