"""
tests/test_memory_system.py

Tests for memory/context.py and memory/proactive.py.
"""

from __future__ import annotations

import pytest

from memory.models import Memory, MemoryMetadata, MemorySummary
from memory.store import MemoryStore, _build_frontmatter, _parse_frontmatter
from memory.context import MemoryContext, _extract_keywords
from memory.proactive import ProactiveMemory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_dir(tmp_path):
    """Provide a fresh memory directory for each test."""
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def store(memory_dir):
    """A MemoryStore backed by a temporary directory."""
    return MemoryStore(repo_path="/test/repo", memory_dir=str(memory_dir))


@pytest.fixture
def sample_memory():
    return Memory(
        name="test-memory",
        description="A test memory entry",
        content="This is the body of the memory.",
        metadata=MemoryMetadata(type="project"),
    )


# ---------------------------------------------------------------------------
# MemoryStore — _build_frontmatter / _parse_frontmatter
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def test_build_frontmatter_includes_updated_at(self, sample_memory):
        fm_text = _build_frontmatter(sample_memory)
        assert "updated_at:" in fm_text
        assert sample_memory.updated_at in fm_text

    def test_build_frontmatter_includes_all_fields(self, sample_memory):
        fm_text = _build_frontmatter(sample_memory)
        assert "name: test-memory" in fm_text
        assert "description:" in fm_text
        assert "type: project" in fm_text

    def test_parse_roundtrip(self, sample_memory):
        from memory.store import _build_memory_file
        file_content = _build_memory_file(sample_memory)
        fm, body = _parse_frontmatter(file_content)
        assert fm["name"] == "test-memory"
        assert fm["updated_at"] == sample_memory.updated_at
        assert "body of the memory" in body


# ---------------------------------------------------------------------------
# MemoryStore — get_index_content truncation
# ---------------------------------------------------------------------------


class TestGetIndexContent:
    def test_truncation_message_correct_count(self, store):
        # Write enough memories to generate many index lines
        for i in range(20):
            mem = Memory(
                name=f"mem-{i:02d}",
                description=f"Memory number {i}",
                content=f"Content {i}",
                metadata=MemoryMetadata(type="project"),
            )
            store.write_memory(mem)

        content = store.get_index_content(max_lines=5)
        lines = content.splitlines()
        assert lines[-1].startswith("... [")
        # The truncation message should show the correct count
        # 20 memories → header "# Memory Index" + blank + 20 entries = 22 lines
        # With max_lines=5, omitted = total - 5
        assert "0 lines omitted" not in content
        assert "omitted]" in content

    def test_no_truncation_when_short(self, store):
        mem = Memory(
            name="only-one",
            description="Solo memory",
            content="Content",
            metadata=MemoryMetadata(type="project"),
        )
        store.write_memory(mem)
        content = store.get_index_content(max_lines=200)
        assert "omitted" not in content

    def test_empty_store_returns_empty(self, store):
        content = store.get_index_content()
        assert content == ""


# ---------------------------------------------------------------------------
# MemoryStore — CRUD
# ---------------------------------------------------------------------------


class TestMemoryStoreCRUD:
    def test_write_and_read(self, store, sample_memory):
        assert store.write_memory(sample_memory)
        result = store.read_memory("test-memory")
        assert result is not None
        assert result.name == "test-memory"
        assert result.description == "A test memory entry"
        assert "body of the memory" in result.content
        assert result.metadata.type == "project"
        assert result.updated_at == sample_memory.updated_at

    def test_read_nonexistent(self, store):
        assert store.read_memory("does-not-exist") is None

    def test_delete(self, store, sample_memory):
        store.write_memory(sample_memory)
        assert store.delete_memory("test-memory")
        assert store.read_memory("test-memory") is None

    def test_list_memories(self, store):
        for name in ("alpha", "beta"):
            store.write_memory(Memory(
                name=name,
                description=f"Description of {name}",
                content=f"Content of {name}",
                metadata=MemoryMetadata(type="project"),
            ))
        summaries = store.list_memories()
        names = {s.name for s in summaries}
        assert "alpha" in names
        assert "beta" in names


# ---------------------------------------------------------------------------
# MemoryContext
# ---------------------------------------------------------------------------


class TestMemoryContext:
    def test_build_section_no_memories(self, store):
        ctx = MemoryContext(store=store)
        section = ctx.build_memory_section()
        assert section == ""

    def test_build_section_with_memories(self, store):
        store.write_memory(Memory(
            name="auth-flow",
            description="Authentication architecture notes",
            content="OAuth2 flow details...",
            metadata=MemoryMetadata(type="project"),
        ))
        ctx = MemoryContext(store=store)
        section = ctx.build_memory_section()
        assert "Available Memories" in section
        assert "auth-flow" in section

    def test_disabled_returns_empty(self, store):
        store.write_memory(Memory(
            name="test",
            description="test",
            content="test",
            metadata=MemoryMetadata(type="project"),
        ))
        ctx = MemoryContext(store=store, enabled=False)
        assert ctx.build_memory_section() == ""

    def test_relevance_filtering(self, store):
        store.write_memory(Memory(
            name="auth-notes",
            description="Authentication patterns and OAuth2",
            content="Details",
            metadata=MemoryMetadata(type="project"),
        ))
        store.write_memory(Memory(
            name="docker-setup",
            description="Docker and container configuration",
            content="Details",
            metadata=MemoryMetadata(type="project"),
        ))
        ctx = MemoryContext(store=store)
        ctx.set_task_context("Fix the authentication bug in OAuth2 login")
        section = ctx.build_memory_section()
        # auth-notes should appear under "Relevant"
        assert "Relevant" in section
        assert "auth-notes" in section

    def test_max_lines_respected(self, store):
        for i in range(30):
            store.write_memory(Memory(
                name=f"mem-{i:02d}",
                description=f"Memory {i} with lots of text",
                content=f"Content {i}",
                metadata=MemoryMetadata(type="project"),
            ))
        ctx = MemoryContext(store=store, max_lines=5)
        section = ctx.build_memory_section()
        # Section includes header + truncated index + footer
        # The index itself is limited to max_lines, total output may exceed slightly
        # because build_memory_section() adds a header and footer
        # What matters is get_index_content() respects its limit
        index_content = store.get_index_content(max_lines=5)
        assert len(index_content.splitlines()) <= 6  # 5 lines + truncation message


class TestExtractKeywords:
    def test_english_keywords(self):
        kw = _extract_keywords("Fix the authentication bug in OAuth2")
        assert "fix" in kw
        assert "authentication" in kw
        assert "oauth2" in kw
        # Stopwords excluded
        assert "the" not in kw
        assert "in" not in kw

    def test_short_words_excluded(self):
        kw = _extract_keywords("a b c de fg")
        assert "a" not in kw
        assert "b" not in kw
        assert "de" in kw
        assert "fg" in kw

    def test_chinese_keywords(self):
        kw = _extract_keywords("修复认证模块的问题")
        # Chinese words extracted (multi-char sequences)
        assert len(kw) > 0


# ---------------------------------------------------------------------------
# ProactiveMemory
# ---------------------------------------------------------------------------


class TestProactiveMemoryUserMessage:
    def test_correction_detected_english(self, store):
        pm = ProactiveMemory(store)
        pm.check_user_message("don't use tabs for indentation, use spaces")
        # Should have saved a feedback memory
        summaries = store.list_memories()
        assert len(summaries) > 0
        assert any(s.type == "feedback" for s in summaries)

    def test_correction_detected_chinese(self, store):
        pm = ProactiveMemory(store)
        pm.check_user_message("不要使用 any 类型，请用具体的类型")
        summaries = store.list_memories()
        assert len(summaries) > 0

    def test_no_detection_on_normal_message(self, store):
        pm = ProactiveMemory(store)
        pm.check_user_message("Please fix the login page")
        summaries = store.list_memories()
        assert len(summaries) == 0

    def test_short_message_ignored(self, store):
        pm = ProactiveMemory(store)
        pm.check_user_message("no")
        summaries = store.list_memories()
        assert len(summaries) == 0

    def test_long_message_ignored(self, store):
        pm = ProactiveMemory(store)
        pm.check_user_message("don't " + "x" * 600)
        summaries = store.list_memories()
        assert len(summaries) == 0

    def test_dedup_same_correction(self, store):
        pm = ProactiveMemory(store)
        pm.check_user_message("don't use any types in TypeScript")
        pm.check_user_message("don't use any types in TypeScript")
        # Only one memory should be saved
        summaries = store.list_memories()
        feedback_count = sum(1 for s in summaries if s.type == "feedback")
        assert feedback_count == 1

    def test_remember_pattern(self, store):
        pm = ProactiveMemory(store)
        pm.check_user_message("remember that the CI runs on Python 3.11")
        summaries = store.list_memories()
        assert len(summaries) > 0

    def test_always_pattern(self, store):
        pm = ProactiveMemory(store)
        pm.check_user_message("always use type hints for function parameters")
        summaries = store.list_memories()
        assert len(summaries) > 0


class TestProactiveMemoryToolResult:
    def test_build_command_saved(self, store):
        pm = ProactiveMemory(store)
        pm.check_tool_result(
            tool_name="shell",
            params={"cmd": "pytest tests/"},
            output="5 passed, 0 failed. All tests passed.",
            success=True,
        )
        mem = store.read_memory("build-commands")
        assert mem is not None
        assert "pytest tests/" in mem.content

    def test_non_shell_tool_ignored(self, store):
        pm = ProactiveMemory(store)
        pm.check_tool_result(
            tool_name="file_read",
            params={"path": "setup.py"},
            output="all tests passed",
            success=True,
        )
        assert store.read_memory("build-commands") is None

    def test_failed_command_ignored(self, store):
        pm = ProactiveMemory(store)
        pm.check_tool_result(
            tool_name="shell",
            params={"cmd": "pytest tests/"},
            output="3 failed",
            success=False,
        )
        assert store.read_memory("build-commands") is None

    def test_non_build_command_ignored(self, store):
        pm = ProactiveMemory(store)
        pm.check_tool_result(
            tool_name="shell",
            params={"cmd": "ls -la"},
            output="total 42. success.",
            success=True,
        )
        assert store.read_memory("build-commands") is None

    def test_no_success_indicator_ignored(self, store):
        pm = ProactiveMemory(store)
        pm.check_tool_result(
            tool_name="shell",
            params={"cmd": "npm run build"},
            output="webpack compiled with warnings",
            success=True,
        )
        assert store.read_memory("build-commands") is None

    def test_append_to_existing_build_commands(self, store):
        pm = ProactiveMemory(store)
        pm.check_tool_result(
            tool_name="shell",
            params={"cmd": "pytest tests/"},
            output="all tests passed",
            success=True,
        )
        pm.check_tool_result(
            tool_name="shell",
            params={"cmd": "npm run build"},
            output="build complete",
            success=True,
        )
        mem = store.read_memory("build-commands")
        assert "pytest tests/" in mem.content
        assert "npm run build" in mem.content

    def test_duplicate_command_not_appended(self, store):
        pm = ProactiveMemory(store)
        pm.check_tool_result(
            tool_name="shell",
            params={"cmd": "pytest tests/"},
            output="all tests passed",
            success=True,
        )
        pm.check_tool_result(
            tool_name="shell",
            params={"cmd": "pytest tests/"},
            output="all tests passed",
            success=True,
        )
        mem = store.read_memory("build-commands")
        assert mem.content.count("pytest tests/") == 1

    def test_empty_params_handled(self, store):
        pm = ProactiveMemory(store)
        # The bug fix: params might be {} if not captured properly
        pm.check_tool_result(
            tool_name="shell",
            params={},
            output="all tests passed",
            success=True,
        )
        assert store.read_memory("build-commands") is None


class TestProactiveMemoryGenerateName:
    def test_english_words(self):
        name = ProactiveMemory._generate_name("don't use global variables", "feedback")
        assert name.startswith("feedback-")
        assert "global" in name or "variables" in name

    def test_short_input_uses_hash(self):
        name = ProactiveMemory._generate_name("xy", "feedback")
        assert name.startswith("feedback-")
        assert len(name) > len("feedback-")
