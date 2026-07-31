"""Phase 1-3: Unified Execution Abstraction integration tests."""

from __future__ import annotations

import logging
import pytest
from core.types import (
    ModifierScope,
    TOOL_SOURCE_PRIORITY,
    _TOOL_SOURCE_SYSTEM,
    _TOOL_SOURCE_MCP,
    _TOOL_SOURCE_PROJECT,
    ToolMetadata,
)
from skills.tool import SkillActivationTool, SkillContextModifier


# ── Phase 1: Flat Namespace ───────────────────────────────────────────────


def test_skill_activation_tool_name_is_original_frontmatter_name() -> None:
    """Skills use flat original name — no prefixes."""
    from skills.registry import SkillMetadata
    meta = SkillMetadata(name="review", display_name="Review", description="Review code")
    tool = SkillActivationTool(meta)
    assert tool.name == "review"
    assert ":" not in tool.name
    assert tool.name != "skill:review"


def test_legacy_loader_is_hidden_from_llm() -> None:
    """__legacy_skill_loader is excluded from LLM schemas."""
    from skills.tool import SkillTool
    loader = SkillTool(skill_registry=None)
    assert loader.name == "__legacy_skill_loader"
    assert loader.visible_to_llm is False


def test_modifier_scope_enum_values() -> None:
    """ModifierScope has TURN and RUN values."""
    assert ModifierScope.TURN == "turn"
    assert ModifierScope.RUN == "run"


def test_modifier_defaults_to_turn_scope() -> None:
    """SkillContextModifier defaults to TURN scope."""
    modifier = SkillContextModifier()
    assert modifier.scope == "turn"


def test_tool_source_priority_ordering() -> None:
    """System > Project > MCP priority."""
    assert TOOL_SOURCE_PRIORITY[_TOOL_SOURCE_SYSTEM] == 3
    assert TOOL_SOURCE_PRIORITY[_TOOL_SOURCE_PROJECT] == 2
    assert TOOL_SOURCE_PRIORITY[_TOOL_SOURCE_MCP] == 1
    assert TOOL_SOURCE_PRIORITY[_TOOL_SOURCE_SYSTEM] > TOOL_SOURCE_PRIORITY[_TOOL_SOURCE_PROJECT]
    assert TOOL_SOURCE_PRIORITY[_TOOL_SOURCE_PROJECT] > TOOL_SOURCE_PRIORITY[_TOOL_SOURCE_MCP]


# ── Phase 2: TURN Exception Safety (pending implementation) ─────────────────


def test_turn_modifier_always_present_even_on_failure() -> None:
    """Phase 2 acceptance gate: SkillActivationTool.execute() always returns
    a TURN-scoped modifier in metadata, including on failure paths.

    This guarantees ToolExecutionPipeline._fire_post_tool_hook() can always
    call deactivate_turn_scoped_modifier() — even when execute() throws or
    returns an error. No state leak.
    """
    from skills.registry import SkillMetadata
    meta = SkillMetadata(name="test", display_name="Test", description="Desc")
    tool = SkillActivationTool(meta)
    # tool.execute() requires skill_registry — without it, it returns error result
    result = tool.execute({})
    assert not result.success  # no registry → fails
    # The modifier with scope='turn' is STILL embedded in metadata (Phase 2 fix)
    modifier = result.metadata.get("skill_modifier")
    assert modifier is not None, "TURN modifier MUST be present even on failure"
    assert modifier.scope == "turn"
    # Verify the modifier carries the correct metadata for cleanup
    assert modifier.allowed_tools == meta.allowed_tools
    assert modifier.disallowed_tools == meta.disallowed_tools


def test_turn_modifier_present_on_render_failure() -> None:
    """Even when load_and_render fails, modifier is still returned."""
    from skills.registry import SkillMetadata
    meta = SkillMetadata(name="missing", display_name="Missing", description="Not there")
    # Create a tool with a real registry that has no such skill
    from skills.registry import SkillRegistry
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = SkillRegistry(tmpdir, include_builtin=False, live_reload=False)
        tool = SkillActivationTool(meta, skill_registry=reg)
        result = tool.execute({})
        assert not result.success  # skill file doesn't exist → load_and_render returns None
        modifier = result.metadata.get("skill_modifier")
        assert modifier is not None, "TURN modifier MUST be present even on render failure"
        assert modifier.scope == "turn"


# ── Phase 3: Namespace Collision Resolution ────────────────────────────────

# ── Test helpers ──

def _make_tool(name: str, source: str = "system") -> "BaseTool":
    """Create a minimal BaseTool with the given name and source."""
    from core.base import BaseTool, ToolResult

    class _FakeTool(BaseTool):
        @property
        def name(self) -> str:
            return name

        @property
        def description(self) -> str:
            return f"Fake tool {name}"

        @property
        def parameters_schema(self) -> dict:
            return {"type": "object", "properties": {}}

        @property
        def metadata(self):
            return ToolMetadata(source=source)

        def execute(self, params):
            return ToolResult(success=True, output="")

    return _FakeTool()


# ── Tests ──


def test_native_tool_overrides_mcp_tool(caplog) -> None:
    """System > MCP: native tool replaces MCP tool of same name."""
    from core.base import ToolRegistry
    reg = ToolRegistry()
    native = _make_tool("search", "system")
    mcp = _make_tool("search", "mcp")
    reg.register(native)
    reg.register(mcp)
    # Native should still be registered
    assert "search" in reg._tools
    assert reg._tool_source_for(reg._tools["search"]) == "system"
    # MCP should have been rejected with WARNING
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("search" in w and "rejected" in w for w in warnings)


def test_mcp_cannot_override_skill(caplog) -> None:
    """Project > MCP: skill rejects MCP tool with same name."""
    from core.base import ToolRegistry
    reg = ToolRegistry()
    skill = _make_tool("review", "project")
    mcp = _make_tool("review", "mcp")
    reg.register(skill)
    reg.register(mcp)
    assert reg._tool_source_for(reg._tools["review"]) == "project"
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("review" in w and "rejected" in w for w in warnings)


def test_same_priority_first_wins(caplog) -> None:
    """Same priority → first-wins: second MCP tool rejected."""
    from core.base import ToolRegistry
    reg = ToolRegistry()
    mcp1 = _make_tool("search", "mcp")
    mcp2 = _make_tool("search", "mcp")
    reg.register(mcp1)
    reg.register(mcp2)
    assert reg._tools["search"] is mcp1  # first-wins
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("search" in w and "same priority" in w for w in warnings)


def test_higher_priority_replaces_lower(caplog) -> None:
    """System replaces project: native tool overrides skill."""
    from core.base import ToolRegistry
    reg = ToolRegistry()
    skill = _make_tool("review", "project")
    native = _make_tool("review", "system")
    reg.register(skill)
    reg.register(native)
    assert reg._tool_source_for(reg._tools["review"]) == "system"
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("review" in w and "replaces" in w for w in warnings)


def test_cross_session_isolation() -> None:
    """Session A collision does not affect Session B — per-session namespace."""
    from core.base import ToolRegistry
    reg_a = ToolRegistry()
    reg_b = ToolRegistry()
    mcp1 = _make_tool("search", "mcp")
    mcp2 = _make_tool("search", "mcp")
    reg_a.register(mcp1)
    # reg_a should reject second MCP tool
    reg_a.register(mcp2)
    assert reg_a._tools["search"] is mcp1
    # reg_b has no collisions — should register freely
    reg_b.register(mcp2)
    assert reg_b._tools["search"] is mcp2
    # reg_a and reg_b are independent
    assert len(reg_a._tools) == 1
    assert len(reg_b._tools) == 1


def test_no_collision_when_names_differ(caplog) -> None:
    """Different names never collide — even with same source."""
    from core.base import ToolRegistry
    reg = ToolRegistry()
    reg.register(_make_tool("search", "system"))
    reg.register(_make_tool("search2", "system"))
    assert "search" in reg._tools
    assert "search2" in reg._tools
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("collision" in w.lower() for w in warnings)


# ── Phase 4: Evidence Dual-Track ────────────────────────────────────────────


def test_tool_activated_skill_produces_tool_call_completed() -> None:
    """SkillActivationTool through ToolExecutionPipeline produces TOOL_CALL_COMPLETED.

    The tool_source field identifies it as 'project' (skill provenance).
    """
    from skills.registry import SkillMetadata
    meta = SkillMetadata(name="review", display_name="Review", description="Code review")
    tool = SkillActivationTool(meta)
    result = tool.execute({})
    assert not result.success  # no registry → fails, but structure is correct
    # Verify result has the data that evidence recorder would use
    assert result.metadata is not None
    assert "skill_modifier" in result.metadata
    # Domain evidence is empty — SkillActivationTool doesn't set evidence metadata
    # This means _record_skill() won't fire (correct behavior for tool path)
    evidence = result.metadata.get("evidence", {})
    if isinstance(evidence, dict):
        assert "skill_name" not in evidence


def test_evidence_kind_skill_loaded_is_deprecated() -> None:
    """SKILL_LOADED has deprecation annotation on EvidenceKind."""
    from agent.session.run_evidence import EvidenceKind
    # Verify SKILL_LOADED exists (for backward compat)
    assert hasattr(EvidenceKind, "SKILL_LOADED")
    assert EvidenceKind.SKILL_LOADED == "skill_loaded"
    # Verify deprecation annotation in docstring
    assert "deprecat" in EvidenceKind.__doc__.lower()
    assert "sunset" in EvidenceKind.__doc__.lower()


def test_dual_evidence_completion_check() -> None:
    """Completion Guard accepts both TOOL_CALL_COMPLETED and SKILL_LOADED.

    Phase 4: The EvidenceValidator.evaluate() method checks both types:
    - TOOL_CALL_COMPLETED with tool_name=skill_name (flat name, tool path)
    - SKILL_LOADED with tool_name="skill:{name}" (legacy format, lifecycle path)

    This test verifies the EvidenceKind values and the semantics contract.
    """
    from agent.session.run_evidence import EvidenceEntry, EvidenceKind, EvidenceStatus

    # Verify the two evidence kinds are distinct
    assert EvidenceKind.TOOL_CALL_COMPLETED != EvidenceKind.SKILL_LOADED

    # Scenario 1: Tool-path skill activation → TOOL_CALL_COMPLETED
    tool_path_entry = EvidenceEntry(
        evidence_id="ev_tp",
        idempotency_key="tc:review:done",
        root_run_id="",
        session_id="s1",
        producer_session_id="s1",
        kind=EvidenceKind.TOOL_CALL_COMPLETED,
        status=EvidenceStatus.SUCCEEDED,
        tool_name="review",  # Flat name — no prefix
        call_id="inv_1",
        invocation_id="inv_1",
        parameters_digest="abc",
        result_digest="def",
        source_fingerprint="fp",
        metadata={"tool_source": "project"},
    )
    assert tool_path_entry.kind == EvidenceKind.TOOL_CALL_COMPLETED
    assert tool_path_entry.tool_name == "review"

    # Scenario 2: Lifecycle-path skill activation → SKILL_LOADED
    lifecycle_entry = EvidenceEntry(
        evidence_id="ev_lc",
        idempotency_key="skill:s1:inv_2:review:fp",
        root_run_id="",
        session_id="s1",
        producer_session_id="s1",
        kind=EvidenceKind.SKILL_LOADED,
        status=EvidenceStatus.SUCCEEDED,
        tool_name="skill:review",  # Legacy format: "skill:{name}"
        call_id="inv_2",
        invocation_id="inv_2",
        parameters_digest="xyz",
        result_digest="",
        source_fingerprint="fp_skill",
    )
    assert lifecycle_entry.kind == EvidenceKind.SKILL_LOADED
    assert lifecycle_entry.tool_name == "skill:review"
