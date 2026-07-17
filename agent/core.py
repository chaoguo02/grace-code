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

import json
import logging
import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from xml.etree import ElementTree as ET

from core.policy import TaskPolicy, build_task_policy
from agent.runtime_controller import RecoveryAction, ToolDecision
from agent.event_log import EventLog, summarize_run
from context.evidence import EvidenceLedger
from context.history import ConversationHistory, ConversationSnapshot
from context.repo_map import RepoMap
from context.token_budget import TokenBudget
from agent.prompt import (
    build_system_prompt,
    build_system_prompt_core,
    build_system_prompt_variable,
    build_task_prompt,
    consume_prompt_usage_metadata,
    reflection_test_failed,
    set_project_dir,
)
from agent.task import (
    Action, ActionType, Event, EventType,
    Observation, ObservationStatus, RunResult, RunStatus, Task, TaskIntent, ToolCall,
    TerminationReason, ToolOutcome, VerificationReason, VerificationStatus,
)
from context.artifacts import ArtifactStore
from context.compaction import ConversationCompactor
from context.manager import ContextManager, ContextManagerConfig, RequestContext
from llm.base import LLMBackend, LLMMessage, LLMToolSchema
from hooks.events import HookEvent
from observability.datasets import append_failure_dataset_item
from observability.models import (
    build_analysis_run_metadata,
    build_generation_input,
    build_generation_metadata,
    build_generation_output,
    build_run_metadata,
    build_run_output,
    build_tool_input,
    build_tool_output,
    merge_metadata,
)
from observability.scores import build_run_scores
from observability.tracing import get_observer
from core.base import (
    ToolConcurrency,
    ToolEffect,
    ToolErrorType,
    ToolRegistry,
    ToolRetryDirective,
    ToolRole,
)

if TYPE_CHECKING:
    from agent.completion_guard import CompletionCheckResult
    from memory.context import MemoryContext
    from memory.session_memory import SessionMemoryTracker
    from agent.session.task_state_machine import TaskStateMachine
    from agent.session.run_context import CancellationToken

logger = logging.getLogger(__name__)

_V2_DELEGATION_BLOCK_PREFIX = "BLOCKED_BY_DELEGATION_POLICY:"
_MAX_STOP_HOOK_RETRIES = 3


class _ChildTurnPhase(str, Enum):
    NONE = "none"
    SYNTHESIS = "synthesis"
    RESOLUTION_PENDING = "resolution_pending"


@dataclass(frozen=True)
class _TaskNotificationFacts:
    worktree_disposition: str | None = None


def _task_notification_facts_from_text(text: str) -> tuple[_TaskNotificationFacts, ...]:
    """Parse Runtime-owned task-notification payloads into typed facts."""
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


def _without_new_agent_spawns(
    tools: list[LLMToolSchema],
    *,
    phase: _ChildTurnPhase,
) -> list[LLMToolSchema]:
    """Withdraw fresh Agent spawning on child-result turns.

    Existing child control and worktree review tools remain visible. This keeps
    the parent focused on synthesizing or resolving just-finished child work
    rather than immediately fanning out again in the same recovery turn.
    """
    if phase is _ChildTurnPhase.NONE:
        return tools
    return [tool for tool in tools if tool.name != "Agent"]


def _observations_include_child_notifications(
    observations: list[Observation],
) -> bool:
    """Return whether this tool batch yielded child-completion payloads."""
    return any(
        isinstance(observation.output, str)
        and "<task-notification>" in observation.output
        for observation in observations
    )


def _phase_from_observations(
    observations: list[Observation],
) -> _ChildTurnPhase:
    phase = _ChildTurnPhase.NONE
    for observation in observations:
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

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Agent 运行时配置，从 config/default.yaml 加载后传入。"""
    max_steps: int = 40
    budget_tokens: int = 160_000            # task spend 上限（billable tokens）
    request_budget_tokens: int = 110_000   # 单次 request 输入上下文预算 (85% of 128K)
    artifact_threshold_tokens: int = 2_000 # 工具输出超过此值时 artifact 化
    artifact_storage_dir: str = ""  # optional absolute override; default is isolated state root
    missing_test_target_max_followups: int = 2  # pytest 路径缺失后最多允许的确认性探索步数
    max_parallel_tool_calls: int = 3  # Runtime cap; model guidance is not enforcement
    history_max_messages: int = 200        # 历史最大条数
    llm_max_retries: int = 3               # LLM 调用失败最大重试次数
    llm_retry_delay: float = 2.0           # 重试间隔（秒，指数退避）
    stream: bool = False                   # 是否启用流式输出
    stream_callback: object = None         # StreamCallback，最终回答流式回调
    thought_callback: object = None        # StreamCallback，推理过程流式回调（推理模型专用）
    token_callback: Callable[[int], None] | None = None
    """Receives cumulative billable token usage after each model response."""
    cancellation_token: "CancellationToken | None" = None
    """Runtime-owned cooperative cancellation shared with delegated runs."""
    completion_fact_check: "Callable[[], CompletionCheckResult] | None" = None
    """Runtime-injected objective completion check; no LLM interpretation."""
    runtime_message_source: Callable[[], list[LLMMessage]] | None = None
    """Pulls typed Runtime events into history before each model request."""
    stop_hook_event: HookEvent = HookEvent.STOP
    """Typed terminal hook for this agent role (Stop or SubagentStop)."""
    hook_session_id: str = ""
    hook_agent_id: str = ""
    hook_agent_type: str = ""
    hook_dispatcher: object = None
    """Runtime-owned lifecycle dispatcher; registry remains the fallback."""
    confirm_dangerous: bool = False        # 是否对危险命令要求用户确认
    effort: str = ""                       # reasoning effort (low/medium/high/xhigh/max)
    confirm_callback: object = None        # ConfirmCallback，None=跳过确认
    compact_history: bool = True           # 是否启用历史压缩
    is_subagent: bool = False              # True=使用精简 system prompt
    circuit_breaker: object = None         # CircuitBreaker | None — 代码级熔断器
    streaming_tool_execution: bool = False
    """CC-aligned: dispatch tool_use blocks during LLM streaming (Phase 1b)."""
    token_budget_continuation: bool = False
    """CC-aligned: nudge model to continue when token budget has room (Phase 2)."""


# ---------------------------------------------------------------------------
# Recovery State (CC-aligned continue-site tracking)
# ---------------------------------------------------------------------------

@dataclass
class RecoveryState:
    """Tracks recovery attempts across loop iterations (CC: State fields).

    CC's queryLoop uses immutable State replacement; we use a mutable dataclass
    that the agent loop updates in place for simplicity.
    """
    # max_output_tokens escalation
    escalation_applied: bool = False
    """First 8k truncation → silently bump to 64k (CC: maxOutputTokensOverride)."""
    output_recovery_count: int = 0
    """Times 'Resume directly' injected after escalation (CC: maxOutputTokensRecoveryCount)."""
    # reactive compact
    has_attempted_reactive_compact: bool = False
    """Circuit breaker: only attempt full reactive compaction once (CC: same name)."""
    # token budget continuation (nudge)
    nudge_count: int = 0
    """Consecutive nudge rounds in the current continuation phase."""
    last_nudge_tokens: int = 0
    """Billable tokens at the previous nudge check (for diminishing-returns detection)."""

    _MAX_OUTPUT_RECOVERY: int = 3
    _DIMINISHING_THRESHOLD: int = 500
    _COMPLETION_RATIO: float = 0.9
    _ESCALATED_MAX_TOKENS: int = 64000

    def can_escalate(self, current_max_tokens: int) -> bool:
        return not self.escalation_applied and current_max_tokens < self._ESCALATED_MAX_TOKENS

    def can_recover_output(self) -> bool:
        return self.output_recovery_count < self._MAX_OUTPUT_RECOVERY

    def can_reactive_compact(self) -> bool:
        return not self.has_attempted_reactive_compact

    def is_diminishing(self, current_tokens: int) -> bool:
        """CC: 3+ continuations AND delta < 500 twice in a row."""
        if self.nudge_count < 3:
            return False
        delta = current_tokens - self.last_nudge_tokens
        return delta < self._DIMINISHING_THRESHOLD

    def should_nudge(self, total_tokens: int, budget: int) -> bool:
        """Return True if budget has room (>10%) AND not diminishing."""
        if budget <= 0:
            return False
        return (
            total_tokens < int(budget * self._COMPLETION_RATIO)
            and not self.is_diminishing(total_tokens)
        )

    def reset_for_new_turn(self) -> None:
        """Reset per-turn guards (called after successful recovery)."""
        self.has_attempted_reactive_compact = False


# ---------------------------------------------------------------------------
# 共享工具函数
# ---------------------------------------------------------------------------

def _capture_git_state(repo_path: str) -> "GitState":
    """Capture an objective, side-effect-free workspace baseline."""
    from executor.workspace_facts import capture_workspace_snapshot
    from executor.process import GitState

    state = GitState(repo_path=repo_path)
    snapshot = capture_workspace_snapshot(repo_path)
    state.baseline_snapshot = snapshot
    state.current_snapshot = snapshot
    state.base_commit = snapshot.head_commit
    state.base_commit_short = snapshot.head_commit[:8]
    state.is_git_repo = snapshot.is_git_repo
    state.dirty_at_start = bool(snapshot.files or snapshot.current_patch)
    return state


def _refresh_git_state(state: "GitState") -> "GitState":
    """Compare current workspace facts with the immutable run baseline."""
    from executor.workspace_facts import capture_workspace_snapshot, compare_workspace_snapshots

    if not state.is_git_repo:
        return state
    baseline = state.baseline_snapshot or capture_workspace_snapshot(state.repo_path)
    current = capture_workspace_snapshot(state.repo_path)
    delta = compare_workspace_snapshots(baseline, current)
    state.current_snapshot = current
    state.has_changes = delta.has_changes
    state.files_changed = list(delta.changed_paths)
    state.current_diff = delta.attributable_patch
    return state


# ---------------------------------------------------------------------------
# ReActAgent — ReAct (Reasoning + Acting) 主循环
# ---------------------------------------------------------------------------

class ReActAgent:
    """
    ReAct 主循环实现。

    用法：
        agent = ReActAgent(backend, registry, config)
        result = agent.run(task, log)

    这是一个纯粹的 ReAct agent：每步 思考→行动→观察，循环直到完成或超限。
    V2 入口通过 SessionRuntime + agent_name 区分权限和可见性。
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
    ) -> None:
        self._backend = backend
        self._full_registry = registry
        self._registry = registry
        self._cfg = config or AgentConfig()
        self._controller_factory = controller_factory  # injected by AgentFactory
        self._memory_context = memory_context
        self._session_memory_tracker = session_memory_tracker
        self._state_machine = state_machine  # Runtime-centric: TSM is the SSOT for task lifecycle
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
        self._stop_hook_count = 0

    @property
    def step_count(self) -> int:
        """返回当前执行的步数（第几步）。"""
        return getattr(self, "_current_step", 0)

    @property
    def artifact_store(self) -> ArtifactStore:
        return self._artifact_store

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

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
        from executor.state_paths import ProjectStatePaths, StateIsolationError
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
        set_project_dir(task.repo_path)

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

    def _run_body(self, task: Task, log: EventLog, *, policy: TaskPolicy) -> RunResult:
        """核心循环：所有 return 路径都走这里，由 run() 负责策略包裹和恢复。"""
        self._active_policy = policy
        # 按 repo_path 隔离 repo_map 缓存，换 repo 时自动重建
        cache_key = task.repo_path
        if getattr(self, "_repo_map_cache_key", None) != cache_key:
            if hasattr(self, "_repo_map_cache"):
                del self._repo_map_cache
            self._repo_map_cache_key = cache_key

        # 设置任务上下文，用于记忆相关性过滤
        if self._memory_context:
            self._memory_context.set_task_context(task.description)

        # ── Long-term memory: build once, cached for the run ─────
        if hasattr(self, "_long_term_context"):
            del self._long_term_context
        self._build_long_term_context()

        self._accessed_files: set[str] = set()
        self._feedback_injected_files: set[str] = set()
        self._explicit_memory_write_this_run = False
        self._evidence_ledger = EvidenceLedger()
        evidence_ledger_ref = getattr(self._full_registry, "_evidence_ledger_ref", None)
        if evidence_ledger_ref is not None:
            evidence_ledger_ref.ledger = self._evidence_ledger
        self._accumulated_structured_findings: list[dict] = []
        observer = get_observer()
        task_context = observer.start_task(task)
        task_obs = task_context.__enter__()
        task_obs_closed = False
        log.log_task_start(task)
        logger.info("Agent starting task %s", task.task_id)

        # 初始化上下文管理器
        # 如果调用方（ChatSession）注入了共享 history，直接复用；
        # 否则新建（单次 run 模式）
        if hasattr(self, "_pending_history") and self._pending_history is not None:
            history = self._pending_history
        else:
            history = ConversationHistory(max_messages=self._cfg.history_max_messages)
            # 单次模式：把任务描述作为第一条 user 消息
            from agent.prompt import build_task_prompt
            history.add(LLMMessage(
                role="user",
                content=build_task_prompt(
                    task.description, task.repo_path, task.issue_url,
                    intent=self._task_intent,
                ),
            ))

        # ── Capability Snapshot: environment facts as deterministic Runtime input ──
        from executor.project_environment import CapabilitySnapshot
        _caps = CapabilitySnapshot.probe(task.repo_path)
        history.add(LLMMessage(role="user", content=_caps.render_for_agent()))
        logger.info("Capability: %s", _caps.render_for_agent())

        token_budget = TokenBudget(total=self._cfg.request_budget_tokens)
        repo_map = RepoMap(task.repo_path)

        # ── Baseline git state: capture BEFORE this run ──
        # Track file names (not raw diff) for true incremental diff at finish.
        # This prevents prior worktree dirt from being reported as "this run's changes."
        _git_state = _capture_git_state(task.repo_path)

        total_tokens = 0
        # Verification is an observed fact. Missing tooling is UNAVAILABLE,
        # never equivalent to a successful validation.
        _verification_ok = False
        _test_was_run = False  # True if ANY test/validate tool was invoked (regardless of result)
        self._stop_hook_verify_count = 0  # Stop Hook: retry count for verification
        # consecutive_failures is now derived from CircuitBreaker — the single source of truth.
        # No more manual local counter. See _get_consecutive_failures().
        def _get_consecutive_failures() -> int:
            if self._cfg.circuit_breaker is not None:
                return getattr(self._cfg.circuit_breaker, "_consecutive_tool_errors", 0)
            return 0

        _max_consecutive_failures = (
            self._cfg.circuit_breaker.config.max_consecutive_tool_errors
            if self._cfg.circuit_breaker is not None
            else 3
        )
        reflection_counts: dict[str, int] = {}  # reason -> count
        # ── Task completion guard (Runtime validates before accepting FINISH) ──
        from agent.completion_guard import CompletionContext, TaskCompletionGuard
        completion_ctx = CompletionContext()
        completion_guard = TaskCompletionGuard()

        # ── P0: Unified execution budget ──
        from agent.session.execution_budget import ExecutionBudget, ExecutionBudgetConfig, BudgetLevel
        _execution_budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=task.budget_tokens,
            step_limit=task.max_steps,
        ))
        _execution_budget.start()
        from agent.session.run_context import CancellationToken, RunContext
        _cancellation = self._cfg.cancellation_token or CancellationToken()
        # Child authority starts from effects that are physically visible to
        # the parent after registry + task policy filtering. Result delivery
        # remains available as a control-plane capability.
        _delegation_effects = {ToolEffect.PRODUCE_DELIVERABLE}
        for _tool_name in self._registry.tool_names:
            _tool_metadata = self._registry.metadata_for(_tool_name)
            if (
                _tool_metadata is not None
                and ToolRole.DELEGATE not in _tool_metadata.roles
            ):
                _delegation_effects.update(_tool_metadata.effects)
        _delegation_effects = frozenset(_delegation_effects)
        _base_run_context = RunContext(
            budget=_execution_budget,
            cancellation=_cancellation,
            delegation_step_limit=task.max_steps,
            phase_policy=policy.execution,
            delegation_effects=_delegation_effects,
        )
        self._registry = self._registry.with_run_context(_base_run_context)
        missing_test_target_followups: int | None = None
        missing_test_target_message: str | None = None
        missing_test_target_detected_step: int | None = None
        _cumulative_tool_calls = 0
        # 累计 prompt caching 统计
        from llm.base import CacheStats
        cumulative_cache = CacheStats()

        # ── Runtime Controller: injected by AgentFactory (DI, not internal new) ──
        from agent.runtime_controller import RuntimeController, StepAction
        _ControllerCls = self._controller_factory or RuntimeController
        _runtime_controller = _ControllerCls(
            budget=_execution_budget,
            breaker=self._cfg.circuit_breaker,
            max_steps=task.max_steps,
            budget_tokens=task.budget_tokens,
            max_consecutive_failures=_max_consecutive_failures,
        )

        # ── TaskStateMachine: Runtime's central authority for task lifecycle ──
        # If AgentFactory injected a TSM, use it; otherwise create one here
        # (backward compat for callers that don't go through AgentFactory).
        _tsm = self._state_machine
        if _tsm is None:
            from agent.session.task_state_machine import TaskStateMachine, TaskState
            _tsm = TaskStateMachine(task_id=task.task_id)
        else:
            # Update placeholder task_id with the real one
            _tsm.task_id = task.task_id

        # ── Register TSM guards — Runtime-enforced transition conditions ──
        from agent.session.task_state_machine import (
            circuit_breaker_guard,
            consecutive_failures_guard, git_diff_guard,
            stop_hook_retry_guard,
            GuardContext, GuardTransition,
        )
        _tsm.add_guard(GuardTransition.RUNNING_TO_FAILED, circuit_breaker_guard)
        _tsm.add_guard(GuardTransition.RUNNING_TO_FAILED, consecutive_failures_guard)
        _tsm.add_guard(GuardTransition.COMPLETING_TO_COMPLETED, git_diff_guard)
        _tsm.add_guard(GuardTransition.COMPLETING_TO_FAILED, stop_hook_retry_guard)

        def _finish_run(
            *,
            status: RunStatus,
            summary: str,
            steps_taken: int,
            total_tokens_used: int,
            patch: str | None = None,
            error: str | None = None,
            cache_stats: CacheStats | None = None,
        ) -> RunResult:
            nonlocal task_obs_closed

            # ── Refresh objective workspace facts for the completion record ──
            _refresh_git_state(_git_state)
            if _git_state.has_changes:
                _patch_text = (
                    f"\n{_git_state.current_diff[:3000]}"
                    if _git_state.current_diff else
                    "\nRaw incremental patch is not attributable; the workspace revision changed."
                )
                _changed_text = (
                    ", ".join(sorted(_git_state.files_changed)[:10])
                    or "(revision changed; no attributable path list)"
                )
                summary = (
                    f"{summary}\n\n"
                    f"--- WORKSPACE DELTA (this run: {len(_git_state.files_changed)} files) ---\n"
                    f"Changed: {_changed_text}\n"
                    f"{_patch_text}"
                )

            # Verification outcome is orthogonal to lifecycle state.
            _needs_unverified_tag = _git_state.has_changes or (
                completion_ctx.had_any_write and not _git_state.is_git_repo
            )
            if status == RunStatus.SUCCESS and _needs_unverified_tag and not _verification_ok:
                _reason = _tsm.verification_reason
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
                task_id=task.task_id,
                status=status,
                summary=summary,
                steps_taken=steps_taken,
                total_tokens=total_tokens_used,
                patch=patch,
                error=error,
                cache_stats=cache_stats,
                termination_reason=_tsm.termination_reason,
                verification_status=_tsm.verification_status,
                verification_reason=_tsm.verification_reason,
            )
            run_stats = summarize_run(log)
            analysis_metadata = build_analysis_run_metadata(
                run_stats=run_stats,
                context_stats=getattr(self, "_last_context_stats", None),
            )
            task_obs.update(
                output=build_run_output(result),
                metadata=merge_metadata(
                    build_run_metadata(result),
                    {
                        "reflections": reflection_counts,
                        "consecutive_failures": _get_consecutive_failures(),
                    },
                    analysis_metadata,
                ),
            )
            for score in build_run_scores(task, result, stats=run_stats):
                task_obs.score(
                    name=score.name,
                    value=score.value,
                    comment=score.comment,
                    metadata=score.metadata,
                )
            append_failure_dataset_item(task, result, log_path=log.path, stats=run_stats)
            if not task_obs_closed:
                task_context.__exit__(None, None, None)
                task_obs_closed = True
            return result

        # ── Workspace setup: ensure isolated environment before RUNNING ──
        from executor.process import LocalRuntime as _SetupRuntime
        _setup_rt = _SetupRuntime()
        _setup_rt.setup_workspace(task.repo_path)

        # Transition to RUNNING — the Runtime now owns the lifecycle
        from agent.session.task_state_machine import TaskState as TSMState
        _tsm.transition(TSMState.RUNNING, "workspace ready")
        _child_turn_phase = _ChildTurnPhase.NONE
        _recovery = RecoveryState()  # CC: cross-turn recovery tracking

        for step in range(1, task.max_steps + 1):
            if _cancellation.is_cancelled:
                _tsm.cancel(_cancellation.detail)
                log.log_task_failed(steps=step - 1, reason=_cancellation.detail)
                return _finish_run(
                    status=RunStatus.CANCELLED,
                    summary=f"Task cancelled: {_cancellation.detail}",
                    steps_taken=step - 1,
                    total_tokens_used=total_tokens,
                    error=_cancellation.detail,
                    cache_stats=cumulative_cache,
                )
            _tsm.record_step()
            self._current_step = step  # 用于 compaction 日志
            self.compactor.tick_step()
            logger.debug("Step %d/%d", step, task.max_steps)

            _runtime_messages: list[LLMMessage] = []
            if self._cfg.runtime_message_source is not None:
                try:
                    _runtime_messages = self._cfg.runtime_message_source()
                    history.add_many(_runtime_messages)
                except Exception:
                    logger.exception("Failed to load Runtime messages")
            runtime_phase = _phase_from_runtime_messages(_runtime_messages)
            _child_turn_phase = _advance_child_turn_phase(
                _child_turn_phase,
                runtime_phase=runtime_phase,
            )

            # ── Runtime Controller: single pre-step enforcement gate ──
            # Replaces scattered inline checks. Returns StepDecision that the
            # loop MUST obey. The model has no opportunity to override.
            _last_stats = getattr(self, "_last_context_stats", None)
            _ctx_size = _last_stats.estimated_total_tokens if _last_stats else 0
            _req_budget = _last_stats.request_budget_tokens if _last_stats else self._backend.max_context_window
            decision = _runtime_controller.check(
                step=step,
                total_tokens=total_tokens,
                history=history,
                log=log,
                context_size=_ctx_size,
                request_budget=_req_budget,
                consecutive_failures=_get_consecutive_failures(),
            )
            if decision.action == StepAction.TERMINATE:
                _tsm.fail(decision.terminate_reason, decision.terminate_detail)
                log.log_task_failed(steps=step, reason=decision.terminate_detail or decision.terminate_reason.value)
                _term_status = decision.terminate_status or RunStatus.GAVE_UP
                return _finish_run(
                    status=_term_status,
                    summary=decision.terminate_summary,
                    steps_taken=step,
                    total_tokens_used=total_tokens,
                    cache_stats=cumulative_cache,
                )
            if decision.strip_tools:
                # Tools stripped for this step — model can only produce text
                pass

            # ── TSM Guard evaluation: second layer of Runtime defense ──
            # Guards are evaluated AFTER RuntimeController (which handles budget
            # escalation with inject_message). Guards that request termination
            # provide an additional safety net.
            _guard_ctx = GuardContext(
                step=step, max_steps=task.max_steps,
                consecutive_failures=_get_consecutive_failures(),
                task_intent=task.intent,
                budget=_execution_budget,
                breaker=self._cfg.circuit_breaker,
                tsm=_tsm,
            )
            _guard_result = _tsm.evaluate_guards(
                GuardTransition.RUNNING_TO_FAILED,
                _guard_ctx,
            )
            if not _guard_result.passed and _guard_result.terminate:
                _tsm.fail(TerminationReason.GUARD_REJECTED, _guard_result.reason)
                log.log_task_failed(steps=step, reason=_guard_result.reason)
                return _finish_run(
                    status=RunStatus.GAVE_UP,
                    summary=_guard_result.reason,
                    steps_taken=step,
                    total_tokens_used=total_tokens,
                    cache_stats=cumulative_cache,
                )
            if decision.inject_message:
                history.add(LLMMessage(role="user", content=decision.inject_message))

            # ── 1. System-state warnings (MUST inject BEFORE _build_messages) ──
            # These are Runtime-enforced signals, not conversational hints.
            # They must be in history before message assembly so the model sees
            # them THIS turn, not next turn.

            # ── 2. 组装 messages，调用 LLM ──────────────────────────────
            if self._memory_context:
                last_user_msg = history.get_last_user_message()
                if last_user_msg:
                    self._memory_context.set_user_message(last_user_msg)

            messages = self._build_messages(
                history, token_budget, repo_map,
                consumed_tokens=total_tokens,
                max_context_window=self._backend.max_context_window,
            )

            tools = [] if decision.strip_tools else self._registry.get_schemas()
            tools = _without_new_agent_spawns(
                tools, phase=_child_turn_phase,
            )
            _live_spawn_context = None
            if any(
                ToolRole.DELEGATE in self._registry.metadata_for(schema.name).roles
                for schema in tools
            ):
                from agent.session.run_context import AgentSpawnContext
                from context.history import ConversationSnapshotError
                try:
                    # Capture before the provider call: this is the immutable
                    # request boundary, and excludes the assistant action the
                    # provider is about to produce.
                    _live_spawn_context = AgentSpawnContext.capture(
                        messages=messages,
                        parent_session_id=str(
                            task.metadata.get("session_id") or task.task_id
                        ),
                        parent_agent_name=str(
                            task.metadata.get("agent_name") or "primary"
                        ),
                        repo_path=task.repo_path,
                        model_name=self._backend.model_name,
                        tool_schemas=tools,
                    )
                except ConversationSnapshotError as exc:
                    # Named subagents remain fresh-context in this batch.
                    # A future inherited-context request must fail closed
                    # when this typed boundary is unavailable.
                    logger.warning(
                        "Live conversation snapshot unavailable for delegation: %s",
                        exc,
                    )

            # ── LLM call: streaming dispatch (Phase 1b) or classic complete ──
            _streaming_executor: StreamingToolExecutor | None = None
            response: Any = None  # bound in classic path; None for streaming
            if self._cfg.streaming_tool_execution:
                # CC-aligned: dispatch tool_use blocks during LLM streaming.
                # The executor is created BEFORE the LLM call so tool_use events
                # can be enqueued mid-stream (speculative execution).
                _streaming_executor = StreamingToolExecutor(execution_registry)
                try:
                    action = self._stream_and_dispatch(
                        messages, tools, _streaming_executor,
                    )
                except Exception as exc:
                    logger.error("LLM stream failed at step %d: %s", step, exc)
                    _tsm.fail(TerminationReason.MODEL_ERROR, f"LLM error: {exc}")
                    log.log_task_failed(steps=step, reason=f"LLM error: {exc}")
                    return _finish_run(
                        status=RunStatus.FAILED,
                        summary=f"LLM stream failed: {exc}",
                        steps_taken=step, total_tokens_used=total_tokens,
                        error=str(exc), cache_stats=cumulative_cache,
                    )
                # Token estimation for streaming path (refined when backend
                # propagates usage through StreamEvent.FINISH)
                from context.token_budget import estimate_tokens
                _input_est = sum(estimate_tokens(str(m.content)) for m in messages)
                _output_est = estimate_tokens(
                    action.message or action.thought or ""
                )
                billable_tokens = _input_est + _output_est
            else:
                try:
                    response = self._call_with_retry(messages, tools)
                except Exception as exc:
                    _exc_str = str(exc).lower()
                    # ── Recovery C: prompt-too-long → reactive compact (CC: reactive_compact_retry) ──
                    if (
                        any(kw in _exc_str for kw in ("prompt too long", "context length", "413", "reduce the length"))
                        and _recovery.can_reactive_compact()
                        and self.compactor is not None
                    ):
                        _recovery.has_attempted_reactive_compact = True
                        logger.warning(
                            "Prompt too long — attempting reactive compact (CC: reactive_compact_retry)"
                        )
                        try:
                            self.compactor.compact(history, total_tokens)
                            logger.info("Reactive compact succeeded — retrying LLM call")
                            continue
                        except Exception as _cexc:
                            logger.warning("Reactive compact failed: %s", _cexc)
                    logger.error("LLM call failed at step %d after retries: %s", step, exc)
                    _tsm.fail(TerminationReason.MODEL_ERROR, f"LLM error: {exc}")
                    log.log_task_failed(steps=step, reason=f"LLM error: {exc}")
                    return _finish_run(
                        status=RunStatus.FAILED,
                        summary=f"LLM call failed: {exc}",
                        steps_taken=step,
                        total_tokens_used=total_tokens,
                        error=str(exc),
                        cache_stats=cumulative_cache,
                    )
                action = response.action
                billable_tokens = response.total_tokens
                if response.cache_stats and response.cache_stats.has_cache_activity:
                    cumulative_cache.cache_read_tokens += response.cache_stats.cache_read_tokens
                    cumulative_cache.cache_creation_tokens += response.cache_stats.cache_creation_tokens
                    cumulative_cache.non_cached_input_tokens += response.cache_stats.non_cached_input_tokens
                    billable_tokens = max(0, billable_tokens - response.cache_stats.cache_read_tokens)

            total_tokens += billable_tokens
            _execution_budget.consume(billable_tokens)
            _execution_budget.record_step()
            if self._cfg.token_callback is not None:
                self._cfg.token_callback(total_tokens)

            # ── Recovery A: output truncation (CC: max_output_tokens_escalate/recovery) ──
            _truncated = (
                getattr(response, "finish_reason", "") == "length"
                or response.output_tokens >= getattr(self._cfg, "max_tokens", 32000) - 100
            )
            if _truncated and action.action_type != ActionType.TOOL_CALL:
                if _recovery.can_escalate(getattr(self._cfg, "max_tokens", 32000)):
                    _recovery.escalation_applied = True
                    logger.info("Output truncated — escalating max_tokens 8k→64k (CC: max_output_tokens_escalate)")
                    self._cfg.max_tokens = RecoveryState._ESCALATED_MAX_TOKENS
                    continue
                elif _recovery.can_recover_output():
                    _recovery.output_recovery_count += 1
                    logger.info("Output still truncated after escalation — injecting recovery (attempt %d/%d)",
                                _recovery.output_recovery_count, RecoveryState._MAX_OUTPUT_RECOVERY)
                    history.add(LLMMessage(role="user", content=(
                        "[SYSTEM] Output truncated. Resume directly — no apology, no recap."
                    )))
                    continue
                else:
                    logger.warning("Output recovery exhausted after %d attempts", RecoveryState._MAX_OUTPUT_RECOVERY)

            # ── Recovery B: prompt-too-long → reactive compact (CC: reactive_compact_retry) ──
            # Triggered by exception in LLM call above; handled in the except block.
            # We add the compact-attempt check here for the non-exception path
            # (model may signal context pressure via short responses).

            # Provider adapters may omit native call ids (notably text/DSML
            # fallbacks). Runtime owns protocol normalization so persisted
            # assistant/tool pairs always remain provider-valid.
            if action.action_type == ActionType.TOOL_CALL and action.tool_calls:
                import hashlib as _call_hash
                for _call_index, _tool_call in enumerate(action.tool_calls):
                    if not _tool_call.id:
                        _identity = (
                            f"{task.task_id}:{step}:{_call_index}:{_tool_call.name}"
                        ).encode("utf-8")
                        _tool_call.id = (
                            "runtime_call_"
                            + _call_hash.sha256(_identity).hexdigest()[:24]
                        )

            # ── Control Plane: validate tool calls against registered schemas ──
            # The LLM is an "action generator" — its output MUST conform to the
            # tool contract. Invalid tool calls are rejected HERE, at the control
            # plane, BEFORE they reach the Runtime. The LLM gets a structured
            # error and can self-correct on the next turn.
            if action.action_type == ActionType.TOOL_CALL and action.tool_calls and not tools:
                # `tools=[]` is a Runtime authority boundary, not merely a hint
                # to the provider.  Some OpenAI-compatible providers can still
                # emit textual/DSML tool calls after schemas are withdrawn.
                history.add(LLMMessage(
                    role="user",
                    content=(
                        "[SYSTEM] Tool calls are disabled for this finalization turn. "
                        "Return the requested final answer directly without tools."
                    ),
                ))
                continue

            if action.action_type == ActionType.TOOL_CALL and action.tool_calls and tools:
                from llm.tool_call_validator import validate_tool_calls
                _validation = validate_tool_calls(action.tool_calls, tools)
                if not _validation.valid:
                    logger.warning(
                        "Control plane rejected tool call: %s — %s",
                        _validation.error_type, _validation.error_message,
                    )
                    # Build a synthetic error observation — the LLM sees this
                    # and can self-correct on the next turn.
                    from core.base import ToolResult as _TR
                    _fake_result = _TR.from_error(
                        error_type=ToolErrorType.INVALID_PARAMS,
                        retry=ToolRetryDirective.RETRY,
                        detail=_validation.error_message,
                    )
                    _observation = _fake_result.to_observation(
                        _validation.offending_tool or (action.tool_calls[0].name if action.tool_calls else "unknown")
                    )
                    observations = [_observation]
                    # Skip tool execution entirely — go straight to post-tool processing
                    log.log_action(step=step, action=action, raw_content=getattr(response, "raw_content", ""))
                    break  # exit the for-step loop, let the LLM see the error
                else:
                    # Validation passed — proceed to normal tool execution below
                    pass

            # ── SessionMemory tick ─────────────────────────────────────
            _this_turn_has_tools = (
                action.action_type == ActionType.TOOL_CALL and bool(action.tool_calls)
            )
            if _this_turn_has_tools:
                _cumulative_tool_calls += len(action.tool_calls or [])
            if self._session_memory_tracker:
                context_for_extraction = ""
                if history:
                    context_for_extraction = self._build_session_memory_context(history)
                self._session_memory_tracker.tick(
                    current_tokens=total_tokens,
                    current_tool_calls=_cumulative_tool_calls,
                    context_summary=context_for_extraction,
                )

            # ── 2. 写入 Action event ────────────────────────────────────
            log.log_action(step=step, action=action, raw_content=getattr(response, "raw_content", ""))
            logger.info("Step %d: %r", step, action)

            # ── 4. 终止 action ──────────────────────────────────────────
            if action.action_type == ActionType.FINISH:
                # ── Recovery D: token budget continuation (CC: token_budget_continuation) ──
                if self._cfg.token_budget_continuation and _recovery.should_nudge(total_tokens, task.budget_tokens):
                    _recovery.last_nudge_tokens = total_tokens
                    _recovery.nudge_count += 1
                    _remaining = max(0, task.budget_tokens - total_tokens)
                    logger.info(
                        "Token budget nudge %d (remaining=%d, total=%d, budget=%d)",
                        _recovery.nudge_count, _remaining, total_tokens, task.budget_tokens,
                    )
                    history.add(LLMMessage(role="user", content=(
                        f"[SYSTEM] Token budget remaining: {_remaining}. "
                        "Continue working on the task if there are remaining items. "
                        "If you believe the task is complete, call finish again."
                    )))
                    continue

                # ── Runtime: transition to COMPLETING before guard evaluation ──
                _tsm.transition(TSMState.COMPLETING, "model called FINISH")

                fact_check = self._cfg.completion_fact_check
                if fact_check is not None:
                    fact_result = fact_check()
                    if not fact_result.can_complete:
                        logger.warning(
                            "Completion blocked by runtime facts: %s",
                            fact_result.blocked_reason,
                        )
                        history.add(LLMMessage(
                            role="user", content=fact_result.inject_message,
                        ))
                        _tsm.transition(
                            TSMState.RUNNING,
                            f"completion blocked: {fact_result.blocked_reason}",
                        )
                        continue

                stop_message = self._run_stop_hook(
                    history,
                    stop_hook_active=self._stop_hook_count > 0,
                    last_assistant_message=action.message or "",
                )
                if stop_message is not None:
                    next_count = self._stop_hook_count + 1
                    if next_count > _MAX_STOP_HOOK_RETRIES:
                        reason = f"Stop hook retry limit reached: {_MAX_STOP_HOOK_RETRIES}"
                        logger.warning(reason)
                        log.log_task_failed(steps=step, reason=reason)
                        _tsm.fail(TerminationReason.GUARD_REJECTED, reason)
                        return _finish_run(
                            status=RunStatus.GAVE_UP,
                            summary=reason,
                            steps_taken=step,
                            total_tokens_used=total_tokens,
                            cache_stats=cumulative_cache,
                        )
                    self._stop_hook_count = next_count
                    history.add(LLMMessage(role="user", content=stop_message))
                    _tsm.transition(TSMState.RUNNING, "stop hook blocked — back to loop")
                    continue

                self._stop_hook_count = 0

                # ── Completion guard: Runtime validates before accepting FINISH ──
                # The model cannot unilaterally declare "done" — the Runtime must
                # verify all completion conditions.
                # Git diff is the only fact that matters for completion
                _refresh_git_state(_git_state)
                guard_result = completion_guard.check(
                    ctx=completion_ctx,
                    task_intent=task.intent,
                    git_state=_git_state,
                )
                if not guard_result.can_complete:
                    logger.warning(
                        "Completion blocked: %s", guard_result.blocked_reason
                    )
                    _tsm.transition(TSMState.RUNNING, f"completion blocked: {guard_result.blocked_reason}")
                    history.add(LLMMessage(
                        role="user", content=guard_result.inject_message
                    ))
                    continue

                # ── Stop Hook: dispatcher-based (CC-aligned) ──
                # External hooks configured in settings.json fire through the
                # hook_dispatcher and can block finish with block/reason.
                # No hardcoded verification — the dispatcher is the only path.
                _stop_reason = self._run_stop_hook(
                    history=history,
                    stop_hook_active=(self._stop_hook_verify_count > 0),
                    last_assistant_message=action.message or "",
                )
                if _stop_reason is not None:
                    self._stop_hook_verify_count += 1
                    _tsm.transition(TSMState.RUNNING, f"stop hook blocked: {_stop_reason}")
                    continue

                # Reflection is a completion guard activity, not a lifecycle state.
                if not getattr(_tsm, "_reflection_done", False):
                    _tsm._reflection_done = True
                    _guard_ctx = GuardContext(task_intent=task.intent, tsm=_tsm)
                    _reflection_msg = ""
                    _reflection_guards = _tsm._guards.get(
                        GuardTransition.COMPLETING_TO_RUNNING, []
                    )
                    for _guard_fn in _reflection_guards:
                        try:
                            _gr = _guard_fn(_guard_ctx)
                            if _gr.inject_message:
                                _reflection_msg += _gr.inject_message + "\n\n"
                        except Exception:
                            pass
                    if _reflection_msg:
                        history.add(LLMMessage(role="user", content=_reflection_msg.strip()))
                        _tsm.transition(TSMState.RUNNING, "reflection — back to loop")
                        continue

                # GitState was refreshed by completion_guard.check() above.
                if _git_state.has_changes and _verification_ok:
                    _tsm.complete(
                        VerificationStatus.VERIFIED,
                        detail="guards passed + workspace delta + verification confirmed",
                    )
                elif _git_state.has_changes and _test_was_run and not _verification_ok:
                    _tsm.complete(
                        VerificationStatus.FAILED,
                        VerificationReason.TEST_FAILED,
                        "tests ran but failed",
                    )
                elif _git_state.has_changes and not _caps.pytest_available and not _verification_ok:
                    _tsm.complete(
                        VerificationStatus.UNAVAILABLE,
                        VerificationReason.NO_TEST_ENVIRONMENT,
                        "no test environment available",
                    )
                elif _git_state.has_changes and not _verification_ok:
                    _tsm.complete(
                        VerificationStatus.UNVERIFIED,
                        VerificationReason.NOT_RUN,
                        "guards passed — unverified",
                    )
                elif completion_ctx.had_any_write and not _git_state.is_git_repo:
                    _tsm.complete(
                        VerificationStatus.UNAVAILABLE,
                        VerificationReason.NO_VERSION_CONTROL,
                        "no Git fact source available",
                    )
                elif completion_ctx.had_any_write:
                    _tsm.complete(
                        VerificationStatus.UNVERIFIED,
                        VerificationReason.NO_NET_CHANGE,
                        "guards passed — no net workspace changes detected",
                    )
                else:
                    _tsm.complete(
                        VerificationStatus.NOT_APPLICABLE,
                        detail="guards passed — analysis/read-only task",
                    )

                summary = action.message or "Task complete."
                patch = _git_state.current_diff or None
                log.log_task_complete(steps=step, summary=summary)
                self._extract_success_memories(task, log, summary)
                _execution_budget.complete()
                return _finish_run(
                    status=RunStatus.SUCCESS,
                    summary=summary,
                    steps_taken=step,
                    total_tokens_used=total_tokens,
                    patch=patch,
                    cache_stats=cumulative_cache,
                )

            if action.action_type == ActionType.GIVE_UP:
                reason = action.message or "Agent gave up."
                _tsm.fail(TerminationReason.AGENT_GAVE_UP, reason)
                log.log_task_failed(steps=step, reason=reason)
                return _finish_run(
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=step,
                    total_tokens_used=total_tokens,
                    cache_stats=cumulative_cache,
                )

            # ── 5. 执行工具（支持并行 tool_calls）───────────────────────
            if action.action_type == ActionType.TOOL_CALL and action.tool_calls:
                observations: list[Observation] = []
                any_test_failed = False
                missing_test_target_observation: Observation | None = None

                # ── Batch dedup: skip duplicate (name, params) within same action ──
                import hashlib as _hlib, json as _json
                _batch_seen: set[str] = set()
                effective_tool_calls: list[ToolCall] = []
                for tc in action.tool_calls:
                    _tc_key = f"{tc.name}:{_hlib.sha256(_json.dumps(tc.params or {}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]}"
                    if _tc_key in _batch_seen:
                        logger.info("Batch dedup: skipping duplicate %s", tc.name)
                        continue
                    _batch_seen.add(_tc_key)
                    effective_tool_calls.append(tc)

                # ── StreamingToolExecutor: CC-aligned partition + dispatch ──
                # Replaces the old all-or-nothing PARALLEL_SAFE check with
                # per-call concurrency safety. Read-only Bash commands (ls, grep,
                # git status) can now execute in the same batch as Read/Grep.
                from core.streaming_executor import (
                    StreamingToolExecutor,
                    partition_tool_calls,
                )
                _batches = partition_tool_calls(effective_tool_calls, self._registry)

                # Build execution context with spawn_context for delegation tools
                execution_context = _base_run_context
                if any(
                    ToolRole.DELEGATE in self._registry.metadata_for(tc.name).roles
                    for tc in effective_tool_calls
                ):
                    execution_context = replace(
                        execution_context,
                        spawn_context=_live_spawn_context,
                    )
                # Multi-batch or multi-call: set delegation width for parallel-safe batches
                _max_batch = max(len(b) for b in _batches) if _batches else 1
                if _max_batch > 1:
                    execution_context = replace(
                        execution_context,
                        delegation_width=_max_batch,
                    )
                execution_registry = self._registry.with_run_context(execution_context)

                # ── Execute via StreamingToolExecutor (CC-aligned) ──
                # Reuse the streaming executor if already created (Phase 1b streaming
                # dispatch path). Otherwise create a fresh executor for classic mode.
                if _streaming_executor is not None:
                    _executor = _streaming_executor
                    # Tools may already be executing from mid-stream dispatch.
                    # Enqueue any that weren't already registered.
                    for _tc in effective_tool_calls:
                        _executor.enqueue(_tc)
                else:
                    _executor = StreamingToolExecutor(execution_registry)
                    for _batch in _batches:
                        for _tc in _batch:
                            _executor.enqueue(_tc)
                _executor.dispatch()
                # Collect preserves input order — zip with effective_tool_calls
                _ordered_results = _executor.collect()
                # Build lookup by tool name (observability fallback)
                _results_by_tc = dict(zip(
                    [tc.id for tc in effective_tool_calls],
                    _ordered_results,
                ))

                for call_index, tc in enumerate(effective_tool_calls):
                    metadata = self._registry.metadata_for(tc.name)
                    result = _ordered_results[call_index] if call_index < len(_ordered_results) else ToolResult.from_error_str("Tool execution lost result")

                    # Observability: wrap in tool span after execution
                    with observer.start_tool(
                        name=f"tool:{tc.name}",
                        input_data=build_tool_input(
                            tc.name, tc.params, action.thought or "", step,
                        ),
                        metadata=merge_metadata(
                            {"tool_name": tc.name, "step": step}, task.metadata,
                        ),
                    ) as tool_obs:
                        tool_obs.update(
                            output=build_tool_output(
                                result,
                                capture_tool_outputs=(
                                    observer.config.capture_tool_outputs
                                    if observer.config else True
                                ),
                            ),
                            metadata={
                                "tool_name": tc.name,
                                "duration_ms": result.duration_ms,
                            },
                        )
                    observation = result.to_observation(tc.name)

                    # Runtime intercepts typed environment failures before the LLM sees them.
                    if not observation.is_success():
                        _tool_err = getattr(result, "tool_error", None)
                        if (
                            _tool_err is not None
                            and _tool_err.error_type is ToolErrorType.ENVIRONMENT_UNAVAILABLE
                        ):
                            _block_msg = (
                                f"[RUNTIME] Task BLOCKED — environment issue detected:\n"
                                f"{_tool_err.detail}\n"
                                f"Suggestion: {_tool_err.alternative}\n"
                                "The task cannot continue until this is resolved. "
                                "Summarize your findings and call finish."
                            )
                            _tsm.fail(
                                TerminationReason.ENVIRONMENT_UNAVAILABLE,
                                _tool_err.detail,
                            )
                            _tsm.block_detail = {
                                "error_type": _tool_err.error_type.value,
                                "detail": _tool_err.detail,
                                "suggested_fix": _tool_err.alternative,
                                "tool": tc.name,
                            }
                            logger.warning("Runtime intercepted env blocker: %s", _tool_err.detail)
                            log.log_task_failed(steps=step, reason=_tool_err.detail)
                            return _finish_run(
                                status=RunStatus.BLOCKED,
                                summary=_block_msg,
                                steps_taken=step,
                                total_tokens_used=total_tokens,
                                error=_tool_err.detail,
                                cache_stats=cumulative_cache,
                            )

                    if ToolRole.PERSIST_MEMORY in metadata.roles and observation.is_success():
                        self._explicit_memory_write_this_run = True
                    if (
                        observation.error
                        and observation.error.startswith(_V2_DELEGATION_BLOCK_PREFIX)
                    ):
                        observation.metadata["expected_block"] = True
                        observation.metadata["block_kind"] = "v2_delegation_policy"
                    observations.append(observation)

                    # ── Completion guard: track file operations for finish validation ──
                    completion_ctx.record_tool_result(
                        tool_name=tc.name,
                        metadata=metadata,
                        path=str(tc.params.get(metadata.path_parameter) or "")
                        if metadata.path_parameter else "",
                        success=observation.is_success(),
                    )

                    # Delegated work is charged to the parent budget by role;
                    # authority is modeled separately by the tool's effect.
                    if (
                        ToolRole.DELEGATE in metadata.roles
                        and getattr(result, "subagent_tokens_used", 0) > 0
                    ):
                        _execution_budget.consume(result.subagent_tokens_used)
                        logger.debug(
                            "Charged %d subagent tokens to parent budget (total: %d)",
                            result.subagent_tokens_used, _execution_budget.token_used,
                        )
                    _sf = getattr(result, "structured_findings", None)
                    if _sf:
                        self._accumulated_structured_findings.extend(_sf)

                    # 追踪文件读取路径（用于 feedback 记忆触发）
                    if ToolEffect.READ_WORKSPACE in metadata.effects and observation.is_success():
                        file_path = (
                            str(tc.params.get(metadata.path_parameter) or "")
                            if metadata.path_parameter else ""
                        )
                        if file_path:
                            from core.policy import normalize_repo_path
                            self._accessed_files.add(
                                normalize_repo_path(file_path, task.repo_path)
                            )

                    # 追踪是否有文件写操作 + 标记 stale
                    if ToolEffect.WRITE_WORKSPACE in metadata.effects:
                        if observation.is_success():
                            written_path = (
                                str(tc.params.get(metadata.path_parameter) or "")
                                if metadata.path_parameter else ""
                            )
                            if written_path:
                                from core.policy import normalize_repo_path
                                self._mark_stale_for_written_file(
                                    normalize_repo_path(written_path, task.repo_path)
                                )

                    # 追踪测试是否失败
                    if ToolEffect.TEST in metadata.effects:
                        _test_was_run = True
                        if observation.is_success():
                            _verification_ok = True
                        else:
                            any_test_failed = True
                            if self._is_missing_test_target_observation(observation):
                                missing_test_target_observation = observation

                    log.log_observation(step=step, observation=observation)

                    if missing_test_target_observation is not None:
                        missing_test_target_message = self._format_missing_test_target_summary(
                            missing_test_target_observation
                        )
                        logger.info("Stopping immediately after missing pytest target")
                        log.log_task_complete(steps=step, summary=missing_test_target_message)
                        self._extract_success_memories(task, log, missing_test_target_message)
                        return _finish_run(
                            status=RunStatus.SUCCESS,
                            summary=missing_test_target_message,
                            steps_taken=step,
                            total_tokens_used=total_tokens,
                            cache_stats=cumulative_cache,
                        )

                # 缺失的指定测试文件是阻塞条件，不是自动创建测试的授权。
                if missing_test_target_observation is not None:
                    if missing_test_target_message is None:
                        missing_test_target_message = self._format_missing_test_target_summary(
                            missing_test_target_observation
                        )
                        missing_test_target_followups = self._cfg.missing_test_target_max_followups
                        missing_test_target_detected_step = step
                    else:
                        missing_test_target_followups = 0

                # 连续失败计数器 — wired into CircuitBreaker
                all_failed = all(not obs.is_success() for obs in observations)
                expected_blocked = all(obs.is_expected_block() for obs in observations)
                if all_failed and not expected_blocked:
                    if self._cfg.circuit_breaker is not None:
                        self._cfg.circuit_breaker.record_tool_error()
                else:
                    if self._cfg.circuit_breaker is not None:
                        self._cfg.circuit_breaker.record_tool_success()

                #  check _pending_mode_switch (CC-aligned)
                # Uses _full_registry because tools set the flag on the base
                # registry (not the per-step PolicyAwareToolRegistry wrapper).
                self._check_pending_mode_switch(self._full_registry, history)

                # 连续失败超过阈值：强制终止
                _cf = _get_consecutive_failures()
                if _cf >= _max_consecutive_failures:
                    reason = (
                        f"Aborting: {_cf} consecutive tool failures. "
                        f"Last error: {observations[-1].error or observations[-1].output[:200]}"
                    )
                    logger.warning(reason)
                    log.log_task_failed(steps=step, reason=reason)
                    return _finish_run(
                        status=RunStatus.GAVE_UP,
                        summary=reason,
                        steps_taken=step,
                        total_tokens_used=total_tokens,
                        cache_stats=cumulative_cache,
                    )

                # 把 action 和所有 observations 加入对话历史
                if self._backend.supports_function_calling:
                    # Native tool_use mode: structured messages
                    thought_content = action.thought or ""
                    if thought_content == "(no thought)":
                        thought_content = ""
                    history.add(LLMMessage(
                        role="assistant",
                        content=thought_content,
                        tool_calls=effective_tool_calls,
                    ))
                    for i, obs in enumerate(observations):
                        tc = effective_tool_calls[i] if i < len(effective_tool_calls) else None
                        history.add(LLMMessage(
                            role="tool",
                            content=self._build_tool_result_content(obs),
                            tool_call_id=tc.id if tc else None,
                        ))
                else:
                    # Text fallback mode (e.g. DeepSeek R1)
                    history.add(LLMMessage(
                        role="assistant",
                        content=self._format_action_for_history(action),
                    ))
                    history.add(LLMMessage(
                        role="user",
                        content=self._format_observations_for_history(observations),
                    ))

                next_phase = _phase_from_observations(observations)
                _child_turn_phase = _advance_child_turn_phase(
                    _child_turn_phase,
                    observation_phase=next_phase,
                    observations=observations,
                )

                # 缺失测试目标后，只允许少量确认性搜索，随后强制停止。
                if (
                    missing_test_target_message is not None
                    and missing_test_target_followups is not None
                    and missing_test_target_detected_step != step
                ):
                    if self._is_confirmation_search_action(action):
                        missing_test_target_followups -= 1
                    else:
                        missing_test_target_followups = 0

                    if missing_test_target_followups <= 0:
                        logger.info("Stopping after missing pytest target guardrail")
                        log.log_task_complete(steps=step, summary=missing_test_target_message)
                        self._extract_success_memories(task, log, missing_test_target_message)
                        return _finish_run(
                            status=RunStatus.SUCCESS,
                            summary=missing_test_target_message,
                            steps_taken=step,
                            total_tokens_used=total_tokens,
                            cache_stats=cumulative_cache,
                        )

                # ── 6. Reflection 触发判断 ──────────────────────────────

                # Task anchor 用于 reflection，防止 LLM 在反射后丢失当前任务
                _task_anchor = f"\n\n[TASK ANCHOR] Your current task is: {task.description}"

                # 触发条件 A：任一测试工具失败
                if any_test_failed:
                    if missing_test_target_message is not None:
                        reflect_prompt = self._missing_test_target_reflection(missing_test_target_message) + _task_anchor
                        log.log_reflection(
                            step=step,
                            reason="missing_test_target",
                            prompt=reflect_prompt,
                        )
                        history.add(LLMMessage(role="user", content=reflect_prompt))
                        logger.debug("Reflection triggered: missing_test_target at step %d", step)
                        continue

                    reflection_counts["test_failed"] = reflection_counts.get("test_failed", 0) + 1
                    if reflection_counts["test_failed"] >= 3:
                        reason = "Aborting: test failures repeated 3 times without resolution."
                        logger.warning(reason)
                        log.log_task_failed(steps=step, reason=reason)
                        return _finish_run(
                            status=RunStatus.GAVE_UP,
                            summary=reason,
                            steps_taken=step,
                            total_tokens_used=total_tokens,
                            cache_stats=cumulative_cache,
                        )
                    reflect_prompt = reflection_test_failed() + _task_anchor
                    log.log_reflection(
                        step=step,
                        reason="test_failed",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    logger.debug("Reflection triggered: test_failed at step %d", step)

            elif action.action_type == ActionType.REFLECTION:
                # LLM 主动要求 reflection（预留，当前 MockBackend 不产生）
                history.add(LLMMessage(
                    role="assistant",
                    content=action.thought,
                ))

        # ── 7. 超出步数上限（参考 Claude Code max_turns_reached）────────
        # Claude Code:
        #   return { reason: 'max_turns', turnCount: nextTurnCount };
        # 从 history 提取已收集的信息作为最终结果。
        _tsm.fail(TerminationReason.MAX_STEPS, "max steps exceeded")
        summary = self._extract_summary_from_history(history)
        log.log_task_failed(steps=task.max_steps, reason="max_steps")
        return _finish_run(
            status=RunStatus.MAX_STEPS,
            summary=summary,
            steps_taken=task.max_steps,
            total_tokens_used=total_tokens,
            cache_stats=cumulative_cache,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _build_session_memory_context(self, history: ConversationHistory) -> str:
        """Build a concise context summary for session memory extraction."""
        messages = history.get_messages()
        parts: list[str] = []
        for msg in messages[-20:]:
            role = msg.role
            content = msg.content or ""
            if len(content) > 2000:
                content = content[:2000] + "..."
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
        messages = history.to_dicts()
        dispatcher = self._cfg.hook_dispatcher or getattr(
            self._full_registry, "_hook_dispatcher", None
        )
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
                result = dispatcher.dispatch(ctx.event, ctx)
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
                return content[:2000]

        # Pass 2: 提取 tool results（原始工具返回数据，不依赖模型总结）
        tool_contents = []
        for msg in reversed(msgs):
            if msg.get("role") == "tool":
                content = msg.get("content", "").strip()
                if content and len(content) > 10:
                    tool_contents.append(content[:500])
                    if len(tool_contents) >= 5:
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
            from agent.prompt import build_sub_agent_system_prompt
            system_content = build_sub_agent_system_prompt(schemas)
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

        core_text = build_system_prompt_core(repo_path, schemas, self._repo_map_cache)
        variable_text = build_system_prompt_variable(
            memory_section="",
            auto_memory_enabled=bool(self._memory_context and self._memory_context.enabled),
        )
        long_term = self._build_long_term_context()
        anchor = self._build_task_anchor()

        ctx = self._context_manager.build_request_messages(
            history=history,
            token_budget=token_budget,
            system_core_text=core_text,
            variable_text=variable_text,
            long_term_context=long_term,
            task_anchor=anchor,
            artifact_store=self._artifact_store,
            consumed_tokens=consumed_tokens,
            max_context_window=max_context_window,
            repo_map_text=self._repo_map_cache or "",
            compactor_fn=self._compact_history_from_dicts,
            should_compact_fn=self._should_compact,
            history_materializer_fn=None,
        )

        self._compact_triggered_this_step = ctx.compact_triggered
        self._last_context_stats = ctx.stats

        # Post-compaction recovery: re-inject critical context (CC-aligned)
        if ctx.compact_triggered:
            recovery_msgs = self._build_recovery_messages()
            # Also re-inject accumulated structured findings (agent memory)
            findings = getattr(self, "_accumulated_structured_findings", [])
            if findings:
                recovery_msgs.append({
                    "role": "user",
                    "kind": "runtime_notice",
                    "content": "[Accumulated findings]\n" + "\n".join(
                        f"- {f.get('title','')}: {f.get('description','')[:200]}"
                        for f in findings[-10:]  # last 10 findings
                    ),
                })
            if recovery_msgs:
                ctx.messages = list(ctx.messages) + recovery_msgs

        return ctx.messages

    def _build_recovery_messages(self) -> list:
        """Post-compaction context re-injection (CC-aligned)."""
        from context.compaction import CompactionRecovery
        # Locate file cache: FileReadCache is injected into FileReadTool at registration
        _file_cache = None
        _skill_buf = None
        base = self._full_registry
        if getattr(base, "_skill_buffer", None) is not None:
            _skill_buf = base._skill_buffer
        if hasattr(base, "_tools"):
            rt = base._tools.get("Read") or base._tools.get("file_read")
            if rt is not None and hasattr(rt, "_read_cache"):
                _file_cache = rt._read_cache
            if _skill_buf is None:
                st = base._tools.get("Skill")
                if st is not None and hasattr(st, "_buffer"):
                    _skill_buf = st._buffer
        recovery = CompactionRecovery(
            file_cache=_file_cache,
            skill_buffer=_skill_buf,
            project_dir=getattr(self, "_current_repo_path", "."),
        )
        return recovery.build_recovery_messages([])

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
            _f = RunFinalizer(self._memory_context, self._backend)
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
        """委托给 memory/injection_service.py。"""
        if hasattr(self, "_long_term_context"):
            return self._long_term_context
        from memory.injection_service import build_injection_context
        self._long_term_context = build_injection_context(
            memory_context=self._memory_context,
            skills_prompt=getattr(self, "_skills_prompt", ""),
            repo_path=getattr(self, "_current_repo_path", "."),
            session_context=self._session_context,
        )
        return self._long_term_context

    def _build_task_anchor(self) -> str:
        """构建任务锚点（任务描述 + 模式 + 策略 + feedback 规则），每步注入。

        这不是一个独立的记忆子系统 —— 它是 prompt engineering，
        确保模型在每步推理时都能看到当前任务、约束和相关 feedback 规则。
        Feedback 规则嵌入此处而非独立消息，确保 compaction 后不丢失。"""
        parts: list[str] = []

        task_desc = getattr(self, "_current_task_description", "")
        task_metadata = getattr(self, "_current_task_metadata", {}) or {}
        legacy_analysis_prompting_disabled = bool(
            task_metadata.get("v2_disable_legacy_analysis_prompting")
        )
        if task_desc:
            parts.append(f"## Current Task\n{task_desc}")

        if (
            getattr(self, "_task_intent", TaskIntent.EDIT) is TaskIntent.ANALYSIS
            and not legacy_analysis_prompting_disabled
        ):
            parts.append(
                "## Task Mode: Analysis\n"
                "This is a read-only analysis task. Inspect relevant project evidence, "
                "synthesize findings, and verify named gaps.\n"
                "For confirmed conclusions, cite recorded evidence ids like [ev_xxx]. "
                "If a point is not supported by evidence, move it under uncertainty or needs verification.\n"
                "Do NOT edit files. Do NOT run tests. Answer from evidence as soon as you can."
            )

        # V1 analysis phase anchor removed — legacy_analysis_prompting_disabled is always True

        active_policy = getattr(self, "_active_policy", None)
        if active_policy is not None and not legacy_analysis_prompting_disabled:
            prompt_section = active_policy.to_prompt_section("execution")
            if prompt_section:
                parts.append(prompt_section)

        # Feedback 记忆按文件锚点注入
        feedback_section = self._get_feedback_section()
        if feedback_section:
            parts.append(feedback_section)

        if not parts:
            return ""

        return "\n\n".join(parts)


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
    def _truncate_output(text: str, max_chars: int = 8000) -> str:
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
        from agent.prompt import consume_prompt_usage_metadata
        _invoker = getattr(self, "_llm_invoker", None)
        if _invoker is None:
            _invoker = LLMInvoker(backend=self._backend, config=self._cfg)
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

            elif event.kind == StreamEventKind.TOOL_USE:
                if event.tool_call:
                    tool_calls_raw.append(event.tool_call)
                    executor.enqueue(event.tool_call)
                    # After each enqueue, check for newly completed tools
                    executor.process_queue()

            elif event.kind == StreamEventKind.FINISH:
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

        # Stream ended without FINISH — treat as finish with accumulated text
        return Action(
            action_type=ActionType.FINISH,
            thought=accumulated_thought,
            message=accumulated_text or "Stream ended.",
        )

    # ------------------------------------------------------------------
    # 权限模式切换（Plan Mode / Execute Mode）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Compaction（对话压缩）
    # ------------------------------------------------------------------

    _compactor: ConversationCompactor | None = None

    @property
    def compactor(self) -> ConversationCompactor:
        if self._compactor is None:
            self._compactor = ConversationCompactor(backend=self._backend)
        return self._compactor

    def _should_compact(self, history_dicts: list[dict], history_budget: int) -> bool:
        """判断是否需要自动 compaction。"""
        return self.compactor.should_compact(history_dicts, history_budget)

    def _compact_history_from_dicts(self, history_dicts: list[dict]) -> list[dict]:
        """执行 compaction (MicroCompact → full compact)，返回压缩后的 dict 列表。"""
        # Layer 1: MicroCompact — clear old tool outputs before deciding if full compact needed
        from context.compaction import MicroCompactor
        history_dicts = MicroCompactor().compact(history_dicts)

        task_ctx = getattr(self, "_current_task_description", "")
        compacted = self.compactor.compact_history(history_dicts, task_context=task_ctx)
        logger.info(
            "Compaction: %d messages → %d messages",
            len(history_dicts), len(compacted),
        )

        # 持久化 session summary 到磁盘（供跨 session 恢复）
        if self._memory_context and hasattr(self._memory_context, "_store"):
            from context.compaction import persist_compaction_summary
            summary_text = compacted[0]["content"] if compacted else ""
            if summary_text:
                store_dir = str(self._memory_context._store.store_dir.parent)
                persist_compaction_summary(summary_text, store_dir)

        return compacted


# ---------------------------------------------------------------------------
# 向后兼容别名 — 所有旧代码可继续使用 Agent
# ---------------------------------------------------------------------------

Agent = ReActAgent
