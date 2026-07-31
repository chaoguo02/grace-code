"""Phase 1-2: Unified Execution Abstraction integration tests."""

from __future__ import annotations

import pytest
from core.types import ModifierScope, TOOL_SOURCE_PRIORITY, _TOOL_SOURCE_SYSTEM, _TOOL_SOURCE_MCP, _TOOL_SOURCE_PROJECT
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
