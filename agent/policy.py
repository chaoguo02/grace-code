"""Task policy model and parser.

A TaskPolicy is the runtime contract for a task. It is derived from
explicit Task fields (explicit_read_paths, explicit_write_paths, etc.)
and consumed by tool-policy and completion layers. Prompts can show it
to the model, but enforcement must happen outside the model.

Constraints are NEVER inferred from natural language — the caller must
provide them via structured fields or CLI flags (--no-shell, --files, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent.task import Task, TaskIntent
from tools.base import ToolEffect


# ── Scoped tool rule: ToolName(specifier) — Claude Code permission model ──
#
# Claude Code rule format: "ToolName(specifier)" where the specifier format
# is TOOL-TYPE-SPECIFIC:
#   Bash(npm run *)     → command pattern (glob match on shell command/args)
#   Read(~/secrets/**)  → path glob (fnmatch on file path param)
#   Edit(/src/**)       → path glob
#   Skill(deploy *)     → skill name glob
#   Agent(Explore)      → subagent type name match
#   WebFetch(domain:x)  → domain match
#
# Bare tool name = whole-tool rule.  Evaluation order: Deny → Allow.
# Specificity does NOT change the order (unlike firewall rules).

from fnmatch import fnmatch


@dataclass(frozen=True)
class ScopedToolRule:
    """Parameter-scoped rule like 'Bash(rm *)' or 'Read(.env)'.

    Bare tool_name (no specifier fields) = whole-tool rule.
    """

    tool_name: str
    """Exact tool name (e.g. 'shell', 'file_read')."""

    # ── Type-specific specifiers (only ONE should be set) ──
    command_pattern: str = ""
    """Shell/PowerShell: glob pattern matched against command+args (e.g. 'rm *')."""

    path_pattern: str = ""
    """File tools (Read/Edit/Write/Glob/Grep): fnmatch pattern on path param."""

    domain_pattern: str = ""
    """WebFetch: domain suffix match (e.g. 'example.com')."""

    def matches(self, tool_name: str, params: dict) -> bool:
        if tool_name != self.tool_name:
            return False
        # Bare name — whole-tool rule
        if not self.command_pattern and not self.path_pattern and not self.domain_pattern:
            return True
        # Type-specific matching
        if self.command_pattern:
            return _match_command(tool_name, params, self.command_pattern)
        if self.path_pattern:
            return _match_path(params, self.path_pattern)
        if self.domain_pattern:
            return _match_domain(params, self.domain_pattern)
        return False


# ── Specifier matchers (one per tool type) ──

_COMMAND_TOOLS = frozenset({"Bash", "shell", "bash"})


def _match_command(tool_name: str, params: dict, pattern: str) -> bool:
    if tool_name not in _COMMAND_TOOLS:
        return False
    cmd = params.get("command", "") or params.get("cmd", "")
    args = params.get("args", [])
    if isinstance(args, list):
        full = f"{cmd} {' '.join(str(a) for a in args)}".strip()
    else:
        full = str(cmd).strip()
    return fnmatch(full.lower(), pattern.lower())


_PATH_TOOLS = frozenset({
    "Read", "file_read", "file_view", "Edit", "file_edit",
    "Write", "file_write", "Glob", "find_files", "Grep", "search_text",
    "read", "edit", "write", "glob", "grep",
})


def _match_path(params: dict, pattern: str) -> bool:
    path = params.get("path", "") or params.get("file_path", "") or params.get("target", "")
    if not path:
        return False
    # Normalize to forward slashes for cross-platform matching
    normalized = str(path).replace("\\", "/")
    return fnmatch(normalized, pattern)


_WEB_TOOLS = frozenset({"WebFetch", "web_fetch", "web_fetch_with_selector", "webfetch"})


def _match_domain(params: dict, pattern: str) -> bool:
    url = params.get("url", "") or params.get("target_url", "")
    if not url:
        return False
    # Simple domain suffix match: "example.com" matches "https://example.com/path"
    from urllib.parse import urlparse
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    return host == pattern or host.endswith("." + pattern)

READ_ONLY_EFFECTS = frozenset({
    ToolEffect.READ_WORKSPACE,
    ToolEffect.DISCOVER_WORKSPACE,
    ToolEffect.READ_VCS,
    ToolEffect.NETWORK,
    ToolEffect.READ_AGENT_STATE,
    ToolEffect.PRODUCE_DELIVERABLE,
    ToolEffect.DELEGATE_READ_ONLY,
})


def normalize_repo_path(path_text: str, repo_path: str) -> str:
    normalized = path_text.strip().strip("`'\"，,。.;；:：")
    normalized = normalized.replace("\\", "/")
    if normalized.lower() == "readme":
        normalized = "README.md"
    candidate = Path(normalized)
    if candidate.is_absolute():
        try:
            normalized = candidate.resolve().relative_to(Path(repo_path).resolve()).as_posix()
        except ValueError:
            normalized = candidate.as_posix()
    return normalized.lstrip("./")


@dataclass(frozen=True)
class PhasePolicy:
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_effects: frozenset[ToolEffect] | None = None
    denied_effects: frozenset[ToolEffect] = field(default_factory=frozenset)
    allowed_read_paths: frozenset[str] | None = None
    allowed_write_paths: frozenset[str] | None = None
    strict_file_scope: bool = False
    notes: tuple[str, ...] = ()

    # CC-aligned permission mode: "default", "acceptEdits", "auto",
    # "dontAsk", "bypassPermissions", "plan", "manual"
    permission_mode: str = ""

    # Claude Code pattern: ToolName(specifier) parameter-scoped rules.
    # Deny rules evaluated first, then ask, then allow — specificity does NOT
    # change evaluation order.  Bare tool name (no param_contains) = whole-tool.
    scoped_deny_rules: tuple[ScopedToolRule, ...] = ()
    scoped_allow_rules: tuple[ScopedToolRule, ...] = ()

    def is_tool_blocked_by_permission_mode(self, tool_name: str) -> bool:
        """Check if permission_mode blocks this tool.

        CC-aligned:
          - "plan" → blocks Write, Edit, Bash (read-only)
          - "acceptEdits" → allows Write, Edit; Bash still prompts (not blocked here)
          - "dontAsk" → NOT blocked here (handled at pipeline level)
          - "bypassPermissions" → NOT blocked here (all tools allowed)
          - "default" / "" → no additional blocking
        """
        if not self.permission_mode:
            return False
        if self.permission_mode == "plan":
            return tool_name in {"Write", "Edit", "Bash"}
        if self.permission_mode == "dontAsk":
            # dontAsk: only allow tools in allowed_tools list
            if self.allowed_tools is not None and tool_name not in self.allowed_tools:
                return True
        return False

    def check_scoped_rules(self, tool_name: str, params: dict) -> str | None:
        """Evaluate scoped rules Deny→Allow.  Returns denial reason or None."""
        # Deny first (matching Claude Code's Deny→Ask→Allow order)
        for rule in self.scoped_deny_rules:
            if rule.matches(tool_name, params):
                detail = f" matched '{rule.tool_name}({rule.param_contains})'" if rule.param_contains else f" '{rule.tool_name}' is denied"
                return f"[RUNTIME BLOCK] Tool call{detail} by scoped deny rule"
        # Allow overrides
        for rule in self.scoped_allow_rules:
            if rule.matches(tool_name, params):
                return None  # explicitly allowed
        # No scoped rule matched — delegate to general permission check
        return None

    def to_dict(self) -> dict[str, object]:
        def _values(values):
            return None if values is None else sorted(
                value.value if isinstance(value, ToolEffect) else value
                for value in values
            )

        return {
            "allowed_tools": _values(self.allowed_tools),
            "denied_tools": _values(self.denied_tools),
            "allowed_effects": _values(self.allowed_effects),
            "denied_effects": _values(self.denied_effects),
            "allowed_read_paths": _values(self.allowed_read_paths),
            "allowed_write_paths": _values(self.allowed_write_paths),
            "strict_file_scope": self.strict_file_scope,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PhasePolicy":
        def _strings(name: str) -> frozenset[str] | None:
            value = data.get(name)
            if value is None:
                return None
            if not isinstance(value, list):
                raise ValueError(f"{name} must be a list or null")
            return frozenset(str(item) for item in value)

        def _effects(name: str) -> frozenset[ToolEffect] | None:
            values = _strings(name)
            return (
                None if values is None
                else frozenset(ToolEffect(value) for value in values)
            )

        notes = data.get("notes", [])
        if not isinstance(notes, list):
            raise ValueError("notes must be a list")
        strict_file_scope = data.get("strict_file_scope", False)
        if not isinstance(strict_file_scope, bool):
            raise ValueError("strict_file_scope must be a boolean")
        return cls(
            allowed_tools=_strings("allowed_tools"),
            denied_tools=_strings("denied_tools") or frozenset(),
            allowed_effects=_effects("allowed_effects"),
            denied_effects=_effects("denied_effects") or frozenset(),
            allowed_read_paths=_strings("allowed_read_paths"),
            allowed_write_paths=_strings("allowed_write_paths"),
            strict_file_scope=strict_file_scope,
            notes=tuple(str(item) for item in notes),
        )

    def intersect(self, other: "PhasePolicy") -> "PhasePolicy":
        """Return the non-escalating intersection of two authority envelopes."""
        if not isinstance(other, PhasePolicy):
            raise TypeError("other must be a PhasePolicy")

        def _allowed(left, right):
            if left is None:
                return right
            if right is None:
                return left
            return frozenset(left & right)

        return PhasePolicy(
            allowed_tools=_allowed(self.allowed_tools, other.allowed_tools),
            denied_tools=self.denied_tools | other.denied_tools,
            allowed_effects=_allowed(self.allowed_effects, other.allowed_effects),
            denied_effects=self.denied_effects | other.denied_effects,
            allowed_read_paths=_allowed(
                self.allowed_read_paths, other.allowed_read_paths,
            ),
            allowed_write_paths=_allowed(
                self.allowed_write_paths, other.allowed_write_paths,
            ),
            strict_file_scope=self.strict_file_scope or other.strict_file_scope,
            notes=tuple(dict.fromkeys((*self.notes, *other.notes))),
        )

    def with_allowed_tools(self, allowed_tools: set[str] | frozenset[str]) -> "PhasePolicy":
        allowed = frozenset(allowed_tools)
        if self.allowed_tools is not None:
            allowed = allowed & self.allowed_tools
        return PhasePolicy(
            allowed_tools=allowed,
            denied_tools=self.denied_tools,
            allowed_effects=self.allowed_effects,
            denied_effects=self.denied_effects,
            allowed_read_paths=self.allowed_read_paths,
            allowed_write_paths=self.allowed_write_paths,
            strict_file_scope=self.strict_file_scope,
            notes=self.notes,
        )

    def with_denied_tools(self, denied_tools: set[str] | frozenset[str]) -> "PhasePolicy":
        """Return a policy with additional denied tools (SK-06 disallowed-tools).

        When a skill sets disallowed-tools, those tools are removed from the
        available pool while the skill is active. The restriction layers on
        top of any existing denied_tools.
        """
        denied = frozenset(denied_tools)
        if self.denied_tools is not None:
            denied = denied | self.denied_tools
        return PhasePolicy(
            allowed_tools=self.allowed_tools,
            denied_tools=denied,
            allowed_effects=self.allowed_effects,
            denied_effects=self.denied_effects,
            allowed_read_paths=self.allowed_read_paths,
            allowed_write_paths=self.allowed_write_paths,
            strict_file_scope=self.strict_file_scope,
            notes=self.notes,
        )

    def with_allowed_effects(
        self, allowed_effects: set[ToolEffect] | frozenset[ToolEffect],
    ) -> "PhasePolicy":
        """Narrow this phase to an additional typed authority envelope."""
        allowed = frozenset(allowed_effects)
        if self.allowed_effects is not None:
            allowed = allowed & self.allowed_effects
        return PhasePolicy(
            allowed_tools=self.allowed_tools,
            denied_tools=self.denied_tools,
            allowed_effects=allowed,
            denied_effects=self.denied_effects,
            allowed_read_paths=self.allowed_read_paths,
            allowed_write_paths=self.allowed_write_paths,
            strict_file_scope=self.strict_file_scope,
            notes=self.notes,
        )

    def to_prompt_section(self, title: str = "Task Tool Constraints") -> str:
        lines: list[str] = []
        if self.allowed_tools is not None:
            lines.append(f"- Allowed tools: {', '.join(sorted(self.allowed_tools)) or '(none)'}")
        if self.denied_tools:
            lines.append(f"- Blocked tools: {', '.join(sorted(self.denied_tools))}")
        if self.allowed_effects is not None:
            lines.append(f"- Allowed effects: {', '.join(sorted(e.value for e in self.allowed_effects)) or '(none)'}")
        if self.denied_effects:
            lines.append(f"- Blocked effects: {', '.join(sorted(e.value for e in self.denied_effects))}")
        if self.strict_file_scope:
            lines.append("- Strict file scope is active; do not access files outside the allowed paths.")
        if self.allowed_read_paths is not None:
            lines.append(f"- Allowed read paths: {', '.join(sorted(self.allowed_read_paths)) or '(none)'}")
        if self.allowed_write_paths is not None:
            lines.append(f"- Allowed write paths: {', '.join(sorted(self.allowed_write_paths)) or '(none)'}")
        for note in self.notes:
            lines.append(f"- {note}")
        if not lines:
            return ""
        return f"## {title}\n" + "\n".join(lines)


@dataclass(frozen=True)
class CompletionPolicy:
    required_reads: frozenset[str] = field(default_factory=frozenset)
    required_writes: frozenset[str] = field(default_factory=frozenset)
    forbidden_tools: frozenset[str] = field(default_factory=frozenset)
    require_any_write: bool = False
    require_any_read: bool = False
    strict_file_scope: bool = False


@dataclass(frozen=True)
class TaskPolicy:
    planning: PhasePolicy
    execution: PhasePolicy
    completion: CompletionPolicy
    notes: tuple[str, ...] = ()

    @property
    def allowed_read_paths(self) -> frozenset[str] | None:
        return self.execution.allowed_read_paths

    @property
    def allowed_write_paths(self) -> frozenset[str] | None:
        return self.execution.allowed_write_paths

    @property
    def blocked_tools(self) -> frozenset[str]:
        return self.execution.denied_tools

    @property
    def strict_file_scope(self) -> bool:
        return self.execution.strict_file_scope

    @property
    def has_path_scope(self) -> bool:
        return bool(self.allowed_read_paths or self.allowed_write_paths)

    def to_prompt_section(self, phase: str | None = None) -> str:
        phase_policy = self.planning if phase == "planning" else self.execution
        lines: list[str] = []
        if phase:
            lines.append(f"- Current phase: {phase}.")
        if phase_policy.allowed_tools is not None:
            lines.append(f"- Allowed tools: {', '.join(sorted(phase_policy.allowed_tools)) or '(none)'}")
        if phase_policy.denied_tools:
            lines.append(f"- Blocked tools: {', '.join(sorted(phase_policy.denied_tools))}")
        if phase_policy.allowed_effects is not None:
            lines.append(f"- Allowed effects: {', '.join(sorted(e.value for e in phase_policy.allowed_effects)) or '(none)'}")
        if phase_policy.denied_effects:
            lines.append(f"- Blocked effects: {', '.join(sorted(e.value for e in phase_policy.denied_effects))}")
        if phase_policy.strict_file_scope:
            lines.append("- Strict file scope is active; do not access files outside the allowed paths.")
        if phase_policy.allowed_read_paths is not None:
            lines.append(f"- Allowed read paths: {', '.join(sorted(phase_policy.allowed_read_paths)) or '(none)'}")
        if phase_policy.allowed_write_paths is not None:
            lines.append(f"- Allowed write paths: {', '.join(sorted(phase_policy.allowed_write_paths)) or '(none)'}")
        if self.completion.required_reads:
            lines.append(f"- Completion requires reading: {', '.join(sorted(self.completion.required_reads))}")
        if self.completion.required_writes:
            lines.append(f"- Completion requires writing: {', '.join(sorted(self.completion.required_writes))}")
        if self.completion.require_any_write:
            lines.append("- Completion requires at least one file write tool call.")
        if self.completion.require_any_read:
            lines.append("- Completion requires at least one successful file read tool call.")
        for note in self.notes:
            lines.append(f"- {note}")
        if not lines:
            return ""
        return "## Task Policy\n" + "\n".join(lines)


def build_task_policy(task: Task) -> TaskPolicy:
    """Build a TaskPolicy from explicit Task fields only.

    No NLP inference — all constraints must be provided via structured fields
    (explicit_read_paths, explicit_write_paths, blocked_effects) or CLI flags.
    """
    description = task.description
    intent = task.intent

    explicit_read_paths = task.explicit_read_paths
    explicit_write_paths = task.explicit_write_paths
    blocked_effects: set[ToolEffect] = set()
    notes: list[str] = []
    strict_file_scope = bool(explicit_read_paths or explicit_write_paths)

    # Path scope comes ONLY from explicit fields — no NLP inference
    allowed_read_paths: frozenset[str] | None = explicit_read_paths
    allowed_write_paths: frozenset[str] | None = explicit_write_paths

    if intent is TaskIntent.EDIT and explicit_write_paths:
        allowed_read_paths = frozenset(set(allowed_read_paths or ()) | explicit_write_paths)

    if intent is TaskIntent.ANALYSIS:
        planning_allowed = frozenset()
        execution_allowed = None
        execution_allowed_effects = (
            frozenset({
                ToolEffect.READ_WORKSPACE,
                ToolEffect.PRODUCE_DELIVERABLE,
            })
            if allowed_read_paths else READ_ONLY_EFFECTS
        )
        planning_allowed_effects = frozenset()
        required_reads = frozenset(allowed_read_paths or ())
        required_writes = frozenset()
        require_any_write = False
        require_any_read = bool(strict_file_scope and not allowed_read_paths)
    else:
        planning_allowed = None
        execution_allowed = None
        planning_allowed_effects = READ_ONLY_EFFECTS
        execution_allowed_effects = None
        required_reads = frozenset()
        required_writes = frozenset(allowed_write_paths or ())
        require_any_write = not bool(required_writes)
        require_any_read = False

    planning = PhasePolicy(
        allowed_tools=planning_allowed,
        denied_effects=frozenset(blocked_effects),
        allowed_effects=planning_allowed_effects,
        allowed_read_paths=allowed_read_paths,
        allowed_write_paths=None,
        strict_file_scope=strict_file_scope,
        notes=tuple(notes),
    )
    execution = PhasePolicy(
        allowed_tools=execution_allowed,
        denied_effects=frozenset(blocked_effects),
        allowed_effects=execution_allowed_effects,
        allowed_read_paths=allowed_read_paths,
        allowed_write_paths=allowed_write_paths,
        strict_file_scope=strict_file_scope,
        notes=tuple(notes),
    )
    completion = CompletionPolicy(
        required_reads=required_reads,
        required_writes=required_writes,
        forbidden_tools=frozenset(),
        require_any_write=require_any_write,
        require_any_read=require_any_read,
        strict_file_scope=strict_file_scope,
    )
    return TaskPolicy(
        planning=planning,
        execution=execution,
        completion=completion,
        notes=tuple(notes),
    )
