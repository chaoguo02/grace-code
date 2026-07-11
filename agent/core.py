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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agent.policy import TaskPolicy, build_task_policy
from agent.runtime_controller import RecoveryAction, ToolDecision
from agent.task_classifier import classify_task_shape
from agent.event_log import EventLog, summarize_run
from context.evidence import EvidenceLedger
from context.history import ConversationHistory
from context.read_plan import ReadPlan, ReadPlanItem, parse_read_plan_message
from context.repo_map import RepoMap
from context.token_budget import TokenBudget
from agent.prompt import (
    build_system_prompt,
    build_system_prompt_core,
    build_system_prompt_variable,
    build_task_prompt,
    consume_prompt_usage_metadata,
    reflection_no_edit,
    reflection_test_failed,
    set_project_dir,
)
from agent.task import (
    Action, ActionType, Event, EventType,
    Observation, ObservationStatus, RunResult, RunStatus, Task, TaskShape, ToolCall,
)
from context.artifacts import ArtifactStore
from context.compaction import ConversationCompactor
from context.manager import ContextManager, ContextManagerConfig, RequestContext
from llm.base import LLMBackend, LLMMessage, LLMToolSchema
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
from tools.base import ToolRegistry

if TYPE_CHECKING:
    from memory.context import MemoryContext
    from memory.session_memory import SessionMemoryTracker

logger = logging.getLogger(__name__)

_V2_DELEGATION_BLOCK_PREFIX = "BLOCKED_BY_DELEGATION_POLICY:"
_MAX_STOP_HOOK_RETRIES = 3

AnalysisPhase = Literal["plan_reads", "discover", "inspect", "synthesize", "verify", "answer"]

_BROAD_ANALYSIS_RE = re.compile(
    r"(梳理|架构|审计|路线图|优先级|主要问题|优化|review architecture|roadmap|audit)",
    re.IGNORECASE,
)
_DISCOVERY_TOOL_NAMES = frozenset({"find_files", "search_text", "find_symbol"})
_READ_TOOL_NAMES = frozenset({"file_read", "file_view"})

# Tools exempt from semantic (Level 2) loop detection — reading different
# file sections is legitimate exploration, not a loop. FileReadCache already
# prevents wasted re-reads. Exact match (Level 1) still catches true repeats.
_READ_EXPLORE_TOOLS = frozenset({"file_read", "file_view"})


@dataclass
class AnalysisPhaseState:
    """Runtime state for broad read-only phased analysis."""
    enabled: bool = False
    phase: AnalysisPhase = "answer"
    task_shape: str = ""
    files_read: set[str] = field(default_factory=set)
    read_units: set[tuple[str, int, int | None]] = field(default_factory=set)
    discovery_tools_used: int = 0
    inspect_reads: int = 0
    verify_reads: int = 0
    synthesize_requested: bool = False
    phase_summaries: list[str] = field(default_factory=list)
    read_plan_required: bool = False
    read_plan_ready: bool = False
    phase_token_usage: dict[str, int] = field(default_factory=dict)
    phase_llm_calls: dict[str, int] = field(default_factory=dict)
    started_phases: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Agent 运行时配置，从 config/default.yaml 加载后传入。"""
    max_steps: int = 40
    reflection_no_edit_steps: int = 6   # 连续 N 步无文件写操作触发 Reflection
    loop_detection_window: int = 3       # 连续 N 步完全相同 action 判定死循环
    test_tool_names: tuple[str, ...] = ("test", "pytest")  # 触发 Reflection 的工具名
    budget_tokens: int = 80_000            # task spend 上限（billable tokens）
    request_budget_tokens: int = 70_000    # 单次 request 输入上下文预算
    artifact_threshold_tokens: int = 2_000 # 工具输出超过此值时 artifact 化
    artifact_storage_dir: str = ".forge-agent/artifacts"  # repo-relative durable artifact storage
    analysis_inspect_read_limit: int = 5    # broad analysis inspect 阶段最多 distinct file reads
    analysis_verify_read_limit: int = 2     # broad analysis verify 阶段最多额外 file reads
    missing_test_target_max_followups: int = 2  # pytest 路径缺失后最多允许的确认性探索步数
    history_max_messages: int = 40         # 历史最大条数
    llm_max_retries: int = 3               # LLM 调用失败最大重试次数
    llm_retry_delay: float = 2.0           # 重试间隔（秒，指数退避）
    stream: bool = False                   # 是否启用流式输出
    stream_callback: object = None         # StreamCallback，最终回答流式回调
    thought_callback: object = None        # StreamCallback，推理过程流式回调（推理模型专用）
    confirm_dangerous: bool = False        # 是否对危险命令要求用户确认
    confirm_callback: object = None        # ConfirmCallback，None=跳过确认
    compact_history: bool = True           # 是否启用积极的历史压缩（sub-agent 应关闭）
    circuit_breaker: object = None         # CircuitBreaker | None — 代码级熔断器
    plan_budget_ratio: float = 0.33        # plan 模式占主预算的比例 (TaskContract.for_plan 使用)


# ---------------------------------------------------------------------------
# 共享工具函数
# ---------------------------------------------------------------------------

def _git_diff(repo_path: str) -> str | None:
    """抓取 git diff HEAD 作为 patch，失败时静默返回 None。"""
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=repo_path,
            encoding="utf-8", errors="replace",
        )
        diff = proc.stdout.strip()
        return diff if diff else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ReActAgent — ReAct (Reasoning + Acting) 主循环
# ---------------------------------------------------------------------------

def _coerce_finish_tool_call(action: Action) -> Action:
    """Treat a pseudo-tool named finish as a terminal finish action."""
    if action.action_type != ActionType.TOOL_CALL or len(action.tool_calls) != 1:
        return action
    tool_call = action.tool_calls[0]
    if tool_call.name.lower() not in {"finish", "task_complete"}:
        return action
    params = tool_call.params or {}
    message = action.message
    if isinstance(params, dict):
        for key in ("summary", "message", "result", "answer", "final_answer", "action"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                message = value.strip()
                break
        if not message:
            try:
                message = json.dumps(params, ensure_ascii=False)
            except TypeError:
                message = str(params)
    return Action(
        action_type=ActionType.FINISH,
        thought=action.thought,
        message=message or "Task complete.",
    )


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
    ) -> None:
        self._backend = backend
        self._full_registry = registry
        self._registry = registry
        self._cfg = config or AgentConfig()
        self._controller_factory = controller_factory  # injected by AgentFactory
        self._memory_context = memory_context
        self._session_memory_tracker = session_memory_tracker
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
        self._artifact_store.set_storage_dir(Path(task.repo_path) / self._cfg.artifact_storage_dir)
        self._task_shape = self._ensure_task_shape(task)
        self._current_task_description = task.description
        self._current_task_metadata = dict(task.metadata or {})
        self._task_intent = getattr(task, "intent", "edit")
        set_project_dir(task.repo_path)

        # ── Policy enforcement ─────────
        policy = build_task_policy(task)
        if task.explicit_read_paths is None and policy.execution.allowed_read_paths is not None:
            task.explicit_read_paths = policy.execution.allowed_read_paths
            task.shape = None
            self._task_shape = self._ensure_task_shape(task)
        # Registry is always pre-wrapped by AgentFactory — no isinstance check needed
        return self._run_body(task, log, policy=policy)

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
        self._submit_plan_ref = getattr(self._full_registry, "_submit_plan_ref", None)
        if self._submit_plan_ref is not None:
            self._submit_plan_ref.pending_plan = None
            self._submit_plan_ref.task_id = task.task_id
            self._submit_plan_ref.repo_path = task.repo_path
        self._accumulated_structured_findings: list[dict] = []
        self._analysis_phase_state = None  # V1 analysis disabled
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
        token_budget = TokenBudget(total=self._cfg.request_budget_tokens)
        repo_map = RepoMap(task.repo_path)

        # ── Baseline diff: capture git state BEFORE this run ──
        # Used to compute the incremental diff at finish time, so the
        # summary reflects ONLY what THIS run changed — not prior worktree dirt.
        _baseline_diff = _git_diff(task.repo_path) or ""

        total_tokens = 0
        steps_without_edit = 0
        _verification_ok = False  # set True if any test/validate tool succeeds
        self._stop_hook_verify_count = 0  # Stop Hook: retry count for verification
        consecutive_failures = 0
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

        # ── P0: Macro-action loop detector (catches global flow patterns) ──
        from agent.v2.macro_loop_detector import MacroLoopDetector
        from agent.v2.task_intent import TaskIntent
        _task_intent = TaskIntent.from_string(self._task_intent)
        _macro_loop_detector = MacroLoopDetector()
        _macro_loop_detector.task_intent = _task_intent

        # ── P0: Unified execution budget ──
        from agent.v2.execution_budget import ExecutionBudget, ExecutionBudgetConfig, BudgetLevel
        _execution_budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=task.budget_tokens,
            step_limit=task.max_steps,
        ))
        _execution_budget.start()
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
            loop_detector=_macro_loop_detector,
            loop_check_fn=lambda: (self._is_looping(log), self._build_loop_diagnosis(log)),
            max_steps=task.max_steps,
            budget_tokens=task.budget_tokens,
            max_consecutive_failures=_max_consecutive_failures,
        )

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

            # ── Compute incremental diff: what THIS run changed ──
            _current_diff = _git_diff(task.repo_path) or ""
            _incremental_diff = ""
            if _current_diff and _current_diff != _baseline_diff:
                _incremental_diff = _current_diff
                # Inject diff evidence into summary so agent reports facts, not memory
                summary = (
                    f"{summary}\n\n"
                    f"--- INCREMENTAL DIFF (this run only) ---\n"
                    f"{_incremental_diff[:3000]}"
                )

            # ── Verification downgrade: files edited but tests never passed ──
            if completion_ctx.had_any_write and not _verification_ok:
                summary = (
                    f"[UNVERIFIED — test/validation did not run or was unavailable. "
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
                        "steps_without_edit": steps_without_edit,
                        "consecutive_failures": consecutive_failures,
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

        for step in range(1, task.max_steps + 1):
            self._current_step = step  # 用于 compaction 日志
            self.compactor.tick_step()
            logger.debug("Step %d/%d", step, task.max_steps)

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
                consecutive_failures=consecutive_failures,
            )
            if decision.action == StepAction.TERMINATE:
                log.log_task_failed(steps=step, reason=decision.terminate_reason)
                _term_status = RunStatus(decision.terminate_status) if decision.terminate_status else RunStatus.GAVE_UP
                return _finish_run(
                    status=_term_status,
                    summary=decision.terminate_summary,
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

            _analysis_active = False  # V1 analysis permanently disabled
            is_planning = (
                task.metadata.get("phase") == "planning"
                or task.metadata.get("mode") == "v2-plan"
            )
            if (is_planning
                and step >= int(task.max_steps * 0.8)
                and not self._plan_budget_exhaustion_injected):
                self._plan_budget_exhaustion_injected = True
                history.add(LLMMessage(
                    role="user",
                    content=(
                        f"[SYSTEM] Plan exploration budget nearly exhausted "
                        f"({step}/{task.max_steps} steps used). "
                        "Stop exploring and produce your plan NOW using finish. "
                        "Base it on what you have already learned."
                    ),
                ))

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

            tools = [] if decision.strip_tools else self._schemas_for_current_phase()

            try:
                response = self._call_with_retry(messages, tools)
            except Exception as exc:
                logger.error("LLM call failed at step %d after retries: %s", step, exc)
                log.log_task_failed(steps=step, reason=f"LLM error: {exc}")
                return _finish_run(
                    status=RunStatus.FAILED,
                    summary=f"LLM call failed: {exc}",
                    steps_taken=step,
                    total_tokens_used=total_tokens,
                    error=str(exc),
                    cache_stats=cumulative_cache,
                )

            billable_tokens = response.total_tokens
            if response.cache_stats and response.cache_stats.has_cache_activity:
                cumulative_cache.cache_read_tokens += response.cache_stats.cache_read_tokens
                cumulative_cache.cache_creation_tokens += response.cache_stats.cache_creation_tokens
                cumulative_cache.non_cached_input_tokens += response.cache_stats.non_cached_input_tokens
                # Cached prompt tokens still appear in provider usage, but they should not
                # trip this run's hard exploration budget on repeated short tasks.
                billable_tokens = max(0, billable_tokens - response.cache_stats.cache_read_tokens)
            total_tokens += billable_tokens
            _execution_budget.consume(billable_tokens)
            _execution_budget.record_step()

            action = _coerce_finish_tool_call(response.action)

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
            log.log_action(step=step, action=action, raw_content=response.raw_content)
            logger.info("Step %d: %r", step, action)

            # ── 3. Local loop detection (post-action, RuntimeController method) ──
            _is_local_loop, _loop_diagnosis = _runtime_controller.check_local_loop()
            if _is_local_loop:
                logger.warning("Loop detected — terminating: %s", _loop_diagnosis)
                log.log_task_failed(steps=step, reason=_loop_diagnosis)
                _execution_budget.exhaust(_loop_diagnosis)
                return _finish_run(
                    status=RunStatus.GAVE_UP,
                    summary=_loop_diagnosis,
                    steps_taken=step,
                    total_tokens_used=total_tokens,
                    cache_stats=cumulative_cache,
                )

            # ── 4. 终止 action ──────────────────────────────────────────
            if action.action_type == ActionType.FINISH:
                stop_message = self._run_stop_hook(history)
                if stop_message is not None:
                    next_count = self._stop_hook_count + 1
                    if next_count > _MAX_STOP_HOOK_RETRIES:
                        reason = f"Stop hook retry limit reached: {_MAX_STOP_HOOK_RETRIES}"
                        logger.warning(reason)
                        log.log_task_failed(steps=step, reason=reason)
                        return _finish_run(
                            status=RunStatus.GAVE_UP,
                            summary=reason,
                            steps_taken=step,
                            total_tokens_used=total_tokens,
                            cache_stats=cumulative_cache,
                        )
                    self._stop_hook_count = next_count
                    history.add(LLMMessage(role="user", content=stop_message))
                    continue

                self._stop_hook_count = 0

                # ── Completion guard: Runtime validates before accepting FINISH ──
                # The model cannot unilaterally declare "done" — the Runtime must
                # verify all completion conditions.
                active_policy = getattr(self, "_active_policy", None)
                guard_result = completion_guard.check(
                    ctx=completion_ctx,
                    task_intent=task.intent,
                    task_max_steps=task.max_steps,
                    current_step=step,
                    completion_policy=active_policy.completion if active_policy is not None else None,
                )
                if not guard_result.can_complete:
                    logger.warning(
                        "Completion blocked: %s", guard_result.blocked_reason
                    )
                    history.add(LLMMessage(
                        role="user", content=guard_result.inject_message
                    ))
                    continue

                # ── Stop Hook: verify before accepting FINISH ──
                # Claude Code pattern: if files were modified but no test/validate
                # tool ran successfully, BLOCK the finish. Force the agent to
                # verify by reading the changed files directly.
                _stop_hook_blocked = False
                if completion_ctx.had_any_write and not _verification_ok:
                    if self._stop_hook_verify_count < 1:
                        self._stop_hook_verify_count += 1
                        _stop_hook_blocked = True
                        history.add(LLMMessage(
                            role="user",
                            content=(
                                "[SYSTEM] Stop Hook blocked FINISH — test/validation tools "
                                "are not available or did not run successfully. "
                                "Before calling finish, you MUST verify your changes:\n"
                                "1. Read each modified file to confirm the code is syntactically correct\n"
                                "2. Explain what you checked and why it's correct\n"
                                "Do NOT retry shell commands or pytest — use file_read instead."
                            ),
                        ))
                    # After 2 retries, allow finish with [UNVERIFIED] marker

                if _stop_hook_blocked:
                    continue

                summary = action.message or "Task complete."
                patch = self._get_git_diff(task.repo_path)
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
                any_edit = False
                gated_read_count = 0

                for tc in action.tool_calls:
                    # V1 analysis gating (no-op in V2 — state.enabled is False)
                    gated_decision = None
                    if _analysis_active:
                        gated_decision = self._read_plan_tool_decision(tc, task.repo_path)
                        if gated_decision is None:
                            gated_decision = self._verification_read_tool_decision(tc, task.repo_path)
                    if gated_decision is not None and not gated_decision.allowed:
                        gated_read_count += 1
                        observation = self._tool_decision_to_observation(tc, gated_decision)
                        file_path = tc.params.get("path") or tc.params.get("file_path") or ""
                        self._analysis_tool_decision_count += 1
                        self._analysis_deferred_read_count += 1
                        log.log_tool_decision(
                            step=step,
                            tool_name=tc.name,
                            allowed=False,
                            reason=gated_decision.reason,
                            path=str(file_path),
                            phase=getattr(getattr(self, "_analysis_phase_state", None), "phase", ""),
                        )
                        task_obs.event(
                            name="tool_decision",
                            metadata={
                                "tool_name": tc.name,
                                "allowed": False,
                                "reason": gated_decision.reason,
                                "phase": getattr(getattr(self, "_analysis_phase_state", None), "phase", ""),
                                "step": step,
                            },
                            input_data=tc.params,
                            output_data={"synthetic_observation": gated_decision.synthetic_observation or ""},
                            level="WARNING",
                        )
                    else:
                        with observer.start_tool(
                                name=f"tool:{tc.name}",
                                input_data=build_tool_input(
                                    tc.name,
                                    tc.params,
                                    action.thought or "",
                                    step,
                                ),
                                metadata=merge_metadata(
                                    {"tool_name": tc.name, "step": step},
                                    task.metadata,
                                ),
                            ) as tool_obs:
                                result = self._registry.execute_tool(tc.name, tc.params, thought=action.thought or "")
                                tool_obs.update(
                                    output=build_tool_output(
                                        result,
                                        capture_tool_outputs=observer.config.capture_tool_outputs if observer.config else True,
                                    ),
                                    metadata={"tool_name": tc.name, "duration_ms": result.duration_ms},
                                )
                        observation = result.to_observation(tc.name)
                        if tc.name == "memory_write" and observation.is_success():
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
                        path=tc.params.get("path", ""),
                        success=observation.is_success() and gated_decision is None,
                    )

                    # ── P0-4: Charge subagent token consumption to parent budget ──
                    if tc.name == "task" and getattr(result, "subagent_tokens_used", 0) > 0:
                        _execution_budget.consume(result.subagent_tokens_used)
                        logger.debug(
                            "Charged %d subagent tokens to parent budget (total: %d)",
                            result.subagent_tokens_used, _execution_budget.token_used,
                        )
                    # Signal subagent loop detection to macro detector (breaks repetition)
                    if tc.name == "task" and getattr(result, "subagent_terminated_by_loop", False):
                        _macro_loop_detector.record_reflection("subagent_loop_killed")
                    _sf = getattr(result, "structured_findings", None)
                    if _sf:
                        self._accumulated_structured_findings.extend(_sf)

                    if _analysis_active and observation.is_success() and gated_decision is None:
                        # submit_read_plan: consume pending plan and transition
                        if tc.name == "submit_read_plan":
                            plan = self._consume_submitted_plan(task)
                            if plan is not None:
                                state = self._analysis_phase_state
                                previous_phase = state.phase
                                self._end_analysis_phase_span(
                                    log, task_obs, step=step,
                                    phase=previous_phase, reason="read_plan_approved",
                                )
                                state.phase = "inspect"
                                self._start_analysis_phase_span(
                                    log, task_obs, step=step,
                                    phase=state.phase, reason="read_plan_approved",
                                )
                                log.log_analysis_phase(
                                    step=step,
                                    previous_phase=previous_phase,
                                    current_phase=state.phase,
                                    reason="read_plan_approved",
                                    files_read=len(state.files_read),
                                    inspect_reads=state.inspect_reads,
                                    verify_reads=state.verify_reads,
                                )
                                history.add(LLMMessage(
                                    role="user",
                                    content=self._read_plan_feedback_prompt(task, plan),
                                ))

                        transition = self._update_analysis_phase(tc, task.repo_path)
                        evidence_phase = None
                        if transition is not None:
                            previous_phase, current_phase, reason = transition
                            if reason == "inspect_read_limit":
                                evidence_phase = previous_phase
                            state = self._analysis_phase_state
                            self._end_analysis_phase_span(
                                log,
                                task_obs,
                                step=step,
                                phase=previous_phase,
                                reason=reason,
                            )
                            self._start_analysis_phase_span(
                                log,
                                task_obs,
                                step=step,
                                phase=current_phase,
                                reason=reason,
                            )
                            log.log_analysis_phase(
                                step=step,
                                previous_phase=previous_phase,
                                current_phase=current_phase,
                                reason=reason,
                                files_read=len(state.files_read),
                                inspect_reads=state.inspect_reads,
                                verify_reads=state.verify_reads,
                            )
                        evidence_record = self._record_evidence(tc, observation, task.repo_path, phase=evidence_phase)
                        if evidence_record is not None:
                            log.log_evidence_record(step=step, record=evidence_record)

                    # 追踪文件读取路径（用于 feedback 记忆触发）
                    if tc.name in ("file_read", "file_view") and observation.is_success() and gated_decision is None:
                        file_path = tc.params.get("path") or tc.params.get("file_path") or ""
                        if file_path:
                            from agent.policy import normalize_repo_path
                            self._accessed_files.add(
                                normalize_repo_path(file_path, task.repo_path)
                            )

                    # 追踪是否有文件写操作 + 标记 stale
                    if tc.name in ("file_write", "file_edit", "edit"):
                        any_edit = True
                        if observation.is_success():
                            written_path = tc.params.get("path") or tc.params.get("file_path") or ""
                            if written_path:
                                from agent.policy import normalize_repo_path
                                self._mark_stale_for_written_file(
                                    normalize_repo_path(written_path, task.repo_path)
                                )

                    # 追踪测试是否失败
                    if tc.name in self._cfg.test_tool_names:
                        if observation.is_success():
                            _verification_ok = True
                        else:
                            any_test_failed = True
                            if self._is_missing_test_target_observation(observation):
                                missing_test_target_observation = observation

                    log.log_observation(step=step, observation=observation)

                    # ── P0: Macro loop detection & circuit breaker same-tool tracking ──
                    _macro_loop_detector.record_tool_call(tc.name, tc.params)
                    if self._cfg.circuit_breaker is not None:
                        # Build a stable params hash for same-tool loop detection
                        _params_key = str(sorted(tc.params.items())) if tc.params else ""
                        self._cfg.circuit_breaker.record_tool_call(tc.name, _params_key)

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

                # 更新 steps_without_edit（整体判断）
                if any_edit:
                    steps_without_edit = 0
                else:
                    steps_without_edit += 1

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
                    consecutive_failures += 1
                    if self._cfg.circuit_breaker is not None:
                        self._cfg.circuit_breaker.record_tool_error()
                else:
                    consecutive_failures = 0
                    if self._cfg.circuit_breaker is not None:
                        self._cfg.circuit_breaker.record_tool_success()

                # 连续失败超过阈值：强制终止
                if consecutive_failures >= _max_consecutive_failures:
                    reason = (
                        f"Aborting: {consecutive_failures} consecutive tool failures. "
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
                        tool_calls=action.tool_calls,
                    ))
                    for i, obs in enumerate(observations):
                        tc = action.tool_calls[i] if i < len(action.tool_calls) else None
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

                if gated_read_count:
                    if getattr(self, "_deferred_read_reflection_injected", False):
                        summary = self._analysis_answer_boundary_summary(gated_read_count)
                        finish_action = Action(action_type=ActionType.FINISH, thought="", message=summary)
                        log.log_action(step=step, action=finish_action, raw_content=summary)
                        log.log_task_complete(steps=step, summary=summary)
                        self._extract_success_memories(task, log, summary)
                        return _finish_run(
                            status=RunStatus.SUCCESS,
                            summary=summary,
                            steps_taken=step,
                            total_tokens_used=total_tokens,
                            cache_stats=cumulative_cache,
                        )
                    self._deferred_read_reflection_injected = True
                    self._analysis_answer_phase_forced = True
                    if state is not None and state.enabled:
                        previous_phase = state.phase
                        self._end_analysis_phase_span(
                            log,
                            task_obs,
                            step=step,
                            phase=previous_phase,
                            reason="analysis_deferred_read_answer_boundary",
                        )
                        state.phase = "answer"
                        self._start_analysis_phase_span(
                            log,
                            task_obs,
                            step=step,
                            phase=state.phase,
                            reason="analysis_deferred_read_answer_boundary",
                        )
                    reflect_prompt = self._deferred_read_answer_prompt(gated_read_count)
                    self._analysis_recovery_action_count += 1
                    log.log_recovery_action(
                        step=step,
                        kind="force_answer",
                        reason="analysis_deferred_read_answer_boundary",
                        prompt=reflect_prompt,
                    )
                    task_obs.event(
                        name="recovery_action",
                        metadata={
                            "kind": "force_answer",
                            "reason": "analysis_deferred_read_answer_boundary",
                            "gated_read_count": gated_read_count,
                            "step": step,
                        },
                        output_data={"prompt": reflect_prompt},
                        level="WARNING",
                    )
                    log.log_reflection(
                        step=step,
                        reason="analysis_deferred_read_answer_boundary",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    logger.debug("Reflection triggered: deferred read answer boundary at step %d", step)
                    continue

                analysis_guardrail_prompt = None  # V1 removed
                if analysis_guardrail_prompt:
                    analysis_reason = "analysis_read_guardrail"
                    state = getattr(self, "_analysis_phase_state", None)
                    if state is not None and state.enabled:
                        analysis_reason = "analysis_phase_synthesize"
                        self._log_new_phase_claims(log, step=step, phase="inspect", task_obs=task_obs)
                    log.log_reflection(
                        step=step,
                        reason=analysis_reason,
                        prompt=analysis_guardrail_prompt,
                    )
                    history.add(LLMMessage(role="user", content=analysis_guardrail_prompt))
                    logger.debug("Reflection triggered: %s at step %d", analysis_reason, step)
                    continue

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
                    _macro_loop_detector.record_reflection("test_failed")
                    logger.debug("Reflection triggered: test_failed at step %d", step)

                # 触发条件 B：连续 N 步无编辑（仅 edit 类型任务触发）
                # 跳过规划阶段：plan/planning 阶段是先天只读的，不应因无写操作而终止。
                elif (
                    steps_without_edit >= self._cfg.reflection_no_edit_steps
                    and self._task_intent == "edit"
                    and task.metadata.get("phase") not in ("planning", "plan_reads")
                ):
                    reflection_counts["no_edit"] = reflection_counts.get("no_edit", 0) + 1
                    if reflection_counts["no_edit"] >= 2:
                        reason = "Aborting: stuck in exploration without making progress."
                        logger.warning(reason)
                        log.log_task_failed(steps=step, reason=reason)
                        return _finish_run(
                            status=RunStatus.GAVE_UP,
                            summary=reason,
                            steps_taken=step,
                            total_tokens_used=total_tokens,
                            cache_stats=cumulative_cache,
                        )
                    reflect_prompt = reflection_no_edit(steps_without_edit) + _task_anchor
                    log.log_reflection(
                        step=step,
                        reason="no_edit",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    steps_without_edit = 0  # 重置计数，避免每步都触发
                    _macro_loop_detector.record_reflection("no_edit")
                    logger.debug("Reflection triggered: no_edit at step %d", step)

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

    def _run_stop_hook(self, history: ConversationHistory) -> str | None:
        messages = history.to_dicts()
        dispatcher = getattr(self._full_registry, "_hook_dispatcher", None)
        if dispatcher is not None:
            try:
                from hooks.events import HookContext, HookEvent
                ctx = HookContext(
                    event=HookEvent.STOP,
                    messages=messages,
                )
                result = dispatcher.dispatch_stop(ctx)
            except Exception as exc:
                logger.debug("Stop hook dispatch failed: %s", exc)
                result = None
            if result is not None and result.blocked:
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
        """识别 pytest exit code 4 中的缺失目标，避免进入修复代码流程。"""
        text = f"{observation.error or ''}\n{observation.output or ''}".lower()
        return (
            observation.tool_name in self._cfg.test_tool_names
            and "pytest" in text
            and (
                "requested test target is missing" in text
                or "file or directory not found" in text
                or "no such file or directory" in text
            )
        )

    def _format_missing_test_target_summary(self, observation: Observation) -> str:
        requested_path = "(unknown)"
        match = re.search(r"Requested path:\s*(.+)", observation.output)
        if match:
            requested_path = match.group(1).strip()
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
        allowed_tools = {"find_files", "search_text", "shell"}
        if action.action_type != ActionType.TOOL_CALL or not action.tool_calls:
            return False
        return all(
            tc.name in allowed_tools and self._is_targeted_confirmation_call(tc)
            for tc in action.tool_calls
        )

    def _is_targeted_confirmation_call(self, tc: ToolCall) -> bool:
        if tc.name in {"find_files", "search_text"}:
            return True
        if tc.name != "shell":
            return False
        cmd = str(tc.params.get("cmd", "")).lower()
        if not any(token in cmd for token in ("test_compaction", "compaction", "tests")):
            return False
        return any(cmd.strip().startswith(prefix) for prefix in ("rg", "find", "dir", "ls", "python -c"))

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

        # Sub-agent 模式（compact_history=False）：精简 system prompt，跳过所有裁剪
        if not self._cfg.compact_history:
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
        return ctx.messages

    def _schemas_for_current_phase(self) -> list[LLMToolSchema]:
        """Return tool schemas visible in the current runtime phase."""
        if getattr(self, "_analysis_answer_phase_forced", False):
            return []
        state = getattr(self, "_analysis_phase_state", None)
        schemas = self._registry.get_schemas()
        if state is None or not state.enabled:
            return schemas
        if state.phase == "answer":
            return []
        if state.phase == "plan_reads":
            # Hide file_read/file_view, keep discovery tools + submit_read_plan
            return [schema for schema in schemas if schema.name not in _READ_TOOL_NAMES]
        return schemas

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

    def _ensure_task_shape(self, task: Task) -> TaskShape:
        if task.shape is not None:
            return task.shape
        pre = task.metadata.get("classified_shape")
        if pre:
            from agent.task import TaskShape as _TS
            shape = _TS(kind=pre, reason=task.metadata.get("classified_shape_reason", "upstream"))
        else:
            from agent.task import TaskShape as _TS
            shape = _TS(kind="simple_edit" if getattr(task, "intent", "edit") == "edit" else "simple_answer", reason="default")
        task.shape = shape
        task.metadata["task_shape"] = shape.kind
        task.metadata["task_shape_reason"] = shape.reason
        return shape


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
            getattr(self, "_task_intent", "edit") == "analysis"
            and not legacy_analysis_prompting_disabled
        ):
            parts.append(
                "## Task Mode: Analysis\n"
                "This is a read-only analysis task. Classify the task shape first, then follow the active phase.\n"
                "For broad analysis, discover structure, submit a compact read plan, inspect only planned files, "
                "synthesize evidence, then verify only named gaps.\n"
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
        return build_tool_result_content(
            observation, artifact_store=self._artifact_store,
        )

    @staticmethod
    def _truncate_output(text: str, max_chars: int = 8000) -> str:
        """委托给 observation_rendering。"""
        from agent.observation_rendering import truncate_output
        return truncate_output(text, max_chars)

    def _format_observations_for_history(self, observations: list[Observation]) -> str:
        """委托给 observation_rendering。"""
        from agent.observation_rendering import format_observations_for_history
        return format_observations_for_history(
            observations, artifact_store=self._artifact_store,
        )

    def _is_looping(self, log: EventLog) -> bool:
        """
        Detect dead loops. Two-level check:

        1. Exact match: last N actions have identical (tool_name, params).
        2. Semantic match: last N+1 actions use the same tool_name multiset.

        File operations (file_read, file_view, file_edit, file_write) are
        handled by FileReadCache at the tool layer — repeated reads return
        cached content with [CACHED] markers.

        file_read and file_view are EXEMPT from Level 2 (semantic) detection:
        reading different file sections is legitimate exploration. They are
        still subject to Level 1 (exact) — identical params repeated N times
        IS a loop regardless of tool type.
        """
        n = self._cfg.loop_detection_window
        actions = log.get_actions()
        if len(actions) < n:
            return False

        recent = actions[-n:]
        if not all(a.action_type == ActionType.TOOL_CALL for a in recent):
            return False
        if not all(a.tool_calls for a in recent):
            return False

        def _serialize_exact(action: Action) -> tuple:
            return tuple(
                (tc.name, tuple(sorted(tc.params.items())))
                for tc in action.tool_calls
            )

        def _serialize_names(action: Action) -> tuple:
            return tuple(sorted(
                tc.name for tc in action.tool_calls
            ))

        # Level 1: exact match (window = N)
        first_exact = _serialize_exact(recent[0])
        if all(_serialize_exact(a) == first_exact for a in recent[1:]):
            return True

        # Level 2: semantic match (window = N+1, more evidence needed)
        # Exempt file_read/file_view — reading different file sections is
        # legitimate exploration, not a loop. FileReadCache already prevents
        # wasted re-reads. Exact match (Level 1) still catches true repeats.
        semantic_window = n + 1
        if len(actions) >= semantic_window:
            semantic_recent = actions[-semantic_window:]
            if all(a.action_type == ActionType.TOOL_CALL and a.tool_calls for a in semantic_recent):
                first_names = _serialize_names(semantic_recent[0])
                if first_names and not set(first_names).issubset(_READ_EXPLORE_TOOLS):
                    if all(_serialize_names(a) == first_names for a in semantic_recent[1:]):
                        return True

        return False

    def _build_loop_diagnosis(self, log: EventLog) -> str:
        """Build a structured diagnosis string for a detected loop.

        Returns a line-oriented ASCII report so the parent agent can parse
        key facts without scanning prose.
        """
        n = self._cfg.loop_detection_window
        actions = log.get_actions()
        recent = actions[-n:]

        # Collect tool-call facts from the recent window
        tool_names: list[str] = []
        repeated_params: list[str] = []
        seen_params: set[str] = set()
        for action in recent:
            for tc in (action.tool_calls or []):
                tool_names.append(tc.name)
                params_key = str(sorted(tc.params.items()))
                if params_key in seen_params:
                    if tc.name not in repeated_params:
                        repeated_params.append(tc.name)
                seen_params.add(params_key)

        all_same_name = len(set(tool_names)) <= 1
        diagnosis_parts = [
            "Loop detected:",
            f"  detection: {'exact match' if all_same_name and repeated_params else 'semantic match'}",
            f"  window: {n} consecutive tool-call steps",
            f"  repeated_tools: {list(dict.fromkeys(tool_names))}",
            f"  repeated_params: {repeated_params or 'varied'}",
            f"  total_steps_consumed: {len(actions)}",
        ]
        return "\n".join(diagnosis_parts)

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

    def _get_git_diff(self, repo_path: str) -> str | None:
        """抓取 git diff HEAD 作为 patch，失败时静默返回 None。"""
        return _git_diff(repo_path)

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
        """执行 compaction，返回压缩后的 dict 列表。"""
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
