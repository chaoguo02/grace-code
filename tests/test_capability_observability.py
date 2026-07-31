"""Phase 5.5: Observability integration tests for capability context stats."""

from __future__ import annotations

import json

from capabilities.models import CapabilityKind, CapabilitySection
from context.history import ConversationHistory
from context.manager import ContextManager
from context.stats import ContextStats, ContextTrace
from context.token_budget import TokenBudget
from llm.base import LLMMessage


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_sections(*titles: str) -> list[CapabilitySection]:
    return [
        CapabilitySection(
            title=title,
            content=f"content for {title}",
            priority=i * 10,
            token_estimate=5 + i,
            kind_filter=CapabilityKind.SKILL,
            descriptor_count=1,
            source_fingerprint=f"fp_{title}",
        )
        for i, title in enumerate(titles)
    ]


def _build_with_sections(sections: list[CapabilitySection]) -> ContextStats:
    history = ConversationHistory()
    history.add(LLMMessage(role="user", content="hello"))
    result = ContextManager().build_request_messages(
        history=history,
        token_budget=TokenBudget(total=20_000),
        system_core_text="system",
        capability_sections=sections,
    )
    return result.stats


# ── fingerprint & descriptor count ───────────────────────────────────────────


def test_stats_includes_capability_fingerprint() -> None:
    sections = _make_sections("Skills", "MCP Tool Discovery")
    stats = _build_with_sections(sections)
    assert stats.capability_fingerprint
    assert len(stats.capability_fingerprint) > 0
    # SHA256 truncated to 16 hex chars
    assert len(stats.capability_fingerprint) <= 16


def test_stats_includes_capability_descriptor_count() -> None:
    sections = _make_sections("Skills", "MCP Tool Discovery", "Subagents")
    stats = _build_with_sections(sections)
    assert stats.capability_descriptor_count == 3


def test_stats_includes_capability_section_titles() -> None:
    sections = _make_sections("Skills", "Subagents")
    stats = _build_with_sections(sections)
    assert "Skills" in stats.capability_sections
    assert "Subagents" in stats.capability_sections


def test_stats_includes_capability_token_estimate() -> None:
    sections = _make_sections("Skills", "MCP Tool Discovery")
    stats = _build_with_sections(sections)
    assert stats.capability_token_estimate > 0


def test_stats_includes_capability_trimmed_count() -> None:
    sections = _make_sections("Skills", "MCP Tool Discovery")
    stats = _build_with_sections(sections)
    assert stats.capability_trimmed_count >= 0


def test_no_sections_leaves_capability_fields_at_default() -> None:
    history = ConversationHistory()
    history.add(LLMMessage(role="user", content="hello"))
    result = ContextManager().build_request_messages(
        history=history,
        token_budget=TokenBudget(total=20_000),
        system_core_text="system",
    )
    stats = result.stats
    assert stats.capability_fingerprint == ""
    assert stats.capability_descriptor_count == 0
    assert stats.capability_sections == []
    assert stats.capability_token_estimate == 0
    assert stats.capability_trimmed_count == 0


# ── fingerprint stability ────────────────────────────────────────────────────


def test_fingerprint_stable_across_repeated_identical_snapshots() -> None:
    sections = _make_sections("Skills", "MCP Tool Discovery")
    fps = [_build_with_sections(sections).capability_fingerprint for _ in range(5)]
    assert len(set(fps)) == 1


def test_fingerprint_changes_when_section_content_differs() -> None:
    sections_a = _make_sections("Skills")
    sections_b = _make_sections("Skills", "MCP Tool Discovery")
    fp_a = _build_with_sections(sections_a).capability_fingerprint
    fp_b = _build_with_sections(sections_b).capability_fingerprint
    assert fp_a != fp_b


def test_fingerprint_changes_when_priority_differs() -> None:
    s1 = CapabilitySection(
        title="Skills", content="same", priority=10, token_estimate=5,
        kind_filter=CapabilityKind.SKILL, descriptor_count=1, source_fingerprint="fp",
    )
    s2 = CapabilitySection(
        title="Skills", content="same", priority=90, token_estimate=5,
        kind_filter=CapabilityKind.SKILL, descriptor_count=1, source_fingerprint="fp",
    )
    fp_1 = _build_with_sections([s1]).capability_fingerprint
    fp_2 = _build_with_sections([s2]).capability_fingerprint
    assert fp_1 != fp_2


def test_fingerprint_changes_when_descriptor_count_differs() -> None:
    s1 = CapabilitySection(
        title="Skills", content="same", priority=10, token_estimate=5,
        kind_filter=CapabilityKind.SKILL, descriptor_count=1, source_fingerprint="fp",
    )
    s2 = CapabilitySection(
        title="Skills", content="same", priority=10, token_estimate=5,
        kind_filter=CapabilityKind.SKILL, descriptor_count=7, source_fingerprint="fp",
    )
    fp_1 = _build_with_sections([s1]).capability_fingerprint
    fp_2 = _build_with_sections([s2]).capability_fingerprint
    assert fp_1 != fp_2


def test_fingerprint_stable_when_only_token_estimate_differs() -> None:
    """Token estimates are excluded from fingerprint keys (they're estimates, not content)."""
    history = ConversationHistory()
    history.add(LLMMessage(role="user", content="hello"))
    s1 = CapabilitySection(
        title="Skills", content="same", priority=10, token_estimate=5,
        kind_filter=CapabilityKind.SKILL, descriptor_count=3, source_fingerprint="fp",
    )
    s2 = CapabilitySection(
        title="Skills", content="same", priority=10, token_estimate=9999,
        kind_filter=CapabilityKind.SKILL, descriptor_count=3, source_fingerprint="fp",
    )

    def _fingerprint(section: CapabilitySection) -> str:
        result = ContextManager().build_request_messages(
            history=history,
            token_budget=TokenBudget(total=100_000),
            system_core_text="system",
            capability_sections=[section],
            max_context_window=100_000,
        )
        return result.stats.capability_fingerprint

    fp_1 = _fingerprint(s1)
    fp_2 = _fingerprint(s2)
    assert fp_1
    assert fp_1 == fp_2


def test_fingerprint_changes_when_source_fingerprint_differs() -> None:
    s1 = CapabilitySection(
        title="Skills", content="same", priority=10, token_estimate=5,
        kind_filter=CapabilityKind.SKILL, descriptor_count=3, source_fingerprint="fp_a",
    )
    s2 = CapabilitySection(
        title="Skills", content="same", priority=10, token_estimate=5,
        kind_filter=CapabilityKind.SKILL, descriptor_count=3, source_fingerprint="fp_b",
    )
    fp_1 = _build_with_sections([s1]).capability_fingerprint
    fp_2 = _build_with_sections([s2]).capability_fingerprint
    assert fp_1 != fp_2


# ── trimmed sections reported ────────────────────────────────────────────────


def test_trimmed_count_reported_when_section_exceeds_budget() -> None:
    history = ConversationHistory()
    history.add(LLMMessage(role="user", content="hello"))
    sections = [
        CapabilitySection(
            title="Big",
            content="x " * 6000,
            priority=90,
            token_estimate=6000,
            kind_filter=CapabilityKind.AGENT,
        ),
        CapabilitySection(
            title="Small",
            content="important",
            priority=1,
            token_estimate=2,
            kind_filter=CapabilityKind.MCP_SERVER,
        ),
    ]

    result = ContextManager().build_request_messages(
        history=history,
        token_budget=TokenBudget(total=8000),
        system_core_text="system",
        capability_sections=sections,
        max_context_window=8000,
    )

    assert result.stats.capability_trimmed_count >= 1
    assert "Small" in result.stats.capability_sections
    assert "Big" not in result.stats.capability_sections


def test_trimmed_count_zero_when_all_fit() -> None:
    sections = _make_sections("Skills", "Subagents")
    stats = _build_with_sections(sections)
    assert stats.capability_trimmed_count == 0


# ── summary_line ─────────────────────────────────────────────────────────────


def test_summary_line_includes_capability_info_when_sections_present() -> None:
    sections = _make_sections("Skills")
    stats = _build_with_sections(sections)
    line = stats.summary_line()
    assert "capabilities" in line
    assert "sections 1" in line
    assert stats.capability_fingerprint in line


def test_summary_line_omits_capability_info_when_no_sections() -> None:
    history = ConversationHistory()
    history.add(LLMMessage(role="user", content="hello"))
    result = ContextManager().build_request_messages(
        history=history,
        token_budget=TokenBudget(total=20_000),
        system_core_text="system",
    )
    line = result.stats.summary_line()
    assert "capabilities" not in line


# ── ContextTrace serialisation ───────────────────────────────────────────────


def test_context_trace_to_dict_includes_capability_fields() -> None:
    trace = ContextTrace(
        task_id="test-1",
        step=1,
        stats=ContextStats(
            capability_fingerprint="abc123",
            capability_descriptor_count=5,
            capability_sections=["Skills", "Subagents"],
            capability_token_estimate=42,
            capability_trimmed_count=1,
        ),
    )
    d = trace.to_dict()
    assert d["stats"]["capability_fingerprint"] == "abc123"
    assert d["stats"]["capability_descriptor_count"] == 5
    assert d["stats"]["capability_sections"] == ["Skills", "Subagents"]
    assert d["stats"]["capability_token_estimate"] == 42
    assert d["stats"]["capability_trimmed_count"] == 1


def test_context_trace_to_dict_includes_capability_fields_defaults() -> None:
    trace = ContextTrace(task_id="test-1", step=0)
    d = trace.to_dict()
    assert d["stats"]["capability_fingerprint"] == ""
    assert d["stats"]["capability_descriptor_count"] == 0
    assert d["stats"]["capability_sections"] == []
    assert d["stats"]["capability_token_estimate"] == 0
    assert d["stats"]["capability_trimmed_count"] == 0


def test_context_trace_to_dict_is_json_serializable() -> None:
    trace = ContextTrace(
        task_id="test-1",
        step=2,
        stats=ContextStats(
            capability_fingerprint="deadbeef00112233",
            capability_descriptor_count=7,
            capability_sections=["Skills", "MCP Tool Discovery", "Subagents"],
            capability_token_estimate=128,
            capability_trimmed_count=0,
        ),
    )
    d = trace.to_dict()
    raw = json.dumps(d)
    parsed = json.loads(raw)
    assert parsed["stats"]["capability_fingerprint"] == "deadbeef00112233"
    assert parsed["stats"]["capability_descriptor_count"] == 7
    assert len(parsed["stats"]["capability_sections"]) == 3
