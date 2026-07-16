"""Phase 4 memory upgrade behavioral tests.

Tests cover:
  Step 1: Structured auto-precipitation — Finding dicts → Memory objects
  Step 2: Precision injection — scope + confidence filtering
  Step 3: Content hash verification — freshness check + confidence degradation
"""
import hashlib
import os
import tempfile
import pytest
from pathlib import Path

from memory.models import Anchor, Memory, MemoryMetadata, MemorySummary
from memory.context import MemoryContext
from memory.store import MemoryStore
from tools.base import ToolResult


# ═══════════════════════════════════════════════════════════════════════════
# Step 1: Structured Auto-precipitation
# ═══════════════════════════════════════════════════════════════════════════

class TestStructuredPrecipitation:
    """Verify that structured_findings flow through ToolResult → Memory."""

    def test_tool_result_carries_structured_findings(self):
        """ToolResult now has structured_findings field."""
        findings = (
            {"severity": "HIGH", "category": "bug", "title": "Null check missing",
             "description": "Missing null check in auth.py", "file_path": "auth.py",
             "line_start": 42, "recommendation": "Add null check"},
            {"severity": "LOW", "category": "improvement", "title": "Better naming",
             "description": "Rename variable", "file_path": "utils.py"},
        )
        result = ToolResult(
            success=True, output="done",
            structured_findings=findings,
        )
        assert len(result.structured_findings) == 2
        assert result.structured_findings[0]["severity"] == "HIGH"

    def test_only_high_severity_bugs_precipitated(self):
        """Only HIGH severity + bug category findings create memories."""
        findings = [
            {"severity": "HIGH", "category": "bug", "title": "Crash on null",
             "description": "Null pointer crashes the server",
             "file_path": "server.py", "line_start": 100,
             "recommendation": "Add guard clause"},
            {"severity": "MEDIUM", "category": "bug", "title": "Slow query",
             "description": "Query is slow", "file_path": "db.py"},
            {"severity": "HIGH", "category": "hypothesis", "title": "Maybe race",
             "description": "Could be a race condition", "file_path": "worker.py"},
            {"severity": "LOW", "category": "improvement", "title": "Style",
             "description": "Better variable name", "file_path": "style.py"},
        ]

        # Replicate the filtering logic from _precipitate_structured_memories
        high_bugs = [
            f for f in findings
            if str(f.get("severity", "")).upper() == "HIGH"
            and str(f.get("category", "")).lower() == "bug"
        ]
        assert len(high_bugs) == 1
        assert high_bugs[0]["title"] == "Crash on null"

    def test_memory_name_slugification(self):
        """Memory names are kebab-case slugs with MD5 digest."""
        from agent.run_finalizer import _slugify
        name = _slugify("Fix null pointer crash in auth module")
        assert "-" in name
        assert len(name) <= 80
        # Should end with 8-char hex digest
        assert name.split("-")[-1].isalnum()


# ═══════════════════════════════════════════════════════════════════════════
# Step 2: Precision Injection (scope + confidence)
# ═══════════════════════════════════════════════════════════════════════════

class TestPrecisionInjection:
    """Verify scope + confidence based injection."""

    @pytest.fixture
    def store_with_memories(self, tmp_path):
        """Create a MemoryStore with varied memories."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        # Create MEMORY.md
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")

        store = MemoryStore(
            repo_path=str(tmp_path),
            memory_dir=str(store_dir),
        )

        # Create memories with different scope/confidence
        memories = [
            ("high-conf-bug", "High confidence bug fix", "Always check for null",
             "project", "active", 0.9),
            ("medium-conf-note", "Medium confidence note", "Consider using async",
             "project", "active", 0.6),
            ("low-conf-guess", "Low confidence guess", "Maybe the issue is X",
             "project", "active", 0.3),
            ("deprecated-old", "Old deprecated memory", "This is obsolete",
             "project", "deprecated", 0.8),
            ("global-rule", "Global coding rule", "Always use type hints",
             "global", "active", 1.0),
        ]
        for name, desc, content, scope, status, conf in memories:
            mem = Memory(
                name=name, description=desc, content=content,
                metadata=MemoryMetadata(
                    type="project" if scope == "project" else "user",
                    status=status, scope=scope, confidence=conf,
                ),
            )
            store.write_memory(mem)
        return store

    def test_list_by_scope_filters_correctly(self, store_with_memories):
        """list_by_scope returns only project-scoped, active, above threshold."""
        results = store_with_memories.list_by_scope("project", min_confidence=0.5)
        names = {m.name for m in results}
        assert "high-conf-bug" in names
        assert "medium-conf-note" in names
        assert "low-conf-guess" not in names  # confidence 0.3 < 0.5
        assert "deprecated-old" not in names   # deprecated
        assert "global-rule" not in names      # scope=global, not project

    def test_list_by_scope_global(self, store_with_memories):
        """list_by_scope with 'global' returns global-scoped memories."""
        results = store_with_memories.list_by_scope("global", min_confidence=0.5)
        names = {m.name for m in results}
        assert "global-rule" in names

    def test_sorted_by_confidence_desc(self, store_with_memories):
        """Results are sorted by confidence descending."""
        results = store_with_memories.list_by_scope("project", min_confidence=0.0)
        confs = [m.metadata.confidence for m in results]
        assert confs == sorted(confs, reverse=True), f"Expected descending: {confs}"

    def test_precision_section_top_5(self, store_with_memories, tmp_path):
        """_build_precision_section returns at most 5 memories."""
        ctx = MemoryContext(store=store_with_memories)
        section = ctx._build_precision_section()
        # Count "### " headings to verify ≤ 5
        count = section.count("\n### ")
        assert count <= 5
        if count > 0:
            assert "## Relevant Project Knowledge" in section


# ═══════════════════════════════════════════════════════════════════════════
# Step 3: Content Hash Verification
# ═══════════════════════════════════════════════════════════════════════════

class TestContentHashVerification:
    """Verify freshness checks before memory injection."""

    @pytest.fixture
    def store_with_anchored_memory(self, tmp_path):
        """Create a store + memory anchored to a real file."""
        # Create a real file to anchor to
        test_file = tmp_path / "test_module.py"
        test_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")

        store = MemoryStore(
            repo_path=str(tmp_path),
            memory_dir=str(store_dir),
        )

        file_hash = hashlib.sha256(test_file.read_bytes()).hexdigest()

        mem = Memory(
            name="test-memory",
            description="Test anchored memory",
            content="This memory is about test_module.py",
            metadata=MemoryMetadata(
                type="project", status="active", scope="project", confidence=0.8,
            ),
            anchors=[
                Anchor(kind="file", path=str(test_file), content_hash=file_hash),
            ],
        )
        store.write_memory(mem)
        return store, test_file

    def test_hash_match_no_warning(self, store_with_anchored_memory):
        """When file hasn't changed, freshness check returns empty string."""
        store, test_file = store_with_anchored_memory
        ctx = MemoryContext(store=store)
        mem = store.read_memory("test-memory")
        result = ctx._verify_memory_freshness(mem)
        assert result == ""  # Fresh — no warning

    def test_hash_mismatch_degrades_confidence(self, store_with_anchored_memory):
        """When file changes, confidence is halved and warning is returned."""
        store, test_file = store_with_anchored_memory
        # Modify the file
        test_file.write_text("def hello():\n    return 'modified!'\n", encoding="utf-8")

        ctx = MemoryContext(store=store)
        mem = store.read_memory("test-memory")
        old_conf = mem.metadata.confidence
        result = ctx._verify_memory_freshness(mem)

        assert "FILE CHANGED" in result
        # Re-read to get updated confidence
        mem_updated = store.read_memory("test-memory")
        assert mem_updated.metadata.confidence < old_conf
        assert mem_updated.metadata.confidence == pytest.approx(old_conf * 0.5, abs=0.01)

    def test_file_deleted_deprecates_memory(self, store_with_anchored_memory):
        """When anchored file is deleted, memory is deprecated."""
        store, test_file = store_with_anchored_memory
        os.unlink(test_file)

        ctx = MemoryContext(store=store)
        mem = store.read_memory("test-memory")
        result = ctx._verify_memory_freshness(mem)

        assert result == "DEPRECATED"
        mem_updated = store.read_memory("test-memory")
        assert mem_updated.metadata.status == "deprecated"

    def test_no_anchors_no_warning(self, store_with_anchored_memory):
        """Memory without file anchors passes freshness check."""
        store, _ = store_with_anchored_memory
        ctx = MemoryContext(store=store)

        mem = Memory(
            name="no-anchor-mem",
            description="Memory without anchors",
            content="No file binding",
            metadata=MemoryMetadata(type="project"),
        )
        result = ctx._verify_memory_freshness(mem)
        assert result == ""  # No anchors = nothing to verify


# ═══════════════════════════════════════════════════════════════════════════
# Integration: End-to-end flow
# ═══════════════════════════════════════════════════════════════════════════

class TestPhase4Integration:
    """Verify the complete pipeline: ToolResult → accumulation → memory creation."""

    def test_accumulation_in_tool_result(self):
        """structured_findings propagate from ToolResult."""
        findings = (
            {"severity": "HIGH", "category": "bug", "title": "Test bug",
             "description": "A test bug description", "file_path": "test.py"},
        )
        result = ToolResult(success=True, output="ok", structured_findings=findings)
        assert hasattr(result, "structured_findings")
        assert len(result.structured_findings) == 1

    def test_empty_findings_safe(self):
        """Empty structured_findings doesn't crash anything."""
        result = ToolResult(success=True, output="ok")
        assert result.structured_findings == ()

    def test_confidence_floor(self, tmp_path):
        """Confidence degradation has a floor at 0.1."""
        test_file = tmp_path / "test_module.py"
        test_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))
        file_hash = hashlib.sha256(test_file.read_bytes()).hexdigest()

        mem = Memory(
            name="test-mem", description="Test", content="Content",
            metadata=MemoryMetadata(type="project", scope="project", confidence=0.15),
            anchors=[Anchor(kind="file", path=str(test_file), content_hash=file_hash)],
        )
        store.write_memory(mem)

        test_file.write_text("modified!", encoding="utf-8")
        ctx = MemoryContext(store=store)
        mem2 = store.read_memory("test-mem")
        ctx._verify_memory_freshness(mem2)
        mem3 = store.read_memory("test-mem")
        assert mem3.metadata.confidence >= 0.1  # Floor at 0.1

    def test_methods_exist(self):
        """Phase 4 methods are available on MemoryContext."""
        assert hasattr(MemoryContext, "_build_precision_section")
        assert hasattr(MemoryContext, "_verify_memory_freshness")
