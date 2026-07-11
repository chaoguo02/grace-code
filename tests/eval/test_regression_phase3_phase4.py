"""Phase 5.1: Regression tests — Phase 3 + Phase 4 integration.

Verifies that SubAgent contracts (required_tools/completion_requires) and
memory precipitation (structured_findings → Memory → precision injection)
work together correctly in the combined pipeline.

Scenarios:
  A: Subagent findings → precipitation → next task picks them up
  B: required_tools contract + memory precipitation don't conflict
  C: CompletionGuard enforces contract even when memory extraction is enabled
"""
import hashlib
import os
import tempfile
from pathlib import Path

import pytest

from agent.completion_guard import CompletionContext, TaskCompletionGuard
from agent.core import AgentConfig, ReActAgent
from memory.models import Anchor, Memory, MemoryMetadata
from memory.store import MemoryStore
from memory.context import MemoryContext
from tools.base import ToolResult


# ═══════════════════════════════════════════════════════════════════════════
# Scenario A: Subagent findings → precipitation → next task injection
# ═══════════════════════════════════════════════════════════════════════════

class TestFindingsToInjectionPipeline:
    """End-to-end: structured_findings → Memory → _build_precision_section."""

    @pytest.fixture
    def store(self, tmp_path):
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        return MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))

    def test_finding_becomes_visible_in_next_injection(self, store):
        """A precipitated memory shows up in precision injection."""
        # Step 1: Simulate _precipitate_structured_memories
        findings = [{
            "severity": "HIGH", "category": "bug",
            "title": "Null pointer crash in login handler",
            "description": "Missing null check at auth.py:42 causes crash",
            "file_path": "auth.py", "line_start": 42,
            "recommendation": "Add guard clause before dereference",
        }]

        from agent.run_finalizer import RunFinalizer
        finalizer = RunFinalizer(memory_context=None, backend=None)
        finalizer._precipitate(findings, store)

        # Verify memory was created
        mems = store.list_memories()
        assert len(mems) >= 1
        names = {m.name for m in mems}
        assert any("null-pointer" in n or "crash" in n for n in names)

        # Step 2: Verify precision injection sees it
        ctx = MemoryContext(store=store)
        section = ctx._build_precision_section()
        assert "Relevant Project Knowledge" in section
        # The memory content should be in the section
        assert "Null pointer crash" in section or "null" in section.lower()

    def test_multiple_findings_all_precipitated(self, store):
        """Multiple HIGH bugs all get created as separate memories."""
        findings = [
            {"severity": "HIGH", "category": "bug",
             "title": "Bug A: SQL injection", "description": "Injection in query.py",
             "file_path": "query.py", "line_start": 10},
            {"severity": "HIGH", "category": "bug",
             "title": "Bug B: XSS vulnerability", "description": "XSS in template.py",
             "file_path": "template.py", "line_start": 55},
            {"severity": "MEDIUM", "category": "bug",  # should be skipped
             "title": "Bug C: Slow response", "description": "Slow API",
             "file_path": "api.py"},
            {"severity": "HIGH", "category": "improvement",  # should be skipped
             "title": "Better error handling", "description": "Add try/except",
             "file_path": "utils.py"},
        ]

        from agent.run_finalizer import RunFinalizer
        finalizer = RunFinalizer(memory_context=None, backend=None)
        count = finalizer._precipitate(findings, store)
        # Only Bug A and Bug B should be precipitated (HIGH + bug)
        assert count == 2

    def test_low_confidence_not_injected(self, store):
        """Memories with confidence < 0.5 are NOT auto-injected."""
        mem = Memory(
            name="low-conf-mem", description="Low confidence guess",
            content="Maybe the issue is threading but I'm not sure",
            metadata=MemoryMetadata(
                type="project", scope="project", confidence=0.3,
            ),
        )
        store.write_memory(mem)

        ctx = MemoryContext(store=store)
        section = ctx._build_precision_section()
        assert "Low confidence" not in section
        assert "threading" not in section


# ═══════════════════════════════════════════════════════════════════════════
# Scenario B: required_tools contract + memory precipitation compatibility
# ═══════════════════════════════════════════════════════════════════════════

class TestContractAndMemoryCompatibility:
    """required_tools enforcement and memory extraction don't interfere."""

    def test_contract_blocks_finish_without_submit_findings(self):
        """completion_requires blocks FINISH when submit_findings not called."""
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        # Only read files, no submit_findings
        ctx.record_tool_result("file_read", "test.py", True)
        ctx.record_tool_result("search_text", "test.py", True)

        result = guard.check(
            ctx=ctx, task_intent="analysis",
            completion_requires={"submit_findings": 1},
        )
        assert result.can_complete is False
        assert "submit_findings" in result.inject_message

    def test_contract_passes_after_submit_findings(self):
        """After calling submit_findings, FINISH is accepted."""
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "test.py", True)
        ctx.record_tool_result("submit_findings", None, True)
        ctx.record_tool_result("search_text", "test.py", True)

        result = guard.check(
            ctx=ctx, task_intent="analysis",
            completion_requires={"submit_findings": 1},
        )
        assert result.can_complete is True

    def test_contract_and_memory_accumulation_independent(self):
        """ToolResult carries both structured_findings AND normal output."""
        findings = (
            {"severity": "HIGH", "category": "bug", "title": "Test",
             "description": "Test bug", "file_path": "test.py"},
        )
        result = ToolResult(
            success=True, output="<task-notification>...</task-notification>",
            structured_findings=findings,
        )
        # Both the XML output AND the structured findings are present
        assert "<task-notification>" in result.output
        assert len(result.structured_findings) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Scenario C: CompletionGuard enforcement with memory context present
# ═══════════════════════════════════════════════════════════════════════════

class TestGuardWithMemoryContext:
    """CompletionGuard works correctly regardless of memory state."""

    def test_guard_unaffected_by_memory_store(self, tmp_path):
        """Guard checks are deterministic even when MemoryStore is attached."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))

        # Create some existing memories
        for i in range(5):
            store.write_memory(Memory(
                name=f"mem-{i}", description=f"Memory {i}",
                content=f"Content {i}",
                metadata=MemoryMetadata(type="project", scope="project", confidence=0.7),
            ))

        # Guard should work exactly the same with or without memories
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "test.py", True)

        result = guard.check(
            ctx=ctx, task_intent="analysis",
            completion_requires={"submit_findings": 1},
        )
        assert result.can_complete is False  # Still blocked
        # The block reason is about submit_findings, not about memories
        assert "submit_findings" in result.blocked_reason

    def test_precision_injection_respects_already_surfaced(self, tmp_path):
        """_already_surfaced prevents re-injecting the same memory."""
        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))

        mem = Memory(
            name="test-mem", description="Test",
            content="Important project knowledge",
            metadata=MemoryMetadata(type="project", scope="project", confidence=0.9),
        )
        store.write_memory(mem)

        ctx = MemoryContext(store=store)
        # First injection: memory should appear
        section1 = ctx._build_precision_section()
        assert "Important project knowledge" in section1

        # Second injection: same memory should NOT appear (already surfaced)
        section2 = ctx._build_precision_section()
        assert "Important project knowledge" not in section2
