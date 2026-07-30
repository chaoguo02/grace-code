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
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 内置 skills 目录（随代码分发）
BUILTIN_SKILLS_DIR = str(Path(__file__).parent / "builtin")


@dataclass(frozen=True)
class SkillSource:
    """One ordered skill fact source."""

    name: str
    directories: tuple[str, ...]
    priority: int
    trusted: bool = True
    legacy_commands: bool = False


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
    mcp_servers: frozenset[str] = frozenset()
    hooks: tuple[dict, ...] = ()
    source: str = "project"
    trusted: bool = True
    file_path: str = ""

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
    - 项目级 .grace/skills/（用户自定义）

    用法：
        registry = SkillRegistry("/path/to/.grace/skills")
        skills = registry.list_skills()
        rendered = registry.load_and_render("code-review", "auth module")
    """

    def __init__(
        self,
        skills_dir: str,
        extra_dirs: list[str] | None = None,
        include_builtin: bool = True,
        *,
        sources: list["SkillSource"] | None = None,
        live_reload: bool = False,
    ) -> None:
        if sources is None:
            sources = []
            if skills_dir:
                sources.append(SkillSource("project", (skills_dir,), 2))
            if extra_dirs:
                sources.append(
                    SkillSource("additional", tuple(extra_dirs), 3),
                )
            if include_builtin:
                sources.append(
                    SkillSource("bundled", (BUILTIN_SKILLS_DIR,), 5),
                )
        self._sources = self._dedupe_sources(sources)
        self._skills_dirs = [
            directory
            for source in self._sources
            for directory in source.directories
        ]
        self._metadata: dict[str, SkillMetadata] = {}
        self._nested_metadata: dict[str, SkillMetadata] = {}  # SK-19: dir-prefixed skills
        self._lock = threading.RLock()
        self._fingerprint = ""
        self._change_detector: SkillChangeDetector | None = None
        self._discover()
        if live_reload:
            self._change_detector = SkillChangeDetector(self)
            self._change_detector.start()

    @classmethod
    def for_project(
        cls,
        project_dir: str,
        *,
        additional_dirs: list[str] | None = None,
        mcp_dirs: list[str] | None = None,
        live_reload: bool = True,
    ) -> "SkillRegistry":
        """Create the canonical seven-source skill registry."""
        root = Path(project_dir).resolve()
        user_home = Path.home()
        managed = os.environ.get("GRACE_MANAGED_SKILLS_DIR", "")
        sources = [
            SkillSource(
                "managed",
                tuple(filter(None, (managed,))),
                0,
            ),
            SkillSource(
                "user",
                (
                    str(user_home / ".grace" / "skills"),
                    str(user_home / ".claude" / "skills"),
                ),
                1,
            ),
            SkillSource(
                "project",
                (
                    str(root / ".grace" / "skills"),
                    str(root / ".claude" / "skills"),
                    str(root / ".forge-agent" / "skills"),
                ),
                2,
            ),
            SkillSource(
                "additional",
                tuple(additional_dirs or ()),
                3,
            ),
            SkillSource(
                "legacy",
                (str(root / ".claude" / "commands"),),
                4,
                legacy_commands=True,
            ),
            SkillSource("bundled", (BUILTIN_SKILLS_DIR,), 5),
            SkillSource(
                "mcp",
                tuple(mcp_dirs or ()),
                6,
                trusted=False,
            ),
        ]
        return cls(
            "",
            include_builtin=False,
            sources=sources,
            live_reload=live_reload,
        )

    @staticmethod
    def _dedupe_sources(
        sources: list["SkillSource"],
    ) -> list["SkillSource"]:
        seen: set[str] = set()
        result: list[SkillSource] = []
        for source in sorted(sources, key=lambda item: item.priority):
            directories: list[str] = []
            for raw in source.directories:
                canonical = os.path.realpath(os.path.expanduser(raw))
                if canonical in seen:
                    continue
                seen.add(canonical)
                directories.append(canonical)
            result.append(
                SkillSource(
                    source.name,
                    tuple(directories),
                    source.priority,
                    trusted=source.trusted,
                    legacy_commands=source.legacy_commands,
                ),
            )
        return result

    def _discover(self) -> None:
        """Load all configured sources concurrently, then merge by priority."""
        with ThreadPoolExecutor(
            max_workers=max(1, min(7, len(self._sources))),
            thread_name_prefix="grace-skill-discovery",
        ) as executor:
            futures = [
                (source, executor.submit(self._discover_source, source))
                for source in self._sources
            ]
            discovered = []
            for source, future in futures:
                try:
                    result = future.result()
                except Exception:
                    logger.warning(
                        "Skill source discovery failed: %s",
                        source.name,
                        exc_info=True,
                    )
                    result = ({}, {})
                discovered.append((source, result))

        metadata: dict[str, SkillMetadata] = {}
        nested: dict[str, SkillMetadata] = {}
        for source, (root_items, nested_items) in sorted(
            discovered,
            key=lambda item: item[0].priority,
        ):
            for name, item in root_items.items():
                metadata.setdefault(name, item)
            for name, item in nested_items.items():
                nested.setdefault(name, item)
        with self._lock:
            self._metadata = metadata
            self._nested_metadata = nested
            self._fingerprint = self._source_fingerprint()

        total = len(metadata) + len(nested)
        logger.info("Discovered %d skills total (%d root, %d nested)", total, len(self._metadata), len(self._nested_metadata))

    def _discover_source(
        self,
        source: "SkillSource",
    ) -> tuple[dict[str, SkillMetadata], dict[str, SkillMetadata]]:
        root_items: dict[str, SkillMetadata] = {}
        nested_items: dict[str, SkillMetadata] = {}
        for directory in source.directories:
            skills_path = Path(directory)
            if not skills_path.is_dir():
                continue
            if source.legacy_commands:
                for command in sorted(skills_path.glob("*.md")):
                    metadata = self._parse_frontmatter(
                        command,
                        command.stem,
                        source=source,
                    )
                    if metadata:
                        root_items.setdefault(metadata.name, metadata)
                continue
            self._scan_skills_dir(
                skills_path,
                source=source,
                target=root_items,
            )
            if source.name != "project":
                continue
            try:
                project_root = skills_path.parent.parent
                for sub in project_root.rglob(".claude/skills"):
                    if sub == skills_path:
                        continue
                    depth = len(sub.relative_to(project_root).parts)
                    if depth > 5:
                        continue
                    prefix = (
                        str(sub.parent.relative_to(project_root))
                        .replace("\\", "/")
                        + ":"
                    )
                    self._scan_skills_dir(
                        sub,
                        source=source,
                        target=nested_items,
                        prefix=prefix,
                    )
            except (OSError, ValueError):
                logger.debug(
                    "Nested skill discovery failed under %s",
                    skills_path,
                    exc_info=True,
                )
        return root_items, nested_items

    def _scan_skills_dir(
        self,
        skills_path: Path,
        *,
        source: "SkillSource",
        target: dict[str, SkillMetadata],
        prefix: str = "",
    ) -> None:
        """Scan one skills directory into a source-local result."""
        for entry in sorted(skills_path.iterdir()):
            if not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                continue

            try:
                metadata = self._parse_frontmatter(
                    skill_file,
                    entry.name,
                    source=source,
                )
                if metadata:
                    target.setdefault(f"{prefix}{metadata.name}", metadata)
            except Exception as e:
                logger.warning("Failed to parse skill %s: %s", entry.name, e)

    def _parse_frontmatter(
        self,
        skill_file: Path,
        dir_name: str,
        *,
        source: "SkillSource",
    ) -> SkillMetadata | None:
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
                source=source.name,
                trusted=source.trusted,
                file_path=str(skill_file),
            )

        try:
            fm_dict: dict = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError as exc:
            logger.warning(
                "Invalid skill frontmatter in %s: %s",
                skill_file,
                exc,
            )
            return None

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

        # Parse hooks from frontmatter
        raw_hooks = fm_dict.get("hooks", {})
        if isinstance(raw_hooks, dict):
            hooks = (raw_hooks,)
        elif isinstance(raw_hooks, list):
            hooks = tuple(h for h in raw_hooks if isinstance(h, dict))
        else:
            hooks = ()

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
            mcp_servers=self._load_mcp_dependencies(skill_file.parent),
            hooks=hooks,
            source=source.name,
            trusted=source.trusted,
            file_path=str(skill_file),
        )

    @staticmethod
    def _load_mcp_dependencies(skill_dir: Path) -> frozenset[str]:
        """Read declarative MCP dependencies from agents/openai.yaml."""
        metadata_file = skill_dir / "agents" / "openai.yaml"
        if not metadata_file.is_file():
            return frozenset()
        try:
            raw = yaml.safe_load(
                metadata_file.read_text(encoding="utf-8"),
            ) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "Invalid Skill UI metadata in %s: %s",
                metadata_file,
                exc,
            )
            return frozenset()
        dependencies = raw.get("dependencies", {})
        tools = dependencies.get("tools", []) if isinstance(
            dependencies,
            dict,
        ) else []
        if not isinstance(tools, list):
            return frozenset()
        return frozenset(
            str(item.get("value", "")).strip()
            for item in tools
            if (
                isinstance(item, dict)
                and str(item.get("type", "")).strip().lower() == "mcp"
                and str(item.get("value", "")).strip()
            )
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[str, str]:
        """Split frontmatter and body using the shared utility."""
        from utils.frontmatter import split_frontmatter
        return split_frontmatter(content)

    def list_skills(self) -> list[SkillMetadata]:
        """返回所有已发现的 skill metadata（含嵌套 skills）。"""
        with self._lock:
            return (
                list(self._metadata.values())
                + list(self._nested_metadata.values())
            )

    def list_skill_entries(self) -> list[tuple[str, SkillMetadata]]:
        """Return canonical invocation names paired with metadata."""
        with self._lock:
            return (
                list(self._metadata.items())
                + list(self._nested_metadata.items())
            )

    def has_skill(self, name: str) -> bool:
        """检查是否存在指定名称的 skill（含嵌套 skills）。"""
        with self._lock:
            return name in self._metadata or name in self._nested_metadata

    def get_skill_meta(self, name: str) -> SkillMetadata | None:
        """Get metadata for a skill, checking root then nested."""
        with self._lock:
            return self._metadata.get(name) or self._nested_metadata.get(name)

    def get_skill_detail(self, name: str) -> str | None:
        """返回 skill 的完整 body 内容（未做 $ARGUMENTS 替换）。"""
        meta = self.get_skill_meta(name)
        if meta is None:
            return None
        skill_file = Path(meta.file_path or Path(meta.dir_path) / "SKILL.md")
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
        runtime: Any = None,
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
        metadata = self.get_skill_meta(name)
        if metadata is None:
            return None

        skill_file = Path(
            metadata.file_path or Path(metadata.dir_path) / "SKILL.md",
        )

        if not skill_file.is_file():
            logger.warning("Skill file missing: %s", skill_file)
            return None

        content = skill_file.read_text(encoding="utf-8")
        _, body = self._split_frontmatter(content)

        if not body:
            return None

        # Step 1: Expand inline commands (!`cmd` and ```! blocks)
        body = self._expand_inline_commands(
            body,
            cwd=str(skill_file.parent),
            runtime=runtime,
            allow_execution=metadata.trusted,
        )

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
    def _run_skill_command(cmd: str, *, cwd: str, runtime: Any = None) -> str:
        """Execute a skill inline command via Runtime (CC-aligned safety).

        Without a Runtime, execution is refused — this prevents Skill content
        from bypassing the PermissionPipeline, Hooks, and workspace boundaries.
        """
        if runtime is not None:
            try:
                result = runtime.exec(cmd, cwd=cwd, timeout=30)
                return (result.stdout or "").strip()
            except Exception as exc:
                logger.warning("Skill command failed via Runtime: %s", exc)
                return "[command failed: %s]" % exc
        # No Runtime available — refuse to execute (CC-aligned safe fallback)
        logger.warning("Skill inline command blocked (no Runtime): %s", cmd[:80])
        return "[blocked: skill inline command requires Runtime]"

    @staticmethod
    def _expand_inline_commands(
        content: str,
        *,
        cwd: str = ".",
        runtime: Any = None,
        allow_execution: bool = True,
    ) -> str:
        """Expand !`cmd` and ```! blocks, replacing them with command output.

        CC spec: !` at line start or after whitespace triggers execution.
        The command runs once during preprocessing; output is NOT re-scanned.

        CC-aligned safety: when a Runtime is provided, commands go through
        Runtime.execute() → PermissionPipeline → hooks.  Without Runtime,
        commands are refused (safe fallback) rather than executed raw.
        """
        # Fast path: skip if no injection markers present
        if "!`" not in content and "```!" not in content:
            return content
        if not allow_execution:
            logger.warning("Blocked inline commands from untrusted MCP skill")
            blocked = re.sub(
                r"(?ms)^\s*```!\s*\n.*?^\s*```\s*$",
                "[blocked: untrusted skill inline command]",
                content,
            )
            return re.sub(
                r"(?m)^\s*!`[^`]+`\s*$",
                "[blocked: untrusted skill inline command]",
                blocked,
            )

        result_parts: list[str] = []
        in_fence = False
        fence_lines: list[str] = []

        for i, line in enumerate(content.splitlines(True)):
            stripped = line.lstrip()
            if not in_fence and stripped.startswith("```!"):
                in_fence = True
                fence_lines = []
                result_parts.append(line)  # keep the opening ```! line
                continue
            if in_fence:
                if stripped.startswith("```") and not stripped.startswith("```!"):
                    # End of fenced block — execute accumulated command
                    in_fence = False
                    cmd_text = "\n".join(fence_lines).strip()
                    if cmd_text:
                        output = SkillRegistry._run_skill_command(cmd_text, cwd=cwd, runtime=runtime)
                        result_parts.append(output + "\n")
                    result_parts.append(line)  # keep the closing ``` line
                    continue
                fence_lines.append(line.rstrip("\n"))
                continue
            # Regular line — check for inline !`cmd`
            m = re.match(r"(\s*)!`([^`]+)`", line)
            if m:
                indent, cmd = m.group(1), m.group(2).strip()
                output = SkillRegistry._run_skill_command(cmd, cwd=cwd, runtime=runtime)
                result_parts.append(f"{indent}{output}\n")
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

        # Handle escaped \$: protect before substitutions, restore after
        # This prevents \$ARGUMENTS from being replaced by actual argument content
        result = content
        ph_map = {}
        for i, key in enumerate(sorted(subs.keys(), key=len, reverse=True)):
            if key.startswith("$"):
                esc = "\\" + key[1:]
                ph = "\x00SKILL_ESC_" + str(i) + "\x00"
                if esc in result:
                    ph_map[ph] = "$" + key[1:]
                    result = result.replace(esc, ph)

        for key in sorted(subs.keys(), key=len, reverse=True):
            result = result.replace(key, subs[key])

        for ph, orig in ph_map.items():
            result = result.replace(ph, "\\" + orig)

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
        entries = self.list_skill_entries()
        if not entries:
            return ""

        user_skills = [
            (name, meta)
            for name, meta in entries
            if meta.user_can_invoke
        ]
        model_skills = [
            (name, meta)
            for name, meta in entries
            if meta.model_invocable
        ]

        lines = [
            "## Available Skills",
        ]

        # Skills the user can invoke via /name
        if user_skills:
            names = ", ".join(f"/{name}" for name, _ in user_skills)
            lines.append(f"User-invocable: {names}")

        # Skills the LLM can auto-load (respects disable_model_invocation)
        visible = model_skills if llm_invocable_only else entries

        if visible:
            lines.append("Use the `Skill` tool to load a skill (PREFERRED — saves context by injecting instructions without duplicating):")
            for name, meta in visible:
                desc = meta.description or "(no description)"
                if meta.when_to_use:
                    desc += f" (Use when: {meta.when_to_use})"
                if meta.paths:
                    desc += f" (Path scope: {', '.join(meta.paths)})"
                lines.append(f"- **{name}**: {desc}")

        return "\n".join(lines)

    def refresh(self) -> None:
        """Clear discovery memoization and atomically reload changed sources."""
        if self._source_fingerprint() != self._fingerprint:
            self._discover()

    def discover_for_paths(
        self,
        paths: list[str] | tuple[str, ...],
    ) -> list[SkillMetadata]:
        """Return L1 metadata activated by files the model touched."""
        return [
            meta
            for meta in self.list_skills()
            if any(meta.matches_path(path) for path in paths)
        ]

    def list_resources(self, name: str) -> tuple[str, ...]:
        """L3 disclosure: enumerate resources without loading their bodies."""
        meta = self.get_skill_meta(name)
        if meta is None:
            return ()
        base = Path(meta.dir_path)
        resources: list[str] = []
        for directory_name in ("scripts", "templates", "references", "assets"):
            directory = base / directory_name
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    resources.append(
                        str(path.relative_to(base)).replace("\\", "/"),
                    )
        return tuple(resources)

    def close(self, timeout: float = 2.0) -> None:
        detector = self._change_detector
        self._change_detector = None
        if detector is not None:
            detector.close(timeout=timeout)

    def _source_fingerprint(self) -> str:
        import hashlib

        facts: list[str] = []
        for source in self._sources:
            for directory in source.directories:
                root = Path(directory)
                if not root.exists():
                    facts.append(f"{source.name}:{directory}:missing")
                    continue
                patterns = (
                    ("*.md",)
                    if source.legacy_commands
                    else ("SKILL.md", "agents/openai.yaml")
                )
                for pattern in patterns:
                    for path in sorted(root.rglob(pattern)):
                        try:
                            stat = path.stat()
                        except OSError:
                            continue
                        facts.append(
                            f"{source.name}:{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}",
                        )
        return hashlib.sha256("\n".join(facts).encode("utf-8")).hexdigest()


class SkillChangeDetector:
    """Polling live-reload watcher with 300 ms debounce."""

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        interval_seconds: float = 0.3,
    ) -> None:
        self._registry = registry
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="grace-skill-watch",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._thread.join(timeout=max(0.0, timeout))

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._registry.refresh()
            except Exception:
                logger.warning("Skill live reload failed", exc_info=True)
