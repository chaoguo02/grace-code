"""Agent V2 data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

from agent.task import RunStatus, TaskIntent, TerminationReason
from agent.session.result_contract import Finding, SubagentReport


class SessionMode(str, Enum):
    PRIMARY = "primary"
    SUBAGENT = "subagent"


class AgentKind(str, Enum):
    """Identity of an agent session, independent of its workspace."""

    PRIMARY = "primary"
    NAMED_SUBAGENT = "named_subagent"
    FORK = "fork"


@dataclass(frozen=True, order=True)
class AgentDepth:
    """Persisted nesting depth below the main conversation."""

    value: int = 0
    MAX_SUBAGENT_DEPTH: ClassVar[int] = 5

    def __post_init__(self) -> None:
        if not isinstance(self.value, int):
            raise TypeError("agent depth must be an integer")
        if not 0 <= self.value <= self.MAX_SUBAGENT_DEPTH:
            raise ValueError(
                f"agent depth must be between 0 and {self.MAX_SUBAGENT_DEPTH}"
            )

    @property
    def can_spawn(self) -> bool:
        return self.value < self.MAX_SUBAGENT_DEPTH

    def child(self) -> "AgentDepth":
        if not self.can_spawn:
            raise ValueError("maximum subagent depth reached")
        return AgentDepth(self.value + 1)


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


class NotificationDeliveryState(str, Enum):
    """Persistent delivery state for child completion notifications."""

    PENDING = "pending"
    DELIVERED = "delivered"


class AgentMessageOutcome(str, Enum):
    """Objective result of sending a message to an existing child."""

    RUNNING_UNAVAILABLE = "running_unavailable"
    RESUMED_IN_BACKGROUND = "resumed_in_background"


class AgentWaitOutcome(str, Enum):
    TERMINAL = "terminal"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"


class AgentCancelOutcome(str, Enum):
    REQUESTED = "requested"
    ALREADY_TERMINAL = "already_terminal"
    UNAVAILABLE = "unavailable"


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


class PermissionMode(str, Enum):
    """Permission mode for an agent (CC-aligned frontmatter field)."""

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    AUTO = "auto"
    DONT_ASK = "dontAsk"
    BYPASS_PERMISSIONS = "bypassPermissions"
    PLAN = "plan"
    MANUAL = "manual"


class AgentModel(str, Enum):
    """Subagent model selection.

    Supports CC model aliases plus arbitrary model IDs.
    Use classmethod resolve() to normalize user input.
    """

    INHERIT = "inherit"
    SONNET = "sonnet"
    OPUS = "opus"
    HAIKU = "haiku"
    FABLE = "fable"

    @classmethod
    def _missing_(cls, value: object) -> "AgentModel | None":
        """Accept arbitrary model IDs (e.g. 'claude-opus-4-8') as-is."""
        if isinstance(value, str) and value.strip():
            return cls.INHERIT  # passthrough — stored as raw str in AgentDefinition.model
        return None


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
    def from_tools(cls, tools: frozenset[str]) -> "DelegationPolicy":
        """Extract delegation allowlist from tools containing Agent(name) syntax.
        
        CC-aligned: tools: Agent(worker, researcher) restricts spawning to
        only those subagent types. Used during agent definition loading.
        """
        for tool in tools:
            if tool.startswith("Agent(") and tool.endswith(")"):
                inner = tool[6:-1].strip()
                if inner:
                    names = frozenset(
                        n.strip() for n in inner.split(",") if n.strip()
                    )
                    if names:
                        return cls(mode=DelegationMode.ALLOWLIST, allowed_names=names)
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

    @classmethod
    def from_run_status(cls, status: RunStatus) -> "SessionStatus":
        """Converge a primary run into its durable session lifecycle."""
        typed = RunStatus(status)
        if typed is RunStatus.SUCCESS:
            return cls.COMPLETED
        if typed is RunStatus.CANCELLED:
            return cls.CANCELLED
        return cls.FAILED

    @classmethod
    def from_agent_run_status(
        cls, status: "AgentRunStatus",
    ) -> "SessionStatus":
        return {
            AgentRunStatus.COMPLETED: cls.COMPLETED,
            AgentRunStatus.PARTIAL: cls.PARTIAL,
            AgentRunStatus.FAILED: cls.FAILED,
            AgentRunStatus.CANCELLED: cls.CANCELLED,
        }[AgentRunStatus(status)]


class AgentRunStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def from_run_status(cls, status: RunStatus) -> "AgentRunStatus":
        return {
            RunStatus.SUCCESS: cls.COMPLETED,
            RunStatus.MAX_STEPS: cls.PARTIAL,
            RunStatus.CANCELLED: cls.CANCELLED,
        }.get(RunStatus(status), cls.FAILED)

    @classmethod
    def from_session_status(cls, status: SessionStatus) -> "AgentRunStatus":
        typed = SessionStatus(status)
        if typed in {SessionStatus.QUEUED, SessionStatus.RUNNING}:
            raise ValueError("A running session has no terminal agent result")
        return {
            SessionStatus.COMPLETED: cls.COMPLETED,
            SessionStatus.PARTIAL: cls.PARTIAL,
            SessionStatus.FAILED: cls.FAILED,
            SessionStatus.CANCELLED: cls.CANCELLED,
        }[typed]

    @property
    def session_status(self) -> SessionStatus:
        return SessionStatus.from_agent_run_status(self)

    @property
    def run_status(self) -> RunStatus:
        return {
            AgentRunStatus.COMPLETED: RunStatus.SUCCESS,
            AgentRunStatus.PARTIAL: RunStatus.MAX_STEPS,
            AgentRunStatus.FAILED: RunStatus.FAILED,
            AgentRunStatus.CANCELLED: RunStatus.CANCELLED,
        }[self]

    @property
    def termination_reason(self) -> TerminationReason:
        return {
            AgentRunStatus.COMPLETED: TerminationReason.NONE,
            AgentRunStatus.PARTIAL: TerminationReason.MAX_STEPS,
            AgentRunStatus.FAILED: TerminationReason.INTERNAL_ERROR,
            AgentRunStatus.CANCELLED: TerminationReason.USER_CANCELLED,
        }[self]


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
    agent_depth: AgentDepth = field(default_factory=AgentDepth)
    generation: int = 0
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
        if not isinstance(self.agent_depth, AgentDepth):
            self.agent_depth = AgentDepth(self.agent_depth)
        if self.generation < 0:
            raise ValueError("Session generation cannot be negative")
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
    """Runtime-validated agent definition loaded from Markdown frontmatter.

    CC-aligned fields: permission_mode, mcp_servers, skills, memory,
    background, effort, color, initial_prompt, hooks.
    """

    name: str
    description: str
    intent: TaskIntent
    tools: frozenset[str] = frozenset()
    disallowed_tools: frozenset[str] = frozenset()
    delegation_policy: DelegationPolicy = field(
        default_factory=DelegationPolicy.disabled
    )
    delegation_scope: DelegationScope | None = None
    model: str = "inherit"
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
    # ── CC-aligned frontmatter fields ──
    permission_mode: str = ""
    """CC permissionMode: 'default', 'acceptEdits', 'auto', 'plan', etc.
    Empty string means not set (inherits from parent context)."""
    mcp_servers: tuple[str | dict, ...] = field(default_factory=tuple)
    """MCP servers available to this agent (server names or inline defs)."""
    skills: tuple[str, ...] = field(default_factory=tuple)
    """Skill names to preload into this agent's context at startup."""
    memory: str = ""
    """Persistent memory scope: 'user', 'project', 'local', or empty (disabled)."""
    background: bool = False
    """Always run as a background task when True."""
    effort: str = ""
    """Reasoning effort: 'low', 'medium', 'high', 'xhigh', 'max'."""
    color: str = ""
    """Display color: 'red', 'blue', 'green', 'yellow', 'purple', etc."""
    initial_prompt: str = ""
    """Auto-submitted as the first user turn when running via --agent."""
    hooks: tuple[dict, ...] = field(default_factory=tuple)
    """Lifecycle hooks scoped to this agent (PreToolUse, PostToolUse, Stop)."""

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
        if not isinstance(self.model, str):
            object.__setattr__(self, "model", str(self.model))
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
        if self.permission_mode and self.permission_mode not in {
            "default", "acceptEdits", "auto", "dontAsk",
            "bypassPermissions", "plan", "manual",
        }:
            raise ValueError(f"Invalid permission_mode: {self.permission_mode!r}")
        if self.memory and self.memory not in {"user", "project", "local"}:
            raise ValueError(f"Invalid memory scope: {self.memory!r}")
        if self.effort and self.effort not in {
            "low", "medium", "high", "xhigh", "max",
        }:
            raise ValueError(f"Invalid effort level: {self.effort!r}")

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
class AgentSpawnRequest:
    """One child launch with orthogonal identity, context, and workspace facts."""

    agent_kind: AgentKind
    context_origin: ContextOrigin
    execution_placement: ExecutionPlacement
    workspace_mode: WorkspaceMode
    description: str
    prompt: str
    definition: AgentDefinition | None = None
    model_name: str | None = None

    @staticmethod
    def resolve_execution_placement(
        *,
        agent_kind: AgentKind,
        requested: ExecutionPlacement | str | None,
        definition: AgentDefinition | None = None,
    ) -> ExecutionPlacement:
        """Resolve caller-facing placement into a persisted runtime placement.

        AUTO is an input-side convenience, not a durable runtime state. Named
        children honor their definition-level ``background`` default; forks stay
        foreground unless the caller explicitly requests background.
        """
        if requested is None:
            requested = ExecutionPlacement.AUTO
        placement = ExecutionPlacement(requested)
        if placement is not ExecutionPlacement.AUTO:
            return placement
        # CC-aligned (v2.1.198): named subagents default to BACKGROUND.
        # Explicit definition.background=False can override back to FOREGROUND.
        if agent_kind is AgentKind.NAMED_SUBAGENT:
            if definition is not None and definition.background is False:
                return ExecutionPlacement.FOREGROUND
            return ExecutionPlacement.BACKGROUND
        # Forks stay foreground unless caller explicitly requests background.
        return ExecutionPlacement.FOREGROUND

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_kind", AgentKind(self.agent_kind))
        object.__setattr__(self, "context_origin", ContextOrigin(self.context_origin))
        object.__setattr__(
            self, "execution_placement",
            ExecutionPlacement(self.execution_placement),
        )
        object.__setattr__(self, "workspace_mode", WorkspaceMode(self.workspace_mode))
        for field_name in ("description", "prompt"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())
        if self.execution_placement is ExecutionPlacement.AUTO:
            raise ValueError("Spawn requests require a resolved execution placement")
        if self.agent_kind is AgentKind.NAMED_SUBAGENT:
            if self.context_origin not in {
                ContextOrigin.FRESH, ContextOrigin.RESUMED,
            }:
                raise ValueError("Named subagents require fresh or resumed context")
            if (
                self.definition is None
                or self.definition.agent_kind is not AgentKind.NAMED_SUBAGENT
            ):
                raise ValueError("Named subagents require a named definition")
            if self.workspace_mode is not self.definition.workspace_mode:
                raise ValueError("Named subagent workspace must match its definition")
        elif self.agent_kind is AgentKind.FORK:
            if self.context_origin not in {
                ContextOrigin.PARENT_SNAPSHOT, ContextOrigin.RESUMED,
            }:
                raise ValueError("Forks require a parent snapshot or resume history")
            if self.definition is not None:
                raise ValueError("Fork is a spawn-time choice, not a definition")
        else:
            raise ValueError("Primary agents cannot be spawned as children")

    @classmethod
    def named(
        cls,
        *,
        definition: AgentDefinition,
        description: str,
        prompt: str,
        execution_placement: ExecutionPlacement | None = None,
        model_name: str | None = None,
    ) -> "AgentSpawnRequest":
        execution_placement = cls.resolve_execution_placement(
            agent_kind=AgentKind.NAMED_SUBAGENT,
            requested=execution_placement,
            definition=definition,
        )
        return cls(
            agent_kind=AgentKind.NAMED_SUBAGENT,
            context_origin=ContextOrigin.FRESH,
            execution_placement=execution_placement,
            workspace_mode=definition.workspace_mode,
            description=description,
            prompt=prompt,
            definition=definition,
            model_name=model_name,
        )

    @classmethod
    def fork(
        cls,
        *,
        description: str,
        prompt: str,
        workspace_mode: WorkspaceMode = WorkspaceMode.CURRENT,
        execution_placement: ExecutionPlacement = ExecutionPlacement.FOREGROUND,
        model_name: str | None = None,
    ) -> "AgentSpawnRequest":
        return cls(
            agent_kind=AgentKind.FORK,
            context_origin=ContextOrigin.PARENT_SNAPSHOT,
            execution_placement=execution_placement,
            workspace_mode=workspace_mode,
            description=description,
            prompt=prompt,
            model_name=model_name,
        )

    @classmethod
    def resumed(
        cls,
        *,
        agent_kind: AgentKind,
        workspace_mode: WorkspaceMode,
        description: str,
        prompt: str,
        definition: AgentDefinition | None,
    ) -> "AgentSpawnRequest":
        return cls(
            agent_kind=agent_kind,
            context_origin=ContextOrigin.RESUMED,
            execution_placement=ExecutionPlacement.BACKGROUND,
            workspace_mode=workspace_mode,
            description=description,
            prompt=prompt,
            definition=definition,
        )


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


@dataclass(frozen=True)
class BackgroundAgentHandle:
    """Immediate acknowledgement for a child running independently."""

    agent_name: str
    session_id: str
    generation: int = 0
    status: SessionStatus = SessionStatus.RUNNING
    execution_placement: ExecutionPlacement = ExecutionPlacement.BACKGROUND

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", SessionStatus(self.status))
        object.__setattr__(
            self,
            "execution_placement",
            ExecutionPlacement(self.execution_placement),
        )
        if self.status is not SessionStatus.RUNNING:
            raise ValueError("A background handle must identify a running session")
        if self.execution_placement is not ExecutionPlacement.BACKGROUND:
            raise ValueError("A background handle requires background placement")
        if self.generation < 0:
            raise ValueError("Background generation cannot be negative")


@dataclass(frozen=True)
class AgentCompletionNotification:
    """Typed, durable child result awaiting delivery to its parent."""

    parent_session_id: str
    result: AgentRunResult
    generation: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.parent_session_id, str) or not self.parent_session_id:
            raise ValueError("parent_session_id must be a non-empty string")
        if not isinstance(self.result, AgentRunResult):
            raise TypeError("result must be an AgentRunResult")
        if self.generation < 0:
            raise ValueError("Notification generation cannot be negative")

    @property
    def child_session_id(self) -> str:
        return self.result.session_id

    def to_dict(self) -> dict[str, object]:
        return {
            "parent_session_id": self.parent_session_id,
            "generation": self.generation,
            "result": self.result.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentCompletionNotification":
        result = data.get("result")
        if not isinstance(result, dict):
            raise ValueError("Completion notification requires a result object")
        return cls(
            parent_session_id=str(data["parent_session_id"]),
            result=AgentRunResult.from_dict(result),
            generation=int(data.get("generation", 0)),
        )


@dataclass(frozen=True)
class AgentMessageReceipt:
    child_session_id: str
    generation: int
    outcome: AgentMessageOutcome

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", AgentMessageOutcome(self.outcome))
        if self.generation < 0:
            raise ValueError("Message receipt generation cannot be negative")


@dataclass(frozen=True)
class AgentWaitResult:
    child_session_id: str
    generation: int
    outcome: AgentWaitOutcome
    session_status: SessionStatus
    result: AgentRunResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", AgentWaitOutcome(self.outcome))
        object.__setattr__(self, "session_status", SessionStatus(self.session_status))


@dataclass(frozen=True)
class AgentCancelResult:
    child_session_id: str
    generation: int
    outcome: AgentCancelOutcome
    session_status: SessionStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", AgentCancelOutcome(self.outcome))
        object.__setattr__(self, "session_status", SessionStatus(self.session_status))


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
    "git_status", "git_diff", "Bash",
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
    "Agent", "Skill",
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
        permission_mode="default",
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
        system_prompt="""You are a planning agent. Your job is research and plan design — no code execution.
Delegate exploration to subagents via the Agent tool. Use 'explore' for code search,
'code-reviewer' for quality analysis. Spawn multiple agents in one turn for parallel
investigation, wait for all results, then synthesize into a structured ExitPlanMode contract.""",
        permission_mode="plan",
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
        disallowed_tools=frozenset({"Write", "Edit", "Bash", "Agent"}),
        max_turns=50,
        max_tokens=40_000,
        system_prompt="""You are a read-only code analysis agent. Analyze code and return findings.
- Read files with Read (NEVER use shell commands to read files).
- Search code with Grep (NEVER use grep or find in shell).
- Stop as soon as you can answer the question asked.
- Return: Files inspected, Key findings with line numbers, Evidence (actual code read).
- Do NOT edit code or leave follow-up work for the parent.
- Your final message IS your return value.""",
        permission_mode="default",
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
        disallowed_tools=frozenset({"Agent"}),
        max_turns=60,
        system_prompt="""You are a coding subagent. Handle a single, well-scoped task.
- Read files with Read, edit with Edit, write with Write.
- Use Bash ONLY for running tests, builds, and git commands — NEVER for
  reading files (cat/type) or modifying files (sed/awk).
- Search → read → edit → verify.
- If finished: summarize concrete changes.
- If blocked: explain precisely what's missing.
- Your final message IS your return value.""",
        permission_mode="default",
    ),
    "code-reviewer": AgentDefinition(
        name="code-reviewer",
        description="Reviews code for correctness and quality.",
        intent=TaskIntent.ANALYSIS,
        workspace_mode=WorkspaceMode.CURRENT,
        visibility=AgentVisibility.HIDDEN,
        tools=_DEFAULT_READONLY_TOOLS,
        disallowed_tools=frozenset({"Write", "Edit", "Bash", "Agent", "WebFetch", "WebSearch"}),
        max_turns=40,
        max_tokens=30_000,
        required_tools=frozenset({"ReportFindings"}),
        completion_requires={"ReportFindings": 1},
        system_prompt="""You are a code reviewer. Find bugs and quality issues.
- Focus on correctness first, then simplification.
- Do NOT rubber-stamp weak work.
- For each finding: file, line, summary, failure scenario.
- Do NOT edit code. Your final message IS your review.""",
        permission_mode="default",
    ),
}
