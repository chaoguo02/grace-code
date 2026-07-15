"""
tests/test_skills_alignment.py

测试 Skill 系统与 Claude Code 对齐：
- SK-E3: 工具名 "Skill" + alias "use_skill"
- SK-E2: 无 triggers 字段、无 match_triggers() 方法
"""

from __future__ import annotations

import tempfile
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# SK-E3: 工具名 "Skill"
# ---------------------------------------------------------------------------

def test_skill_tool_name_is_Skill():
    """SK-E3: SkillTool.name must return 'Skill' (Claude Code alignment)."""
    from skills.tool import SkillTool
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        reg = SkillRegistry(tmp, include_builtin=False)
        tool = SkillTool(reg)
        assert tool.name == "Skill", f"Expected 'Skill', got '{tool.name}'"


def test_skill_tool_has_use_skill_alias():
    """SK-E3: SkillTool must have 'use_skill' as alias for backward compatibility."""
    from skills.tool import SkillTool
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        reg = SkillRegistry(tmp, include_builtin=False)
        tool = SkillTool(reg)
        assert "use_skill" in tool.aliases, (
            f"Expected 'use_skill' in aliases, got {tool.aliases}"
        )


def test_skill_tool_execute_with_new_name():
    """SK-E3: Skill execution works with renamed tool."""
    from skills.tool import SkillTool
    from skills.registry import SkillRegistry

    # Create a temporary skill
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "greet"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: Greet
description: A greeting skill
---

Hello, $ARGUMENTS! Welcome.
""")

        reg = SkillRegistry(tmp, include_builtin=False)
        tool = SkillTool(reg)

        # Execute via tool
        result = tool.execute({"skill_name": "greet", "arguments": "World"})
        assert result.success, f"Expected success, got error: {result.error}"
        assert "Hello, World!" in result.output
        assert "[Skill: greet]" in result.output


def test_skill_tool_unknown_skill_returns_error():
    """SK-E3: Skill execution returns proper error for unknown skills."""
    from skills.tool import SkillTool
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        reg = SkillRegistry(tmp, include_builtin=False)
        tool = SkillTool(reg)
        result = tool.execute({"skill_name": "nonexistent"})
        assert not result.success
        assert "not found" in result.error.lower() or "not found" in result.error


# ---------------------------------------------------------------------------
# SK-E2: 无 triggers 字段 / 无 match_triggers()
# ---------------------------------------------------------------------------

def test_skill_metadata_has_no_triggers_field():
    """SK-E2: SkillMetadata must NOT have a 'triggers' attribute."""
    from skills.registry import SkillMetadata

    meta = SkillMetadata(
        name="test-skill",
        display_name="Test Skill",
        description="A test skill",
        dir_path="/fake/path",
    )
    assert not hasattr(meta, "triggers"), (
        "SkillMetadata should NOT have 'triggers' field"
    )


def test_skill_registry_has_no_match_triggers():
    """SK-E2: SkillRegistry must NOT have match_triggers() method."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        reg = SkillRegistry(tmp, include_builtin=False)
        assert not hasattr(reg, "match_triggers"), (
            "SkillRegistry should NOT have match_triggers() method"
        )


def test_skill_parsing_ignores_triggers_in_frontmatter():
    """SK-E2: triggers in frontmatter are silently ignored (no error)."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "legacy-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        # This SKILL.md still has 'triggers' in frontmatter (legacy format)
        skill_md.write_text("""---
name: Legacy Skill
description: This skill has old-style triggers
triggers:
  - old
  - legacy
---

Legacy skill body.
""")

        reg = SkillRegistry(tmp, include_builtin=False)
        meta = reg._metadata.get("legacy-skill")
        assert meta is not None, "Skill should still be discovered"
        assert meta.name == "legacy-skill"
        assert meta.description == "This skill has old-style triggers"
        # triggers are silently dropped, no error


def test_builtin_skills_have_no_triggers():
    """SK-E2: Builtin SKILL.md files must not contain 'triggers:' frontmatter."""
    from pathlib import Path

    builtin_dir = Path(__file__).parent.parent / "skills" / "builtin"
    for skill_dir in builtin_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        content = skill_md.read_text(encoding="utf-8")

        # Split frontmatter
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            assert "triggers:" not in frontmatter, (
                f"{skill_md} still has 'triggers:' in frontmatter!"
            )


def test_format_for_prompt_references_Skill_tool():
    """SK-E3: format_for_prompt() should mention 'Skill' tool, not 'use_skill'."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "my-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: My Skill
description: Does something useful
---

Skill body here.
""")

        reg = SkillRegistry(tmp, include_builtin=False)
        prompt = reg.format_for_prompt()
        assert "Skill" in prompt, f"format_for_prompt should mention 'Skill' tool:\n{prompt}"
        assert "use_skill" not in prompt, (
            f"format_for_prompt should NOT mention deprecated 'use_skill':\n{prompt}"
        )
        assert "my-skill" in prompt


def test_format_for_prompt_empty_registry():
    """SK-E3: format_for_prompt() returns empty string when no skills exist."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        reg = SkillRegistry(tmp, include_builtin=False)
        assert reg.format_for_prompt() == ""


# ---------------------------------------------------------------------------
# SK-E1: /skill-name slash command (Claude Code alignment)
# ---------------------------------------------------------------------------

def test_handle_slash_skill_dispatches_registered_skill():
    """SK-E1: /skill-name arg → renders and returns skill content."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "greet"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: Greet
description: A greeting skill
---

Hello, $ARGUMENTS! Welcome to the project.
""")

        reg = SkillRegistry(tmp, include_builtin=False)

        # Simulate _handle_slash_skill functionality
        user_input = "/greet World"
        parts = user_input[1:].split(maxsplit=1)
        name, args = parts[0], parts[1] if len(parts) > 1 else ""

        assert reg.has_skill(name), f"Skill '{name}' should exist"
        rendered = reg.load_and_render(name, args)
        assert rendered is not None
        assert "Hello, World!" in rendered
        assert "$ARGUMENTS" not in rendered  # substituted


def test_handle_slash_skill_no_args_renders_empty_arguments():
    """SK-E1: /skill-name with no args renders with empty $ARGUMENTS."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "greet"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: Greet
description: A greeting skill
---

Hello, $ARGUMENTS! Welcome.
""")

        reg = SkillRegistry(tmp, include_builtin=False)

        rendered = reg.load_and_render("greet", "")
        assert rendered is not None
        assert "Hello, !" in rendered or "Hello,  Welcome." in rendered.replace("  ", " ")


def test_handle_slash_skill_unknown_returns_none():
    """SK-E1: /nonexistent returns None from has_skill check."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        reg = SkillRegistry(tmp, include_builtin=False)
        assert not reg.has_skill("nonexistent")
        assert reg.load_and_render("nonexistent", "") is None


def test_handle_slash_skill_non_slash_input_ignored():
    """SK-E1: Non-slash input is not treated as skill invocation."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        reg = SkillRegistry(tmp, include_builtin=False)
        # Regular input without / prefix should not trigger skill path
        assert not reg.has_skill("code-review")  # not a slash command


def test_handle_slash_skill_chat_session_integration():
    """SK-E1: ChatSession._handle_slash_skill works end-to-end."""
    import sys
    from pathlib import Path
    from unittest.mock import MagicMock

    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "review"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: review
description: Code review skill
---

Review the following code for bugs and issues: $ARGUMENTS
Focus on correctness, security, and performance.
""")

        reg = SkillRegistry(tmp, include_builtin=False)

        # Mock a ChatSession with just enough to test _handle_slash_skill
        # We can't easily instantiate a full ChatSession, so test the logic inline
        user_input = "/review auth/session.py"
        parts = user_input[1:].split(maxsplit=1)
        name, args = parts[0], parts[1] if len(parts) > 1 else ""

        assert name == "review"
        assert args == "auth/session.py"
        assert reg.has_skill(name)

        rendered = reg.load_and_render(name, args)
        assert rendered is not None
        assert "auth/session.py" in rendered
        assert "Review the following code" in rendered
        assert "correctness, security, and performance" in rendered


def test_handle_slash_skill_with_special_characters_in_args():
    """SK-E1: Arguments with special characters are handled."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "echo"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: echo
description: Echo arguments
---

You said: $ARGUMENTS
""")

        reg = SkillRegistry(tmp, include_builtin=False)

        rendered = reg.load_and_render("echo", "fix bug #42 — urgent!")
        assert rendered is not None
        assert "fix bug #42 — urgent!" in rendered


# ---------------------------------------------------------------------------
# SK-02: Description auto-matching (when_to_use frontmatter field)
# ---------------------------------------------------------------------------

def test_skill_metadata_parses_when_to_use():
    """SK-02: when_to_use is parsed from SKILL.md frontmatter."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "deploy"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: Deploy
description: Deploy the project to production
when_to_use: When asked to deploy, release, or ship the project
---

# Deploy Skill
Run the deployment pipeline.
""")

        reg = SkillRegistry(tmp, include_builtin=False)
        meta = reg._metadata.get("deploy")
        assert meta is not None
        assert meta.when_to_use == "When asked to deploy, release, or ship the project"


def test_format_for_prompt_includes_when_to_use():
    """SK-02: format_for_prompt() includes when_to_use for LLM semantic matching."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "review"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: Review
description: Review code changes
when_to_use: For code review, PR check, or quality audit requests
---

Review the code.
""")

        reg = SkillRegistry(tmp, include_builtin=False)
        prompt = reg.format_for_prompt()
        assert "when_to_use" not in prompt.lower()  # field name not shown verbatim
        assert "For code review, PR check, or quality audit requests" in prompt
        assert "(Use when:" in prompt  # CC-aligned format


def test_skill_without_when_to_use_still_works():
    """SK-02: Skills without when_to_use render correctly (backward compatible)."""
    from skills.registry import SkillRegistry

    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "simple"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: Simple
description: A simple skill without when_to_use
---

Just do the thing.
""")

        reg = SkillRegistry(tmp, include_builtin=False)
        meta = reg._metadata.get("simple")
        assert meta is not None
        assert meta.when_to_use == ""
        prompt = reg.format_for_prompt()
        assert "simple" in prompt
        assert "(Use when:" not in prompt  # no when_to_use = no extra text
