"""
tests/test_skills.py

Skill 系统单元测试。
"""

import os
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def skills_dir(tmp_path):
    """创建一个包含示例 skill 的临时目录。"""
    # skill 1: code-review
    skill1_dir = tmp_path / "code-review"
    skill1_dir.mkdir()
    (skill1_dir / "SKILL.md").write_text(
        "---\n"
        "name: code-review\n"
        "description: Review code for bugs and style issues.\n"
        "---\n\n"
        "Review the following code:\n\n$ARGUMENTS\n\nBe thorough.",
        encoding="utf-8",
    )

    # skill 2: explain-error (no description)
    skill2_dir = tmp_path / "explain-error"
    skill2_dir.mkdir()
    (skill2_dir / "SKILL.md").write_text(
        "---\n"
        "name: explain-error\n"
        "description: Explain an error and suggest fixes.\n"
        "---\n\n"
        "Error: $ARGUMENTS\n\nExplain what went wrong.",
        encoding="utf-8",
    )

    # skill 3: no frontmatter
    skill3_dir = tmp_path / "bare-skill"
    skill3_dir.mkdir()
    (skill3_dir / "SKILL.md").write_text(
        "Just a plain skill body without frontmatter.\n$ARGUMENTS",
        encoding="utf-8",
    )

    # not a skill: file instead of directory
    (tmp_path / "not-a-skill.md").write_text("ignore me", encoding="utf-8")

    # not a skill: directory without SKILL.md
    (tmp_path / "empty-dir").mkdir()

    return str(tmp_path)


@pytest.fixture
def empty_skills_dir(tmp_path):
    """空的 skills 目录。"""
    return str(tmp_path)


# ---------------------------------------------------------------------------
# SkillRegistry Tests
# ---------------------------------------------------------------------------

class TestSkillRegistry:
    def test_discover_finds_skills(self, skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(skills_dir, include_builtin=False)
        skills = reg.list_skills()
        names = [s.name for s in skills]
        assert "code-review" in names
        assert "explain-error" in names
        assert "bare-skill" in names
        # 非 skill 不应被发现
        assert "not-a-skill" not in names
        assert "empty-dir" not in names

    def test_discover_empty_dir(self, empty_skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(empty_skills_dir, include_builtin=False)
        assert reg.list_skills() == []

    def test_discover_nonexistent_dir(self):
        from skills.registry import SkillRegistry
        reg = SkillRegistry("/nonexistent/path/to/skills", include_builtin=False)
        assert reg.list_skills() == []

    def test_has_skill(self, skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(skills_dir, include_builtin=False)
        assert reg.has_skill("code-review")
        assert reg.has_skill("explain-error")
        assert not reg.has_skill("nonexistent")

    def test_metadata_parsing(self, skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(skills_dir, include_builtin=False)
        skills = {s.name: s for s in reg.list_skills()}
        cr = skills["code-review"]
        assert cr.display_name == "code-review"
        assert cr.description == "Review code for bugs and style issues."
        assert cr.dir_path == os.path.join(skills_dir, "code-review")

    def test_metadata_no_frontmatter(self, skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(skills_dir, include_builtin=False)
        skills = {s.name: s for s in reg.list_skills()}
        bare = skills["bare-skill"]
        assert bare.display_name == "bare-skill"
        assert bare.description == ""

    def test_load_and_render_basic(self, skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(skills_dir, include_builtin=False)
        rendered = reg.load_and_render("code-review", "def foo(): pass")
        assert rendered is not None
        assert "def foo(): pass" in rendered
        assert "$ARGUMENTS" not in rendered
        assert "Review the following code:" in rendered

    def test_load_and_render_empty_args(self, skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(skills_dir, include_builtin=False)
        rendered = reg.load_and_render("code-review", "")
        assert rendered is not None
        assert "$ARGUMENTS" not in rendered

    def test_load_and_render_nonexistent(self, skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(skills_dir, include_builtin=False)
        assert reg.load_and_render("nonexistent") is None

    def test_load_and_render_no_frontmatter(self, skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(skills_dir, include_builtin=False)
        rendered = reg.load_and_render("bare-skill", "hello")
        assert rendered is not None
        assert "hello" in rendered
        assert "plain skill body" in rendered

    def test_format_for_prompt(self, skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(skills_dir, include_builtin=False)
        prompt = reg.format_for_prompt()
        assert "## Available Skills" in prompt
        assert "code-review" in prompt
        assert "explain-error" in prompt
        assert "use_skill" in prompt

    def test_format_for_prompt_empty(self, empty_skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(empty_skills_dir, include_builtin=False)
        assert reg.format_for_prompt() == ""

    def test_refresh(self, skills_dir):
        from skills.registry import SkillRegistry
        reg = SkillRegistry(skills_dir, include_builtin=False)
        assert len(reg.list_skills()) == 3

        # 添加新 skill
        new_dir = Path(skills_dir) / "new-skill"
        new_dir.mkdir()
        (new_dir / "SKILL.md").write_text(
            "---\nname: new-skill\ndescription: A new skill\n---\nBody here.",
            encoding="utf-8",
        )

        # refresh 后应发现新 skill
        reg.refresh()
        assert len(reg.list_skills()) == 4
        assert reg.has_skill("new-skill")


# ---------------------------------------------------------------------------
# SkillTool Tests
# ---------------------------------------------------------------------------

class TestSkillTool:
    def test_execute_success(self, skills_dir):
        from skills.registry import SkillRegistry
        from skills.tool import SkillTool
        reg = SkillRegistry(skills_dir, include_builtin=False)
        tool = SkillTool(reg)

        result = tool.execute({"skill_name": "code-review", "arguments": "x = 1"})
        assert result.success
        assert "[Skill: code-review]" in result.output
        assert "x = 1" in result.output

    def test_execute_not_found(self, skills_dir):
        from skills.registry import SkillRegistry
        from skills.tool import SkillTool
        reg = SkillRegistry(skills_dir, include_builtin=False)
        tool = SkillTool(reg)

        result = tool.execute({"skill_name": "nonexistent"})
        assert not result.success
        assert "not found" in result.error

    def test_execute_no_skill_name(self, skills_dir):
        from skills.registry import SkillRegistry
        from skills.tool import SkillTool
        reg = SkillRegistry(skills_dir, include_builtin=False)
        tool = SkillTool(reg)

        result = tool.execute({"skill_name": ""})
        assert not result.success

    def test_schema_includes_skill_names(self, skills_dir):
        from skills.registry import SkillRegistry
        from skills.tool import SkillTool
        reg = SkillRegistry(skills_dir, include_builtin=False)
        tool = SkillTool(reg)

        schema = tool.parameters_schema
        enum_values = schema["properties"]["skill_name"]["enum"]
        assert "code-review" in enum_values
        assert "explain-error" in enum_values

    def test_tool_name_and_description(self, skills_dir):
        from skills.registry import SkillRegistry
        from skills.tool import SkillTool
        reg = SkillRegistry(skills_dir, include_builtin=False)
        tool = SkillTool(reg)

        assert tool.name == "use_skill"
        assert "code-review" in tool.description


# ---------------------------------------------------------------------------
# Frontmatter Parsing Edge Cases
# ---------------------------------------------------------------------------

class TestFrontmatterParsing:
    def test_quoted_values(self, tmp_path):
        from skills.registry import SkillRegistry
        skill_dir = tmp_path / "quoted"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            '---\nname: "quoted-name"\ndescription: \'single quoted\'\n---\nBody.',
            encoding="utf-8",
        )
        reg = SkillRegistry(str(tmp_path), include_builtin=False)
        skills = reg.list_skills()
        assert len(skills) == 1
        assert skills[0].display_name == "quoted-name"
        assert skills[0].description == "single quoted"

    def test_multiline_body(self, tmp_path):
        from skills.registry import SkillRegistry
        skill_dir = tmp_path / "multi"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: multi\ndescription: test\n---\n\nLine 1\nLine 2\n$ARGUMENTS\nLine 3",
            encoding="utf-8",
        )
        reg = SkillRegistry(str(tmp_path), include_builtin=False)
        rendered = reg.load_and_render("multi", "REPLACED")
        assert "Line 1" in rendered
        assert "REPLACED" in rendered
        assert "Line 3" in rendered
        assert "$ARGUMENTS" not in rendered

    def test_no_closing_frontmatter(self, tmp_path):
        from skills.registry import SkillRegistry
        skill_dir = tmp_path / "broken"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: broken\nThis has no closing ---\nJust body text.",
            encoding="utf-8",
        )
        reg = SkillRegistry(str(tmp_path), include_builtin=False)
        # 没有闭合的 --- 应该把整个内容当作 body
        skills = reg.list_skills()
        assert len(skills) == 1
        assert skills[0].description == ""
