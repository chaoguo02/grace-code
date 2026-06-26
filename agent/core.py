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

from agent.completion import CompletionValidator
from agent.policy import TaskPolicy, build_task_policy
from agent.policy_registry import PolicyAwareToolRegistry as _PolicyAwareRegistry
from agent.policy_registry import PolicyAwareToolRegistry
from agent.runtime_control import RecoveryAction, ToolDecision
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

logger = logging.getLogger(__name__)

_V2_DELEGATION_BLOCK_PREFIX = "BLOCKED_BY_DELEGATION_POLICY:"

AnalysisPhase = Literal["plan_reads", "discover", "inspect", "synthesize", "verify", "answer"]

_BROAD_ANALYSIS_RE = re.compile(
    r"(梳理|架构|审计|路线图|优先级|主要问题|优化|review architecture|roadmap|audit)",
    re.IGNORECASE,
)
_DISCOVERY_TOOL_NAMES = frozenset({"find_files", "search_text", "find_symbol"})
_READ_TOOL_NAMES = frozenset({"file_read", "file_view"})


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


# 只读工具白名单（Plan Mode 下可用）
_READONLY_TOOLS = frozenset({
    "file_read", "file_view", "find_files", "find_symbol",
    "search_text", "git_status", "git_diff",
    "web_search", "web_fetch",
    "artifact_list", "artifact_read", "artifact_search",
    "evidence_list", "evidence_get",
    "memory_read", "memory_list",
})

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
    对于需要"先规划后执行"的多步骤复杂任务，使用 PlanExecuteAgent 作为编排层。
    """

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        config: AgentConfig | None = None,
        memory_context: "MemoryContext | None" = None,
    ) -> None:
        self._backend = backend
        self._full_registry = registry  # 保存完整注册表
        self._registry = registry
        self._readonly_registry = self._build_readonly_registry()
        self._cfg = config or AgentConfig()
        self._memory_context = memory_context
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
        self._analysis_read_plan: ReadPlan | None = None

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

        # ── Policy enforcement (shared with PlanExecuteAgent) ─────────
        policy = build_task_policy(task)
        if task.explicit_read_paths is None and policy.execution.allowed_read_paths is not None:
            task.explicit_read_paths = policy.execution.allowed_read_paths
            task.shape = None
            self._task_shape = self._ensure_task_shape(task)
        if isinstance(self._full_registry, _PolicyAwareRegistry):
            # PlanExecuteAgent already wrapped; skip double-wrapping
            return self._run_body(task, log, policy=policy)
        original_registry = self._registry
        self._registry = _PolicyAwareRegistry(
            base=self._full_registry,
            phase_policy=policy.execution,
            repo_path=task.repo_path,
            phase_name="execution",
        )
        try:
            return self._run_body(task, log, policy=policy)
        finally:
            self._registry = original_registry

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

        self._loop_break_injected = False
        self._accessed_files: set[str] = set()
        self._procedural_injected_files: set[str] = set()
        self._read_file_ranges: set[tuple[str, int, int | None]] = set()
        self._analysis_read_guardrail_injected = False
        self._deferred_read_reflection_injected = False
        self._analysis_answer_phase_forced = False
        self._analysis_read_plan = None
        self._evidence_ledger = EvidenceLedger()
        evidence_ledger_ref = getattr(self._full_registry, "_evidence_ledger_ref", None)
        if evidence_ledger_ref is not None:
            evidence_ledger_ref.ledger = self._evidence_ledger
        self._submit_plan_ref = getattr(self._full_registry, "_submit_plan_ref", None)
        if self._submit_plan_ref is not None:
            self._submit_plan_ref.pending_plan = None
            self._submit_plan_ref.task_id = task.task_id
            self._submit_plan_ref.repo_path = task.repo_path
        self._analysis_tool_decision_count = 0
        self._analysis_recovery_action_count = 0
        self._analysis_deferred_read_count = 0
        self._plan_reads_budget_warning_injected = False
        self._analysis_logged_claim_ids: set[str] = set()
        self._analysis_phase_state = self._init_analysis_phase_state(task, policy)
        observer = get_observer()
        task_context = observer.start_task(task)
        task_obs = task_context.__enter__()
        task_obs_closed = False
        log.log_task_start(task)
        state = getattr(self, "_analysis_phase_state", None)
        if state is not None and state.enabled:
            self._start_analysis_phase_span(
                log,
                task_obs,
                step=0,
                phase=state.phase,
                reason="analysis_phase_initialized",
            )
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

        total_tokens = 0
        steps_without_edit = 0
        consecutive_failures = 0
        _max_consecutive_failures = 3
        reflection_counts: dict[str, int] = {}  # reason -> count
        missing_test_target_followups: int | None = None
        missing_test_target_message: str | None = None
        missing_test_target_detected_step: int | None = None
        # 累计 prompt caching 统计
        from llm.base import CacheStats
        cumulative_cache = CacheStats()

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
            self._close_active_analysis_phase(log, task_obs, step=steps_taken, reason=f"run_{status.value}")
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

            # ── 1. 组装 messages，调用 LLM ──────────────────────────────
            # 设置最新用户消息，供 RAG 主动检索使用
            if self._memory_context:
                last_user_msg = history.get_last_user_message()
                if last_user_msg:
                    self._memory_context.set_user_message(last_user_msg)

            messages = self._build_messages(
                history, token_budget, repo_map,
                consumed_tokens=total_tokens,
                max_context_window=self._backend.max_context_window,
            )

            state = getattr(self, "_analysis_phase_state", None)
            if state is not None and state.enabled and state.phase == "plan_reads":
                plan_reads_token_budget = int(self._cfg.budget_tokens * 0.15)
                plan_reads_tokens_used = int(state.phase_token_usage.get("plan_reads", 0))
                # Warn when 80% of plan_reads budget consumed
                if (
                    plan_reads_tokens_used >= int(plan_reads_token_budget * 0.8)
                    and not getattr(self, "_plan_reads_budget_warning_injected", False)
                ):
                    self._plan_reads_budget_warning_injected = True
                    remaining = max(0, plan_reads_token_budget - plan_reads_tokens_used)
                    history.add(LLMMessage(
                        role="user",
                        content=(
                            f"[SYSTEM] plan_reads token budget is nearly exhausted ({plan_reads_tokens_used}/{plan_reads_token_budget} tokens used, ~{remaining} remaining). "
                            "Call submit_read_plan now with your structured read plan."
                        ),
                    ))

            tools = self._schemas_for_current_phase()

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
            self._record_analysis_phase_llm_usage(billable_tokens)

            # ── Token budget 硬上限 ────────────────────────────────────
            if total_tokens > self._cfg.budget_tokens:
                ctx_breakdown = ""
                last_stats = getattr(self, "_last_context_stats", None)
                if last_stats:
                    ctx_breakdown = f" Context breakdown: {last_stats.summary_line()}"
                reason = (
                    f"Token budget exceeded: {total_tokens} > {self._cfg.budget_tokens}. "
                    f"Stopping to prevent unbounded cost.{ctx_breakdown}"
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

            action = _coerce_finish_tool_call(response.action)

            # ── 2. 写入 Action event ────────────────────────────────────
            log.log_action(step=step, action=action, raw_content=response.raw_content)
            logger.info("Step %d: %r", step, action)

            if getattr(self, "_analysis_answer_phase_forced", False) and action.action_type == ActionType.TOOL_CALL:
                summary = self._analysis_answer_boundary_summary(len(action.tool_calls or []))
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

            # ── 3. 检测死循环（连续相同 action）────────────────────────
            if self._is_looping(log):
                if not getattr(self, "_loop_break_injected", False):
                    # 第一次检测到循环：注入反射消息，让 LLM 停止重复
                    self._loop_break_injected = True
                    logger.warning("Loop detected — injecting break reflection")
                    history.add(LLMMessage(
                        role="user",
                        content=(
                            "[SYSTEM] You are repeating the same action. STOP repeating. "
                            "You already have the information you need. "
                            "Produce your final answer NOW using the finish action.\n\n"
                            f"[TASK ANCHOR] Your current task is: {task.description}"
                        ),
                    ))
                    continue
                reason = f"Loop detected: same action repeated {self._cfg.loop_detection_window} times"
                logger.warning(reason)
                log.log_task_failed(steps=step, reason=reason)
                return _finish_run(
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=step,
                    total_tokens_used=total_tokens,
                    cache_stats=cumulative_cache,
                )

            # ── 4. 终止 action ──────────────────────────────────────────
            if action.action_type == ActionType.FINISH:
                summary = action.message or "Task complete."
                patch = self._get_git_diff(task.repo_path)
                verdict = CompletionValidator().validate(
                    log,
                    policy,
                    task.repo_path,
                    task=task,
                    evidence_ledger=getattr(self, "_evidence_ledger", None),
                    final_summary=summary,
                )
                if not verdict.success:
                    if verdict.retryable and verdict.reason_code == "analysis_answer_grounding_failed":
                        reflection_counts[verdict.reason_code] = reflection_counts.get(verdict.reason_code, 0) + 1
                        if reflection_counts[verdict.reason_code] >= 2:
                            log.log_task_failed(steps=step, reason=verdict.reason)
                            return _finish_run(
                                status=RunStatus.GAVE_UP,
                                summary=verdict.reason,
                                steps_taken=step,
                                total_tokens_used=total_tokens,
                                patch=patch,
                                cache_stats=cumulative_cache,
                            )
                        reflect_prompt = self._analysis_answer_grounding_retry_prompt(task, verdict.reason)
                        self._analysis_recovery_action_count += 1
                        log.log_recovery_action(
                            step=step,
                            kind="reflect",
                            reason=verdict.reason_code or "completion_validation_failed",
                            prompt=reflect_prompt,
                        )
                        task_obs.event(
                            name="recovery_action",
                            metadata={
                                "kind": "reflect",
                                "reason": verdict.reason_code or "completion_validation_failed",
                                "step": step,
                            },
                            output_data={"prompt": reflect_prompt},
                            level="WARNING",
                        )
                        log.log_reflection(
                            step=step,
                            reason=verdict.reason_code or "completion_validation_failed",
                            prompt=reflect_prompt,
                        )
                        history.add(LLMMessage(role="user", content=reflect_prompt))
                        logger.debug("Reflection triggered: %s at step %d", verdict.reason_code, step)
                        continue
                    log.log_task_failed(steps=step, reason=verdict.reason)
                    return _finish_run(
                        status=RunStatus.GAVE_UP,
                        summary=verdict.reason,
                        steps_taken=step,
                        total_tokens_used=total_tokens,
                        patch=patch,
                        cache_stats=cumulative_cache,
                    )
                log.log_task_complete(steps=step, summary=summary)
                self._extract_success_memories(task, log, summary)
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
                        duplicate_observation = self._duplicate_file_read_observation(tc, task.repo_path)
                        if duplicate_observation is not None:
                            observation = duplicate_observation
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
                            if (
                                observation.error
                                and observation.error.startswith(_V2_DELEGATION_BLOCK_PREFIX)
                            ):
                                observation.metadata["expected_block"] = True
                                observation.metadata["block_kind"] = "v2_delegation_policy"
                    observations.append(observation)

                    if observation.is_success() and gated_decision is None:
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

                    # 追踪文件读取路径（用于 procedural 记忆触发）
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
                    if tc.name in self._cfg.test_tool_names and not observation.is_success():
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

                # 连续失败计数器
                all_failed = all(not obs.is_success() for obs in observations)
                expected_blocked = all(obs.is_expected_block() for obs in observations)
                if all_failed and not expected_blocked:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

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

                analysis_guardrail_prompt = self._analysis_read_guardrail_prompt()
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
                    logger.debug("Reflection triggered: test_failed at step %d", step)

                # 触发条件 B：连续 N 步无编辑（仅 edit 类型任务触发）
                elif (steps_without_edit >= self._cfg.reflection_no_edit_steps
                      and self._task_intent == "edit"):
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
                    logger.debug("Reflection triggered: no_edit at step %d", step)

            elif action.action_type == ActionType.REFLECTION:
                # LLM 主动要求 reflection（预留，当前 MockBackend 不产生）
                history.add(LLMMessage(
                    role="assistant",
                    content=action.thought,
                ))

        # ── 7. 超出步数上限：从 history 提取已收集的信息作为 summary ────
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

        # Merge session context into long_term if available
        if self._session_context and long_term:
            long_term = f"{long_term}\n\n## Session Context (completed tasks)\n{self._session_context}"
        elif self._session_context:
            long_term = f"## Session Context (completed tasks)\n{self._session_context}"

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
            history_materializer_fn=self._materialize_analysis_history,
        )

        self._compact_triggered_this_step = ctx.compact_triggered
        self._annotate_context_stats_with_analysis_phase(ctx.stats)
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
        """成功任务结束后用 LLM reflection 提取长期记忆；失败不影响主流程。"""
        if not self._memory_context or not self._memory_context.enabled:
            return
        store = getattr(self._memory_context, "store", None)
        if store is None:
            return
        try:
            from memory.extractor import MemoryExtractor
            extractor = MemoryExtractor(backend=self._backend)
            # 尝试获取 external_store 用于合并去重
            external_store = None
            retriever = getattr(self._memory_context, "_retriever", None)
            if retriever is not None:
                external_store = getattr(retriever, "_store", None)
            written = extractor.write_success_memories(
                task, log, summary, store, external_store=external_store,
            )
            if written:
                logger.debug("Extracted %d success memories", written)
        except Exception as exc:
            logger.warning("Success memory extraction skipped: %s", exc)

    def _is_anthropic_backend(self) -> bool:
        """判断当前 backend 是否为 Anthropic（支持 prompt cache）。"""
        backend_type = type(self._backend).__name__
        return "anthropic" in backend_type.lower()

    def _build_long_term_context(self) -> str | None:
        """构建长期记忆上下文（项目规则 + 记忆索引 + skills），任务开始时构建一次。"""
        if hasattr(self, "_long_term_context"):
            return self._long_term_context

        parts: list[str] = []

        if self._memory_context and self._memory_context.enabled:
            memory_section = self._memory_context.build_memory_section()
            if memory_section:
                parts.append(memory_section)

        rules_content = self._load_project_rules()
        if rules_content:
            parts.append(f"## Project Rules\n{rules_content}")

        skills_prompt = getattr(self, "_skills_prompt", "")
        if skills_prompt:
            parts.append(skills_prompt)

        if not parts:
            self._long_term_context = None
            return None

        self._long_term_context = "\n\n".join(parts)
        return self._long_term_context

    def _ensure_task_shape(self, task: Task) -> TaskShape:
        shape = task.shape or classify_task_shape(task)
        task.shape = shape
        task.metadata["task_shape"] = shape.kind
        task.metadata["task_shape_reason"] = shape.reason
        return shape

    def _init_analysis_phase_state(self, task: Task, policy: TaskPolicy) -> AnalysisPhaseState:
        """Enable phased analysis only for broad unscoped read-only analysis tasks."""
        shape = self._ensure_task_shape(task)
        if task.metadata.get("v2_disable_legacy_analysis_prompting"):
            return AnalysisPhaseState(enabled=False, phase="answer", task_shape=shape.kind)
        if task.intent != "analysis":
            return AnalysisPhaseState(enabled=False, phase="answer")
        if policy.execution.allowed_read_paths or policy.execution.strict_file_scope:
            return AnalysisPhaseState(enabled=False, phase="answer")
        if shape.kind != "broad_analysis":
            return AnalysisPhaseState(enabled=False, phase="answer", task_shape=shape.kind)
        return AnalysisPhaseState(
            enabled=True,
            phase="plan_reads" if shape.requires_read_plan else "discover",
            task_shape=shape.kind,
            read_plan_required=shape.requires_read_plan,
            read_plan_ready=not shape.requires_read_plan,
        )

    def _normalize_read_plan(self, plan: ReadPlan, repo_path: str) -> ReadPlan:
        from agent.policy import normalize_repo_path

        normalized_items = [
            ReadPlanItem(
                path=normalize_repo_path(item.path, repo_path),
                reason=item.reason,
                closes_gap=item.closes_gap,
                priority=item.priority,
                max_ranges=item.max_ranges,
            )
            for item in plan.items
        ]
        return ReadPlan(
            task_id=plan.task_id,
            subsystem=plan.subsystem,
            items=normalized_items,
            stop_condition=plan.stop_condition,
            approved=plan.approved,
        )

    def _consume_submitted_plan(self, task: Task) -> ReadPlan | None:
        """Consume a pending plan from the submit_read_plan tool ref."""
        ref = self._submit_plan_ref
        if ref is None or ref.pending_plan is None:
            return None
        plan = self._normalize_read_plan(ref.pending_plan, task.repo_path)
        self._analysis_read_plan = plan
        state = getattr(self, "_analysis_phase_state", None)
        if state is not None:
            state.read_plan_ready = True
        ref.pending_plan = None
        return plan

    def _approve_read_plan_from_message(self, message: str | None, task: Task) -> ReadPlan:
        if not message or not message.strip():
            raise ValueError("read plan submission is empty")
        plan = parse_read_plan_message(message, task_id=task.task_id)
        return self._normalize_read_plan(plan, task.repo_path)

    def _read_plan_feedback_prompt(self, task: Task, plan: ReadPlan) -> str:
        return (
            "[SYSTEM] Read plan approved. You are now in the Inspect phase.\n"
            f"Subsystem: {plan.subsystem}\n"
            f"Planned reads: {plan.summary()}\n"
            f"Stop condition: {plan.stop_condition}\n"
            "Read only the planned source files unless you later reach a verification gap.\n\n"
            f"[TASK ANCHOR] Your current task is: {task.description}"
        )

    def _analysis_answer_grounding_retry_prompt(self, task: Task, reason: str) -> str:
        ledger = getattr(self, "_evidence_ledger", None)
        summary = ledger.latest_phase_summary_text() if ledger is not None else ""
        claims = self._build_grounding_claims_summary()
        return (
            "[SYSTEM] Analysis answer grounding FAILED — your answer was rejected.\n"
            f"Reason: {reason}\n\n"
            "FIX REQUIRED: Rewrite your answer and embed [ev_xxx] citations from the list below.\n"
            "- Pick evidence IDs from the list below and write them inline like: 'The system uses X [ev_abc123].'\n"
            "- You need AT LEAST ONE [ev_xxx] citation in your answer\n"
            "- Points without evidence support must go under 'Hypotheses / Needs verification'\n"
            "- Do NOT invent evidence IDs — only use the exact IDs listed below\n\n"
            f"{summary}\n\n"
            f"{claims}\n\n"
            f"[TASK ANCHOR] Your current task is: {task.description}"
        )

    def _log_new_phase_claims(self, log: EventLog, *, step: int, phase: str, task_obs=None) -> None:
        ledger = getattr(self, "_evidence_ledger", None)
        if ledger is None:
            return
        summary = ledger.phase_summary_for(phase)
        if summary is None:
            return
        logged_ids = getattr(self, "_analysis_logged_claim_ids", set())
        for claim in summary.claims:
            if claim.claim_id in logged_ids:
                continue
            log.log_claim_created(step=step, phase=phase, claim=claim)
            if task_obs is not None:
                task_obs.event(
                    name="claim_created",
                    metadata={"phase": phase, "claim_id": claim.claim_id, "status": claim.status},
                    output_data=claim.to_dict(),
                    level="DEFAULT",
                )
            logged_ids.add(claim.claim_id)
        self._analysis_logged_claim_ids = logged_ids

    def _annotate_context_stats_with_analysis_phase(self, stats) -> None:
        """Attach broad-analysis phase metadata to request context stats."""
        state = getattr(self, "_analysis_phase_state", None)
        if state is None or not state.enabled:
            return
        stats.analysis_phase = state.phase
        stats.analysis_files_read = len(state.files_read)
        stats.analysis_inspect_reads = state.inspect_reads
        stats.analysis_verify_reads = state.verify_reads
        ledger = getattr(self, "_evidence_ledger", None)
        if ledger is not None:
            stats.analysis_evidence_records = ledger.evidence_count
            stats.analysis_phase_summaries = ledger.phase_summary_count
            stats.analysis_claims = ledger.total_claim_count()
        stats.analysis_tool_decisions = int(getattr(self, "_analysis_tool_decision_count", 0))
        stats.analysis_recovery_actions = int(getattr(self, "_analysis_recovery_action_count", 0))
        stats.analysis_deferred_reads = int(getattr(self, "_analysis_deferred_read_count", 0))
        stats.analysis_phase_token_costs = dict(state.phase_token_usage)

    def _record_analysis_phase_llm_usage(self, tokens_used: int) -> None:
        state = getattr(self, "_analysis_phase_state", None)
        if state is None or not state.enabled:
            return
        phase = state.phase
        state.phase_token_usage[phase] = int(state.phase_token_usage.get(phase, 0)) + max(0, int(tokens_used))
        state.phase_llm_calls[phase] = int(state.phase_llm_calls.get(phase, 0)) + 1

    def _start_analysis_phase_span(self, log: EventLog, task_obs, *, step: int, phase: str, reason: str) -> None:
        state = getattr(self, "_analysis_phase_state", None)
        if state is None or not state.enabled or phase in state.started_phases:
            return
        state.started_phases.add(phase)
        log.log_phase_start(
            step=step,
            phase=phase,
            reason=reason,
            tokens_so_far=int(state.phase_token_usage.get(phase, 0)),
        )
        if task_obs is not None:
            task_obs.event(
                name="phase_start",
                metadata={"phase": phase, "reason": reason, "step": step},
                output_data={
                    "tokens_so_far": int(state.phase_token_usage.get(phase, 0)),
                    "llm_calls": int(state.phase_llm_calls.get(phase, 0)),
                },
                level="DEFAULT",
            )

    def _end_analysis_phase_span(self, log: EventLog, task_obs, *, step: int, phase: str, reason: str) -> None:
        state = getattr(self, "_analysis_phase_state", None)
        if state is None or not state.enabled or phase not in state.started_phases:
            return
        state.started_phases.remove(phase)
        log.log_phase_end(
            step=step,
            phase=phase,
            reason=reason,
            tokens_total=int(state.phase_token_usage.get(phase, 0)),
            llm_calls=int(state.phase_llm_calls.get(phase, 0)),
        )
        if task_obs is not None:
            task_obs.event(
                name="phase_end",
                metadata={"phase": phase, "reason": reason, "step": step},
                output_data={
                    "tokens_total": int(state.phase_token_usage.get(phase, 0)),
                    "llm_calls": int(state.phase_llm_calls.get(phase, 0)),
                },
                level="DEFAULT",
            )

    def _close_active_analysis_phase(self, log: EventLog, task_obs, *, step: int, reason: str) -> None:
        state = getattr(self, "_analysis_phase_state", None)
        if state is None or not state.enabled:
            return
        phase = state.phase
        if phase in state.started_phases:
            self._end_analysis_phase_span(log, task_obs, step=step, phase=phase, reason=reason)

    def _record_evidence(
        self,
        tool_call: ToolCall,
        observation: Observation,
        repo_path: str,
        phase: str | None = None,
    ):
        """Record successful analysis evidence from read/search observations."""
        state = getattr(self, "_analysis_phase_state", None)
        ledger = getattr(self, "_evidence_ledger", None)
        if state is None or not state.enabled or ledger is None:
            return
        if tool_call.name not in (_READ_TOOL_NAMES | _DISCOVERY_TOOL_NAMES):
            return
        output = observation.output or ""
        if not output:
            return
        path = tool_call.params.get("path") or tool_call.params.get("file_path") or ""
        if path:
            from agent.policy import normalize_repo_path
            path = normalize_repo_path(path, repo_path)
        range_text = ""
        if tool_call.name == "file_view":
            start_line = max(1, int(tool_call.params.get("start_line", 1)))
            from tools.file_tool import VIEW_WINDOW_LINES
            range_text = f"lines {start_line}-{start_line + VIEW_WINDOW_LINES - 1}"
        artifact_id = ""
        if self._artifact_store is not None:
            artifact = self._artifact_store.store(tool_call.name, output)
            artifact_id = artifact.artifact_id if artifact else ""
        return ledger.add_observation(
            phase=phase or state.phase,
            tool_name=tool_call.name,
            output=output,
            path=path,
            range_text=range_text,
            artifact_id=artifact_id,
            key_evidence=tool_call.name in _READ_TOOL_NAMES,
        )

    def _update_analysis_phase(
        self,
        tool_call: ToolCall,
        repo_path: str,
    ) -> tuple[str, str, str] | None:
        """Update broad-analysis phase state after a successful relevant tool call."""
        state = getattr(self, "_analysis_phase_state", None)
        if state is None or not state.enabled:
            return None

        previous_phase = state.phase
        reason = ""

        if tool_call.name in _DISCOVERY_TOOL_NAMES:
            state.discovery_tools_used += 1
            if state.phase == "plan_reads":
                return None
            if state.phase == "discover":
                state.phase = "inspect"
                reason = "discovery_tool_used"
            return (previous_phase, state.phase, reason) if state.phase != previous_phase else None

        if tool_call.name not in _READ_TOOL_NAMES:
            return None

        if state.phase == "plan_reads":
            return None

        file_path = tool_call.params.get("path") or tool_call.params.get("file_path") or ""
        if not file_path:
            return None
        from agent.policy import normalize_repo_path
        normalized = normalize_repo_path(file_path, repo_path)
        state.files_read.add(normalized)
        unit_key = self._file_read_range_key(tool_call, repo_path)
        already_read_unit = unit_key in state.read_units if unit_key is not None else False
        if unit_key is not None:
            state.read_units.add(unit_key)

        if state.phase == "discover":
            state.phase = "inspect"
            reason = "first_file_read"
        if already_read_unit:
            return (previous_phase, state.phase, reason) if state.phase != previous_phase else None
        if state.phase == "inspect":
            state.inspect_reads += 1
            if state.inspect_reads >= self._cfg.analysis_inspect_read_limit:
                state.phase = "synthesize"
                reason = "inspect_read_limit"
        elif state.phase == "synthesize":
            state.phase = "verify"
            state.verify_reads += 1
            reason = "verification_read_after_synthesis"
            if state.verify_reads >= self._cfg.analysis_verify_read_limit:
                state.phase = "answer"
                reason = "verify_read_limit"
        elif state.phase == "verify":
            state.verify_reads += 1
            if state.verify_reads >= self._cfg.analysis_verify_read_limit:
                state.phase = "answer"
                reason = "verify_read_limit"
        return (previous_phase, state.phase, reason) if state.phase != previous_phase else None

    def _analysis_phase_files_summary(self, max_files: int = 5) -> str:
        state = getattr(self, "_analysis_phase_state", None)
        if state is None or not state.files_read:
            return "(none)"
        files = sorted(state.files_read)
        summary = ", ".join(files[:max_files])
        if len(files) > max_files:
            summary += f", ... and {len(files) - max_files} more"
        return summary

    def _build_analysis_phase_summary(self) -> str:
        """Build deterministic phase summary metadata for broad analysis compaction."""
        state = getattr(self, "_analysis_phase_state", None)
        if state is None:
            return ""
        return (
            f"phase={state.phase}; "
            f"files_read={len(state.files_read)}; "
            f"read_units={len(state.read_units)}; "
            f"discovery_tools={state.discovery_tools_used}; "
            f"inspect_reads={state.inspect_reads}; "
            f"verify_reads={state.verify_reads}; "
            f"files={self._analysis_phase_files_summary()}"
        )

    def _deferred_read_answer_prompt(self, gated_read_count: int) -> str:
        """Force final synthesis after post-synthesis source reads were deferred."""
        task_desc = getattr(self, "_current_task_description", "")
        ledger = getattr(self, "_evidence_ledger", None)
        summary = ledger.latest_phase_summary_text() if ledger is not None else ""
        claims = self._build_grounding_claims_summary()
        return (
            "[SYSTEM] Phased analysis answer boundary:\n"
            f"{gated_read_count} source read(s) were deferred after the synthesis boundary.\n"
            "The next turn is answer phase: no tools will be available. Produce the final answer now.\n\n"
            "CRITICAL CITATION REQUIREMENT:\n"
            "- You MUST embed at least one [ev_xxx] citation in your answer text\n"
            "- Use the exact evidence IDs listed below (e.g. [ev_abc123])\n"
            "- Every confirmed architectural finding must have a citation\n"
            "- Points without evidence support go under 'Hypotheses / Needs verification'\n"
            "- Do NOT invent evidence IDs — only use IDs from the list below\n\n"
            f"{summary}\n\n"
            f"{claims}\n\n"
            f"[TASK ANCHOR] Your current task is: {task_desc}"
        )

    def _analysis_answer_boundary_summary(self, gated_read_count: int) -> str:
        """Return a safe terminal summary when the model keeps reading after deferral."""
        ledger = getattr(self, "_evidence_ledger", None)
        summary = ledger.latest_phase_summary_text() if ledger is not None else ""
        if summary:
            return (
                "Stopped after repeated deferred source reads beyond the synthesis boundary. "
                "Use the phase summary and confidence boundaries below.\n\n"
                f"{summary}"
            )
        return (
            "Stopped after repeated deferred source reads beyond the synthesis boundary. "
            f"Deferred reads in last step: {gated_read_count}. Answer from already collected evidence."
        )

    def _analysis_read_guardrail_prompt(self) -> str | None:
        """Prompt the agent to synthesize broad analysis before reading more files."""
        state = getattr(self, "_analysis_phase_state", None)
        if state is not None and state.enabled:
            if state.phase != "synthesize" or state.synthesize_requested:
                return None
            state.synthesize_requested = True
            state.phase_summaries.append(self._build_analysis_phase_summary())
            ledger = getattr(self, "_evidence_ledger", None)
            if ledger is not None:
                ledger.summarize_phase_semantically(
                    "inspect",
                    self._backend,
                    task_description=getattr(self, "_current_task_description", ""),
                )
            task_desc = getattr(self, "_current_task_description", "")
            claims = self._build_grounding_claims_summary()
            return (
                "[SYSTEM] Phased analysis controller:\n"
                f"You have completed the Inspect phase after reading {len(state.files_read)} files.\n"
                "Do not read more files now.\n"
                "Synthesize:\n"
                "- confirmed architecture\n"
                "- confirmed issues\n"
                "- uncertainty\n"
                "- named gaps\n"
                "Then either answer or request one specific verification read.\n\n"
                "IMPORTANT: When you produce the final answer, you MUST cite evidence IDs inline like [ev_xxx]. "
                "Use only the IDs listed below.\n\n"
                f"{claims}\n\n"
                f"[TASK ANCHOR] Your current task is: {task_desc}"
            )

        if self._task_intent != "analysis":
            return None
        if getattr(self, "_analysis_read_guardrail_injected", False):
            return None
        accessed = sorted(getattr(self, "_accessed_files", set()))
        if len(accessed) < 5:
            return None
        self._analysis_read_guardrail_injected = True
        files = ", ".join(accessed[:8])
        if len(accessed) > 8:
            files += f", ... and {len(accessed) - 8} more"
        task_desc = getattr(self, "_current_task_description", "")
        return (
            "[SYSTEM] Broad analysis guardrail: you have already read "
            f"{len(accessed)} distinct files ({files}). Pause broad exploration now. "
            "Synthesize the architecture, confirmed findings, uncertainty, and next-step gaps from the evidence already read. "
            "Only read more files if you name a specific gap that cannot be answered from current evidence, and keep any further reads narrowly targeted.\n\n"
            f"[TASK ANCHOR] Your current task is: {task_desc}"
        )

    def _build_task_anchor(self) -> str:
        """构建任务锚点（任务描述 + 模式 + 策略 + procedural 规则），每步注入。

        这不是一个独立的记忆子系统 —— 它是 prompt engineering，
        确保模型在每步推理时都能看到当前任务、约束和相关 procedural 规则。
        Procedural 规则嵌入此处而非独立消息，确保 compaction 后不丢失。"""
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

        phase_section = "" if legacy_analysis_prompting_disabled else self._build_analysis_phase_anchor()
        if phase_section:
            parts.append(phase_section)

        active_policy = getattr(self, "_active_policy", None)
        if active_policy is not None and not legacy_analysis_prompting_disabled:
            prompt_section = active_policy.to_prompt_section("execution")
            if prompt_section:
                parts.append(prompt_section)

        # Procedural 记忆按文件锚点注入
        procedural_section = self._get_procedural_section()
        if procedural_section:
            parts.append(procedural_section)

        if not parts:
            return ""

        return "\n\n".join(parts)

    def _materialize_analysis_history(self, history_dicts: list[dict]) -> list[dict]:
        """Materialize completed phase evidence as compact references for prompts."""
        state = getattr(self, "_analysis_phase_state", None)
        ledger = getattr(self, "_evidence_ledger", None)
        if state is None or not state.enabled or ledger is None or not ledger.phase_summaries:
            return history_dicts

        materialized: list[dict] = []
        for message in history_dicts:
            content = message.get("content", "")
            replacement = ""
            if message.get("role") in {"tool", "user"} and isinstance(content, str):
                replacement = ledger.compact_reference_for_tool_result(content) or ""
            if replacement:
                new_message = dict(message)
                new_message["content"] = replacement
                materialized.append(new_message)
            else:
                materialized.append(message)
        return materialized

    def _build_analysis_phase_anchor(self) -> str:
        """Build a compact phase anchor for broad read-only analysis."""
        state = getattr(self, "_analysis_phase_state", None)
        if state is None or not state.enabled:
            return ""
        parts = [
            "## Phased Analysis Controller",
            f"Current phase: {state.phase}",
            f"Task shape: {state.task_shape or 'analysis'}",
            f"Files read: {len(state.files_read)} ({self._analysis_phase_files_summary()})",
            "Phase rules: plan_reads uses discovery tools and a compact read plan; "
            "Inspect reads only key planned files; Synthesize stops and summarizes evidence; "
            "Verify reads only named gaps; Answer uses evidence without more tools.",
        ]
        read_plan = getattr(self, "_analysis_read_plan", None)
        if read_plan is not None:
            parts.append(f"Read plan: {read_plan.summary()}")
            parts.append(f"Stop condition: {read_plan.stop_condition}")
        elif state.phase == "plan_reads":
            parts.append(
                "Read plan contract: use discovery tools first, then submit FINISH with JSON only: "
                '{"subsystem":"...","stop_condition":"...","items":[{"path":"...","reason":"...","closes_gap":"...","priority":1,"max_ranges":1}]}'
            )
        ledger = getattr(self, "_evidence_ledger", None)
        if ledger is not None:
            summary_text = ledger.latest_phase_summary_text()
            if summary_text:
                parts.append(summary_text)
        elif state.phase_summaries:
            parts.append(f"Phase summary: {state.phase_summaries[-1]}")
        return "\n".join(parts)

    def _build_grounding_claims_summary(self) -> str:
        ledger = getattr(self, "_evidence_ledger", None)
        if ledger is None:
            return ""
        lines: list[str] = []
        claims = ledger.latest_claims()
        if claims:
            lines.append("Available grounded claims:")
            for claim in claims[:6]:
                lines.append(claim.prompt_text())

        records = ledger.key_evidence_records()
        if records:
            lines.append("")
            lines.append("Evidence IDs you MUST cite in your answer (use [ev_xxx] format):")
            for record in records[:12]:
                loc = record.path or "(no path)"
                lines.append(f"  [{record.evidence_id}] {loc}: {record.summary[:80]}")
            lines.append("")
            lines.append("Example citation format:")
            sample_id = records[0].evidence_id
            lines.append(f'  "The module uses X pattern [{sample_id}]."')

        if not lines:
            all_records = ledger.all_records()
            if all_records:
                lines.append("Evidence IDs you MUST cite in your answer (use [ev_xxx] format):")
                for record in all_records[:12]:
                    loc = record.path or "(no path)"
                    lines.append(f"  [{record.evidence_id}] {loc}: {record.summary[:80]}")
                lines.append("")
                lines.append("Example citation format:")
                sample_id = all_records[0].evidence_id
                lines.append(f'  "The module uses X pattern [{sample_id}]."')

        return "\n".join(lines)

    def _read_plan_gate_observation(
        self,
        tool_call: ToolCall,
        repo_path: str,
    ) -> Observation | None:
        decision = self._read_plan_tool_decision(tool_call, repo_path)
        if decision is None or decision.allowed:
            return None
        return self._tool_decision_to_observation(tool_call, decision)

    def _read_plan_tool_decision(
        self,
        tool_call: ToolCall,
        repo_path: str,
    ) -> ToolDecision | None:
        state = getattr(self, "_analysis_phase_state", None)
        if state is None or not state.enabled or tool_call.name not in _READ_TOOL_NAMES:
            return None

        # plan_reads phase: unconditionally gate all read tools regardless of path
        read_plan = getattr(self, "_analysis_read_plan", None)
        if state.phase == "plan_reads" or not state.read_plan_ready or read_plan is None:
            return ToolDecision(
                allowed=False,
                reason="read_plan_required",
                synthetic_observation=(
                    "Deferred source read by phased analysis controller: broad analysis requires a read plan before "
                    "source reads. Use discovery tools (find_files, search_text, find_symbol) first, then call "
                    "submit_read_plan with your structured plan."
                ),
            )

        from agent.policy import normalize_repo_path

        normalized = normalize_repo_path(
            tool_call.params.get("path") or tool_call.params.get("file_path") or "",
            repo_path,
        )
        if not normalized:
            return None

        allowed_paths = read_plan.allowed_paths()
        if state.phase == "inspect" and normalized not in allowed_paths:
            allowed_text = ", ".join(sorted(allowed_paths)) or "(none)"
            return ToolDecision(
                allowed=False,
                reason="path_not_in_read_plan",
                synthetic_observation=(
                    "Deferred source read by phased analysis controller: "
                    f"{normalized} is not part of the approved inspect read plan. "
                    f"Approved paths: {allowed_text}. Inspect only planned files, or synthesize current evidence first."
                ),
            )
        if state.phase == "inspect":
            item = read_plan.item_for_path(normalized)
            unit_key = self._file_read_range_key(tool_call, repo_path)
            if item is not None and unit_key is not None and unit_key not in state.read_units:
                used_ranges = self._count_used_read_plan_ranges(normalized)
                if used_ranges >= item.max_ranges:
                    return ToolDecision(
                        allowed=False,
                        reason="read_plan_range_budget_exhausted",
                        synthetic_observation=(
                            "Deferred source read by phased analysis controller: "
                            f"{normalized} already used {used_ranges} planned read range(s), which reaches the "
                            f"approved max_ranges={item.max_ranges} budget for this inspect item. "
                            "Synthesize current evidence first, or move to a named verification gap instead of "
                            "broadening inspect reads."
                        ),
                    )
        return None

    def _tool_decision_to_observation(self, tool_call: ToolCall, decision: ToolDecision) -> Observation:
        return Observation(
            status=ObservationStatus.SUCCESS,
            tool_name=tool_call.name,
            output=decision.synthetic_observation or decision.reason,
        )

    def _count_used_read_plan_ranges(self, normalized_path: str) -> int:
        state = getattr(self, "_analysis_phase_state", None)
        if state is None:
            return 0
        return sum(1 for path, _start, _end in state.read_units if path == normalized_path)

    def _get_procedural_section(self) -> str:
        """获取当前已访问文件对应的 procedural 记忆内容。

        对新文件触发 record_access 递增计数，
        对全部已访问文件每步都展示规则（嵌入 task anchor 不丢失）。
        """
        if not self._memory_context or not self._memory_context.enabled:
            return ""
        accessed = getattr(self, "_accessed_files", None)
        if not accessed:
            return ""
        try:
            new_files = accessed - getattr(self, "_procedural_injected_files", set())
            if new_files:
                # 新文件：record_access + 更新跟踪
                self._memory_context.get_procedural_for_files(new_files, record_access=True)
                self._procedural_injected_files.update(new_files)
            # 每步都返回全部已访问文件的 procedural（不重复 record_access）
            return self._memory_context.get_procedural_for_files(accessed, record_access=False)
        except Exception as exc:
            logger.debug("Procedural section build failed: %s", exc)
            return ""

    def _load_project_rules(self) -> str:
        """加载项目规则文件（.forge-agent/rules.md），不存在时返回空字符串。"""
        import os
        repo_path = getattr(self, "_current_repo_path", ".")
        rules_path = os.path.join(repo_path, ".forge-agent", "rules.md")
        try:
            if os.path.isfile(rules_path):
                with open(rules_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    logger.debug("Loaded project rules from %s", rules_path)
                    return content
        except OSError as exc:
            logger.debug("Failed to load project rules: %s", exc)
        return ""

    def _file_read_range_key(self, tool_call: ToolCall, repo_path: str) -> tuple[str, int, int | None] | None:
        """Return a normalized file read range key for duplicate-read suppression."""
        if tool_call.name not in ("file_read", "file_view"):
            return None
        file_path = tool_call.params.get("path") or tool_call.params.get("file_path") or ""
        if not file_path:
            return None
        from agent.policy import normalize_repo_path
        normalized = normalize_repo_path(file_path, repo_path)
        if tool_call.name == "file_view":
            start_line = max(1, int(tool_call.params.get("start_line", 1)))
            from tools.file_tool import VIEW_WINDOW_LINES
            return (normalized, start_line, start_line + VIEW_WINDOW_LINES - 1)
        from tools.file_tool import MAX_READ_LINES
        return (normalized, 1, MAX_READ_LINES)

    def _verification_read_gate_observation(
        self,
        tool_call: ToolCall,
        repo_path: str,
    ) -> Observation | None:
        """Gate post-synthesis source reads to semantic recommended verification reads."""
        decision = self._verification_read_tool_decision(tool_call, repo_path)
        if decision is None or decision.allowed:
            return None
        return self._tool_decision_to_observation(tool_call, decision)

    def _verification_read_tool_decision(
        self,
        tool_call: ToolCall,
        repo_path: str,
    ) -> ToolDecision | None:
        """Gate post-synthesis source reads to semantic recommended verification reads."""
        state = getattr(self, "_analysis_phase_state", None)
        ledger = getattr(self, "_evidence_ledger", None)
        if state is None or not state.enabled or ledger is None:
            return None
        if state.phase not in {"synthesize", "verify", "answer"}:
            return None
        if tool_call.name not in _READ_TOOL_NAMES:
            return None
        file_path = tool_call.params.get("path") or tool_call.params.get("file_path") or ""
        if not file_path:
            return None
        from agent.policy import normalize_repo_path
        normalized = normalize_repo_path(file_path, repo_path)
        allowed = ledger.recommended_reads_for_phase("inspect")
        if normalized in allowed:
            return None
        allowed_text = ", ".join(sorted(allowed)) if allowed else "(none)"
        return ToolDecision(
            allowed=False,
            reason="verification_path_not_recommended",
            synthetic_observation=(
                "Deferred source read by phased analysis controller: "
                f"{normalized} is not in the recommended verification reads for the completed inspect phase. "
                f"Recommended reads: {allowed_text}. Use the phase summary and artifact_read for raw evidence, "
                "or answer with current confidence boundaries instead of broadening file reads."
            ),
        )

    def _find_overlapping_file_read_range(
        self,
        key: tuple[str, int, int | None],
    ) -> tuple[str, int, int | None] | None:
        """Return an existing read range that fully covers key, if any."""
        path, start_line, end_line = key
        if not hasattr(self, "_read_file_ranges"):
            self._read_file_ranges = set()
        for existing in self._read_file_ranges:
            existing_path, existing_start, existing_end = existing
            if existing_path != path:
                continue
            if existing_end is None:
                return existing
            if end_line is None:
                continue
            if existing_start <= start_line and existing_end >= end_line:
                return existing
        return None

    def _duplicate_file_read_observation(
        self,
        tool_call: ToolCall,
        repo_path: str,
    ) -> Observation | None:
        """Return a synthetic observation when an identical file read was already done."""
        key = self._file_read_range_key(tool_call, repo_path)
        if key is None:
            return None
        overlapping_key = self._find_overlapping_file_read_range(key)
        if overlapping_key is None:
            self._read_file_ranges.add(key)
            return None
        path, start_line, end_line = overlapping_key
        if end_line is None:
            range_text = "the full file"
        else:
            range_text = f"lines {start_line}-{end_line}"
        return Observation(
            status=ObservationStatus.SUCCESS,
            tool_name=tool_call.name,
            output=(
                f"Skipped duplicate {tool_call.name}: {path} {range_text} was already read in this run. "
                "Use the earlier observation instead of reading it again."
            ),
        )

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
    _ARTIFACT_EXEMPT_TOOLS = frozenset({
        "file_read", "file_view", "file_edit", "file_write",
        "find_files", "find_symbol",
        "git_status", "git_add", "git_commit",
        "memory_read", "memory_list", "memory_search",
    })

    def _build_tool_result_content(self, observation: Observation) -> str:
        """构建 native tool_use 模式下的工具结果内容（不含 [Tool:] 包装）。"""
        parts: list[str] = []
        if observation.output:
            output = observation.output
            # Artifact 化：对非豁免工具的大输出，存入 artifact store
            if (
                observation.tool_name not in self._ARTIFACT_EXEMPT_TOOLS
                and self._artifact_store is not None
            ):
                output, was_stored = self._artifact_store.maybe_store(
                    observation.tool_name, output
                )
                if was_stored:
                    logger.debug(
                        "Artifacted output from %s (%d tokens stored)",
                        observation.tool_name,
                        self._artifact_store.total_tokens_stored,
                    )
            else:
                output = self._truncate_output(output)
            parts.append(output)
        if observation.error and not observation.is_success():
            parts.append(f"Error: {observation.error}")
        return "\n".join(parts) if parts else "(no output)"

    @staticmethod
    def _truncate_output(text: str, max_chars: int = 8000) -> str:
        """对超长 tool output 做预截断，保留首尾关键部分。"""
        if len(text) <= max_chars:
            return text
        keep = max_chars // 2
        head = text[:keep]
        tail = text[-keep:]
        omitted = len(text) - max_chars
        return f"{head}\n\n... [{omitted} chars omitted] ...\n\n{tail}"

    def _format_observations_for_history(self, observations: list[Observation]) -> str:
        """把多条 Observation 格式化为一条 user 消息（并行 tool_calls 用，text fallback mode）。"""
        lines = []
        for obs in observations:
            status = "SUCCESS" if obs.is_success() else "ERROR"
            lines.append(f"[Tool: {obs.tool_name} | {status}]")
            if obs.output:
                output = obs.output
                if (
                    obs.tool_name not in self._ARTIFACT_EXEMPT_TOOLS
                    and self._artifact_store is not None
                ):
                    output, _ = self._artifact_store.maybe_store(obs.tool_name, output)
                else:
                    output = self._truncate_output(output)
                lines.append(output)
            if obs.error and not obs.is_success():
                lines.append(f"Error: {obs.error}")
        return "\n".join(lines)

    def _is_looping(self, log: EventLog) -> bool:
        """
        检测是否陷入死循环。两级检测：

        1. 严格匹配：最近 N 条 action 的 (tool_name, params) 完全相同
        2. 语义匹配：最近 N+1 条 action 使用的 tool_name 多重集相同
           （反复调用相同类型的工具，只是参数略有不同——需要更多证据）
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
            names = []
            for tool_call in action.tool_calls:
                # Reading distinct files/ranges is progress; exact duplicate params are still caught above.
                if tool_call.name in ("file_read", "file_view"):
                    continue
                names.append(tool_call.name)
            return tuple(sorted(names))

        # Level 1: exact match (window = N)
        first_exact = _serialize_exact(recent[0])
        if all(_serialize_exact(a) == first_exact for a in recent[1:]):
            return True

        # Level 2: semantic loop — same tool name multiset repeated N+1 times
        # Requires one more repetition than exact match to reduce false positives
        semantic_window = n + 1
        if len(actions) >= semantic_window:
            semantic_recent = actions[-semantic_window:]
            if all(a.action_type == ActionType.TOOL_CALL and a.tool_calls for a in semantic_recent):
                first_names = _serialize_names(semantic_recent[0])
                if first_names and all(_serialize_names(a) == first_names for a in semantic_recent[1:]):
                    return True

        return False

    def _call_with_retry(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ):
        """
        带指数退避重试的 LLM 调用。
        stream=True 时走 backend.stream()，否则走 complete()。
        不重试：认证失败（401/403）、参数错误（400）。
        """
        import time as _time

        last_exc: Exception | None = None
        delay = self._cfg.llm_retry_delay
        observer = get_observer()
        capture_prompts = observer.config.capture_prompts if observer.config else True
        capture_llm_outputs = observer.config.capture_llm_outputs if observer.config else True
        provider = getattr(self, "_provider_name", None)
        if provider is None:
            provider = type(self._backend).__name__.removesuffix("Backend").lower()
        prompt_metadata = consume_prompt_usage_metadata()

        for attempt in range(1, self._cfg.llm_max_retries + 1):
            try:
                with observer.start_generation(
                    name="llm-completion",
                    model=self._backend.model_name,
                    input_data=build_generation_input(
                        messages,
                        tools,
                        capture_prompts=capture_prompts,
                    ),
                    metadata={
                        "attempt": attempt,
                        "provider": provider,
                        "model": self._backend.model_name,
                        "prompts": prompt_metadata,
                    },
                ) as generation_obs:
                    if self._cfg.stream:
                        cb = self._cfg.stream_callback
                        thought_cb = self._cfg.thought_callback
                        if hasattr(self._backend, "stream"):
                            response = self._backend.stream(
                                messages, tools,
                                on_text=cb,
                                on_thought=thought_cb,
                            )
                            generation_obs.update(
                                output=build_generation_output(
                                    response,
                                    capture_llm_outputs=capture_llm_outputs,
                                ),
                                metadata=merge_metadata(
                                    build_generation_metadata(
                                        response,
                                        attempt=attempt,
                                        provider=provider,
                                        model=self._backend.model_name,
                                    ),
                                    {"prompts": prompt_metadata},
                                ),
                            )
                            return response
                    response = self._backend.complete(messages, tools)
                    generation_obs.update(
                        output=build_generation_output(
                            response,
                            capture_llm_outputs=capture_llm_outputs,
                        ),
                        metadata=merge_metadata(
                            build_generation_metadata(
                                response,
                                attempt=attempt,
                                provider=provider,
                                model=self._backend.model_name,
                            ),
                            {"prompts": prompt_metadata},
                        ),
                    )
                    return response
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()
                if any(kw in exc_str for kw in (
                    "401", "403", "invalid api key", "authentication",
                    "400", "bad request",
                )):
                    raise
                if attempt < self._cfg.llm_max_retries:
                    logger.warning(
                        "LLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt, self._cfg.llm_max_retries, exc, delay,
                    )
                    _time.sleep(delay)
                    delay *= 2

        raise last_exc  # type: ignore[misc]

    def _get_git_diff(self, repo_path: str) -> str | None:
        """抓取 git diff HEAD 作为 patch，失败时静默返回 None。"""
        return _git_diff(repo_path)

    # ------------------------------------------------------------------
    # 权限模式切换（Plan Mode / Execute Mode）
    # ------------------------------------------------------------------

    def switch_to_plan_mode(self) -> None:
        """切换到规划模式（只读工具）。"""
        self._registry = self._readonly_registry
        logger.info("Switched to plan mode (readonly tools)")

    def switch_to_execute_mode(self) -> None:
        """切换到执行模式（完整工具）。"""
        self._registry = self._full_registry
        logger.info("Switched to execute mode (full tools)")

    def switch_to_no_tool_mode(self) -> None:
        """切换到无工具模式（用于只读问答任务的纯计划阶段）。"""
        self._registry = ToolRegistry()
        logger.info("Switched to no-tool mode")

    def _build_readonly_registry(self) -> ToolRegistry:
        """从完整注册表构建只读版本（仅含 _READONLY_TOOLS 白名单中的工具）。"""
        from tools.base import ToolRegistry
        if isinstance(self._full_registry, PolicyAwareToolRegistry):
            return self._full_registry.with_allowed_tools(_READONLY_TOOLS)
        readonly = ToolRegistry()
        for name, tool in self._full_registry._tools.items():
            if name in _READONLY_TOOLS:
                readonly._tools[name] = tool
        return readonly

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


# ---------------------------------------------------------------------------
# PlanExecuteAgent — Claude Code 风格 Plan-then-Execute
# ---------------------------------------------------------------------------


class PlanExecuteAgent:
    """
    Claude Code 风格的 Plan-then-Execute Agent。

    两阶段同上下文设计：
    - Phase 1（规划）: 只读探索 → 生成 markdown 计划 → 用户审批
    - Phase 2（执行）: 全工具权限 → 在同一对话上下文中执行计划

    与旧设计的区别：
    - 规划阶段有工具访问权（只读），可以实际探索代码
    - 计划是 markdown（人类可读），不是 JSON subtask 列表
    - 需要用户审批后才执行
    - 执行阶段保留规划阶段的对话历史（不丢失探索上下文）

    用法：
        agent = PlanExecuteAgent(backend, registry, config, plan_config)
        result = agent.run(task, log)
    """

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        agent_config: AgentConfig | None = None,
        plan_config: "PlanExecuteConfig | None" = None,
        planning_backend: LLMBackend | None = None,
        memory_context: "MemoryContext | None" = None,
    ) -> None:
        self._backend = backend
        self._planning_backend = planning_backend or backend
        self._registry = registry
        self._cfg = agent_config or AgentConfig()
        self._memory_context = memory_context
        from agent.plan import PlanExecuteConfig as _PlanCfg
        self._plan_cfg = plan_config or _PlanCfg()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run(self, task: Task, log: EventLog) -> RunResult:
        """
        两阶段 Plan-then-Execute（同实例切换权限）。

        Phase 1: 只读规划 → 生成 markdown 计划
        Phase 2: 用户审批后按任务意图执行
          - edit: 切换到完整工具权限实施修改
          - analysis: 继续只读执行并生成答案
        """
        from agent.plan import Plan
        from agent.prompt import get_plan_mode_injection, get_plan_execution_injection

        set_project_dir(task.repo_path)
        observer = get_observer()
        task_context = observer.start_task(task)
        task_obs = task_context.__enter__()
        task_obs_closed = False

        def _finish_plan_result(result: RunResult, phase: str = "plan_execute") -> RunResult:
            nonlocal task_obs_closed
            run_stats = summarize_run(log)
            analysis_metadata = build_analysis_run_metadata(
                run_stats=run_stats,
                context_stats=getattr(agent, "_last_context_stats", None),
            )
            task_obs.update(
                output=build_run_output(result),
                metadata=merge_metadata(
                    build_run_metadata(result),
                    {"phase": phase, **task.metadata},
                    analysis_metadata,
                ),
            )
            for score in build_run_scores(task, result, stats=run_stats):
                task_obs.score(
                    name=score.name,
                    value=score.value,
                    comment=score.comment,
                    metadata=merge_metadata(score.metadata, {"phase": phase}),
                )
            append_failure_dataset_item(task, result, log_path=log.path, stats=run_stats)
            if not task_obs_closed:
                task_context.__exit__(None, None, None)
                task_obs_closed = True
            return result

        log.log_task_start(task)
        logger.info("PlanExecuteAgent starting task %s", task.task_id)

        policy = build_task_policy(task)
        execution_registry = PolicyAwareToolRegistry(
            base=self._registry,
            phase_policy=policy.execution,
            repo_path=task.repo_path,
            phase_name="execution",
        )
        agent = ReActAgent(
            self._backend, execution_registry, self._cfg,
            memory_context=self._memory_context,
        )
        history = ConversationHistory(
            max_messages=self._cfg.history_max_messages,
        )
        agent._pending_history = history

        total_plan_tokens = 0
        total_plan_steps = 0
        revision_feedback = ""
        max_plan_attempts = max(1, self._plan_cfg.max_replans + 1)
        plan_result: RunResult | None = None
        plan_text = ""

        for attempt in range(1, max_plan_attempts + 1):
            plan_result = self._run_planning_phase(
                agent, history, task, log, get_plan_mode_injection(), policy, revision_feedback
            )
            total_plan_tokens += plan_result.total_tokens
            total_plan_steps += plan_result.steps_taken
            plan_text = plan_result.summary or ""
            if not plan_text.strip():
                return _finish_plan_result(
                    self._fallback_after_empty_plan(agent, task, log),
                    phase="fallback",
                )

            plan = Plan.from_markdown(plan_text, task.description)
            log.log_plan_generated(plan)
            logger.info("Plan generated (%d chars): %s...", len(plan_text), plan_text[:100])

            approval = self._request_plan_approval(plan_text)
            if not approval.approved:
                reason = approval.feedback or "Plan rejected by user"
                log.log_task_failed(steps=total_plan_steps, reason=reason)
                return _finish_plan_result(RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=total_plan_steps,
                    total_tokens=total_plan_tokens,
                ), phase="planning")
            if approval.action != "revise":
                break
            revision_feedback = approval.feedback or "Please revise the plan before execution."
            if attempt == max_plan_attempts:
                reason = f"Plan revision requested but max revisions reached: {revision_feedback}"
                log.log_task_failed(steps=total_plan_steps, reason=reason)
                return _finish_plan_result(RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=total_plan_steps,
                    total_tokens=total_plan_tokens,
                ), phase="planning")

        assert plan_result is not None

        exec_result = self._run_execution_phase(
            agent=agent,
            history=history,
            task=task,
            log=log,
            plan_text=plan_text,
            plan_result=plan_result,
            exec_injection=get_plan_execution_injection(),
            policy=policy,
            consumed_plan_steps=total_plan_steps,
            consumed_plan_tokens=total_plan_tokens,
        )

        if hasattr(agent, "_pending_history"):
            del agent._pending_history

        total_tokens = total_plan_tokens + exec_result.total_tokens
        total_steps = total_plan_steps + exec_result.steps_taken

        if exec_result.is_success():
            patch = _git_diff(task.repo_path) if task.intent == "edit" else None
            verdict = CompletionValidator().validate(
                log,
                policy,
                task.repo_path,
                task=task,
                evidence_ledger=getattr(agent, "_evidence_ledger", None),
                final_summary=exec_result.summary,
            )
            if not verdict.success:
                log.log_task_failed(steps=total_steps, reason=verdict.reason)
                return _finish_plan_result(RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=verdict.reason,
                    steps_taken=total_steps,
                    total_tokens=total_tokens,
                    patch=patch,
                    cache_stats=exec_result.cache_stats,
                ), phase="execution")
            summary = exec_result.summary or "Plan executed successfully"
            log.log_task_complete(steps=total_steps, summary=summary)
            return _finish_plan_result(RunResult(
                task_id=task.task_id,
                status=RunStatus.SUCCESS,
                summary=summary,
                steps_taken=total_steps,
                total_tokens=total_tokens,
                patch=patch,
                cache_stats=exec_result.cache_stats,
            ), phase="execution")

        log.log_task_failed(steps=total_steps, reason=exec_result.summary)
        return _finish_plan_result(RunResult(
            task_id=task.task_id,
            status=exec_result.status,
            summary=exec_result.summary,
            steps_taken=total_steps,
            total_tokens=total_tokens,
            error=exec_result.error,
            cache_stats=exec_result.cache_stats,
        ), phase="execution")

    def _run_planning_phase(
        self,
        agent: ReActAgent,
        history: ConversationHistory,
        task: Task,
        log: EventLog,
        plan_injection: str,
        policy: TaskPolicy,
        revision_feedback: str = "",
    ) -> RunResult:
        intent_label = "read-only answer" if task.intent == "analysis" else "implementation"
        revision_section = ""
        if revision_feedback:
            revision_section = f"\n\n## User Revision Feedback\n{revision_feedback}\nRevise the plan to address this feedback."
        constraint_section = ""
        prompt_constraints = policy.to_prompt_section("planning")
        if task.intent == "analysis":
            constraint_section = "\n\n## Enforced Planning Constraint\nNo tools are available during planning for read-only answer tasks."
        if prompt_constraints:
            constraint_section += f"\n\n{prompt_constraints}"
        history.add(LLMMessage(
            role="user",
            content=(
                f"{plan_injection}\n\n"
                f"## Repository\n{task.repo_path}\n\n"
                f"## Task Type\n{intent_label}\n\n"
                f"## Task\n{task.description}"
                f"{constraint_section}"
                f"{revision_section}\n\n"
                "Produce only an approval plan for the execution phase. "
                "Do not include the final answer or completed work in the plan. "
                "Call finish with the plan when ready."
            ),
        ))

        plan_steps = max(3, task.max_steps // 3)
        plan_task = Task(
            description=task.description,
            repo_path=task.repo_path,
            intent=task.intent,
            issue_url=task.issue_url,
            test_cmd=task.test_cmd,
            max_steps=plan_steps,
            budget_tokens=max(1, task.budget_tokens // 3),
            metadata={**task.metadata, "phase": "planning", "parent_task_id": task.task_id},
        )

        if task.intent == "analysis":
            agent.switch_to_no_tool_mode()
        else:
            agent.switch_to_plan_mode()
        plan_log = EventLog.create(plan_task, log_dir=self._plan_cfg.plan_subtask_log_dir)
        try:
            return agent.run(plan_task, plan_log)
        finally:
            plan_log.close()

    def _fallback_after_empty_plan(self, agent: ReActAgent, task: Task, log: EventLog) -> RunResult:
        logger.warning("Plan generation produced empty result — falling back")
        if task.intent == "analysis":
            agent.switch_to_plan_mode()
        else:
            agent.switch_to_execute_mode()
        fallback_task = Task(
            description=task.description,
            repo_path=task.repo_path,
            intent=task.intent,
            issue_url=task.issue_url,
            test_cmd=task.test_cmd,
            max_steps=task.max_steps,
            budget_tokens=task.budget_tokens,
            metadata={**task.metadata, "phase": "fallback", "parent_task_id": task.task_id},
        )
        return agent.run(fallback_task, log)

    def _request_plan_approval(self, plan_text: str):
        from agent.plan import PlanApproval

        approval_cb = self._plan_cfg.plan_approval_callback
        if not approval_cb:
            return PlanApproval(approved=True)

        raw = approval_cb(plan_text)
        if isinstance(raw, PlanApproval):
            return raw
        return PlanApproval(approved=bool(raw))

    def _run_execution_phase(
        self,
        agent: ReActAgent,
        history: ConversationHistory,
        task: Task,
        log: EventLog,
        plan_text: str,
        plan_result: RunResult,
        exec_injection: str,
        policy: TaskPolicy,
        consumed_plan_steps: int | None = None,
        consumed_plan_tokens: int | None = None,
    ) -> RunResult:
        used_steps = plan_result.steps_taken if consumed_plan_steps is None else consumed_plan_steps
        used_tokens = plan_result.total_tokens if consumed_plan_tokens is None else consumed_plan_tokens
        exec_steps = max(3, task.max_steps - used_steps)
        exec_budget = max(1, task.budget_tokens - used_tokens)
        exec_task = Task(
            description=task.description,
            repo_path=task.repo_path,
            intent=task.intent,
            issue_url=task.issue_url,
            test_cmd=task.test_cmd,
            max_steps=exec_steps,
            budget_tokens=exec_budget,
            metadata={**task.metadata, "phase": "execution", "parent_task_id": task.task_id},
        )

        mode_instruction = (
            "This is a read-only answer task in the execution phase. You must read the approved source file now and answer from that content; do not produce another plan."
            if task.intent == "analysis"
            else "This is an implementation task. You must perform the approved edit now; do not produce another plan or finish until the required file write is done."
        )
        prompt_constraints = policy.to_prompt_section("execution")
        if prompt_constraints:
            mode_instruction += "\n\n" + prompt_constraints
        exec_history = ConversationHistory(max_messages=self._cfg.history_max_messages)
        agent._pending_history = exec_history

        exec_history.add(LLMMessage(
            role="user",
            content=(
                f"## Original Task\n{task.description}\n\n"
                f"## Approved Plan\n{plan_text}\n\n"
                f"{exec_injection}\n\n"
                f"## Execution Permission\n{mode_instruction}"
            ),
        ))

        if task.intent == "analysis":
            agent.switch_to_plan_mode()
        else:
            agent.switch_to_execute_mode()
        return agent.run(exec_task, log)

    def _get_git_diff(self, repo_path: str) -> str | None:
        return _git_diff(repo_path)
