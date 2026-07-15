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


class AgentKind(str, Enum):
    """Identity of an agent session, independent of its workspace."""

    PRIMARY = "primary"
    NAMED_SUBAGENT = "named_subagent"
    FORK = "fork"


class ContextOrigin(str, Enum):
    """Objective source of the conversation loaded for an agent run."""

    FRESH = "fresh"
    PARENT_SNAPSHOT = "parent_snapshot"
    RESUMED = "resumed"


class ExecutionPlacement(str, Enum):
    """Where an agent run executes relative to its caller."""

    AUTO = "auto"
    FOREGROUND = "foreground"
    BACKGROUND = "background"


class WorkspaceMode(str, Enum):
    """Filesystem placement, orthogonal to context inheritance."""

    CURRENT = "current"
    WORKTREE = "worktree"


# Temporary source-compatibility alias for callers that only used WORKTREE.
# NONE/SHARED intentionally do not exist: identity and current-workspace use
# now have their own strongly typed fields.
AgentIsolation = WorkspaceMode


class WorktreeChange(str, Enum):
    """Git-observed state of an isolated child workspace."""

    NONE = "none"
    UNCOMMITTED = "uncommitted"
    COMMITTED = "committed"
    BOTH = "both"
    UNKNOWN = "unknown"


class WorktreeDisposition(str, Enum):
    """Lifecycle state of an isolated child workspace result."""

    NOT_APPLICABLE = "not_applicable"
    CLEANED = "cleaned"
    PRESERVED = "preserved"
    RETAINED = "retained"
    APPLIED = "applied"
    DISCARDED = "discarded"


class WorktreeAvailability(str, Enum):
    """Physical availability of a persisted managed worktree."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class WorktreeResolutionAction(str, Enum):
    """Explicit administrative action for a managed worktree."""

    APPLY = "apply"
    DISCARD = "discard"


class AgentVisibility(str, Enum):
    PUBLIC = "public"
    HIDDEN = "hidden"


class AgentModel(str, Enum):
    """Subagent model selection currently supported by this Runtime."""

    INHERIT = "inherit"


class DelegationScope(str, Enum):
    """Maximum authority a parent may grant to a child agent."""

    READ_ONLY = "read_only"
    ANY = "any"


class DelegationMode(str, Enum):
    """Whether an agent may delegate, independent of child authority scope."""

    DISABLED = "disabled"
    ALLOWLIST = "allowlist"


class DelegationOrigin(str, Enum):
    """Runtime entrypoint that objectively caused a child session to exist."""

    TOOL = "task"
    EXPLICIT = "explicit"


@dataclass(frozen=True)
class ExplicitDelegationRequest:
    """Typed one-shot request that guarantees a named child is dispatched."""

    agent_name: str
    description: str
    prompt: str

    def __post_init__(self) -> None:
        for field_name in ("agent_name", "description", "prompt"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())


@dataclass(frozen=True)
class DelegationPolicy:
    """Declarative subagent grant with no implicit or unbounded state."""

    mode: DelegationMode = DelegationMode.DISABLED
    allowed_names: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.mode, DelegationMode):
            object.__setattr__(self, "mode", DelegationMode(self.mode))
        if not isinstance(self.allowed_names, frozenset):
            raise TypeError("delegation policy names must be a frozenset")
        if not all(isinstance(name, str) for name in self.allowed_names):
            raise TypeError("delegation policy names must be strings")
        normalized = frozenset(
            name.strip() for name in self.allowed_names if name.strip()
        )
        object.__setattr__(self, "allowed_names", normalized)
        if self.mode is DelegationMode.DISABLED and normalized:
            raise ValueError("disabled delegation policy cannot name subagents")
        if self.mode is DelegationMode.ALLOWLIST and not normalized:
            raise ValueError("allowlist delegation policy requires subagent names")

    @classmethod
    def disabled(cls) -> "DelegationPolicy":
        return cls()

    @classmethod
    def allowlist(cls, names: frozenset[str]) -> "DelegationPolicy":
        return cls(mode=DelegationMode.ALLOWLIST, allowed_names=names)

    def permits(self, name: str) -> bool:
        return (
            self.mode is DelegationMode.ALLOWLIST
            and name in self.allowed_names
        )


class SessionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRunStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Compatibility alias while execution APIs are migrated in Batch 3.
ForkStatus = AgentRunStatus


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
    agent_kind: AgentKind = AgentKind.PRIMARY
    context_origin: ContextOrigin = ContextOrigin.FRESH
    execution_placement: ExecutionPlacement = ExecutionPlacement.FOREGROUND
    workspace_mode: WorkspaceMode = WorkspaceMode.CURRENT
    summary: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    agent_result: AgentRunResult | None = None

    def __post_init__(self) -> None:
        self.mode = SessionMode(self.mode)
        self.status = SessionStatus(self.status)
        self.agent_kind = AgentKind(self.agent_kind)
        self.context_origin = ContextOrigin(self.context_origin)
        self.execution_placement = ExecutionPlacement(self.execution_placement)
        self.workspace_mode = WorkspaceMode(self.workspace_mode)
        if (self.mode is SessionMode.PRIMARY) != (
            self.agent_kind is AgentKind.PRIMARY
        ):
            raise ValueError("Session mode and agent kind describe different roles")
        if self.execution_placement is ExecutionPlacement.AUTO:
            raise ValueError("Persisted sessions require a resolved execution placement")
        if (
            self.context_origin is ContextOrigin.PARENT_SNAPSHOT
            and self.agent_kind is not AgentKind.FORK
        ):
            raise ValueError(
                "Only fork sessions may originate from a parent snapshot"
            )
        if (
            self.agent_kind is AgentKind.FORK
            and self.context_origin is ContextOrigin.FRESH
        ):
            raise ValueError("Fork sessions require a parent snapshot or resume history")

    @property
    def fork_result(self) -> "AgentRunResult | None":
        """Compatibility view for persisted records created before Batch 1."""
        return self.agent_result


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
    """Runtime-validated agent definition loaded from Markdown frontmatter."""

    name: str
    description: str
    intent: TaskIntent
    tools: frozenset[str] = frozenset()
    disallowed_tools: frozenset[str] = frozenset()
    delegation_policy: DelegationPolicy = field(
        default_factory=DelegationPolicy.disabled
    )
    delegation_scope: DelegationScope | None = None
    model: AgentModel = AgentModel.INHERIT
    agent_kind: AgentKind = AgentKind.NAMED_SUBAGENT
    workspace_mode: WorkspaceMode = WorkspaceMode.CURRENT
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
        if not isinstance(self.agent_kind, AgentKind):
            object.__setattr__(self, "agent_kind", AgentKind(self.agent_kind))
        if self.agent_kind is AgentKind.FORK:
            raise ValueError("Fork is a spawn-time context choice, not an agent definition")
        if not isinstance(self.workspace_mode, WorkspaceMode):
            object.__setattr__(
                self, "workspace_mode", WorkspaceMode(self.workspace_mode)
            )
        if not isinstance(self.visibility, AgentVisibility):
            object.__setattr__(self, "visibility", AgentVisibility(self.visibility))
        if not isinstance(self.model, AgentModel):
            object.__setattr__(self, "model", AgentModel(self.model))
        if self.delegation_scope is not None and not isinstance(
            self.delegation_scope, DelegationScope
        ):
            object.__setattr__(
                self, "delegation_scope", DelegationScope(self.delegation_scope)
            )
        if not isinstance(self.delegation_policy, DelegationPolicy):
            raise TypeError("delegation_policy must be a DelegationPolicy")
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max_tokens must be positive when provided")

    @property
    def mode(self) -> SessionMode:
        return (
            SessionMode.PRIMARY
            if self.agent_kind is AgentKind.PRIMARY
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
        if not self.delegation_policy.permits(child.name):
            return False
        if self.effective_delegation_scope is DelegationScope.READ_ONLY:
            return child.intent is TaskIntent.ANALYSIS
        return True


@dataclass(frozen=True)
class AgentRunResult:
    """Typed result from any child-agent run."""

    agent_name: str
    session_id: str
    status: AgentRunStatus
    summary: str
    error: str = ""
    artifacts: tuple[str, ...] = ()
    turns_used: int = 0
    tokens_used: int = 0
    report: SubagentReport | None = None
    failure_diagnosis: str = ""  # structured diagnosis when status is "failed"
    warning: str = ""
    worktree: "WorktreeEvidence | None" = None
    worktree_disposition: WorktreeDisposition = WorktreeDisposition.NOT_APPLICABLE

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AgentRunStatus(self.status))
        object.__setattr__(
            self,
            "worktree_disposition",
            WorktreeDisposition(self.worktree_disposition),
        )
        if self.worktree is not None and not isinstance(self.worktree, WorktreeEvidence):
            raise TypeError("worktree must be WorktreeEvidence when provided")
        evidence_dispositions = {
            WorktreeDisposition.PRESERVED,
            WorktreeDisposition.RETAINED,
        }
        if (
            self.worktree_disposition in evidence_dispositions
        ) != (self.worktree is not None):
            raise ValueError(
                "worktree evidence must exist exactly while disposition is "
                "preserved or retained"
            )

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
            "worktree_disposition": self.worktree_disposition.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentRunResult":
        raw_report = data.get("report")
        report = (
            SubagentReport.from_dict(raw_report)
            if isinstance(raw_report, dict) else None
        )
        raw_worktree = data.get("worktree")
        raw_disposition = data.get("worktree_disposition")
        if raw_disposition is None:
            raw_disposition = (
                WorktreeDisposition.PRESERVED.value
                if isinstance(raw_worktree, dict)
                else WorktreeDisposition.NOT_APPLICABLE.value
            )
        return cls(
            agent_name=str(data["agent_name"]),
            session_id=str(data["session_id"]),
            status=AgentRunStatus(data["status"]),
            summary=str(data.get("summary", "")),
            error=str(data.get("error", "")),
            artifacts=tuple(str(item) for item in data.get("artifacts", [])),
            turns_used=int(data.get("turns_used", 0)),
            tokens_used=int(data.get("tokens_used", 0)),
            report=report,
            failure_diagnosis=str(data.get("failure_diagnosis", "")),
            warning=str(data.get("warning", "")),
            worktree=(
                WorktreeEvidence.from_dict(raw_worktree)
                if isinstance(raw_worktree, dict) else None
            ),
            worktree_disposition=WorktreeDisposition(raw_disposition),
        )


# Compatibility alias while execution APIs are migrated in Batch 3.
ForkResult = AgentRunResult


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


@dataclass(frozen=True)
class ManagedWorktreeRecord:
    """Fresh inventory view joining Session DB state with Git facts."""

    child_session_id: str
    parent_session_id: str
    disposition: WorktreeDisposition
    availability: WorktreeAvailability
    evidence: WorktreeEvidence
    error: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "disposition", WorktreeDisposition(self.disposition))
        object.__setattr__(self, "availability", WorktreeAvailability(self.availability))

    def to_dict(self) -> dict[str, object]:
        return {
            "child_session_id": self.child_session_id,
            "parent_session_id": self.parent_session_id,
            "disposition": self.disposition.value,
            "availability": self.availability.value,
            "evidence": self.evidence.to_dict(),
            "error": self.error,
        }

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
        delegation_policy=DelegationPolicy.allowlist(
            frozenset({"explore", "general", "code-reviewer"})
        ),
        agent_kind=AgentKind.PRIMARY,
        visibility=AgentVisibility.PUBLIC,
        max_turns=100,
        system_prompt="",
    ),
    "plan": AgentDefinition(
        name="plan",
        description="Read-only planning agent. Explores codebase and produces structured plans.",
        intent=TaskIntent.ANALYSIS,
        tools=_DEFAULT_READONLY_TOOLS,
        delegation_policy=DelegationPolicy.allowlist(
            frozenset({"explore", "code-reviewer"})
        ),
        agent_kind=AgentKind.PRIMARY,
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
        workspace_mode=WorkspaceMode.CURRENT,
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
        workspace_mode=WorkspaceMode.CURRENT,
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
        workspace_mode=WorkspaceMode.CURRENT,
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
