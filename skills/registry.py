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
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# 内置 skills 目录（随代码分发）
BUILTIN_SKILLS_DIR = str(Path(__file__).parent / "builtin")


@dataclass
class SkillMetadata:
    """技能元数据 — aligned with Claude Code Skill frontmatter reference.

    https://code.claude.com/docs/en/skills#frontmatter-reference

    Core:
      name:        directory name, also the invocation command /name
      display_name: frontmatter 'name' field (human-readable label in listings)
      description: frontmatter 'description' (LLM uses this to decide when to load)
      when_to_use: frontmatter 'when_to_use' — extra context for LLM auto-load
      dir_path:    absolute path to the skill directory

    Invocation control:
      disable_model_invocation:  true → only user /name can invoke, LLM cannot auto-load
      user_invocable:            false → hidden from / menu, only LLM can invoke

    Execution:
      model:   model override when skill is active ("" = inherit)
      effort:  effort level override ("" = inherit): low|medium|high|xhigh|max
      context: "" | "fork" — run in forked subagent context
      agent:   subagent type when context=fork

    Activation scope:
      paths: glob patterns limiting auto-activation to matching files

    Tool control:
      allowed_tools:    tools granted without per-use approval while active
      disallowed_tools: tools removed from available pool while active
    """
    name: str              # 目录名，也是调用名（/name）
    display_name: str      # frontmatter 中的 name 字段
    description: str       # frontmatter 中的 description
    dir_path: str = ""     # 技能目录的绝对路径
    when_to_use: str = ""  # frontmatter 中的 when_to_use

    # ── Invocation control ──
    disable_model_invocation: bool = False
    user_invocable: bool = True

    # ── Execution overrides ──
    model: str = ""    # "" = inherit session model
    effort: str = ""   # "" = inherit session effort
    context: str = ""  # "" | "fork"
    agent: str = ""    # subagent type when context=fork

    # ── Activation scope / arguments ──
    paths: tuple[str, ...] = ()
    arguments: tuple[str, ...] = ()  # named positional arguments for $name substitution

    # ── Tool control ──
    allowed_tools: frozenset[str] = frozenset()
    disallowed_tools: frozenset[str] = frozenset()

    # ── Derived helpers ──

    @property
    def model_invocable(self) -> bool:
        """Can the LLM auto-invoke this skill? Inverse of disable_model_invocation."""
        return not self.disable_model_invocation

    @property
    def user_can_invoke(self) -> bool:
        """Can the user type /name to invoke this skill?"""
        return self.user_invocable

    def matches_path(self, file_path: str) -> bool:
        """Check whether this skill should activate for the given file path.

        Uses pathlib.PurePosixPath.match() which supports ** (recursive)
        unlike fnmatch.fnmatch() on Python < 3.13.
        """
        if not self.paths:
            return True
        p = file_path.replace("\\", "/")
        pp = Path(p)
        return any(pp.match(pat) for pat in self.paths)


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
        if include_builtin:
            self._skills_dirs.append(BUILTIN_SKILLS_DIR)
        if skills_dir:
            self._skills_dirs.append(skills_dir)
        if extra_dirs:
            self._skills_dirs.extend(extra_dirs)

        self._metadata: dict[str, SkillMetadata] = {}
        self._nested_metadata: dict[str, SkillMetadata] = {}  # SK-19: dir-prefixed skills
        self._dir_mtimes: dict[str, float] = {}  # SK-18: mtime tracking
        self._discover()

    def _discover(self) -> None:
        """扫描所有 skills 目录 + 嵌套目录（SK-19）。

        SK-18: tracks directory mtimes for efficient refresh().
        SK-19: scans nested .claude/skills/ up to 3 levels deep for monorepo support.
        """
        self._metadata.clear()
        self._nested_metadata.clear()

        for skills_dir in self._skills_dirs:
            skills_path = Path(skills_dir)
            if not skills_path.is_dir():
                logger.debug("Skills directory does not exist: %s", skills_dir)
                continue

            # SK-18: record mtime for this directory
            try:
                self._dir_mtimes[skills_dir] = skills_path.stat().st_mtime
            except OSError:
                pass

            # Main skills directory
            self._scan_skills_dir(skills_path, prefix="")

            # SK-19: nested skills in subdirectories (up to 3 levels)
            try:
                for sub in skills_path.parent.rglob(".claude/skills"):
                    if sub == skills_path:
                        continue
                    if sub.is_relative_to(skills_path) or skills_path in sub.parents:
                        continue
                    depth = len(sub.relative_to(skills_path.parent).parts)
                    if depth <= 4:  # e.g. apps/web/.claude/skills = 3 parts
                        rel_dir = str(sub.parent.relative_to(skills_path.parent)).replace("\\", "/")
                        prefix = rel_dir + ":"
                        self._scan_skills_dir(sub, prefix=prefix)
            except (OSError, ValueError):
                pass

        total = len(self._metadata) + len(self._nested_metadata)
        logger.info("Discovered %d skills total (%d root, %d nested)", total, len(self._metadata), len(self._nested_metadata))

    def _scan_skills_dir(self, skills_path: Path, *, prefix: str = "") -> None:
        """Scan one skills directory for SKILL.md files."""
        for entry in sorted(skills_path.iterdir()):
            if not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                continue

            try:
                metadata = self._parse_frontmatter(skill_file, entry.name)
                if metadata:
                    if prefix:
                        self._nested_metadata[f"{prefix}{metadata.name}"] = metadata
                        logger.debug("Nested skill: %s%s (from %s)", prefix, metadata.name, skills_path)
                    else:
                        self._metadata[metadata.name] = metadata
                        logger.debug("Discovered skill: %s (from %s)", metadata.name, skills_path)
            except Exception as e:
                logger.warning("Failed to parse skill %s: %s", entry.name, e)

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

        # ── Parse paths: string, comma/space-separated, or YAML list ──
        raw_paths = fm_dict.get("paths", ())
        if isinstance(raw_paths, str):
            paths = tuple(
                p.strip() for p in raw_paths.replace(",", " ").split()
                if p.strip()
            )
        elif isinstance(raw_paths, list):
            paths = tuple(str(p).strip() for p in raw_paths if str(p).strip())
        else:
            paths = ()

        # ── Parse arguments: string or YAML list ──
        raw_args = fm_dict.get("arguments", ())
        if isinstance(raw_args, str):
            named_args = tuple(a.strip() for a in raw_args.replace(",", " ").split() if a.strip())
        elif isinstance(raw_args, list):
            named_args = tuple(str(a).strip() for a in raw_args if str(a).strip())
        else:
            named_args = ()

        # ── Parse allowed/disallowed tools ──
        def _parse_tool_set(raw) -> frozenset[str]:
            if isinstance(raw, str):
                return frozenset(t.strip() for t in raw.replace(",", " ").split() if t.strip())
            if isinstance(raw, list):
                return frozenset(str(t).strip() for t in raw if str(t).strip())
            return frozenset()

        return SkillMetadata(
            name=dir_name,
            display_name=str(fm_dict.get("name", dir_name)),
            description=str(fm_dict.get("description", "")),
            when_to_use=str(fm_dict.get("when_to_use", "")),
            dir_path=str(skill_file.parent),
            disable_model_invocation=bool(fm_dict.get("disable-model-invocation", False)),
            user_invocable=bool(fm_dict.get("user-invocable", True)),
            model=str(fm_dict.get("model", "")),
            effort=str(fm_dict.get("effort", "")),
            context=str(fm_dict.get("context", "")),
            agent=str(fm_dict.get("agent", "")),
            paths=paths,
            arguments=named_args,
            allowed_tools=_parse_tool_set(fm_dict.get("allowed-tools", [])),
            disallowed_tools=_parse_tool_set(fm_dict.get("disallowed-tools", [])),
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[str, str]:
        """Split frontmatter and body using the shared utility."""
        from utils.frontmatter import split_frontmatter
        return split_frontmatter(content)

    def list_skills(self) -> list[SkillMetadata]:
        """返回所有已发现的 skill metadata（含嵌套 skills）。"""
        return list(self._metadata.values()) + list(self._nested_metadata.values())

    def has_skill(self, name: str) -> bool:
        """检查是否存在指定名称的 skill（含嵌套 skills）。"""
        return name in self._metadata or name in self._nested_metadata

    def _get_skill_meta(self, name: str) -> SkillMetadata | None:
        """Get metadata for a skill, checking root then nested."""
        return self._metadata.get(name) or self._nested_metadata.get(name)

    def get_skill_detail(self, name: str) -> str | None:
        """返回 skill 的完整 body 内容（未做 $ARGUMENTS 替换）。"""
        meta = self._get_skill_meta(name)
        if meta is None:
            return None
        skill_file = Path(meta.dir_path) / "SKILL.md"
        if not skill_file.is_file():
            return None
        content = skill_file.read_text(encoding="utf-8")
        _, body = self._split_frontmatter(content)
        return body or None

    # ── Skills that use !`cmd` injection ──
    _INLINE_CMD_RE = None  # compiled lazily

    def load_and_render(
        self, name: str, arguments: str = "",
        *,
        session_id: str = "",
        project_dir: str = "",
        effort_level: str = "",
    ) -> str | None:
        """
        Load and render a skill with full CC-aligned substitutions and injections.

        Processing order:
          1. Read SKILL.md body
          2. SK-09: Expand `` !`cmd` `` and ```! blocks (run commands, inline output)
          3. SK-10~16: Apply string substitutions ($ARGUMENTS, $N, $name, ${...})
          4. SK-17: Append supporting files index if available
          5. Return rendered content

        Reference: https://code.claude.com/docs/en/skills#available-string-substitutions
        """
        metadata = self._get_skill_meta(name)
        if metadata is None:
            return None

        skill_file = Path(metadata.dir_path) / "SKILL.md"

        if not skill_file.is_file():
            logger.warning("Skill file missing: %s", skill_file)
            return None

        content = skill_file.read_text(encoding="utf-8")
        _, body = self._split_frontmatter(content)

        if not body:
            return None

        # Step 1: Expand inline commands (!`cmd` and ```! blocks)
        body = self._expand_inline_commands(body, cwd=str(skill_file.parent))

        # Step 2: Apply string substitutions
        body = self._apply_substitutions(
            body, metadata, arguments,
            session_id=session_id,
            project_dir=project_dir,
            skill_dir=metadata.dir_path,
            effort_level=effort_level,
        )

        # Step 3 (SK-17): Append supporting files index
        supporting = self._list_supporting_files(metadata.dir_path)
        if supporting:
            body += "\n\n## Supporting Files\n" + supporting

        return body

    # ── SK-17: Supporting files ──────────────────────────────────────

    @staticmethod
    def _list_supporting_files(skill_dir: str) -> str:
        """List supporting files in the skill directory (reference.md, scripts/, etc.).

        CC reference: https://code.claude.com/docs/en/skills#add-supporting-files
        """
        lines: list[str] = []
        try:
            for entry in sorted(Path(skill_dir).iterdir()):
                if entry.name == "SKILL.md":
                    continue
                if entry.name.startswith("."):
                    continue
                if entry.is_file():
                    lines.append(f"- `{entry.name}` — {entry.stat().st_size} bytes")
                elif entry.is_dir():
                    sub_files = list(entry.iterdir())
                    lines.append(f"- `{entry.name}/` — {len(sub_files)} file(s)")
        except OSError:
            return ""
        return "\n".join(lines) if lines else ""

    # ── SK-09: Dynamic context injection ────────────────────────────

    @staticmethod
    def _expand_inline_commands(content: str, *, cwd: str = ".") -> str:
        """Expand !`cmd` and ```! blocks, replacing them with command output.

        CC spec: !` at line start or after whitespace triggers execution.
        The command runs once during preprocessing; output is NOT re-scanned.
        """
        # Fast path: skip if no injection markers present
        if "!`" not in content and "```!" not in content:
            return content

        # Lazy import — subprocess is expensive on Windows
        from subprocess import run as _subprocess_run
        _FENCED_BLOCK_RE = None  # compiled lazily at module level if needed
        result_parts: list[str] = []
        in_fence = False
        fence_lines: list[str] = []
        fence_start = 0

        for i, line in enumerate(content.splitlines(True)):
            stripped = line.lstrip()
            if not in_fence and stripped.startswith("```!"):
                in_fence = True
                fence_lines = []
                fence_start = i
                result_parts.append(line)  # keep the opening ```! line
                continue
            if in_fence:
                if stripped.startswith("```") and not stripped.startswith("```!"):
                    # End of fenced block — execute accumulated command
                    in_fence = False
                    cmd_text = "\n".join(fence_lines).strip()
                    if cmd_text:
                        try:
                            output = _subprocess_run(
                                cmd_text, shell=True, capture_output=True, text=True,
                                timeout=30, cwd=cwd,
                            ).stdout.strip()
                            result_parts.append(output + "\n")
                        except Exception as exc:
                            logger.warning("Skill inline command failed: %s", exc)
                            result_parts.append(f"[command failed: {exc}]\n")
                    result_parts.append(line)  # keep the closing ``` line
                    continue
                fence_lines.append(line.rstrip("\n"))
                continue
            # Regular line — check for inline !`cmd`
            m = re.match(r"(\s*)!`([^`]+)`", line)
            if m:
                indent, cmd = m.group(1), m.group(2).strip()
                try:
                    output = _subprocess_run(
                        cmd, shell=True, capture_output=True, text=True,
                        timeout=30, cwd=cwd,
                    ).stdout.strip()
                    result_parts.append(f"{indent}{output}\n")
                except Exception as exc:
                    logger.warning("Skill inline command failed: %s", exc)
                    result_parts.append(f"{indent}[command failed: {exc}]\n")
                continue
            result_parts.append(line)

        return "".join(result_parts)

    # ── SK-10~16: String substitutions ──────────────────────────────

    @staticmethod
    def _apply_substitutions(
        content: str,
        metadata,
        arguments: str,
        *,
        session_id: str = "",
        project_dir: str = "",
        skill_dir: str = "",
        effort_level: str = "",
    ) -> str:
        """Apply all CC-aligned string substitutions to skill content.

        Order matters: indexed args before simple $ARGUMENTS to avoid
        partial matches (e.g. $ARGUMENTS[0] vs $ARGUMENTS).
        """
        # Parse arguments with shell-style quoting
        args_list = SkillRegistry._parse_args(arguments)

        # Build substitution map
        subs: dict[str, str] = {}

        # $ARGUMENTS[N] — indexed (must come before plain $ARGUMENTS)
        for i in range(len(args_list)):
            subs[f"$ARGUMENTS[{i}]"] = args_list[i]

        # $N — shorthand
        for i in range(len(args_list)):
            subs[f"${i}"] = args_list[i]

        # Named arguments from frontmatter (SK-12)
        if hasattr(metadata, 'arguments') and metadata.arguments:
            for idx, arg_name in enumerate(metadata.arguments):
                if idx < len(args_list):
                    subs[f"${arg_name}"] = args_list[idx]

        # ${CLAUDE_*} variables
        if session_id:
            subs["${CLAUDE_SESSION_ID}"] = session_id
        if project_dir:
            subs["${CLAUDE_PROJECT_DIR}"] = project_dir
        if skill_dir:
            subs["${CLAUDE_SKILL_DIR}"] = skill_dir
        if effort_level:
            subs["${CLAUDE_EFFORT}"] = effort_level

        # $ARGUMENTS — plain (last, to avoid partial matches on indexed forms)
        subs["$ARGUMENTS"] = arguments

        # Apply substitutions in order of longest key first (prevents partial matches)
        result = content
        for key in sorted(subs.keys(), key=len, reverse=True):
            result = result.replace(key, subs[key])

        # Handle escaped \$ — remove the backslash
        result = result.replace("\\$", "$")

        return result

    @staticmethod
    def _parse_args(arguments: str) -> list[str]:
        """Parse arguments with shell-style quoting.

        "/my-skill \"hello world\" second" → ["hello world", "second"]
        """
        import shlex
        try:
            return shlex.split(arguments)
        except ValueError:
            # Fallback: split on whitespace
            return arguments.split()

    def format_for_prompt(self, *, llm_invocable_only: bool = True) -> str:
        """
        Format skill list for system prompt injection.

        Aligned with Claude Code frontmatter:
        - Skills with disable_model_invocation=true are hidden from LLM listing.
          The user can still invoke them via /name, but the LLM won't auto-load.
        - user-invocable=false skills are still listed (LLM can auto-load them).
        - when_to_use is appended to description for semantic matching.

        Args:
            llm_invocable_only: if True (default), exclude skills that set
                               disable-model-invocation: true.
        """
        if not self._metadata:
            return ""

        all_meta = list(self._metadata.values()) + list(self._nested_metadata.values())
        user_skills = [m for m in all_meta if m.user_can_invoke]
        model_skills = [m for m in all_meta if m.model_invocable]

        lines = [
            "## Available Skills",
        ]

        # Skills the user can invoke via /name
        if user_skills:
            names = ", ".join(f"/{m.name}" for m in user_skills)
            lines.append(f"User-invocable: {names}")

        # Skills the LLM can auto-load (respects disable_model_invocation)
        visible = model_skills if llm_invocable_only else list(self._metadata.values())

        if visible:
            lines.append("Use the `Skill` tool to load a skill, or type /skill-name directly:")
            for meta in visible:
                desc = meta.description or "(no description)"
                if meta.when_to_use:
                    desc += f" (Use when: {meta.when_to_use})"
                lines.append(f"- **/{meta.name}**: {desc}")

        return "\n".join(lines)

    def refresh(self) -> None:
        """SK-18: mtime-based live change detection.

        Only rescans directories whose mtime has changed since last scan.
        If no changes detected, returns immediately (no-op).
        """
        changed = False
        for skills_dir in self._skills_dirs:
            try:
                current_mtime = Path(skills_dir).stat().st_mtime
            except OSError:
                continue
            if self._dir_mtimes.get(skills_dir) != current_mtime:
                changed = True
                break

        if not changed:
            return  # Nothing changed, skip rescan

        self._metadata.clear()
        self._nested_metadata.clear()
        self._dir_mtimes.clear()
        self._discover()
