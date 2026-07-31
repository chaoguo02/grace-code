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
