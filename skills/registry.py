"""
skills/registry.py

SkillRegistry — 技能发现、加载、渲染。

发现流程：
1. 扫描多个 skills 目录（内置 + 项目级）
2. 每个子目录中查找 SKILL.md
3. 解析 YAML frontmatter 提取 metadata（name, description）
4. 调用时才读取 body 并执行 $ARGUMENTS 替换

Aligned with Claude Code: no keyword-based triggers; LLM matches skills
via description semantic similarity in the system prompt listing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# 内置 skills 目录（随代码分发）
BUILTIN_SKILLS_DIR = str(Path(__file__).parent / "builtin")


@dataclass
class SkillMetadata:
    """技能元数据（启动时加载，低成本）。

    Aligned with Claude Code Skill frontmatter:
    - name: directory name, also the invocation name (/name)
    - display_name: frontmatter 'name' field (human-readable)
    - description: frontmatter 'description' field (used for LLM semantic matching)
    - dir_path: absolute path to skill directory

    Note: 'triggers' keyword matching has been removed per Claude Code alignment.
    Claude Code uses description-based semantic matching by the LLM, not substring matching.
    """
    name: str              # 目录名，也是调用名（/name）
    display_name: str      # frontmatter 中的 name 字段
    description: str       # frontmatter 中的 description
    dir_path: str = ""     # 技能目录的绝对路径
    when_to_use: str = ""  # frontmatter 中的 when_to_use（LLM 自主匹配）


class SkillRegistry:
    """
    技能注册表。负责发现、索引、加载和渲染技能。

    支持多目录发现：
    - 内置 skills/builtin/（随代码提交）
    - 项目级 .forge-agent/skills/（用户自定义）

    用法：
        registry = SkillRegistry("/path/to/.forge-agent/skills")
        skills = registry.list_skills()
        rendered = registry.load_and_render("code-review", "auth module")
    """

    def __init__(self, skills_dir: str, extra_dirs: list[str] | None = None, include_builtin: bool = True) -> None:
        self._skills_dirs: list[str] = []
        # 内置目录（可选，测试时可关闭）
        if include_builtin:
            self._skills_dirs.append(BUILTIN_SKILLS_DIR)
        # 项目级目录
        if skills_dir:
            self._skills_dirs.append(skills_dir)
        # 额外目录（如用户级 ~/.forge-agent/skills/）
        if extra_dirs:
            self._skills_dirs.extend(extra_dirs)

        self._metadata: dict[str, SkillMetadata] = {}
        self._discover()

    def _discover(self) -> None:
        """扫描所有 skills 目录，解析每个 SKILL.md 的 frontmatter。"""
        for skills_dir in self._skills_dirs:
            skills_path = Path(skills_dir)
            if not skills_path.is_dir():
                logger.debug("Skills directory does not exist: %s", skills_dir)
                continue

            for entry in sorted(skills_path.iterdir()):
                if not entry.is_dir():
                    continue
                skill_file = entry / "SKILL.md"
                if not skill_file.is_file():
                    continue

                try:
                    metadata = self._parse_frontmatter(skill_file, entry.name)
                    if metadata:
                        # 项目级覆盖内置（后扫描的目录覆盖先扫描的）
                        self._metadata[metadata.name] = metadata
                        logger.debug("Discovered skill: %s (from %s)", metadata.name, skills_dir)
                except Exception as e:
                    logger.warning("Failed to parse skill %s: %s", entry.name, e)

        logger.info("Discovered %d skills total", len(self._metadata))

    def _parse_frontmatter(self, skill_file: Path, dir_name: str) -> SkillMetadata | None:
        """Parse SKILL.md YAML frontmatter.

        Supported fields (aligned with Claude Code):
          name, description, when_to_use, model, effort,
          disable-model-invocation, user-invocable, allowed-tools,
          disallowed-tools, context, agent, paths, arguments

        Note: 'triggers' has been removed — Claude Code uses LLM semantic
        matching via description, not keyword-based substring matching.
        """
        content = skill_file.read_text(encoding="utf-8")
        frontmatter, _ = self._split_frontmatter(content)

        if not frontmatter:
            return SkillMetadata(
                name=dir_name,
                display_name=dir_name,
                description="",
                dir_path=str(skill_file.parent),
            )

        try:
            fm_dict: dict = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError:
            fm_dict = {}

        return SkillMetadata(
            name=dir_name,
            display_name=str(fm_dict.get("name", dir_name)),
            description=str(fm_dict.get("description", "")),
            when_to_use=str(fm_dict.get("when_to_use", "")),
            dir_path=str(skill_file.parent),
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[str, str]:
        """Split frontmatter and body using the shared utility."""
        from utils.frontmatter import split_frontmatter
        return split_frontmatter(content)

    def list_skills(self) -> list[SkillMetadata]:
        """返回所有已发现的 skill metadata。"""
        return list(self._metadata.values())

    def has_skill(self, name: str) -> bool:
        """检查是否存在指定名称的 skill。"""
        return name in self._metadata

    def get_skill_detail(self, name: str) -> str | None:
        """返回 skill 的完整 body 内容（未做 $ARGUMENTS 替换）。供 /skill show 使用。"""
        if name not in self._metadata:
            return None
        metadata = self._metadata[name]
        skill_file = Path(metadata.dir_path) / "SKILL.md"
        if not skill_file.is_file():
            return None
        content = skill_file.read_text(encoding="utf-8")
        _, body = self._split_frontmatter(content)
        return body or None

    def load_and_render(self, name: str, arguments: str = "") -> str | None:
        """
        加载并渲染 skill。

        1. 查找 metadata
        2. 读取 SKILL.md body
        3. $ARGUMENTS 替换
        4. 返回渲染后的完整内容
        """
        if name not in self._metadata:
            return None

        metadata = self._metadata[name]
        skill_file = Path(metadata.dir_path) / "SKILL.md"

        if not skill_file.is_file():
            logger.warning("Skill file missing: %s", skill_file)
            return None

        content = skill_file.read_text(encoding="utf-8")
        _, body = self._split_frontmatter(content)

        if not body:
            return None

        rendered = body.replace("$ARGUMENTS", arguments)
        return rendered

    def format_for_prompt(self) -> str:
        """
        Format skill list for system prompt injection.

        Aligned with Claude Code: skills are listed with description so the LLM
        can semantically match and decide when to invoke them. Users can type
        /skill-name directly (see entry/chat.py).

        Returns formatted string:
            ## Available Skills
            Use the `Skill` tool to invoke, or use /skill-name directly:
            - **code-review**: Review code changes for bugs...
            - **explain-error**: Explain an error message...
        """
        if not self._metadata:
            return ""

        lines = [
            "## Available Skills",
            "Use the `Skill` tool to invoke these skills, or type /skill-name directly:",
        ]
        for meta in self._metadata.values():
            desc = meta.description or "(no description)"
            if meta.when_to_use:
                desc += f" (Use when: {meta.when_to_use})"
            lines.append(f"- **/{meta.name}**: {desc}")

        return "\n".join(lines)

    def refresh(self) -> None:
        """重新扫描 skills 目录（用于运行时热加载）。"""
        self._metadata.clear()
        self._discover()
