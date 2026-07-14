"""Agent V2 data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent.task import TaskIntent
from agent.v2.result_contract import Finding, SubagentReport


class SessionMode(str, Enum):
    PRIMARY = "primary"
    SUBAGENT = "subagent"


class AgentIsolation(str, Enum):
    NONE = "none"
    FORK = "fork"
    WORKTREE = "worktree"


class WorktreeChange(str, Enum):
    """Git-observed state of an isolated child workspace."""

    NONE = "none"
    UNCOMMITTED = "uncommitted"
    COMMITTED = "committed"
    BOTH = "both"
    UNKNOWN = "unknown"


class AgentVisibility(str, Enum):
    PUBLIC = "public"
    HIDDEN = "hidden"


class DelegationScope(str, Enum):
    """Maximum authority a parent may grant to a child agent."""

    READ_ONLY = "read_only"
    ANY = "any"


class SessionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ForkStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class SessionRecord:
    id: str
    parent_id: str | None
    root_id: str
    agent_name: str
    mode: SessionMode
    title: str
    status: SessionStatus
    repo_path: str
    summary: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    fork_result: ForkResult | None = None

    def __post_init__(self) -> None:
        self.mode = SessionMode(self.mode)
        self.status = SessionStatus(self.status)


@dataclass
class SessionMessageRecord:
    id: int
    session_id: str
    role: str
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    created_at: str = ""


@dataclass(frozen=True)
class AgentDefinition:
    """Agent definition loaded from .md YAML frontmatter (Claude Code compatible)."""

    name: str
    description: str
    intent: TaskIntent
    tools: frozenset[str] = frozenset()
    disallowed_tools: frozenset[str] = frozenset()
    allowed_subagents: frozenset[str] | None = None
    delegation_scope: DelegationScope | None = None
    model: str = "inherit"
    isolation: AgentIsolation = AgentIsolation.FORK
    visibility: AgentVisibility = AgentVisibility.PUBLIC
    max_turns: int = 50
    max_tokens: int | None = None
    system_prompt: str = ""
    # ── Runtime-enforced contracts (not prompt-based) ──
    required_tools: frozenset[str] = frozenset()
    """Tools this subagent MUST call at least once before FINISH.
    The CompletionGuard enforces this — the model cannot finish without
    calling every tool in this set. Empty = no requirement."""
    completion_requires: dict[str, int] = field(default_factory=dict)
    """Per-tool minimum call counts required before FINISH is accepted.
    e.g. {"submit_findings": 1} means the subagent MUST call submit_findings ≥ 1 time.
    The CompletionGuard enforces this at the Runtime level."""

    def __post_init__(self) -> None:
        if not isinstance(self.intent, TaskIntent):
            object.__setattr__(self, "intent", TaskIntent(self.intent))
        if not isinstance(self.isolation, AgentIsolation):
            object.__setattr__(self, "isolation", AgentIsolation(self.isolation))
        if not isinstance(self.visibility, AgentVisibility):
            object.__setattr__(self, "visibility", AgentVisibility(self.visibility))
        if self.delegation_scope is not None and not isinstance(
            self.delegation_scope, DelegationScope
        ):
            object.__setattr__(
                self, "delegation_scope", DelegationScope(self.delegation_scope)
            )
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max_tokens must be positive when provided")

    @property
    def mode(self) -> SessionMode:
        return (
            SessionMode.PRIMARY
            if self.isolation is AgentIsolation.NONE
            else SessionMode.SUBAGENT
        )

    @property
    def effective_delegation_scope(self) -> DelegationScope:
        if self.delegation_scope is not None:
            return self.delegation_scope
        return (
            DelegationScope.READ_ONLY
            if self.intent is TaskIntent.ANALYSIS
            else DelegationScope.ANY
        )

    def permits_subagent(self, child: "AgentDefinition") -> bool:
        if self.allowed_subagents is not None and child.name not in self.allowed_subagents:
            return False
        if self.effective_delegation_scope is DelegationScope.READ_ONLY:
            return child.intent is TaskIntent.ANALYSIS
        return True


@dataclass(frozen=True)
class ForkResult:
    """Result from a forked subagent run."""

    agent_name: str
    session_id: str
    status: ForkStatus
    summary: str
    error: str = ""
    artifacts: tuple[str, ...] = ()
    turns_used: int = 0
    tokens_used: int = 0
    report: SubagentReport | None = None
    failure_diagnosis: str = ""  # structured diagnosis when status is "failed"
    warning: str = ""
    worktree: "WorktreeEvidence | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ForkStatus(self.status))
        if self.worktree is not None and not isinstance(self.worktree, WorktreeEvidence):
            raise TypeError("worktree must be WorktreeEvidence when provided")

    @property
    def structured_findings(self) -> tuple[Finding, ...]:
        return self.report.findings if self.report is not None else ()

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "session_id": self.session_id,
            "status": self.status.value,
            "summary": self.summary,
            "error": self.error,
            "artifacts": list(self.artifacts),
            "turns_used": self.turns_used,
            "tokens_used": self.tokens_used,
            "report": self.report.to_dict() if self.report is not None else None,
            "failure_diagnosis": self.failure_diagnosis,
            "warning": self.warning,
            "worktree": self.worktree.to_dict() if self.worktree is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ForkResult":
        raw_report = data.get("report")
        report = (
            SubagentReport.from_dict(raw_report)
            if isinstance(raw_report, dict) else None
        )
        return cls(
            agent_name=str(data["agent_name"]),
            session_id=str(data["session_id"]),
            status=ForkStatus(data["status"]),
            summary=str(data.get("summary", "")),
            error=str(data.get("error", "")),
            artifacts=tuple(str(item) for item in data.get("artifacts", [])),
            turns_used=int(data.get("turns_used", 0)),
            tokens_used=int(data.get("tokens_used", 0)),
            report=report,
            failure_diagnosis=str(data.get("failure_diagnosis", "")),
            warning=str(data.get("warning", "")),
            worktree=(
                WorktreeEvidence.from_dict(data["worktree"])
                if isinstance(data.get("worktree"), dict) else None
            ),
        )


@dataclass(frozen=True)
class WorktreeEvidence:
    """Immutable Git facts returned for a preserved child worktree."""

    change: WorktreeChange
    path: str
    branch: str
    base_branch: str
    base_commit: str = ""
    changed_files: tuple[str, ...] = ()
    revision: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "change", WorktreeChange(self.change))

    def to_dict(self) -> dict[str, object]:
        return {
            "change": self.change.value,
            "path": self.path,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "base_commit": self.base_commit,
            "changed_files": list(self.changed_files),
            "revision": self.revision,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorktreeEvidence":
        return cls(
            change=WorktreeChange(data["change"]),
            path=str(data.get("path", "")),
            branch=str(data.get("branch", "")),
            base_branch=str(data.get("base_branch", "")),
            base_commit=str(data.get("base_commit", "")),
            changed_files=tuple(str(item) for item in data.get("changed_files", [])),
            revision=str(data.get("revision", "")),
            error=str(data.get("error", "")),
        )

# ── Built-in agent definitions (fallback when no .md files exist) ──

_DEFAULT_READONLY_TOOLS = frozenset({
    "Read", "Glob", "Grep", "file_view", "WebFetch", "WebSearch",
    "git_status", "git_diff",
    "artifact_list", "artifact_read", "artifact_search",
    "evidence_list", "evidence_get",
    "memory_read", "memory_list", "memory_search",
})

_DEFAULT_GENERAL_TOOLS = frozenset({
    "Read", "Glob", "Grep", "file_view", "Write", "Edit", "Bash",
    "WebFetch", "WebSearch",
    "git_status", "git_diff", "git_add", "git_commit",
    "pytest",
    "artifact_list", "artifact_read", "artifact_search",
    "evidence_list", "evidence_get",
    "memory_read", "memory_list", "memory_search", "memory_write", "memory_delete",
    "Task",
})

_BUILTIN_AGENTS: dict[str, AgentDefinition] = {
    "build": AgentDefinition(
        name="build",
        description="Primary coding agent with full tool access. Can delegate to subagents.",
        intent=TaskIntent.EDIT,
        tools=_DEFAULT_GENERAL_TOOLS,
        allowed_subagents=frozenset({"explore", "general", "code-reviewer"}),
        isolation=AgentIsolation.NONE,
        visibility=AgentVisibility.PUBLIC,
        max_turns=100,
        system_prompt="",
    ),
    "plan": AgentDefinition(
        name="plan",
        description="Read-only planning agent. Explores codebase and produces structured plans.",
        intent=TaskIntent.ANALYSIS,
        tools=_DEFAULT_READONLY_TOOLS,
        allowed_subagents=frozenset({"explore", "code-reviewer"}),
        isolation=AgentIsolation.NONE,
        visibility=AgentVisibility.PUBLIC,
        max_turns=60,
        system_prompt="",
    ),
    "explore": AgentDefinition(
        name="explore",
        description="Fast read-only agent for code exploration, search, and analysis. "
        "Use for: finding files, searching code, analyzing code for bugs, "
        "answering questions about the codebase. Uses file_read/search_text — NO shell.",
        intent=TaskIntent.ANALYSIS,
        isolation=AgentIsolation.FORK,
        visibility=AgentVisibility.PUBLIC,
        tools=_DEFAULT_READONLY_TOOLS,
        disallowed_tools=frozenset({"Write", "Edit", "Bash", "Task"}),
        max_turns=50,
        max_tokens=40_000,
        system_prompt="""You are a read-only code analysis agent. Analyze code and return findings.
- Read files with file_read (NEVER use shell commands to read files).
- Search code with search_text (NEVER use grep or find in shell).
- Stop as soon as you can answer the question asked.
- Return: Files inspected, Key findings with line numbers, Evidence (actual code read).
- Do NOT edit code or leave follow-up work for the parent.
- Your final message IS your return value.""",
    ),
    "general": AgentDefinition(
        name="general",
        description="General-purpose coding subagent with full tool access "
        "including shell. Use ONLY when Write, Edit, or Bash is required. "
        "For read-only analysis, code search, or bug-finding, use 'explore' instead.",
        intent=TaskIntent.EDIT,
        isolation=AgentIsolation.FORK,
        visibility=AgentVisibility.PUBLIC,
        tools=_DEFAULT_GENERAL_TOOLS,
        disallowed_tools=frozenset({"Task"}),
        max_turns=60,
        system_prompt="""You are a coding subagent. Handle a single, well-scoped task.
- Read files with file_read, edit with file_edit, write with file_write.
- Use shell ONLY for running tests, builds, and git commands — NEVER for
  reading files (cat/type) or modifying files (sed/awk).
- Search → read → edit → verify.
- If finished: summarize concrete changes.
- If blocked: explain precisely what's missing.
- Your final message IS your return value.""",
    ),
    "code-reviewer": AgentDefinition(
        name="code-reviewer",
        description="Reviews code for correctness and quality.",
        intent=TaskIntent.ANALYSIS,
        isolation=AgentIsolation.FORK,
        visibility=AgentVisibility.HIDDEN,
        tools=_DEFAULT_READONLY_TOOLS,
        disallowed_tools=frozenset({"Write", "Edit", "Bash", "Task", "WebFetch", "WebSearch"}),
        max_turns=40,
        max_tokens=30_000,
        required_tools=frozenset({"submit_findings"}),
        completion_requires={"submit_findings": 1},
        system_prompt="""You are a code reviewer. Find bugs and quality issues.
- Focus on correctness first, then simplification.
- Do NOT rubber-stamp weak work.
- For each finding: file, line, summary, failure scenario.
- Do NOT edit code. Your final message IS your review.""",
    ),
}
