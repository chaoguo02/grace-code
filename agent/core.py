"""
agent/core.py

ReAct 主循环。整个 agent 的大脑。

职责（只做这些，不做别的）：
- 维护对话历史，每轮组装 messages 调用 LLM
- 拿到 Action 后调用 ToolRegistry 执行
- 把 Action + Observation 写入 EventLog
- 检测三种终止/Reflection 触发条件
- 返回 RunResult

不负责：
- 任何 LLM 细节（交给 LLMBackend）
- 任何工具实现（交给 Tool）
- 上下文压缩（由 context/ 模块负责）
"""

from __future__ import annotations

import errno
import json
import logging
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from xml.etree import ElementTree as ET

from agent.runtime_controller import RecoveryAction, RuntimeController, ToolDecision
from agent.event_log import EventLog, summarize_run
from context.evidence import EvidenceLedger
from context.history import ConversationHistory, ConversationSnapshot
from context.repo_map import RepoMap
from context.token_budget import TokenBudget
from prompts.builder import (
    consume_prompt_usage_metadata,
    create_prompt_renderer,
    PromptRenderer,
)
from agent.task import (
    Action, ActionType, Event, EventType,
    Observation, ObservationStatus, RunResult, RunStatus, Task, TaskIntent, ToolCall,
    TerminationReason, ToolOutcome, VerificationReason, VerificationStatus,
)
from context.artifacts import ArtifactStore
from context.compaction import ConversationCompactor
from context.manager import ContextManager, ContextManagerConfig, RequestContext
from llm.base import CacheStats, LLMBackend, LLMMessage, LLMToolSchema
from hooks.events import HookEvent
from observability.datasets import append_failure_dataset_item
from observability.models import (
    build_analysis_run_metadata,
    build_generation_input,
    build_generation_metadata,
    build_generation_output,
    build_replay_action_snapshot,
    build_replay_runtime_decision,
    build_replay_step_record,
    build_replay_tool_execution,
    build_run_metadata,
    build_run_output,
    build_tool_input,
    build_tool_output,
    merge_metadata,
    ReplayToolExecution,
)
from observability.scores import build_run_scores
from observability.tracing import get_observer
from core.base import (
    ToolConcurrency,
    ToolEffect,
    ToolErrorType,
    ToolRegistry,
    ToolResult,
    ToolRole,
)
from core.policy import TaskPolicy, build_task_policy, normalize_repo_path
from core.process import LocalRuntime
from core.project_environment import CapabilitySnapshot
from core.streaming_executor import StreamingToolExecutor

if TYPE_CHECKING:
    from memory.context import MemoryContext
    from memory.session_memory import SessionMemoryTracker
    from agent.session.task_state_machine import TaskStateMachine
    from agent.session.run_context import CancellationToken

# P2-2: moved from inline in _run_body — verified no circular import
from agent.completion_guard import CompletionContext, TaskCompletionGuard

# P2-2: kept inline — circular import via agent.session.__init__ → runtime → core:
#   agent.session.run_context
#   agent.session.execution_budget

logger = logging.getLogger(__name__)

from agent.constants import (
    BUDGET_COMPACT_PCT, BUDGET_WARNING_PCT, COMPLETION_BLOCK_THRESHOLD,
    DEFAULT_HISTORY_BUDGET_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_REQUEST_BUDGET_TOKENS, DEFAULT_TRUNCATE_OUTPUT_CHARS,
    DIFF_PREVIEW_MAX_CHARS, FINDING_DESC_CHARS, MAX_TOOL_RESULTS_EXTRACT,
    RECENT_FILES_WINDOW, RECOVERY_MAX_FINDINGS,
    SESSION_MEMORY_MSG_WINDOW, SUMMARY_TRUNCATION_CHARS,
    TEST_FAILURE_REFLECTION_LIMIT, TOOL_EXTRACT_CHARS,
    TRUNCATION_BUFFER_TOKENS,
)
# deferred imports — circular dependency (P1-8)
from agent.agent_config import AgentConfig
from agent.recovery import (
    AgentTurnState,
    RecoveryState,
    Transition,
    TransitionReason,
    TurnOutcome,
)
from agent.context_trimming import (
    ContextTrimmingState,
    _micro_compact,
    _snip_history,
    prepare_history_for_turn,
)
from agent.loop.types import CompletionBlockTracker
from agent.loop.turns import (
    ActionContractStatus,
    CompletionEvaluation,
    CompletionFacts,
    CompletionOutcome,
    CompletionRetrySource,
    PreStepEvaluation,
    PreStepOutcome,
    PostObservationOutcome,
    ProviderErrorOutcome,
    OutputRecoveryOutcome,
    ToolResultAnalysis,
    analyze_tool_result,
    build_action_history,
    evaluate_completion,
    evaluate_early_step_gate,
    evaluate_observation_batch,
    evaluate_output_recovery,
    evaluate_post_observation,
    evaluate_provider_error,
    evaluate_runtime_step_gate,
    execute_action,
    invoke_provider_turn,
    prepare_provider_request,
    validate_action_contract,
)

# Prefix for errors injected when delegation policy blocks a child agent
# spawn.  The agent loop checks tool-result errors for this prefix and marks
# them as expected blocks rather than real failures (P2-1).
_V2_DELEGATION_BLOCK_PREFIX = "BLOCKED_BY_DELEGATION_POLICY:"
# Maximum number of stop-hook retries before the agent loop force-terminates.
# Uses COMPLETION_BLOCK_THRESHOLD (3) from agent.constants (P2-1).
_MAX_STOP_HOOK_RETRIES = COMPLETION_BLOCK_THRESHOLD


class _ChildTurnPhase(str, Enum):
    """Parent-turn phase after receiving child subagent results.

    CC-aligned child lifecycle overlay (subagent report P1-1).
    Connects SessionStatus (child-side) to parent turn discipline (parent-side).

    Lifecycle: NONE → SYNTHESIS (child completed, parent should synthesize)
                      → RESOLUTION_PENDING (worktree needs explicit apply/discard)
                      → NONE (resolution complete or synthesis done)
    """
    NONE = "none"
    SYNTHESIS = "synthesis"
    """Child completed with results; parent should synthesize before next action."""
    RESOLUTION_PENDING = "resolution_pending"
    """Worktree child completed; parent MUST inspect + apply/discard/retain before finishing."""


@dataclass(frozen=True)
class _TaskNotificationFacts:
    worktree_disposition: str | None = None


def _task_notification_facts_from_result(result: Any) -> tuple[_TaskNotificationFacts, ...]:
    """Extract child-result facts from ToolResult — metadata first, XML fallback.

    Subagent P1-2: prefers typed ForkResult in metadata over text parsing.
    """
    # Primary path: typed metadata (subagent P1-2)
    meta = getattr(result, "metadata", None) or {}
    fork_dict = meta.get("fork_result")
    if isinstance(fork_dict, dict):
        facts: list[_TaskNotificationFacts] = []
        wt = fork_dict.get("worktree") or {}
        disposition = fork_dict.get("worktree_disposition", "")
        facts.append(_TaskNotificationFacts(
            worktree_disposition=(
                disposition.strip() if disposition else
                ("preserved" if wt else None)
            ),
        ))
        return tuple(facts)

    # Fallback: XML text parsing (backward compat)
    text = getattr(result, "output", "") or ""
    return _task_notification_facts_from_text(text)


def _task_notification_facts_from_text(text: str) -> tuple[_TaskNotificationFacts, ...]:
    """Parse Runtime-owned task-notification payloads from XML text (legacy)."""
    if "<task-notification>" not in text:
        return ()
    facts: list[_TaskNotificationFacts] = []
    for match in re.finditer(
        r"<task-notification>.*?</task-notification>", text, re.DOTALL,
    ):
        block = match.group(0)
        try:
            node = ET.fromstring(block)
        except ET.ParseError:
            continue
        disposition = node.findtext("worktree-disposition")
        facts.append(_TaskNotificationFacts(
            worktree_disposition=disposition.strip() if disposition else None,
        ))
    return tuple(facts)


def _has_resolution_pending_notification(text: str) -> bool:
    return any(
        facts.worktree_disposition == "preserved"
        for facts in _task_notification_facts_from_text(text)
    )


def _has_child_completion_notifications(messages: list[LLMMessage]) -> bool:
    """Return whether Runtime injected fresh child completion payloads."""
    return any(
        message.role == "user"
        and isinstance(message.content, str)
        and "<task-notification>" in message.content
        for message in messages
    )


def _phase_from_runtime_messages(
    messages: list[LLMMessage],
) -> _ChildTurnPhase:
    phase = _ChildTurnPhase.NONE
    for message in messages:
        if message.role != "user" or not isinstance(message.content, str):
            continue
        text = message.content
        if "<task-notification>" not in text:
            continue
        if _has_resolution_pending_notification(text):
            return _ChildTurnPhase.RESOLUTION_PENDING
        phase = _ChildTurnPhase.SYNTHESIS
    return phase


def _observations_include_child_notifications(
    observations: list[Observation],
) -> bool:
    """Return whether this tool batch yielded child-completion payloads."""
    for observation in observations:
        # Subagent P1-2: typed metadata first
        meta = observation.metadata if hasattr(observation, "metadata") else {}
        if isinstance(meta, dict) and "fork_result" in meta:
            return True
        text = observation.output if isinstance(observation.output, str) else ""
        if "<task-notification>" in text:
            return True
    return False


def _phase_from_observations(
    observations: list[Observation],
) -> _ChildTurnPhase:
    phase = _ChildTurnPhase.NONE
    for observation in observations:
        # Subagent P1-2: prefer typed metadata over text parsing
        meta = observation.metadata if hasattr(observation, "metadata") else {}
        fork_dict = meta.get("fork_result") if isinstance(meta, dict) else None
        if isinstance(fork_dict, dict):
            disposition = fork_dict.get("worktree_disposition", "")
            if disposition == "preserved":
                return _ChildTurnPhase.RESOLUTION_PENDING
            phase = _ChildTurnPhase.SYNTHESIS
            continue
        # Legacy XML fallback
        text = observation.output if isinstance(observation.output, str) else ""
        if "<task-notification>" in text:
            if _has_resolution_pending_notification(text):
                return _ChildTurnPhase.RESOLUTION_PENDING
            phase = _ChildTurnPhase.SYNTHESIS
    return phase


def _resolution_was_completed(observations: list[Observation]) -> bool:
    for observation in observations:
        if observation.tool_name not in {
            "subagent_worktree_apply",
            "subagent_worktree_discard",
            "subagent_worktree_retain",
        }:
            continue
        text = observation.output if isinstance(observation.output, str) else ""
        try:
            node = ET.fromstring(text)
        except ET.ParseError:
            continue
        if node.tag != "subagent-worktree-operation":
            continue
        if node.attrib.get("status") in {"applied", "discarded", "retained"}:
            return True
    return False


def _advance_child_turn_phase(
    current: _ChildTurnPhase,
    *,
    runtime_phase: _ChildTurnPhase = _ChildTurnPhase.NONE,
    observation_phase: _ChildTurnPhase = _ChildTurnPhase.NONE,
    observations: list[Observation] | None = None,
) -> _ChildTurnPhase:
    """Advance child-result turn state using typed runtime facts only."""
    if runtime_phase is _ChildTurnPhase.RESOLUTION_PENDING:
        return runtime_phase
    if (
        runtime_phase is _ChildTurnPhase.SYNTHESIS
        and current is _ChildTurnPhase.NONE
    ):
        current = runtime_phase

    if observation_phase is _ChildTurnPhase.RESOLUTION_PENDING:
        return observation_phase
    if observation_phase is _ChildTurnPhase.SYNTHESIS:
        return observation_phase
    if observations is None:
        return current
    if (
        current is _ChildTurnPhase.RESOLUTION_PENDING
        and _resolution_was_completed(observations)
    ):
        return _ChildTurnPhase.NONE
    if current is _ChildTurnPhase.SYNTHESIS:
        return _ChildTurnPhase.NONE
    return current

# ── Git state helpers (restored after refactoring) ──────────────────────────


@dataclass
class _GitState:
    """Mutable workspace git state tracked during one agent run."""
    is_git_repo: bool = False
    has_changes: bool = False
    current_diff: str = ""
    files_changed: set[str] = field(default_factory=set)
    _baseline_revision: str = ""
    _baseline_dirty_files: set[str] = field(default_factory=set)
    _last_git_error: str = ""
    _refresh_error_logged: bool = False


@dataclass
class _FinishRunContext:
    """Mutable context for the completion-result builder (P1-2).

    Contains ONLY reference-type fields that are mutated in-place during
    the run (git_state, completion_ctx, tsm).  Value-type fields that
    change during execution MUST be passed as explicit parameters to
    ``_build_run_result()`` — putting them here creates stale copies.
    """
    git_state: _GitState
    task: Any   # Task
    completion_ctx: Any  # CompletionContext
    tsm: Any  # TaskStateMachine
    reflection_counts: dict[str, int]
    get_consecutive_failures: Callable[[], int]
    log: Any  # EventLog
    task_obs: Any  # Langfuse observation
    task_context: Any  # Langfuse context manager
    task_obs_closed: bool = False


@dataclass(frozen=True)
class _PostObservationApplication:
    state: AgentTurnState
    missing_followups: int | None
    result: RunResult | None = None
    continue_loop: bool = False


@dataclass(frozen=True)
class _TerminalApplication:
    state: AgentTurnState
    completion_blocked: int
    result: RunResult | None = None
    continue_loop: bool = False


@dataclass(frozen=True)
class _ToolBatchApplication:
    tool_calls: tuple[ToolCall, ...]
    observations: tuple[Observation, ...]
    test_was_run: bool = False
    verification_ok: bool = False
    any_test_failed: bool = False
    missing_test_target_observation: Observation | None = None
    result: RunResult | None = None


@dataclass(frozen=True)
class _AcceptedActionApplication:
    state: AgentTurnState
    cumulative_tool_calls: int
    retry_loop: bool = False


@dataclass(frozen=True)
class _RunSetup:
    observer: Any
    history: ConversationHistory
    capabilities: Any
    token_budget: TokenBudget
    repo_map: RepoMap
    git_state: _GitState
    block_tracker: CompletionBlockTracker
    get_consecutive_failures: Callable[[], int]
    max_consecutive_failures: int
    reflection_counts: dict[str, int]
    completion_context: CompletionContext
    completion_guard: TaskCompletionGuard
    execution_budget: Any
    cancellation: Any
    base_run_context: Any
    runtime_controller: Any
    state_machine: Any
    finish_context: _FinishRunContext
    state: AgentTurnState


@dataclass(frozen=True)
class _ProviderPhaseApplication:
    state: AgentTurnState
    total_tokens: int
    cumulative_tool_calls: int
    action: Action | None = None
    response: Any = None
    tools: tuple[LLMToolSchema, ...] = ()
    spawn_context: Any = None
    streaming_executor: StreamingToolExecutor | None = None
    result: RunResult | None = None
    retry_loop: bool = False


@dataclass(frozen=True)
class _ToolTurnApplication:
    state: AgentTurnState
    missing_followups: int | None
    missing_message: str | None
    missing_detected_step: int | None
    result: RunResult | None = None
    continue_loop: bool = False


@dataclass(frozen=True)
class _StepGateApplication:
    state: AgentTurnState
    decision: Any = None
    result: RunResult | None = None


def _capture_git_state(repo_path: str) -> _GitState:
    """Capture git baseline before the agent run starts.

    Returns a ``_GitState`` with both the commit revision AND the set of
    files already dirty in the working tree.  Subsequent calls to
    ``_refresh_git_state()`` diff against the commit, and the completion
    guard subtracts baseline-dirty files so prior worktree dirt is never
    attributed to this run.
    """
    state = _GitState()
    # Import git exception types safely (git may not be installed)
    _git_exc: tuple[type, ...] = ()
    try:
        from git.exc import GitError, InvalidGitRepositoryError, NoSuchPathError  # noqa: F811
        _git_exc = (InvalidGitRepositoryError, NoSuchPathError, GitError)
    except ImportError:
        pass  # git not available — _git_exc stays empty

    try:
        import git
        repo = git.Repo(repo_path)
        state.is_git_repo = True
        state._baseline_revision = repo.head.commit.hexsha
        # Snapshot which files were ALREADY dirty before this run started.
        # The completion guard uses this to compute the run's incremental delta.
        _dirty = repo.git.diff("--name-only", "HEAD").strip()
        state._baseline_dirty_files = set(
            f for f in _dirty.split("\n") if f
        ) if _dirty else set()
        state.files_changed = set()
        state.current_diff = ""
        state.has_changes = False
    except ImportError:
        # GitPython not installed — not an error, just no git tracking
        state.is_git_repo = False
        state._last_git_error = "git module not installed"
        logger.debug("Git not installed — skipping git state capture")
    except _git_exc as exc:
        state.is_git_repo = False
        state._last_git_error = str(exc)
        logger.debug("Git unavailable (%s): %s", type(exc).__name__, exc)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise  # Permission denied is a systemic error — propagate
        # ENOENT, ENOTDIR, etc. — path issues, degrade gracefully
        state.is_git_repo = False
        state._last_git_error = str(exc)
        logger.warning("Cannot access git repository at %s: %s", repo_path, exc)
    return state


def _refresh_git_state(state: _GitState, repo_path: str) -> None:
    """Refresh git state against the captured baseline.

    Must be called after any tool execution that may have modified files.
    Uses ``repo_path`` to open the git repository and diffs against the
    baseline revision captured at run start — so prior worktree dirt is
    never attributed to this run.

    When the working tree was already dirty at baseline (e.g. uncommitted
    changes from a previous run), the diff still picks up new edits because
    it compares the current working tree against the baseline commit, not
    against the last-refreshed state.
    """
    if not state.is_git_repo:
        return
    try:
        import git
        from git.exc import GitError, InvalidGitRepositoryError  # noqa: F811
        repo = git.Repo(repo_path)
        # Diff working tree against the baseline commit (not HEAD).
        # This catches ALL uncommitted changes including files that were
        # already dirty when the run started — the completion guard's
        # ctx.had_any_write filter ensures we only care about files the
        # agent actually touched.
        diff = repo.git.diff(state._baseline_revision, name_only=True) or ""
        files = {line.strip() for line in diff.split("\n") if line.strip()}
        state.files_changed = files
        state.current_diff = repo.git.diff(state._baseline_revision) or ""
        state.has_changes = bool(files) or bool(state.current_diff)
        # Refresh succeeded — reset error state for the next failure cycle
        if state._refresh_error_logged:
            state._refresh_error_logged = False
            state._last_git_error = ""
    except ImportError:
        state.is_git_repo = False
        state._last_git_error = "git module unavailable during refresh"
        _log_level = logging.WARNING if not state._refresh_error_logged else logging.DEBUG
        logger.log(_log_level, "Git import failed during refresh — marking repo as unavailable")
        state._refresh_error_logged = True
    except (InvalidGitRepositoryError, GitError) as exc:
        state.is_git_repo = False
        state._last_git_error = str(exc)
        _log_level = logging.WARNING if not state._refresh_error_logged else logging.DEBUG
        logger.log(_log_level, "Git refresh failed — marking repo as unavailable: %s", exc)
        state._refresh_error_logged = True
    except OSError as exc:
        state.is_git_repo = False
        state._last_git_error = str(exc)
        if exc.errno in (errno.EACCES, errno.EPERM):
            _log_level = logging.WARNING if not state._refresh_error_logged else logging.DEBUG
            logger.log(_log_level, "Git refresh permission denied: %s", exc)
        else:
            logger.debug("Git refresh failed (OSError): %s", exc)
        state._refresh_error_logged = True


def _compute_file_diff(filepath: str, repo_path: str) -> str | None:
    """Compute ``git diff -- <filepath>`` for a single file.

    Returns the unified-diff string or ``None`` on any error.
    Must only be called after ``_refresh_git_state`` has confirmed the
    repository is accessible.
    """
    try:
        import subprocess
        _repo = Path(repo_path).resolve()
        _fp = Path(filepath).resolve()
        try:
            _fp = _fp.relative_to(_repo)
        except ValueError:
            # filepath is outside the repo — still try git diff
            _fp = Path(filepath)
        result = subprocess.run(
            ["git", "diff", "--", str(_fp)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(_repo), timeout=5,
        )
        diff = result.stdout.strip()
        return diff if diff else None
    except Exception:
        return None


class ReActAgent:
    """
    ReAct 主循环实现。

    用法：
        agent = ReActAgent(backend, registry, config)
        result = agent.run(task, log)

    这是一个纯粹的 ReAct agent：每步 思考→行动→观察，循环直到完成或超限。
    入口通过 SessionRuntime + agent_name 区分权限和可见性。
    """

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        config: AgentConfig | None = None,
        memory_context: "MemoryContext | None" = None,
        session_memory_tracker: "SessionMemoryTracker | None" = None,
        controller_factory: "type | None" = None,
        state_machine: "TaskStateMachine | None" = None,
        inherited_context: ConversationSnapshot | None = None,
        prompt_renderer: PromptRenderer | None = None,
    ) -> None:
        self._backend = backend
        self._full_registry = registry
        self._registry = registry
        self._cfg = config or AgentConfig()
        self._controller_factory = controller_factory  # injected by AgentFactory
        self._memory_context = memory_context
        self._session_memory_tracker = session_memory_tracker
        self._state_machine = state_machine  # Runtime-centric: TSM is the SSOT for task lifecycle
        self._prompt_renderer = prompt_renderer
        self._prompt_renderer_is_injected = prompt_renderer is not None
        if inherited_context is not None:
            if not isinstance(inherited_context, ConversationSnapshot):
                raise TypeError("inherited_context must be a ConversationSnapshot")
        self._inherited_context = inherited_context
        self._artifact_store = ArtifactStore(
            threshold_tokens=self._cfg.artifact_threshold_tokens,
        )
        artifact_store_ref = getattr(registry, "_artifact_store_ref", None)
        if artifact_store_ref is not None:
            artifact_store_ref.store = self._artifact_store
        evidence_ledger_ref = getattr(registry, "_evidence_ledger_ref", None)
        if evidence_ledger_ref is not None:
            evidence_ledger_ref.ledger = None
        self._context_manager = ContextManager(ContextManagerConfig(
            request_budget_tokens=self._cfg.request_budget_tokens,
            history_max_messages=self._cfg.history_max_messages,
            compact_history=self._cfg.compact_history,
            enable_caching=False,  # updated per-request in _build_messages
        ))
        self._session_context: str | None = None  # set by ChatSession per round

    @property
    def step_count(self) -> int:
        """返回当前执行的步数（第几步）。"""
        return getattr(self, "_current_step", 0)

    def reset_context_planning(self) -> None:
        """Reset planner thrash protection after explicit user interaction."""
        self._context_manager.planner.reset_compaction_series()

    @property
    def artifact_store(self) -> ArtifactStore:
        return self._artifact_store

    def _require_prompt_renderer(self) -> PromptRenderer:
        """Return this run's renderer without consulting active project globals."""
        renderer = getattr(self, "_prompt_renderer", None)
        if renderer is None:
            renderer = create_prompt_renderer(
                getattr(self, "_current_repo_path", None),
                self._cfg.prompt_config,
            )
            self._prompt_renderer = renderer
        return renderer

    def _load_project_instructions(self, repo_path: str) -> str:
        """Discover and load CLAUDE.md project instructions.

        Loaded once per run; subsequent calls return the cached text.
        Returns ``""`` if no CLAUDE.md files exist.
        """
        # Invalidate project instructions cache when repo path changes
        if getattr(self, "_project_instructions_repo", "") != repo_path:
            if hasattr(self, "_project_instructions"):
                del self._project_instructions
            self._project_instructions_repo = repo_path
        if not hasattr(self, "_project_instructions"):
            from context.claude_md import load
            self._project_instructions = load(repo_path)
        return self._project_instructions

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    @property
    def _circuit_breaker_tripped(self) -> bool:
        """Check if the permission pipeline's circuit breaker has tripped.

        CC-aligned: when 3 consecutive denials or 20 total denials occur
        in headless Web mode, the permission layer emits a termination signal
        and the agent loop should force GIVE_UP immediately.
        """
        signal = self._full_registry.permission_control_signal()
        return bool(signal and signal.terminate_session)

    def run(self, task: Task, log: EventLog) -> RunResult:
        """
        执行一次完整的 agent 运行。

        Args:
            task: 任务描述
            log:  已初始化的 EventLog（由调用方创建并传入）

        Returns:
            RunResult，包含最终状态和统计信息
        """
        self._current_repo_path = task.repo_path
        from core.state_paths import ProjectStatePaths, StateIsolationError
        _state_paths = ProjectStatePaths.for_project(task.repo_path)
        _configured_artifacts = Path(self._cfg.artifact_storage_dir).expanduser()
        if self._cfg.artifact_storage_dir and _configured_artifacts.is_absolute():
            _artifact_dir = _configured_artifacts.resolve()
            try:
                _artifact_dir.relative_to(Path(task.repo_path).resolve())
            except ValueError:
                pass
            else:
                raise StateIsolationError(
                    f"artifact storage must be outside project: {_artifact_dir}"
                )
        else:
            _artifact_dir = _state_paths.artifacts
        self._artifact_store.set_storage_dir(_artifact_dir)
        self._current_task_description = task.description
        self._current_task_metadata = dict(task.metadata or {})
        self._task_intent = task.intent
        if not self._prompt_renderer_is_injected:
            self._prompt_renderer = create_prompt_renderer(
                task.repo_path,
                self._cfg.prompt_config,
            )

        # ── Policy enforcement ─────────
        policy = build_task_policy(task)
        if task.explicit_read_paths is None and policy.execution.allowed_read_paths is not None:
            task.explicit_read_paths = policy.execution.allowed_read_paths
        previous_registry = self._registry
        with_phase_policy = getattr(previous_registry, "with_phase_policy", None)
        if callable(with_phase_policy):
            self._registry = with_phase_policy(policy.execution)
        else:
            from core.policy_registry import PolicyAwareToolRegistry
            self._registry = PolicyAwareToolRegistry(
                base=previous_registry,
                phase_policy=policy.execution,
                repo_path=task.repo_path,
                phase_name="task_execution",
                base_allowed_tools=frozenset(previous_registry.tool_names),
            )
        try:
            return self._run_body(task, log, policy=policy)
        finally:
            self._registry = previous_registry
            # Close the Langfuse observation span on any exit path.
            # The normal-path close in _build_run_result only fires on
            # structured completion, not on unhandled exceptions.
            _task_ctx = getattr(self, "_active_task_context", None)
            if _task_ctx is not None:
                try:
                    _task_ctx.__exit__(None, None, None)
                except Exception:
                    pass
            self._active_task_context = None
            # Clear accumulated findings even on exception so stale
            # findings never leak into a subsequent run() call.
            self._accumulated_structured_findings.clear()

    def _build_run_result(
        self,
        *,
        status: RunStatus,
        summary: str,
        steps_taken: int,
        total_tokens_used: int,
        ctx: _FinishRunContext,
        patch: str | None = None,
        error: str | None = None,
        cache_stats: CacheStats | None = None,
        completion_blocked: int = 0,
    ) -> RunResult:
        """Build the final RunResult from the completion context (P1-2).

        Extracted from the ``_finish_run`` closure inside ``_run_body()``.
        Uses ``ctx`` (a ``_FinishRunContext``) instead of implicitly captured
        variables, making the method independently testable.
        """
        # ── Refresh objective workspace facts for the completion record ──
        # The git diff is available via RunResult.patch (set by the caller
        # from _git_state.current_diff).  Do NOT concatenate it into
        # summary — that is a display concern that pollutes the data layer
        # and forces every downstream consumer to strip it.
        _refresh_git_state(ctx.git_state, ctx.task.repo_path)

        # Verification outcome is read from TSM — the single source of
        # truth, computed in the FINISH path (lines 1546-1585).
        # _FinishRunContext contains only reference types; value-type
        # fields that change during execution are explicit parameters.
        _v_needs_tag = ctx.tsm.verification_status in (
            VerificationStatus.UNVERIFIED,
            VerificationStatus.UNAVAILABLE,
            VerificationStatus.FAILED,
        )
        _needs_unverified_tag = ctx.git_state.has_changes or (
            ctx.completion_ctx.had_any_write and not ctx.git_state.is_git_repo
        )
        if status == RunStatus.SUCCESS and _needs_unverified_tag and _v_needs_tag:
            _reason = ctx.tsm.verification_reason
            if _reason == VerificationReason.NO_TEST_ENVIRONMENT:
                _tag = "UNVERIFIED — no test environment available"
            elif _reason == VerificationReason.NO_VERSION_CONTROL:
                _tag = "UNVERIFIED — project has no Git fact source"
            elif _reason == VerificationReason.TEST_FAILED:
                _tag = "UNVERIFIED — tests ran but failed"
            else:
                _tag = "UNVERIFIED — test/validation did not run or was unavailable"
            summary = (
                f"[{_tag}. "
                f"Code changes were made but NOT independently verified.]\n\n"
                f"{summary}"
            )

        result = RunResult(
            task_id=ctx.task.task_id,
            status=status,
            summary=summary,
            steps_taken=steps_taken,
            total_tokens=total_tokens_used,
            patch=patch,
            error=error,
            cache_stats=cache_stats,
            contract=self._accumulated_plan_contract,
            termination_reason=ctx.tsm.termination_reason,
            verification_status=ctx.tsm.verification_status,
            verification_reason=ctx.tsm.verification_reason,
            completion_blocked=completion_blocked,
        )
        run_stats = summarize_run(ctx.log)
        analysis_metadata = build_analysis_run_metadata(
            run_stats=run_stats,
            context_stats=getattr(self, "_last_context_stats", None),
        )
        ctx.task_obs.update(
            output=build_run_output(result),
            metadata=merge_metadata(
                build_run_metadata(result),
                {
                    "reflections": ctx.reflection_counts,
                    "consecutive_failures": ctx.get_consecutive_failures(),
                },
                analysis_metadata,
            ),
        )
        for score in build_run_scores(ctx.task, result, stats=run_stats):
            ctx.task_obs.score(
                name=score.name,
                value=score.value,
                comment=score.comment,
                metadata=score.metadata,
            )
        append_failure_dataset_item(ctx.task, result, log_path=ctx.log.path, stats=run_stats)
        if not ctx.task_obs_closed:
            ctx.task_context.__exit__(None, None, None)
            ctx.task_obs_closed = True
        # First-party stats: record session end
        _sc2 = self._cfg.stats_collector
        if _sc2 is not None:
            try:
                _sc2.record_session_end(
                    self._cfg.stats_session_id,
                    agent_name=self._cfg.stats_agent_name,
                    total_steps=steps_taken,
                    total_tokens=total_tokens_used,
                    status=status.value if hasattr(status, 'value') else str(status),
                    completion_blocked=completion_blocked,
                )
            except Exception:
                pass
        return result

    def _run_body(self, task: Task, log: EventLog, *, policy: TaskPolicy) -> RunResult:
        """核心循环：所有 return 路径都走这里，由 run() 负责策略包裹和恢复。"""
        setup = self._initialize_run(task, log, policy)
        observer = setup.observer
        history = setup.history
        _caps = setup.capabilities
        token_budget = setup.token_budget
        repo_map = setup.repo_map
        _git_state = setup.git_state
        _block_tracker = setup.block_tracker
        _get_consecutive_failures = setup.get_consecutive_failures
        _max_consecutive_failures = setup.max_consecutive_failures
        reflection_counts = setup.reflection_counts
        completion_ctx = setup.completion_context
        completion_guard = setup.completion_guard
        _execution_budget = setup.execution_budget
        _cancellation = setup.cancellation
        _base_run_context = setup.base_run_context
        _runtime_controller = setup.runtime_controller
        _tsm = setup.state_machine
        _finish_ctx = setup.finish_context
        _state = setup.state

        _completion_blocked = 0
        total_tokens = 0
        _verification_ok = False
        _test_was_run = False
        missing_test_target_followups: int | None = None
        missing_test_target_message: str | None = None
        missing_test_target_detected_step: int | None = None
        _cumulative_tool_calls = 0
        cumulative_cache = CacheStats()
        self._context_trimming_state = ContextTrimmingState()

        for step in range(1, task.max_steps + 1):
            step_gate = self._prepare_step(
                state=_state,
                history=history,
                task=task,
                log=log,
                cancellation=_cancellation,
                runtime_controller=_runtime_controller,
                state_machine=_tsm,
                finish_context=_finish_ctx,
                cache_stats=cumulative_cache,
                execution_budget=_execution_budget,
                get_consecutive_failures=_get_consecutive_failures,
                total_tokens=total_tokens,
                step=step,
            )
            _state = step_gate.state
            if step_gate.result is not None:
                return step_gate.result
            decision = step_gate.decision
            provider_phase = self._run_provider_phase(
                decision=decision,
                state=_state,
                history=history,
                task=task,
                log=log,
                state_machine=_tsm,
                finish_context=_finish_ctx,
                cache_stats=cumulative_cache,
                token_budget=token_budget,
                repo_map=repo_map,
                base_run_context=_base_run_context,
                execution_budget=_execution_budget,
                total_tokens=total_tokens,
                cumulative_tool_calls=_cumulative_tool_calls,
                step=step,
            )
            _state = provider_phase.state
            total_tokens = provider_phase.total_tokens
            _cumulative_tool_calls = provider_phase.cumulative_tool_calls
            if provider_phase.result is not None:
                return provider_phase.result
            if provider_phase.retry_loop:
                continue
            action = provider_phase.action
            assert action is not None
            response = provider_phase.response
            tools = list(provider_phase.tools)
            _live_spawn_context = provider_phase.spawn_context
            _streaming_executor = provider_phase.streaming_executor

            if action.is_terminal():
                terminal = self._handle_terminal_action(
                    action=action,
                    state=_state,
                    history=history,
                    task=task,
                    log=log,
                    state_machine=_tsm,
                    finish_context=_finish_ctx,
                    cache_stats=cumulative_cache,
                    execution_budget=_execution_budget,
                    completion_guard=completion_guard,
                    completion_context=completion_ctx,
                    block_tracker=_block_tracker,
                    git_state=_git_state,
                    pytest_available=_caps.pytest_available,
                    verification_ok=_verification_ok,
                    test_was_run=_test_was_run,
                    completion_blocked=_completion_blocked,
                    step=step,
                    total_tokens=total_tokens,
                )
                _state = terminal.state
                _completion_blocked = terminal.completion_blocked
                if terminal.result is not None:
                    return terminal.result
                if terminal.continue_loop:
                    continue

            # ── 5. 执行工具（支持并行 tool_calls）───────────────────────
            if action.action_type == ActionType.TOOL_CALL and action.tool_calls:
                tool_batch = self._execute_tool_batch(
                    action=action,
                    base_run_context=_base_run_context,
                    spawn_context=_live_spawn_context,
                    streaming_executor=_streaming_executor,
                    observer=observer,
                    task=task,
                    log=log,
                    state_machine=_tsm,
                    finish_context=_finish_ctx,
                    cache_stats=cumulative_cache,
                    completion_context=completion_ctx,
                    execution_budget=_execution_budget,
                    git_state=_git_state,
                    step=step,
                    total_tokens=total_tokens,
                    cancellation=_cancellation,
                )
                if tool_batch.result is not None:
                    return tool_batch.result
                # Check cancellation after tool batch — long-running tool
                # executions may have completed during a cancel request.
                if _cancellation.is_cancelled:
                    return self._build_run_result(
                        ctx=_finish_ctx,
                        status=RunStatus.CANCELLED,
                        summary=_cancellation.detail or "Cancelled during execution",
                        steps_taken=step,
                        total_tokens_used=total_tokens,
                        cache_stats=cumulative_cache,
                    )
                _test_was_run = (
                    _test_was_run or tool_batch.test_was_run
                )
                _verification_ok = (
                    _verification_ok or tool_batch.verification_ok
                )
                tool_turn = self._finish_tool_turn(
                    action=action,
                    tool_batch=tool_batch,
                    decision=decision,
                    visible_tools=tools,
                    state=_state,
                    history=history,
                    task=task,
                    log=log,
                    finish_context=_finish_ctx,
                    cache_stats=cumulative_cache,
                    get_consecutive_failures=_get_consecutive_failures,
                    max_consecutive_failures=_max_consecutive_failures,
                    reflection_counts=reflection_counts,
                    missing_message=missing_test_target_message,
                    missing_followups=missing_test_target_followups,
                    missing_detected_step=missing_test_target_detected_step,
                    total_tokens=total_tokens,
                    step=step,
                )
                _state = tool_turn.state
                missing_test_target_followups = (
                    tool_turn.missing_followups
                )
                missing_test_target_message = tool_turn.missing_message
                missing_test_target_detected_step = (
                    tool_turn.missing_detected_step
                )
                if tool_turn.result is not None:
                    return tool_turn.result
                if tool_turn.continue_loop:
                    continue

            else:
                # ── Replay step record: non-tool-call turn ──
                log.log_replay_step(build_replay_step_record(
                    step=step,
                    decision=decision,
                    visible_tools=list(tools),
                    action=action,
                    outcome="continue",
                ))
                non_tool_result = self._handle_non_tool_action(
                    action=action,
                    history=history,
                    task=task,
                    log=log,
                    state_machine=_tsm,
                    finish_context=_finish_ctx,
                    cache_stats=cumulative_cache,
                    step=step,
                    total_tokens=total_tokens,
                )
                if non_tool_result is not None:
                    return non_tool_result

        # ── 7. 超出步数上限（参考 Claude Code max_turns_reached）────────
        # Claude Code:
        #   return { reason: 'max_turns', turnCount: nextTurnCount };
        # 从 history 提取已收集的信息作为最终结果。
        _tsm.fail(TerminationReason.MAX_STEPS, "max steps exceeded")
        summary = self._extract_summary_from_history(history)
        log.log_task_failed(steps=task.max_steps, reason="max_steps")
        return self._build_run_result(ctx=_finish_ctx,
            status=RunStatus.MAX_STEPS,
            summary=summary,
            steps_taken=task.max_steps,
            total_tokens_used=total_tokens,
            patch=_git_state.current_diff or None,
            cache_stats=cumulative_cache,
        )

    def _initialize_run(
        self,
        task: Task,
        log: EventLog,
        policy: TaskPolicy,
    ) -> _RunSetup:
        """Assemble run-scoped resources before entering the transition loop."""
        self._active_policy = policy
        if getattr(self, "_repo_map_cache_key", None) != task.repo_path:
            if hasattr(self, "_repo_map_cache"):
                del self._repo_map_cache
            self._repo_map_cache_key = task.repo_path
        if self._memory_context:
            self._memory_context.set_task_context(task.description)
        if hasattr(self, "_long_term_context"):
            del self._long_term_context
        self._build_long_term_context()

        self._accessed_files = set()
        self._feedback_injected_files = set()
        self._explicit_memory_write_this_run = False
        self._evidence_ledger = EvidenceLedger()
        evidence_ref = getattr(
            self._full_registry,
            "_evidence_ledger_ref",
            None,
        )
        if evidence_ref is not None:
            evidence_ref.ledger = self._evidence_ledger
        self._accumulated_structured_findings = []
        self._accumulated_plan_contract = None

        observer = get_observer()
        task_context = observer.start_task(task)
        task_observation = task_context.__enter__()
        # Store on self so run()'s finally block can close the span
        # even when _run_body throws an unhandled exception.
        self._active_task_context = task_context
        log.log_task_start(task)
        logger.info("Agent starting task %s", task.task_id)

        history = getattr(self, "_pending_history", None)
        _had_pending_history = history is not None
        self._pending_history = None  # Consume once, never reuse across run() calls
        if history is None:
            history = ConversationHistory(
                max_messages=self._cfg.history_max_messages,
            )
            history.add(LLMMessage(
                role="user",
                content=self._require_prompt_renderer().task(
                    task.description,
                    task.repo_path,
                    task.issue_url,
                    intent=self._task_intent,
                ),
            ))
        capabilities = CapabilitySnapshot.probe(task.repo_path)
        # Only inject capabilities when building a fresh history.
        # If _pending_history was provided (session resume), capabilities
        # were already injected in the original run.
        if not _had_pending_history:
            history.add(LLMMessage(
                role="user",
                content=capabilities.render_for_agent(),
            ))
        logger.info("Capability: %s", capabilities.render_for_agent())

        stats_collector = self._cfg.stats_collector
        if stats_collector is not None:
            try:
                stats_collector.record_session_start(
                    self._cfg.stats_session_id,
                    self._cfg.stats_agent_name,
                )
            except Exception:
                pass

        def get_consecutive_failures() -> int:
            breaker = self._cfg.circuit_breaker
            return (
                getattr(breaker, "_consecutive_tool_errors", 0)
                if breaker is not None
                else 0
            )

        breaker = self._cfg.circuit_breaker
        max_failures = (
            breaker.config.max_consecutive_tool_errors
            if breaker is not None
            else 3
        )
        completion_context = CompletionContext()
        completion_guard = TaskCompletionGuard()

        from agent.session.execution_budget import (
            ExecutionBudget,
            ExecutionBudgetConfig,
        )
        from agent.session.run_context import CancellationToken, RunContext

        execution_budget = ExecutionBudget(
            config=ExecutionBudgetConfig(
                token_limit=task.budget_tokens,
                step_limit=task.max_steps,
            ),
        )
        execution_budget.start()
        cancellation = self._cfg.cancellation_token or CancellationToken()
        delegation_effects = {ToolEffect.PRODUCE_DELIVERABLE}
        for tool_name in self._registry.tool_names:
            metadata = self._registry.metadata_for(tool_name)
            if metadata is not None and ToolRole.DELEGATE not in metadata.roles:
                delegation_effects.update(metadata.effects)
        base_run_context = RunContext(
            budget=execution_budget,
            cancellation=cancellation,
            delegation_step_limit=task.max_steps,
            phase_policy=policy.execution,
            delegation_effects=frozenset(delegation_effects),
        )
        self._registry = self._registry.with_run_context(base_run_context)

        controller_class = self._controller_factory or RuntimeController
        runtime_controller = controller_class(
            budget=execution_budget,
            breaker=breaker,
            max_steps=task.max_steps,
            budget_tokens=task.budget_tokens,
            max_consecutive_failures=max_failures,
        )
        state_machine = self._state_machine
        if state_machine is None:
            from agent.session.task_state_machine import TaskStateMachine

            state_machine = TaskStateMachine(task_id=task.task_id)
        else:
            state_machine.task_id = task.task_id
        from agent.session.task_state_machine import (
            GuardTransition,
            TaskState,
            circuit_breaker_guard,
            consecutive_failures_guard,
            git_diff_guard,
            stop_hook_retry_guard,
        )

        state_machine.add_guard(
            GuardTransition.RUNNING_TO_FAILED,
            circuit_breaker_guard,
        )
        state_machine.add_guard(
            GuardTransition.RUNNING_TO_FAILED,
            consecutive_failures_guard,
        )
        state_machine.add_guard(
            GuardTransition.COMPLETING_TO_COMPLETED,
            git_diff_guard,
        )
        state_machine.add_guard(
            GuardTransition.COMPLETING_TO_FAILED,
            stop_hook_retry_guard,
        )

        git_state = _capture_git_state(task.repo_path)
        reflection_counts: dict[str, int] = {}
        finish_context = _FinishRunContext(
            git_state=git_state,
            task=task,
            completion_ctx=completion_context,
            tsm=state_machine,
            reflection_counts=reflection_counts,
            get_consecutive_failures=get_consecutive_failures,
            log=log,
            task_obs=task_observation,
            task_context=task_context,
        )
        LocalRuntime().setup_workspace(task.repo_path)
        state_machine.transition(TaskState.RUNNING, "workspace ready")
        return _RunSetup(
            observer=observer,
            history=history,
            capabilities=capabilities,
            token_budget=TokenBudget(
                total=self._cfg.request_budget_tokens,
            ),
            repo_map=RepoMap(task.repo_path),
            git_state=git_state,
            block_tracker=CompletionBlockTracker(
                threshold=COMPLETION_BLOCK_THRESHOLD,
            ),
            get_consecutive_failures=get_consecutive_failures,
            max_consecutive_failures=max_failures,
            reflection_counts=reflection_counts,
            completion_context=completion_context,
            completion_guard=completion_guard,
            execution_budget=execution_budget,
            cancellation=cancellation,
            base_run_context=base_run_context,
            runtime_controller=runtime_controller,
            state_machine=state_machine,
            finish_context=finish_context,
            state=AgentTurnState(turn_count=0),
        )

    def _prepare_step(
        self,
        *,
        state: AgentTurnState,
        history: ConversationHistory,
        task: Task,
        log: EventLog,
        cancellation: Any,
        runtime_controller: Any,
        state_machine: Any,
        finish_context: _FinishRunContext,
        cache_stats: CacheStats,
        execution_budget: Any,
        get_consecutive_failures: Callable[[], int],
        total_tokens: int,
        step: int,
    ) -> _StepGateApplication:
        """Apply pre-step hooks, runtime messages, and enforcement gates."""
        if step > 1:
            self._dispatch_post_response(history, step - 1)

        early_gate = evaluate_early_step_gate(
            step=step,
            cancellation_requested=cancellation.is_cancelled,
            cancellation_detail=cancellation.detail,
            permission_circuit_tripped=getattr(
                self,
                "_circuit_breaker_tripped",
                False,
            ),
        )
        if early_gate.outcome is PreStepOutcome.TERMINATE:
            return _StepGateApplication(
                state=state,
                result=self._finish_pre_step(
                    early_gate,
                    state_machine=state_machine,
                    log=log,
                    finish_context=finish_context,
                    total_tokens=total_tokens,
                    cache_stats=cache_stats,
                ),
            )

        state_machine.record_step()
        self._current_step = step
        self._context_manager.planner.tick_step()
        logger.debug("Step %d/%d", step, task.max_steps)

        runtime_messages: list[LLMMessage] = []
        if self._cfg.runtime_message_source is not None:
            try:
                runtime_messages = self._cfg.runtime_message_source()
                history.add_many(runtime_messages)
            except (ValueError, TypeError, RuntimeError) as exc:
                logger.warning("Failed to load Runtime messages: %s", exc)
        state = state.with_updates(
            child_turn_phase=_advance_child_turn_phase(
                state.child_turn_phase,
                runtime_phase=_phase_from_runtime_messages(runtime_messages),
            ),
        )

        last_stats = getattr(self, "_last_context_stats", None)
        context_size = (
            last_stats.estimated_total_tokens if last_stats else 0
        )
        request_budget = (
            last_stats.request_budget_tokens
            if last_stats
            else self._backend.max_context_window
        )
        from agent.session.task_state_machine import (
            GuardContext,
            GuardTransition,
        )

        guard_context = GuardContext(
            step=step,
            max_steps=task.max_steps,
            consecutive_failures=get_consecutive_failures(),
            task_intent=task.intent,
            budget=execution_budget,
            breaker=self._cfg.circuit_breaker,
            tsm=state_machine,
        )
        runtime_gate = evaluate_runtime_step_gate(
            step=step,
            controller_check=lambda: runtime_controller.check(
                step=step,
                total_tokens=total_tokens,
                history=history,
                log=log,
                context_size=context_size,
                request_budget=request_budget,
                consecutive_failures=get_consecutive_failures(),
            ),
            guard_check=lambda: state_machine.evaluate_guards(
                GuardTransition.RUNNING_TO_FAILED,
                guard_context,
            ),
        )
        if runtime_gate.outcome is PreStepOutcome.TERMINATE:
            return _StepGateApplication(
                state=state,
                result=self._finish_pre_step(
                    runtime_gate,
                    state_machine=state_machine,
                    log=log,
                    finish_context=finish_context,
                    total_tokens=total_tokens,
                    cache_stats=cache_stats,
                ),
            )
        return _StepGateApplication(
            state=state,
            decision=runtime_gate.decision,
        )

    def _run_provider_phase(
        self,
        *,
        decision: Any,
        state: AgentTurnState,
        history: ConversationHistory,
        task: Task,
        log: EventLog,
        state_machine: Any,
        finish_context: _FinishRunContext,
        cache_stats: CacheStats,
        token_budget: TokenBudget,
        repo_map: RepoMap,
        base_run_context: Any,
        execution_budget: Any,
        total_tokens: int,
        cumulative_tool_calls: int,
        step: int,
    ) -> _ProviderPhaseApplication:
        """Prepare, invoke, recover, and accept one provider turn."""
        trim_result = prepare_history_for_turn(
            history,
            self.compactor,
            step=step,
            enabled=self._cfg.compact_history,
            history_budget=(
                self._cfg.request_budget_tokens
                or DEFAULT_HISTORY_BUDGET_TOKENS
            ),
            state=self._context_trimming_state,
            planner=self._context_manager.planner,
        )
        self._trim_tokens_freed = trim_result.tokens_freed

        if decision.inject_message:
            history.add(LLMMessage(
                role="user",
                content=decision.inject_message,
            ))

        if self._memory_context:
            last_user_msg = history.get_last_user_message()
            if last_user_msg:
                self._memory_context.set_user_message(last_user_msg)
            # Track turn_id for recall tracing
            sid = str(task.metadata.get("session_id") or task.task_id)
            self._memory_context.set_turn_id(f"{sid}-step-{step}")

        messages = self._build_messages(
            history,
            token_budget,
            repo_map,
            consumed_tokens=total_tokens,
            max_context_window=self._backend.max_context_window,
            step=step,
        )
        provider_request = prepare_provider_request(
            messages=messages,
            history_messages=history.messages,
            registry=self._registry,
            execution_context=base_run_context,
            state=state,
            step=step,
            total_tokens=total_tokens,
            strip_tools=decision.strip_tools,
            child_phase_active=(
                state.child_turn_phase is not _ChildTurnPhase.NONE
            ),
            parent_session_id=str(
                task.metadata.get("session_id") or task.task_id
            ),
            parent_agent_name=str(
                task.metadata.get("agent_name") or "primary"
            ),
            repo_path=task.repo_path,
            model_name=self._backend.model_name,
        )
        state = provider_request.state
        prepared_turn = provider_request.turn
        try:
            provider_turn = invoke_provider_turn(
                prepared_turn,
                streaming=self._cfg.streaming_tool_execution,
                stream_call=self._stream_and_dispatch,
                complete_call=self._call_with_retry,
            )
        except Exception as exc:
            provider_error = evaluate_provider_error(
                exc,
                state=state,
                streaming=self._cfg.streaming_tool_execution,
                recover=lambda error, current_state: self._recover_from_llm_error(
                    error,
                    step,
                    history,
                    total_tokens,
                    current_state,
                ),
            )
            state = provider_error.state
            if provider_error.outcome is ProviderErrorOutcome.RETRY:
                return _ProviderPhaseApplication(
                    state=state,
                    total_tokens=total_tokens,
                    cumulative_tool_calls=cumulative_tool_calls,
                    retry_loop=True,
                )
            logger.error(
                "LLM %s failed at step %d after retries: %s",
                provider_error.call_kind,
                step,
                exc,
            )
            state_machine.fail(
                provider_error.termination_reason,
                f"LLM error: {exc}",
            )
            log.log_task_failed(steps=step, reason=f"LLM error: {exc}")
            result = self._build_run_result(
                ctx=finish_context,
                status=RunStatus.FAILED,
                summary=f"LLM {provider_error.call_kind} failed: {exc}",
                steps_taken=step,
                total_tokens_used=total_tokens,
                error=str(exc),
                cache_stats=cache_stats,
            )
            return _ProviderPhaseApplication(
                state=state,
                total_tokens=total_tokens,
                cumulative_tool_calls=cumulative_tool_calls,
                result=result,
            )

        if (
            provider_turn.cache_stats is not None
            and provider_turn.cache_stats.has_cache_activity
        ):
            cache_stats.cache_read_tokens += (
                provider_turn.cache_stats.cache_read_tokens
            )
            cache_stats.cache_creation_tokens += (
                provider_turn.cache_stats.cache_creation_tokens
            )
            cache_stats.non_cached_input_tokens += (
                provider_turn.cache_stats.non_cached_input_tokens
            )

        # Use stream-reported tokens when available (precise provider counts
        # from the FINISH chunk), fall back to estimate for old backends.
        _billable = provider_turn.billable_tokens
        if self._cfg.streaming_tool_execution:
            _stream_usage = getattr(self, "_stream_usage", None)
            if _stream_usage is not None and _stream_usage[0] > 0:
                _billable = _stream_usage[0] + _stream_usage[1]

        total_tokens += _billable
        execution_budget.consume(_billable)
        execution_budget.record_step()
        if self._cfg.token_callback is not None:
            self._cfg.token_callback(total_tokens)

        output_recovery = evaluate_output_recovery(
            provider_turn,
            state=state,
            current_max_tokens=getattr(
                self._cfg,
                "max_tokens",
                DEFAULT_MAX_OUTPUT_TOKENS,
            ),
            escalated_max_tokens=RecoveryState._ESCALATED_MAX_TOKENS,
            truncation_buffer_tokens=TRUNCATION_BUFFER_TOKENS,
        )
        state = output_recovery.state
        if output_recovery.outcome is OutputRecoveryOutcome.RETRY:
            if self._cfg.max_tokens != output_recovery.max_tokens:
                logger.info(
                    "Output truncated — escalating max_tokens to %d",
                    output_recovery.max_tokens,
                )
                self._cfg.max_tokens = output_recovery.max_tokens
            if output_recovery.inject_message:
                history.add(LLMMessage(
                    role="user",
                    content=output_recovery.inject_message,
                ))
            return _ProviderPhaseApplication(
                state=state,
                total_tokens=total_tokens,
                cumulative_tool_calls=cumulative_tool_calls,
                retry_loop=True,
            )
        if output_recovery.exhausted:
            logger.warning(
                "Output recovery exhausted after %d attempts",
                RecoveryState._MAX_OUTPUT_RECOVERY,
            )

        action = provider_turn.action
        tools = tuple(prepared_turn.tools)
        accepted_action = self._accept_provider_action(
            action=action,
            tools=list(tools),
            response=provider_turn.response,
            state=state,
            history=history,
            task=task,
            log=log,
            step=step,
            total_tokens=total_tokens,
            cumulative_tool_calls=cumulative_tool_calls,
        )
        return _ProviderPhaseApplication(
            state=accepted_action.state,
            total_tokens=total_tokens,
            cumulative_tool_calls=accepted_action.cumulative_tool_calls,
            action=action,
            response=provider_turn.response,
            tools=tools,
            spawn_context=provider_request.spawn_context,
            streaming_executor=provider_turn.streaming_executor,
            retry_loop=accepted_action.retry_loop,
        )

    def _accept_provider_action(
        self,
        *,
        action: Action,
        tools: list[LLMToolSchema],
        response: Any,
        state: AgentTurnState,
        history: ConversationHistory,
        task: Task,
        log: EventLog,
        step: int,
        total_tokens: int,
        cumulative_tool_calls: int,
    ) -> _AcceptedActionApplication:
        """Validate, persist, and account for one provider action."""
        contract = validate_action_contract(
            action,
            tools,
            task_id=task.task_id,
            step=step,
        )
        if contract.status is ActionContractStatus.TOOLS_DISABLED:
            history.add(LLMMessage(
                role="user",
                content=(
                    "[SYSTEM] Tool calls are disabled for this finalization "
                    "turn. Return the requested final answer directly "
                    "without tools."
                ),
            ))
            state = state.with_updates(
                transition=Transition.completion_blocked(
                    "tools_disabled_for_finalization",
                ),
            )
            return _AcceptedActionApplication(
                state,
                cumulative_tool_calls,
                retry_loop=True,
            )
        if contract.status is ActionContractStatus.INVALID:
            logger.warning(
                "Control plane rejected tool call: %s — %s",
                contract.error_type,
                contract.error_message,
            )
            log.log_action(
                step=step,
                action=action,
                raw_content=getattr(response, "raw_content", ""),
            )
            history.add_many(build_action_history(
                action,
                [contract.observation],
                supports_function_calling=(
                    self._backend.supports_function_calling
                ),
                render_action=self._format_action_for_history,
                render_observations=self._format_observations_for_history,
                render_tool_result=self._build_tool_result_content,
            ))
            return _AcceptedActionApplication(
                state,
                cumulative_tool_calls,
                retry_loop=True,
            )

        if action.action_type is ActionType.TOOL_CALL and action.tool_calls:
            cumulative_tool_calls += len(action.tool_calls)
        if self._session_memory_tracker:
            context_summary = (
                self._build_session_memory_context(history)
                if history
                else ""
            )
            recent_files = (
                sorted(self._accessed_files)[-RECENT_FILES_WINDOW:]
                if self._accessed_files
                else []
            )
            self._session_memory_tracker.tick(
                current_tokens=total_tokens,
                current_tool_calls=cumulative_tool_calls,
                context_summary=context_summary,
                recent_files=recent_files,
            )
        # Feed active files and recent tools into MemoryContext so recall
        # scoring can match on the agent's runtime context.
        if self._memory_context and self._memory_context.enabled:
            if action.action_type is ActionType.TOOL_CALL and action.tool_calls:
                for tc in action.tool_calls:
                    name = getattr(tc, "name", "") or (tc.get("name") if isinstance(tc, dict) else "")
                    if name:
                        self._memory_context.add_recent_tool(name)
            self._memory_context.set_active_files(
                getattr(self, "_accessed_files", None) or set()
            )
        try:
            log.log_action(
                step=step,
                action=action,
                raw_content=getattr(response, "raw_content", ""),
            )
        except Exception as _log_exc:
            logger.critical("Event log write failed at step %d: %s", step, _log_exc)
            raise RuntimeError(
                f"Critical: event log I/O failure at step {step}"
            ) from _log_exc
        logger.info("Step %d: %r", step, action)
        return _AcceptedActionApplication(
            state,
            cumulative_tool_calls,
        )

    def _handle_non_tool_action(
        self,
        *,
        action: Action,
        history: ConversationHistory,
        task: Task,
        log: EventLog,
        state_machine: Any,
        finish_context: _FinishRunContext,
        cache_stats: CacheStats,
        step: int,
        total_tokens: int,
    ) -> RunResult | None:
        """Apply actions that contain no executable tool calls."""
        if action.action_type is ActionType.REFLECTION:
            history.add(LLMMessage(
                role="assistant",
                content=action.thought,
            ))
            return None

        is_empty_tool_call = action.action_type is ActionType.TOOL_CALL
        detail = (
            "LLM returned empty tool_calls — finishing"
            if is_empty_tool_call
            else f"unknown action_type={action.action_type}"
        )
        if is_empty_tool_call:
            logger.info(
                "LLM returned TOOL_CALL with no tool_calls at step %d — finishing",
                step,
            )
        else:
            logger.warning(
                "Unknown action_type=%s at step %d — treating as finish",
                action.action_type,
                step,
            )
        summary = action.thought or action.message or "Task complete."
        from agent.session.task_state_machine import TaskState
        if state_machine.state is not TaskState.COMPLETING:
            state_machine.transition(TaskState.COMPLETING, detail or "auto-complete")
        state_machine.complete(
            VerificationStatus.NOT_APPLICABLE,
            detail=detail,
        )
        log.log_task_complete(steps=step, summary=summary)
        self._extract_success_memories(task, log, summary)
        return self._build_run_result(
            ctx=finish_context,
            status=RunStatus.SUCCESS,
            summary=summary,
            steps_taken=step,
            total_tokens_used=total_tokens,
            cache_stats=cache_stats,
        )

    def _handle_terminal_action(
        self,
        *,
        action: Action,
        state: AgentTurnState,
        history: ConversationHistory,
        task: Task,
        log: EventLog,
        state_machine: Any,
        finish_context: _FinishRunContext,
        cache_stats: CacheStats,
        execution_budget: Any,
        completion_guard: TaskCompletionGuard,
        completion_context: CompletionContext,
        block_tracker: CompletionBlockTracker,
        git_state: _GitState,
        pytest_available: bool,
        verification_ok: bool,
        test_was_run: bool,
        completion_blocked: int,
        step: int,
        total_tokens: int,
    ) -> _TerminalApplication:
        """Apply FINISH/GIVE_UP policy and lifecycle transitions."""
        if action.action_type is ActionType.GIVE_UP:
            reason = action.message or "Agent gave up."
            state_machine.fail(TerminationReason.AGENT_GAVE_UP, reason)
            log.log_task_failed(steps=step, reason=reason)
            result = self._build_run_result(
                ctx=finish_context,
                status=RunStatus.GAVE_UP,
                summary=reason,
                steps_taken=step,
                total_tokens_used=total_tokens,
                cache_stats=cache_stats,
            )
            return _TerminalApplication(
                state=state,
                completion_blocked=completion_blocked,
                result=result,
            )

        from agent.session.task_state_machine import TaskState

        if state_machine.state is not TaskState.COMPLETING:
            state_machine.transition(
                TaskState.COMPLETING,
                "model called FINISH",
            )
        stop_message = self._run_stop_hook(
            history,
            stop_hook_active=state.stop_hook_count > 0,
            last_assistant_message=action.message or "",
        )
        evaluation = evaluate_completion(
            stop_message=stop_message,
            stop_hook_count=state.stop_hook_count,
            max_stop_hook_retries=_MAX_STOP_HOOK_RETRIES,
            checks=(
                self._cfg.completion_fact_check,
                self._cfg.verify_callback,
            ),
            refresh_workspace=lambda: _refresh_git_state(
                git_state,
                task.repo_path,
            ),
            guard_check=lambda: completion_guard.check(
                ctx=completion_context,
                task_intent=task.intent,
                git_state=git_state,
            ),
            block_tracker=block_tracker,
            block_threshold=COMPLETION_BLOCK_THRESHOLD,
            facts_factory=lambda: CompletionFacts(
                has_changes=git_state.has_changes,
                verification_ok=verification_ok,
                test_was_run=test_was_run,
                pytest_available=pytest_available,
                had_any_write=completion_context.had_any_write,
                is_git_repo=git_state.is_git_repo,
            ),
        )
        completion_blocked += evaluation.completion_blocked_increment
        if evaluation.outcome is CompletionOutcome.RETRY:
            state = self._resume_after_completion_block(
                evaluation,
                state=state,
                history=history,
                state_machine=state_machine,
            )
            return _TerminalApplication(
                state=state,
                completion_blocked=completion_blocked,
                continue_loop=True,
            )
        if evaluation.outcome is CompletionOutcome.GIVE_UP:
            result = self._finish_completion_give_up(
                evaluation,
                history=history,
                state_machine=state_machine,
                log=log,
                finish_context=finish_context,
                step=step,
                total_tokens=total_tokens,
                cache_stats=cache_stats,
            )
            return _TerminalApplication(
                state=state,
                completion_blocked=completion_blocked,
                result=result,
            )

        state = state.with_updates(stop_hook_count=0)
        decision = evaluation.verification
        state_machine.complete(
            decision.status,
            decision.reason,
            decision.detail,
        )
        summary = action.message or "Task complete."
        cache_dict = {
            "read": cache_stats.cache_read_tokens,
            "creation": cache_stats.cache_creation_tokens,
            "uncached": cache_stats.non_cached_input_tokens,
            "hit_rate": round(cache_stats.cache_hit_rate, 3),
        }
        log.log_task_complete(
            steps=step,
            summary=summary,
            contract=self._accumulated_plan_contract,
            cache_stats=cache_dict,
        )
        self._extract_success_memories(task, log, summary)
        execution_budget.complete()
        result = self._build_run_result(
            ctx=finish_context,
            status=RunStatus.SUCCESS,
            summary=summary,
            steps_taken=step,
            total_tokens_used=total_tokens,
            patch=git_state.current_diff or None,
            cache_stats=cache_stats,
            completion_blocked=completion_blocked,
        )
        return _TerminalApplication(
            state=state,
            completion_blocked=completion_blocked,
            result=result,
        )

    def _finish_missing_test_target(
        self,
        *,
        summary: str,
        task: Task,
        log: EventLog,
        finish_context: _FinishRunContext,
        step: int,
        total_tokens: int,
        cache_stats: CacheStats,
    ) -> RunResult:
        """Finish successfully when the requested pytest target is absent."""
        logger.info("Stopping after missing pytest target guardrail")
        log.log_task_complete(steps=step, summary=summary)
        self._extract_success_memories(task, log, summary)
        return self._build_run_result(
            ctx=finish_context,
            status=RunStatus.SUCCESS,
            summary=summary,
            steps_taken=step,
            total_tokens_used=total_tokens,
            cache_stats=cache_stats,
        )

    def _handle_post_observation(
        self,
        *,
        action: Action,
        state: AgentTurnState,
        history: ConversationHistory,
        task: Task,
        log: EventLog,
        finish_context: _FinishRunContext,
        cache_stats: CacheStats,
        total_tokens: int,
        step: int,
        any_test_failed: bool,
        missing_target_message: str | None,
        missing_followups: int | None,
        missing_detected_step: int | None,
        reflection_counts: dict[str, int],
    ) -> _PostObservationApplication:
        """Apply a typed post-observation transition to the active run."""
        evaluation = evaluate_post_observation(
            step=step,
            any_test_failed=any_test_failed,
            missing_target_message=missing_target_message,
            missing_followups=missing_followups,
            missing_detected_step=missing_detected_step,
            confirmation_search=self._is_confirmation_search_action(action),
            test_failure_count=reflection_counts.get("test_failed", 0),
            test_failure_limit=TEST_FAILURE_REFLECTION_LIMIT,
            task_anchor=(
                f"\n\n[TASK ANCHOR] Your current task is: "
                f"{task.description}"
            ),
            missing_reflection=self._missing_test_target_reflection,
            test_failure_reflection=lambda: (
                self._require_prompt_renderer().reflection("test-failed")
            ),
        )
        reflection_counts["test_failed"] = evaluation.reflection_count
        if evaluation.outcome is PostObservationOutcome.COMPLETE:
            result = self._finish_missing_test_target(
                summary=evaluation.summary,
                task=task,
                log=log,
                finish_context=finish_context,
                step=step,
                total_tokens=total_tokens,
                cache_stats=cache_stats,
            )
            return _PostObservationApplication(
                state=state,
                missing_followups=evaluation.missing_followups,
                result=result,
            )
        if evaluation.outcome is PostObservationOutcome.GIVE_UP:
            reason = evaluation.summary
            logger.warning(reason)
            log.log_task_failed(steps=step, reason=reason)
            result = self._build_run_result(
                ctx=finish_context,
                status=RunStatus.GAVE_UP,
                summary=reason,
                steps_taken=step,
                total_tokens_used=total_tokens,
                cache_stats=cache_stats,
            )
            return _PostObservationApplication(
                state=state,
                missing_followups=evaluation.missing_followups,
                result=result,
            )
        if evaluation.outcome is PostObservationOutcome.REFLECT:
            log.log_reflection(
                step=step,
                reason=evaluation.reflection_reason,
                prompt=evaluation.reflection_prompt,
            )
            history.add(LLMMessage(
                role="user",
                content=evaluation.reflection_prompt,
            ))
            if evaluation.reflection_reason == "missing_test_target":
                state = state.with_updates(
                    transition=Transition.reflection(),
                )
            logger.debug(
                "Reflection triggered: %s at step %d",
                evaluation.reflection_reason,
                step,
            )
            return _PostObservationApplication(
                state=state,
                missing_followups=evaluation.missing_followups,
                continue_loop=True,
            )
        return _PostObservationApplication(
            state=state,
            missing_followups=evaluation.missing_followups,
        )

    def _apply_tool_result_analysis(
        self,
        analysis: ToolResultAnalysis,
        *,
        tool_name: str,
        metadata: Any,
        result: ToolResult,
        completion_context: CompletionContext,
        execution_budget: Any,
        task: Task,
        git_state: Any,
    ) -> Observation:
        """Apply typed tool facts to state owned by the active run."""
        observation = analysis.observation
        if analysis.persisted_memory:
            self._explicit_memory_write_this_run = True
            self._invalidate_ltc()

        completion_context.record_tool_result(
            tool_name=tool_name,
            metadata=metadata,
            path=analysis.tool_path,
            success=observation.is_success(),
        )
        if analysis.delegated_tokens > 0:
            execution_budget.consume(analysis.delegated_tokens)
            logger.debug(
                "Charged %d subagent tokens to parent budget (total: %d)",
                analysis.delegated_tokens,
                execution_budget.token_used,
            )
        if analysis.structured_findings:
            self._accumulated_structured_findings.extend(
                analysis.structured_findings,
            )
        if analysis.plan_contract is not None:
            self._accumulated_plan_contract = analysis.plan_contract

        if analysis.read_path:
            self._accessed_files.add(normalize_repo_path(
                analysis.read_path,
                task.repo_path,
            ))
        if analysis.writes_workspace and observation.is_success():
            if analysis.write_path:
                self._mark_stale_for_written_file(normalize_repo_path(
                    analysis.write_path,
                    task.repo_path,
                ))
            _refresh_git_state(git_state, task.repo_path)
            for modified_file in result.modified_files:
                file_diff = _compute_file_diff(
                    modified_file,
                    task.repo_path,
                )
                if file_diff:
                    observation.metadata = {
                        **(observation.metadata or {}),
                        "diff": file_diff,
                    }
                    break
        return observation

    def _finish_tool_turn(
        self,
        *,
        action: Action,
        tool_batch: _ToolBatchApplication,
        decision: Any,
        visible_tools: list[LLMToolSchema],
        state: AgentTurnState,
        history: ConversationHistory,
        task: Task,
        log: EventLog,
        finish_context: _FinishRunContext,
        cache_stats: CacheStats,
        get_consecutive_failures: Callable[[], int],
        max_consecutive_failures: int,
        reflection_counts: dict[str, int],
        missing_message: str | None,
        missing_followups: int | None,
        missing_detected_step: int | None,
        total_tokens: int,
        step: int,
    ) -> _ToolTurnApplication:
        """Commit observations and apply post-tool transition policy."""
        effective_tool_calls = list(tool_batch.tool_calls)
        observations = list(tool_batch.observations)
        missing_observation = tool_batch.missing_test_target_observation
        if missing_observation is not None:
            if missing_message is None:
                missing_message = self._format_missing_test_target_summary(
                    missing_observation,
                )
                missing_followups = (
                    self._cfg.missing_test_target_max_followups
                )
                missing_detected_step = step
            else:
                missing_followups = 0

        breaker = self._cfg.circuit_breaker
        batch_evaluation = evaluate_observation_batch(
            observations,
            record_error=(
                breaker.record_tool_error
                if breaker is not None
                else lambda: None
            ),
            record_success=(
                breaker.record_tool_success
                if breaker is not None
                else lambda: None
            ),
            get_consecutive_failures=get_consecutive_failures,
            max_consecutive_failures=max_consecutive_failures,
            description_limit=FINDING_DESC_CHARS,
        )
        replay_tool_executions = [
            build_replay_tool_execution(
                observation,
                tool_call_id=(
                    getattr(effective_tool_calls[index], "id", "")
                    if index < len(effective_tool_calls)
                    else ""
                ),
                params=(
                    effective_tool_calls[index].params
                    if index < len(effective_tool_calls)
                    else None
                ),
            )
            for index, observation in enumerate(observations)
        ]
        log.log_replay_step(build_replay_step_record(
            step=step,
            decision=decision,
            visible_tools=visible_tools,
            action=action,
            tool_executions=replay_tool_executions,
            outcome="continue",
        ))
        self._check_pending_mode_switch(self._full_registry, history)

        if batch_evaluation.give_up_reason:
            reason = batch_evaluation.give_up_reason
            logger.warning(reason)
            log.log_task_failed(steps=step, reason=reason)
            result = self._build_run_result(
                ctx=finish_context,
                status=RunStatus.GAVE_UP,
                summary=reason,
                steps_taken=step,
                total_tokens_used=total_tokens,
                cache_stats=cache_stats,
            )
            return _ToolTurnApplication(
                state=state,
                missing_followups=missing_followups,
                missing_message=missing_message,
                missing_detected_step=missing_detected_step,
                result=result,
            )

        history.add_many(build_action_history(
            action,
            observations,
            supports_function_calling=self._backend.supports_function_calling,
            tool_calls=effective_tool_calls,
            render_action=self._format_action_for_history,
            render_observations=self._format_observations_for_history,
            render_tool_result=self._build_tool_result_content,
        ))
        next_phase = _phase_from_observations(observations)
        state = state.with_updates(
            child_turn_phase=_advance_child_turn_phase(
                state.child_turn_phase,
                observation_phase=next_phase,
                observations=observations,
            ),
        )
        post_application = self._handle_post_observation(
            action=action,
            state=state,
            history=history,
            task=task,
            log=log,
            finish_context=finish_context,
            cache_stats=cache_stats,
            total_tokens=total_tokens,
            step=step,
            any_test_failed=tool_batch.any_test_failed,
            missing_target_message=missing_message,
            missing_followups=missing_followups,
            missing_detected_step=missing_detected_step,
            reflection_counts=reflection_counts,
        )
        return _ToolTurnApplication(
            state=post_application.state,
            missing_followups=post_application.missing_followups,
            missing_message=missing_message,
            missing_detected_step=missing_detected_step,
            result=post_application.result,
            continue_loop=post_application.continue_loop,
        )

    def _execute_tool_batch(
        self,
        *,
        action: Action,
        base_run_context: Any,
        spawn_context: Any,
        streaming_executor: StreamingToolExecutor | None,
        observer: Any,
        task: Task,
        log: EventLog,
        state_machine: Any,
        finish_context: _FinishRunContext,
        cache_stats: CacheStats,
        completion_context: CompletionContext,
        execution_budget: Any,
        git_state: _GitState,
        step: int,
        total_tokens: int,
        cancellation: Any = None,
    ) -> _ToolBatchApplication:
        """Execute and apply one ordered batch of model tool calls."""
        execution_context = replace(
            base_run_context,
            spawn_context=spawn_context,
        )
        executed = execute_action(
            action.tool_calls,
            self._registry,
            execution_context,
            streaming_executor=streaming_executor,
        )
        tool_calls = tuple(executed.tool_calls)
        ordered_results = tuple(executed.results)
        observations: list[Observation] = []
        test_was_run = False
        verification_ok = False
        any_test_failed = False
        missing_observation = None

        for index, tool_call in enumerate(tool_calls):
            metadata = self._registry.metadata_for(tool_call.name)
            result = (
                ordered_results[index]
                if index < len(ordered_results)
                else ToolResult.from_error(
                    error_type=ToolErrorType.INTERNAL,
                    detail="Tool execution lost result",
                )
            )
            with observer.start_tool(
                name=f"tool:{tool_call.name}",
                input_data=build_tool_input(
                    tool_call.name,
                    tool_call.params,
                    action.thought or "",
                    step,
                ),
                metadata=merge_metadata(
                    {"tool_name": tool_call.name, "step": step},
                    task.metadata,
                ),
            ) as tool_observation:
                tool_observation.update(
                    output=build_tool_output(
                        result,
                        capture_tool_outputs=(
                            observer.config.capture_tool_outputs
                            if observer.config
                            else True
                        ),
                    ),
                    metadata={
                        "tool_name": tool_call.name,
                        "duration_ms": result.duration_ms,
                    },
                )
            analysis = analyze_tool_result(
                tool_name=tool_call.name,
                params=tool_call.params,
                metadata=metadata,
                result=result,
                delegation_block_prefix=_V2_DELEGATION_BLOCK_PREFIX,
            )
            stats_collector = self._cfg.stats_collector
            if stats_collector is not None:
                try:
                    stats_collector.record_tool_call(
                        session_id=self._cfg.stats_session_id,
                        agent_name=self._cfg.stats_agent_name,
                        step=step,
                        tool_name=tool_call.name,
                        success=result.success,
                        duration_ms=result.duration_ms,
                        tool_params=tool_call.params or {},
                    )
                except Exception:
                    pass

            if analysis.environment_block is not None:
                block = analysis.environment_block
                summary = (
                    "[RUNTIME] Task BLOCKED — environment issue detected:\n"
                    f"{block.detail}\n"
                    f"Suggestion: {block.alternative}\n"
                    "The task cannot continue until this is resolved. "
                    "Summarize your findings and call finish."
                )
                # Log the observation BEFORE returning — event log replay
                # requires every tool_call to have a paired observation.
                observation = self._apply_tool_result_analysis(
                    analysis,
                    tool_name=tool_call.name,
                    metadata=metadata,
                    result=result,
                    completion_context=completion_context,
                    execution_budget=execution_budget,
                    task=task,
                    git_state=git_state,
                )
                observations.append(observation)
                log.log_observation(
                    step=step,
                    observation=observation,
                    tool_call_id=tool_call.id,
                )
                state_machine.fail(
                    TerminationReason.ENVIRONMENT_UNAVAILABLE,
                    block.detail,
                )
                state_machine.block_detail = {
                    "error_type": block.error_type.value,
                    "detail": block.detail,
                    "suggested_fix": block.alternative,
                    "tool": tool_call.name,
                }
                logger.warning(
                    "Runtime intercepted env blocker: %s",
                    block.detail,
                )
                log.log_task_failed(steps=step, reason=block.detail)
                run_result = self._build_run_result(
                    ctx=finish_context,
                    status=RunStatus.BLOCKED,
                    summary=summary,
                    steps_taken=step,
                    total_tokens_used=total_tokens,
                    error=block.detail,
                    cache_stats=cache_stats,
                )
                return _ToolBatchApplication(
                    tool_calls=tool_calls,
                    observations=tuple(observations),
                    result=run_result,
                )

            observation = self._apply_tool_result_analysis(
                analysis,
                tool_name=tool_call.name,
                metadata=metadata,
                result=result,
                completion_context=completion_context,
                execution_budget=execution_budget,
                task=task,
                git_state=git_state,
            )
            observations.append(observation)
            test_was_run = test_was_run or analysis.test_was_run
            verification_ok = verification_ok or analysis.verification_ok
            any_test_failed = (
                any_test_failed or analysis.test_failed
            )
            if analysis.missing_test_target:
                missing_observation = observation

            log.log_observation(
                step=step,
                observation=observation,
                tool_call_id=tool_call.id,
            )
            if missing_observation is not None:
                return _ToolBatchApplication(
                    tool_calls=tool_calls,
                    observations=tuple(observations),
                    test_was_run=test_was_run,
                    verification_ok=verification_ok,
                    any_test_failed=any_test_failed,
                    missing_test_target_observation=missing_observation,
                )

        return _ToolBatchApplication(
            tool_calls=tool_calls,
            observations=tuple(observations),
            test_was_run=test_was_run,
            verification_ok=verification_ok,
            any_test_failed=any_test_failed,
            missing_test_target_observation=missing_observation,
        )

    def _finish_pre_step(
        self,
        evaluation: PreStepEvaluation,
        *,
        state_machine: Any,
        log: EventLog,
        finish_context: _FinishRunContext,
        total_tokens: int,
        cache_stats: CacheStats,
    ) -> RunResult:
        """Apply a pre-step terminal decision at the lifecycle boundary."""
        if evaluation.cancelled:
            state_machine.cancel(evaluation.detail)
        elif evaluation.termination_reason is not None:
            state_machine.fail(
                evaluation.termination_reason,
                evaluation.detail,
            )
        if evaluation.log_failure:
            log.log_task_failed(
                steps=evaluation.steps_taken,
                reason=evaluation.detail or evaluation.summary,
            )
        elif evaluation.status is RunStatus.GAVE_UP:
            logger.warning(evaluation.summary)
        return self._build_run_result(
            ctx=finish_context,
            status=evaluation.status or RunStatus.GAVE_UP,
            summary=evaluation.summary,
            steps_taken=evaluation.steps_taken,
            total_tokens_used=total_tokens,
            error=evaluation.error or None,
            cache_stats=cache_stats,
        )

    def _resume_after_completion_block(
        self,
        evaluation: CompletionEvaluation,
        *,
        state: AgentTurnState,
        history: ConversationHistory,
        state_machine: Any,
    ) -> AgentTurnState:
        """Apply a completion retry to loop-owned lifecycle state."""
        is_stop_hook = (
            evaluation.retry_source is CompletionRetrySource.STOP_HOOK
        )
        transition = (
            Transition.stop_hook_blocking()
            if is_stop_hook
            else Transition.completion_blocked()
        )
        next_state = state.with_updates(
            stop_hook_count=evaluation.stop_hook_count,
            transition=transition,
        )
        if evaluation.inject_message:
            history.add(LLMMessage(
                role="user",
                content=evaluation.inject_message,
            ))
        detail = (
            "stop hook blocked — back to loop"
            if is_stop_hook
            else f"completion blocked: {evaluation.reason}"
        )
        from agent.session.task_state_machine import TaskState

        state_machine.transition(TaskState.RUNNING, detail)
        return next_state

    def _finish_completion_give_up(
        self,
        evaluation: CompletionEvaluation,
        *,
        history: ConversationHistory,
        state_machine: Any,
        log: EventLog,
        finish_context: _FinishRunContext,
        step: int,
        total_tokens: int,
        cache_stats: CacheStats,
    ) -> RunResult:
        """Apply a terminal completion decision and build its run result."""
        reason = evaluation.reason
        logger.warning(reason)
        if evaluation.inject_message:
            history.add(LLMMessage(
                role="user",
                content=evaluation.inject_message,
            ))
        if evaluation.check_aborted:
            from agent.session.task_state_machine import TaskState

            state_machine.transition(TaskState.FAILED, f"abort: {reason}")
        else:
            state_machine.fail(
                evaluation.termination_reason
                or TerminationReason.AGENT_GAVE_UP,
                reason,
            )
        log.log_task_failed(steps=step, reason=reason)
        return self._build_run_result(
            ctx=finish_context,
            status=RunStatus.GAVE_UP,
            summary=reason,
            steps_taken=step,
            total_tokens_used=total_tokens,
            cache_stats=cache_stats,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _build_session_memory_context(self, history: ConversationHistory) -> str:
        """Build a concise context summary for session memory extraction."""
        messages = history.get_messages()
        parts: list[str] = []
        for msg in messages[-SESSION_MEMORY_MSG_WINDOW:]:
            role = msg.role
            content = msg.content or ""
            if len(content) > SUMMARY_TRUNCATION_CHARS:
                content = content[:SUMMARY_TRUNCATION_CHARS] + "..."
            if content.strip():
                parts.append(f"[{role}] {content}")
        return "\n\n".join(parts)

    def _run_stop_hook(
        self,
        history: ConversationHistory,
        *,
        stop_hook_active: bool,
        last_assistant_message: str,
    ) -> str | None:
        """Run the STOP hook via the session-scoped dispatcher.

        Returns an inject-message string if the hook blocked completion,
        or ``None`` to proceed.
        """
        messages = history.to_dicts()
        dispatcher = self._get_hook_dispatcher()
        if dispatcher is not None:
            try:
                from hooks.events import HookContext
                ctx = HookContext(
                    event=self._cfg.stop_hook_event,
                    session_id=self._cfg.hook_session_id,
                    messages=messages,
                    agent_id=self._cfg.hook_agent_id,
                    agent_type=self._cfg.hook_agent_type,
                    last_assistant_message=last_assistant_message,
                    stop_hook_active=stop_hook_active,
                )
                result = dispatcher.dispatch(self._cfg.stop_hook_event, ctx)
            except Exception as exc:
                logger.debug("Stop hook dispatch failed: %s", exc)
                result = None
            from hooks.protocol import HookControl
            if result is not None and result.control is HookControl.BLOCK:
                return (
                    "[Stop hook blocked completion]\n"
                    f"{result.reason}\n"
                    "Continue working until the check passes."
                )
        # Legacy _goal_stop_hook — NOT HookDispatcher-based.
        # This is a separate callback set externally (entry/chat.py).
        # Only runs when the HookDispatcher-based STOP hook didn't block.

        goal_hook = getattr(self, "_goal_stop_hook", None)
        if goal_hook is None:
            return None
        try:
            goal_messages = goal_hook(messages)
        except Exception as exc:
            logger.debug("Goal stop hook failed: %s", exc)
            return None
        if not goal_messages:
            return None
        return str(goal_messages[0].get("content", ""))

    def _dispatch_post_response(
        self, history: ConversationHistory, step: int,
    ) -> None:
        """CC-aligned: fire PostResponse hook after each assistant turn.

        Non-blockable notification dispatched via the session-scoped
        hook dispatcher.  Useful for logging, memory extraction, progress tracking.
        """
        dispatcher = self._get_hook_dispatcher()
        if dispatcher is None:
            return
        try:
            from hooks.events import HookContext, HookEvent
            ctx = HookContext(
                event=HookEvent.POST_RESPONSE,
                session_id=self._cfg.hook_session_id,
                messages=history.to_dicts(),
                agent_id=self._cfg.hook_agent_id,
                agent_type=self._cfg.hook_agent_type,
            )
            dispatcher.dispatch(HookEvent.POST_RESPONSE, ctx)
        except Exception:
            logger.debug("PostResponse hook dispatch failed", exc_info=True)

    def _get_hook_dispatcher(self):
        """Return the session-scoped hook dispatcher, or ``None``.

        The dispatcher is set on ``AgentConfig`` by the session runtime
        (for primary sessions) or by the registry builder (for child
        sessions).  This is the single source of truth — callers must
        not reach into ``_full_registry._hook_dispatcher`` directly.
        """
        return self._cfg.hook_dispatcher

    def _is_missing_test_target_observation(self, observation: Observation) -> bool:
        """Use the tool's typed outcome; never infer control flow from text."""
        return observation.outcome is ToolOutcome.TEST_TARGET_MISSING

    def _format_missing_test_target_summary(self, observation: Observation) -> str:
        # Prefer structured metadata; fall back to legacy regex extraction from output text
        requested_path = observation.metadata.get("requested_path", "")
        if not requested_path:
            match = re.search(r"Requested path:\s*(.+)", observation.output)
            if match:
                requested_path = match.group(1).strip()
        if not requested_path:
            requested_path = "(unknown)"
        return (
            f"`pytest {requested_path}` could not run because the requested test path "
            f"`{requested_path}` does not exist. Pytest reported exit code 4 "
            "(usage error / missing path), so this is not an existing failing test. "
            "I did not modify code or create tests. Please provide the correct test path, "
            "or explicitly ask me to add a new test file."
        )

    def _missing_test_target_reflection(self, summary: str) -> str:
        return (
            "[SYSTEM] Pytest failed because the requested test target is missing.\n"
            "This is a blocker, not permission to create a test file.\n"
            "Do NOT inspect unrelated implementation files or start writing tests/code.\n"
            "You may perform at most targeted confirmation searches for the missing path.\n"
            "Then finish with this conclusion:\n"
            f"{summary}"
        )

    def _is_confirmation_search_action(self, action: Action) -> bool:
        """缺失测试路径后，仅允许查找/搜索类工具做有限确认。"""
        if action.action_type != ActionType.TOOL_CALL or not action.tool_calls:
            return False
        return all(
            self._is_targeted_confirmation_call(tc)
            for tc in action.tool_calls
        )

    def _is_targeted_confirmation_call(self, tc: ToolCall) -> bool:
        metadata = self._registry.metadata_for(tc.name)
        return ToolEffect.DISCOVER_WORKSPACE in metadata.effects

    def _extract_summary_from_history(self, history: ConversationHistory) -> str:
        """从对话历史中提取有意义的 summary（用于 max_steps 截断时）。

        策略：
        1. 倒序扫描 assistant messages，跳过规划性文本（"Let me", "I'll" 等）
        2. 找到含实质内容的 assistant 消息则返回
        3. 如果所有 assistant 都是规划文本，fallback 到 tool results（最可靠的信息源）
        """
        _PLANNING_PREFIXES = (
            "Let me", "I'll", "I need to", "Now let me", "Good,",
            "Now I'll", "Next,", "OK,", "Alright,", "First,",
        )
        msgs = history.to_dicts()

        # Pass 1: 找实质性 assistant 内容（跳过规划文本）
        for msg in reversed(msgs):
            if msg.get("role") == "assistant":
                content = msg.get("content", "").strip()
                if not content or len(content) <= 20:
                    continue
                if any(content.startswith(p) for p in _PLANNING_PREFIXES):
                    continue
                return content[:SUMMARY_TRUNCATION_CHARS]

        # Pass 2: 提取 tool results（原始工具返回数据，不依赖模型总结）
        tool_contents = []
        for msg in reversed(msgs):
            if msg.get("role") == "tool":
                content = msg.get("content", "").strip()
                if content and len(content) > 10:
                    tool_contents.append(content[:TOOL_EXTRACT_CHARS])
                    if len(tool_contents) >= MAX_TOOL_RESULTS_EXTRACT:
                        break

        if tool_contents:
            combined = "\n---\n".join(reversed(tool_contents))
            return f"Raw tool results (max_steps reached):\n{combined}"[:2000]

        return f"Reached max_steps limit ({len(history)}+ messages in history)"

    def _build_messages(
        self,
        history: ConversationHistory,
        token_budget: TokenBudget,
        repo_map: RepoMap,
        consumed_tokens: int = 0,
        max_context_window: int | None = None,
        *,
        step: int = 1,
    ) -> list[LLMMessage]:
        """
        组装发给 LLM 的完整 messages，含 token 裁剪。

        委托 ContextManager 执行实际组装。保留此方法签名以兼容现有调用。
        """
        schemas = self._registry.get_schemas()

        if self._inherited_context is not None:
            ctx = self._context_manager.build_inherited_messages(
                self._inherited_context, history,
            )
            self._last_context_stats = ctx.stats
            return ctx.messages

        # Sub-agent 模式：精简 system prompt
        if self._cfg.is_subagent:
            system_content = self._require_prompt_renderer().sub_agent_system(
                schemas,
            )
            ctx = self._context_manager.build_sub_agent_messages(history, system_content)
            self._last_context_stats = ctx.stats
            return ctx.messages

        # ── 主 agent 正常流程：委托 ContextManager ──────────
        self._context_manager._cfg.enable_caching = self._is_anthropic_backend()

        repo_path = getattr(self, "_current_repo_path", ".")

        # Repo map: build once per repo, cached
        if not hasattr(self, "_repo_map_cache"):
            plan = token_budget.compute_plan(
                consumed_tokens=consumed_tokens,
                max_context_window=max_context_window,
            )
            self._repo_map_cache = repo_map.build(budget=plan.repo_map)

        prompt_renderer = self._require_prompt_renderer()
        core_text = prompt_renderer.system_core(
            repo_path,
            schemas,
            self._repo_map_cache,
        )
        # Inject project instructions (CLAUDE.md) into the core prompt.
        # Loaded once per run, cached on the agent instance.
        _instructions = self._load_project_instructions(repo_path)
        if _instructions:
            core_text = (
                core_text.rstrip()
                + "\n\n## Project Instructions\n"
                + _instructions
            )

        variable_text = prompt_renderer.system_variable(
            memory_section="",
            auto_memory_enabled=bool(self._memory_context and self._memory_context.enabled),
        )
        long_term = self._build_long_term_context()
        anchor = self._build_task_anchor()

        # Apply collapse projection at read time (original messages unchanged)
        effective_history = self._apply_collapse_projection(history)

        ctx = self._context_manager.build_request_messages(
            history=effective_history,
            token_budget=token_budget,
            system_core_text=core_text,
            variable_text=variable_text,
            long_term_context=long_term,
            task_anchor=anchor,
            artifact_store=self._artifact_store,
            consumed_tokens=consumed_tokens,
            max_context_window=max_context_window,
            repo_map_text=self._repo_map_cache or "",
            compactor=self.compactor,
            compaction_task_context=getattr(
                self,
                "_current_task_description",
                "",
            ),
            tokens_freed=getattr(self, "_trim_tokens_freed", 0),
            history_materializer_fn=None,
            step=step,
        )

        self._compact_triggered_this_step = ctx.compact_triggered
        self._last_context_stats = ctx.stats

        # Post-compaction recovery: re-inject critical context (CC-aligned)
        if ctx.compact_triggered:
            self._persist_compaction_summary(ctx.compaction_summary)
            # Do NOT re-inject memory here if it was already injected pre-compaction
            # by build_request_messages (line 2843).  The `long_term` variable was
            # passed as long_term_context; if non-empty, it's already in the message
            # set and reappending it would double-inject.
            ctx.messages = self._inject_recovery_after_compact(
                ctx.messages,
                memory_already_injected=bool(long_term),
            )

        return ctx.messages

    def _apply_collapse_projection(self, history: ConversationHistory) -> ConversationHistory:
        """Apply CollapseStore read-time projection, or return history unchanged."""
        _trimming_state = getattr(self, "_context_trimming_state", None)
        _collapse_store = (
            _trimming_state.collapse_store
            if _trimming_state is not None
            else None
        )
        if _collapse_store is None or _collapse_store.is_empty:
            return history
        from context.collapse import project_view
        _history_dicts = project_view(history.to_dicts(), _collapse_store)
        return ConversationHistory.from_dicts(_history_dicts, max_messages=history.max_messages)

    def _inject_recovery_after_compact(self, messages: list[LLMMessage], *, memory_already_injected: bool = False) -> list[LLMMessage]:
        """Append recovery context after a compaction event."""
        recovery_msgs = self._build_recovery_messages(memory_already_injected=memory_already_injected)
        findings = getattr(self, "_accumulated_structured_findings", [])
        if findings:
            recovery_msgs.append(LLMMessage(role="user", content=(
                "[ACCUMULATED FINDINGS]\n" + "\n".join(
                    f"- {f.get('title','')}: {f.get('description','')[:FINDING_DESC_CHARS]}"
                    for f in findings[-RECOVERY_MAX_FINDINGS:]
                )
            )))
        if recovery_msgs:
            return list(messages) + recovery_msgs
        return messages

    def _build_recovery_messages(self, *, memory_already_injected: bool = False) -> list["LLMMessage"]:
        """Post-compaction context re-injection (CC-aligned).

        Re-injects: file cache, skill buffer, CLAUDE.md, AND memory section.
        When memory_already_injected is True, the memory context was already
        present in the pre-compaction message set and must not be duplicated.
        """
        recovery = _build_compaction_recovery(self._full_registry, self._current_repo_path)
        raw_msgs = recovery.build_recovery_messages([])
        msgs = [LLMMessage(role=m.get("role", "user"), content=m.get("content", "")) for m in raw_msgs]
        # M2: re-inject memory section after compaction (CC: auto-memory survives compaction)
        if not memory_already_injected:
            self._invalidate_ltc()
            _ltc = self._build_long_term_context()
            if _ltc:
                msgs.append(LLMMessage(role="user", content=f"[MEMORY RESTORED]\n{_ltc}"))
        # Reset feedback-injected tracking so rules fire again post-compaction
        if hasattr(self, "_feedback_injected_files"):
            self._feedback_injected_files.clear()
        return msgs

    def _check_pending_mode_switch(self, registry: Any, history: Any) -> None:
        """CC-aligned: delegate to agent/mode_switching.py."""
        from agent.mode_switching import check_pending_mode_switch
        check_pending_mode_switch(registry, history)

    def _mark_stale_for_written_file(self, file_path: str) -> None:
        """文件写入后标记相关 anchored 记忆为 stale。"""
        if not self._memory_context or not self._memory_context.enabled:
            return
        store = getattr(self._memory_context, "store", None)
        if store is None:
            return
        try:
            count = store.mark_stale_for_file(file_path)
            if count:
                logger.debug("Marked %d memories stale for file: %s", count, file_path)
        except Exception as exc:
            logger.debug("mark_stale_for_file skipped: %s", exc)

    def _extract_success_memories(self, task: Task, log: EventLog, summary: str) -> None:
        """委托给 RunFinalizer。"""
        from agent.run_finalizer import RunFinalizer
        _f = getattr(self, "_run_finalizer", None)
        if _f is None:
            def _publish_memory_written(memory, source):
                callback = getattr(self._cfg, "memory_event_callback", None)
                if callback is not None:
                    callback(memory, source)
            _f = RunFinalizer(self._memory_context, self._backend, event_callback=_publish_memory_written)
            self._run_finalizer = _f
        _findings = getattr(self, "_accumulated_structured_findings", [])
        _f.extract(task, log, summary, accumulated_findings=_findings,
                   skip_llm=getattr(self, "_explicit_memory_write_this_run", False))
        self._accumulated_structured_findings = []

    def _is_anthropic_backend(self) -> bool:
        """判断当前 backend 是否为 Anthropic（支持 prompt cache）。"""
        backend_type = type(self._backend).__name__
        return "anthropic" in backend_type.lower()

    def _build_long_term_context(self) -> str | None:
        """委托给 memory/injection_service.py。可被 _invalidate_ltc() 强制刷新。"""
        if hasattr(self, "_long_term_context") and not getattr(self, "_ltc_stale", False):
            return self._long_term_context
        self._ltc_stale = False
        from memory.injection_service import build_injection_context
        self._long_term_context = build_injection_context(
            memory_context=self._memory_context,
            skills_prompt=getattr(self, "_skills_prompt", ""),
            repo_path=getattr(self, "_current_repo_path", "."),
            session_context=self._session_context,
        )
        return self._long_term_context

    def _invalidate_ltc(self) -> None:
        """Mark long-term context as stale — next _build_long_term_context() will refresh."""
        self._ltc_stale = True

    def _build_task_anchor(self) -> str:
        """Build the per-step task anchor injected before every LLM call.

        Orchestrates four independent prompt components — each built by
        its own dedicated helper.  This method only concatenates.
        """
        parts: list[str] = []

        desc = self._build_task_description()
        if desc:
            parts.append(desc)

        mode = self._build_task_mode_guidance()
        if mode:
            parts.append(mode)

        policy = self._build_policy_section()
        if policy:
            parts.append(policy)

        feedback = self._get_feedback_section()
        if feedback:
            parts.append(feedback)

        if not parts:
            return ""
        return "\n\n".join(parts)

    def _build_task_description(self) -> str:
        """The task description line — what the model is currently working on."""
        task_desc = getattr(self, "_current_task_description", "")
        if task_desc:
            return f"## Current Task\n{task_desc}"
        return ""

    def _build_task_mode_guidance(self) -> str:
        """Analysis-mode guidance when the task intent is read-only."""
        task_metadata = getattr(self, "_current_task_metadata", {}) or {}
        legacy_disabled = bool(
            task_metadata.get("v2_disable_legacy_analysis_prompting")
        )
        if legacy_disabled:
            return ""
        if getattr(self, "_task_intent", TaskIntent.EDIT) is not TaskIntent.ANALYSIS:
            return ""
        return (
            "## Task Mode: Analysis\n"
            "This is a read-only analysis task. Inspect relevant project evidence, "
            "synthesize findings, and verify named gaps.\n"
            "For confirmed conclusions, cite recorded evidence ids like [ev_xxx]. "
            "If a point is not supported by evidence, move it under uncertainty or needs verification.\n"
            "Do NOT edit files. Do NOT run tests. Answer from evidence as soon as you can."
        )

    def _build_policy_section(self) -> str:
        """Active policy constraints (allowed paths / effects)."""
        task_metadata = getattr(self, "_current_task_metadata", {}) or {}
        if task_metadata.get("v2_disable_legacy_analysis_prompting"):
            return ""
        active_policy = getattr(self, "_active_policy", None)
        if active_policy is not None:
            return active_policy.to_prompt_section("execution") or ""
        return ""

    def _get_feedback_section(self) -> str:
        """获取当前已访问文件对应的 feedback 记忆内容。

        对新文件触发 record_access 递增计数，
        对全部已访问文件每步都展示规则（嵌入 task anchor 不丢失）。
        """
        if not self._memory_context or not self._memory_context.enabled:
            return ""
        accessed = getattr(self, "_accessed_files", None)
        if not accessed:
            return ""
        try:
            new_files = accessed - getattr(self, "_feedback_injected_files", set())
            if new_files:
                self._memory_context.get_feedback_for_files(new_files, record_access=True)
                self._feedback_injected_files.update(new_files)
            return self._memory_context.get_feedback_for_files(accessed, record_access=False)
        except Exception as exc:
            logger.debug("Feedback section build failed: %s", exc)
            return ""


    def _format_action_for_history(self, action: Action) -> str:
        """把 Action 格式化为 assistant 消息，写入对话历史。支持并行 tool_calls。"""
        parts = [f"Thought: {action.thought}"]
        if action.tool_calls:
            for tc in action.tool_calls:
                parts.append(f"Action: {tc.name}")
                parts.append(f"Params: {json.dumps(tc.params, ensure_ascii=False)}")
        elif action.message:
            parts.append(f"Message: {action.message}")
        return "\n".join(parts)

    def _format_observation_for_history(self, observation: Observation) -> str:
        """把单条 Observation 格式化为 user 消息（text fallback mode）。"""
        status = "SUCCESS" if observation.is_success() else "ERROR"
        lines = [f"[Tool: {observation.tool_name} | {status}]"]
        if observation.output:
            lines.append(observation.output)
        if observation.error and not observation.is_success():
            lines.append(f"Error: {observation.error}")
        return "\n".join(lines)

    # 这些工具的输出不做 artifact 化（LLM 需要完整内容来做下一步决策）
    def _build_tool_result_content(self, observation: Observation) -> str:
        """委托给 observation_rendering。"""
        from agent.observation_rendering import build_tool_result_content
        metadata = self._registry.metadata_for(observation.tool_name)
        return build_tool_result_content(
            observation,
            artifact_store=self._artifact_store,
            tool_roles=metadata.roles if metadata is not None else frozenset(),
        )

    @staticmethod
    def _truncate_output(text: str, max_chars: int = DEFAULT_TRUNCATE_OUTPUT_CHARS) -> str:
        """委托给 observation_rendering。"""
        from agent.observation_rendering import truncate_output
        return truncate_output(text, max_chars)

    def _format_observations_for_history(self, observations: list[Observation]) -> str:
        """委托给 observation_rendering。"""
        from agent.observation_rendering import format_observations_for_history
        roles_by_tool = {}
        for observation in observations:
            metadata = self._registry.metadata_for(observation.tool_name)
            roles_by_tool[observation.tool_name] = (
                metadata.roles if metadata is not None else frozenset()
            )
        return format_observations_for_history(
            observations,
            artifact_store=self._artifact_store,
            roles_by_tool=roles_by_tool,
        )

    def _call_with_retry(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ):
        """委托给 LLMInvoker。prompt_metadata 在此层消费后传入 llm/。"""
        from llm.invoker import LLMInvoker
        from prompts.builder import consume_prompt_usage_metadata
        _invoker = getattr(self, "_llm_invoker", None)
        if _invoker is None:
            _invoker = LLMInvoker(
                backend=self._backend,
                config=self._cfg,
                metrics_callback=getattr(self._cfg, "llm_metrics_callback", None),
            )
            self._llm_invoker = _invoker
        result = _invoker.invoke(
            messages, tools, cumulative_cache=None,
            prompt_metadata=consume_prompt_usage_metadata(),
        )
        return result.response

    def _stream_and_dispatch(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
        executor: "StreamingToolExecutor",
    ) -> "Action":
        """CC-aligned streaming dispatch: yield tool_use blocks during LLM stream.

        Calls backend.stream_iter() and processes events mid-stream:
          - TEXT_DELTA → forwarded to stream_callback (user-visible rendering)
          - TOOL_USE   → enqueued in executor, starts immediately if safe
          - FINISH     → build Action from finish event
          - ERROR      → raise

        When the stream ends, the executor may already have completed some tools
        (speculative execution). The caller must call executor.dispatch() then
        executor.collect() to get all results.
        """
        from llm.base import StreamEventKind

        accumulated_text = ""
        accumulated_thought = ""
        tool_calls_raw: list[ToolCall] = []

        for event in self._backend.stream_iter(messages, tools):
            if event.kind == StreamEventKind.ERROR:
                raise RuntimeError(f"LLM stream error: {event.text}")

            elif event.kind == StreamEventKind.TEXT_DELTA:
                accumulated_text += event.text
                if event.thought:
                    accumulated_thought += event.thought
                # Forward to user-visible rendering
                if self._cfg.stream_callback:
                    self._cfg.stream_callback(event.text)
                # Advance the tool queue during text streaming — tools that
                # completed speculatively may unblock queued successors.
                executor.process_queue()

            elif event.kind == StreamEventKind.TOOL_USE:
                if event.tool_call:
                    tool_calls_raw.append(event.tool_call)
                    executor.enqueue(event.tool_call)
                    # After each enqueue, check for newly completed tools
                    executor.process_queue()

            elif event.kind == StreamEventKind.FINISH:
                self._stream_usage = (event.input_tokens, event.output_tokens)
                if tool_calls_raw:
                    return Action(
                        action_type=ActionType.TOOL_CALL,
                        thought=accumulated_thought or event.thought,
                        tool_calls=tool_calls_raw,
                    )
                return Action(
                    action_type=ActionType.FINISH,
                    thought=accumulated_thought or event.thought,
                    message=event.finish_message or accumulated_text,
                )

        # Stream ended without FINISH (network disruption, backend error, etc.).
        # If speculative tool execution produced tool calls, return them;
        # otherwise report the incomplete stream as a structured finish.
        self._stream_usage = None
        if tool_calls_raw:
            return Action(
                action_type=ActionType.TOOL_CALL,
                thought=accumulated_thought,
                tool_calls=tool_calls_raw,
            )
        return Action(
            action_type=ActionType.FINISH,
            thought=accumulated_thought,
            message=accumulated_text or "Stream ended before model produced a result.",
        )

    # ------------------------------------------------------------------
    # Compaction（对话压缩）
    # ------------------------------------------------------------------

    _compactor: ConversationCompactor | None = None

    @property
    def compactor(self) -> ConversationCompactor:
        if self._compactor is None:
            self._compactor = ConversationCompactor(backend=self._backend)
        return self._compactor

    def _persist_compaction_summary(self, summary_text: str | None) -> None:
        """Persist an already-produced summary without owning compaction."""
        if (
            not summary_text
            or not self._memory_context
            or not hasattr(self._memory_context, "_store")
        ):
            return
        from context.compaction import persist_compaction_summary

        store_dir = str(self._memory_context.store.store_dir.parent)
        persist_compaction_summary(summary_text, store_dir)

    def _recover_from_llm_error(
        self,
        exc: Exception,
        step: int,
        history: Any,
        total_tokens: int,
        state: "AgentTurnState",
    ) -> "tuple[AgentTurnState, bool] | None":
        """Try reactive compact on prompt-too-long errors.

        Returns (new_state, should_continue) if recovery succeeded,
        or ``None`` if the error is unrecoverable — caller should fail.
        """
        _exc_str = str(exc).lower()
        _is_prompt_too_long = any(
            kw in _exc_str
            for kw in ("prompt too long", "context length", "413", "reduce the length")
        )
        if _is_prompt_too_long and state.recovery.can_reactive_compact() and self.compactor is not None:
            logger.warning("Prompt too long at step %d — attempting reactive compact", step)
            return self._attempt_reactive_compact(history, total_tokens, state)
        return None

    def _attempt_reactive_compact(
        self, history, total_tokens, state: "AgentTurnState",
    ) -> "tuple[AgentTurnState, bool]":
        """3-tier waterfall recovery for prompt-too-long (P1-3).

        Returns (new_state, should_continue).  When should_continue is True
        the caller must ``continue`` back to the top of the for-step loop.
        """
        state = state.with_recovery_update(has_attempted_reactive_compact=True)
        # Tier 1: drain — zero-cost SnipCompact + MicroCompact
        drained = 0
        try:
            drained += _snip_history(history)
            drained += _micro_compact(history)
            if drained > 0:
                logger.info(
                    "Drain freed ~%d tokens — retrying LLM call", drained,
                )
                return state.with_updates(
                    transition=Transition.reactive_compact(),
                ), True
        except Exception as dexc:
            logger.debug("Drain failed: %s", dexc)
        # Tier 2: full LLM compact
        try:
            compacted = self.compactor.compact_history(
                history.to_dicts(), total_tokens,
            )
            history.replace_messages(
                history.from_dicts(compacted, history.max_messages),
            )
            # History was replaced — collapse-store indices are now stale.
            # Reset to avoid IndexError or context corruption on next projection.
            self._context_trimming_state.collapse_store = None
            logger.info("Reactive compact succeeded — retrying LLM call")
            return state.with_updates(
                transition=Transition.reactive_compact(),
            ), True
        except Exception as cexc:
            logger.warning("Reactive compact failed: %s", cexc)
        return state, False


# ---------------------------------------------------------------------------
# 向后兼容别名 — 所有旧代码可继续使用 Agent
# ---------------------------------------------------------------------------

def _build_compaction_recovery(registry: Any, project_dir: str | None = None) -> Any:
    """Extract cached tool state for post-compaction context re-injection.

    Reads file-cache and skill-buffer from the tool registry via its
    public attributes — does not reach into ``_tools`` dict directly.
    The registry is expected to expose these as attributes or via its
    wrapped ``_base`` for PolicyAwareToolRegistry chains.
    """
    from context.compaction import CompactionRecovery

    _file_cache = None
    _skill_buf = getattr(registry, "_skill_buffer", None)

    # Walk registry wrappers to find the inner ToolRegistry
    _inner = registry
    while hasattr(_inner, "_base"):
        _inner = _inner._base
    if hasattr(_inner, "_tools"):
        _tools = _inner._tools
        rt = _tools.get("Read") or _tools.get("file_read")
        if rt is not None and hasattr(rt, "_read_cache"):
            _file_cache = rt._read_cache
        if _skill_buf is None:
            st = _tools.get("Skill")
            if st is not None and hasattr(st, "_buffer"):
                _skill_buf = st._buffer

    return CompactionRecovery(
        file_cache=_file_cache,
        skill_buffer=_skill_buf,
        project_dir=project_dir or ".",
    )


Agent = ReActAgent
