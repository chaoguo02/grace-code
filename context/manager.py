"""
context/manager.py

ContextManager — 统一的上下文组装器。

职责：
- 从各层源（system, memory, repo_map, session, history）组装完整 prompt
- 执行分层裁剪（pre-LLM 管线 → compaction → final trim）
- 测量 ContextStats
- 为 artifact 引用提供解析支持

不负责：
- 对话历史的增删改（由 ConversationHistory 管理）
- 压缩决策（由 ChatSession._maybe_auto_compact_after_round 驱动）
- 工具输出的 artifact 化（由 ArtifactStore 在写入历史时处理）

设计原则：
- 纯函数式：每次调用 build_request_messages() 不修改任何内部状态
- 可观测：每次组装都产生 ContextStats
- 可配置：所有阈值通过 ContextManagerConfig 传入
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from context.history import ConversationHistory, ConversationSnapshot
from context.stats import ContextStats
from context.structured import ContextLayer, ContextPriority, StructuredContext
from context.token_budget import TokenBudget, estimate_tokens

if TYPE_CHECKING:
    from agent.task import ToolCall
    from context.artifacts import ArtifactStore
    from context.compaction import ConversationCompactor
    from llm.base import LLMMessage

logger = logging.getLogger(__name__)


@dataclass
class ContextManagerConfig:
    """ContextManager 配置。"""
    request_budget_tokens: int = 110_000
    history_max_messages: int = 200
    compact_history: bool = True
    enable_caching: bool = False


@dataclass
class RequestContext:
    """build_request_messages 的返回结果，包含消息列表和观测数据。"""
    messages: list["LLMMessage"] = field(default_factory=list)
    stats: ContextStats = field(default_factory=ContextStats)
    compact_triggered: bool = False
    compaction_summary: str | None = None


class ContextReduction(str, Enum):
    """A transform selected by the normal in-turn context planner."""

    TOOL_RESULT_BUDGET = "tool_result_budget"
    SNIP = "snip"
    MICRO_COMPACT = "micro_compact"
    COLLAPSE = "collapse"
    COMPACT = "compact"


class ContextPlanningStage(str, Enum):
    PREPARE = "prepare"
    ASSEMBLE = "assemble"


@dataclass(frozen=True)
class ContextSnapshot:
    """Immutable facts available to context reduction policy."""

    message_count: int
    estimated_tokens: int
    step: int = 1
    tokens_freed: int = 0


@dataclass(frozen=True)
class ContextBudget:
    """Budget and feature policy supplied to one planning decision."""

    history_tokens: int
    enabled: bool = True


@dataclass(frozen=True)
class ContextReductionPlan:
    """Ordered transforms selected for one context pipeline stage."""

    stage: ContextPlanningStage
    reductions: tuple[ContextReduction, ...] = ()
    reason: str = ""
    effective_tokens: int = 0

    def includes(self, reduction: ContextReduction) -> bool:
        return reduction in self.reductions


class ContextPlanner:
    """Single owner of normal in-turn context reduction decisions."""

    def __init__(
        self,
        *,
        collapse_ratio: float = 0.75,
        compact_ratio: float = 0.80,
        min_collapse_messages: int = 12,
        min_compact_messages: int = 6,
        compact_cooldown_steps: int = 3,
        max_consecutive_compactions: int = 3,
    ) -> None:
        self._collapse_ratio = collapse_ratio
        self._compact_ratio = compact_ratio
        self._min_collapse_messages = min_collapse_messages
        self._min_compact_messages = min_compact_messages
        self._compact_cooldown_steps = compact_cooldown_steps
        self._max_consecutive_compactions = max_consecutive_compactions
        self._steps_since_compaction = compact_cooldown_steps
        self._consecutive_compactions = 0

    def tick_step(self) -> None:
        self._steps_since_compaction += 1

    def record_compaction(self) -> None:
        self._steps_since_compaction = 0
        self._consecutive_compactions += 1

    def reset_compaction_series(self) -> None:
        self._consecutive_compactions = 0

    def plan(
        self,
        snapshot: ContextSnapshot,
        budget: ContextBudget,
        *,
        stage: ContextPlanningStage,
    ) -> ContextReductionPlan:
        # ``estimated_tokens`` describes the current materialized snapshot.
        # Cheap reductions have already changed that snapshot, so subtracting
        # ``tokens_freed`` again would under-count pressure.
        effective = max(0, snapshot.estimated_tokens)
        if not budget.enabled or budget.history_tokens <= 0:
            return ContextReductionPlan(
                stage=stage,
                reason="context reduction disabled",
                effective_tokens=effective,
            )

        if stage is ContextPlanningStage.PREPARE:
            if snapshot.step <= 1:
                return ContextReductionPlan(
                    stage=stage,
                    reason="first turn preserves full context",
                    effective_tokens=effective,
                )
            reductions = [
                ContextReduction.TOOL_RESULT_BUDGET,
                ContextReduction.SNIP,
                ContextReduction.MICRO_COMPACT,
            ]
            if (
                snapshot.message_count >= self._min_collapse_messages
                and effective
                >= int(budget.history_tokens * self._collapse_ratio)
            ):
                reductions.append(ContextReduction.COLLAPSE)
            return ContextReductionPlan(
                stage=stage,
                reductions=tuple(reductions),
                reason="ordered cheap reductions before provider assembly",
                effective_tokens=effective,
            )

        compact_allowed = (
            snapshot.message_count >= self._min_compact_messages
            and self._steps_since_compaction
            >= self._compact_cooldown_steps
            and self._consecutive_compactions
            < self._max_consecutive_compactions
        )
        if (
            compact_allowed
            and effective > int(budget.history_tokens * self._compact_ratio)
        ):
            return ContextReductionPlan(
                stage=stage,
                reductions=(ContextReduction.COMPACT,),
                reason="history exceeds semantic compaction threshold",
                effective_tokens=effective,
            )
        return ContextReductionPlan(
            stage=stage,
            reason="history remains within semantic compaction policy",
            effective_tokens=effective,
        )


class ContextManager:
    """
    统一上下文组装器。

    从 agent/core.py::_build_messages() 抽取而来。
    负责将各层上下文按优先级、预算组装成发给 LLM 的最终 messages。

    用法：
        mgr = ContextManager(config)
        ctx = mgr.build_request_messages(
            history=history,
            token_budget=token_budget,
            repo_map=repo_map,
            system_core_text=system_core_text,
            variable_text=variable_text,
            long_term_context=long_term_context,
            task_anchor=task_anchor,
            artifact_store=artifact_store,
            consumed_tokens=0,
        )
        messages = ctx.messages
        stats = ctx.stats
    """

    def __init__(
        self,
        config: ContextManagerConfig | None = None,
        planner: ContextPlanner | None = None,
    ) -> None:
        self._cfg = config or ContextManagerConfig()
        self._planner = planner or ContextPlanner()

    @property
    def planner(self) -> ContextPlanner:
        return self._planner

    def build_request_messages(
        self,
        history: ConversationHistory,
        token_budget: TokenBudget,
        system_core_text: str,
        variable_text: str = "",
        long_term_context: str | None = None,
        task_anchor: str | None = None,
        artifact_store: "ArtifactStore | None" = None,
        consumed_tokens: int = 0,
        max_context_window: int | None = None,
        repo_map_text: str = "",
        compactor: "ConversationCompactor | None" = None,
        compaction_task_context: str = "",
        tokens_freed: int = 0,
        history_materializer_fn=None,
        *,
        step: int = 1,
    ) -> RequestContext:
        """
        组装发给 LLM 的完整 messages。

        Args:
            history: 对话历史
            token_budget: token 预算管理器
            system_core_text: 系统核心 prompt 文本（已含 repo_map）
            variable_text: 可变 prompt 部分（auto_memory 指导等）
            long_term_context: 长期记忆上下文
            task_anchor: 任务锚点 prompt
            artifact_store: artifact 存储（用于统计）
            consumed_tokens: 本轮之前已消耗 token
            max_context_window: 模型上下文窗口
            repo_map_text: 预构建的 repo_map 文本（用于统计）
            compactor: 可选的公开 compaction 转换器
            compaction_task_context: 摘要时保留的当前任务语义
            tokens_freed: 本轮廉价裁剪已经释放的 token
        """
        from agent.task import ToolCall
        from llm.base import LLMMessage

        plan = token_budget.compute_plan(
            consumed_tokens=consumed_tokens,
            max_context_window=max_context_window,
        )

        # System prompt via StructuredContext
        structured_ctx = StructuredContext()
        structured_ctx.add_layer(ContextLayer(
            name="system_core",
            priority=ContextPriority.SYSTEM,
            content=system_core_text,
            cacheable=True,
        ))
        if variable_text:
            structured_ctx.add_layer(ContextLayer(
                name="memory_guidance",
                priority=ContextPriority.PROJECT,
                content=variable_text,
                cacheable=True,
            ))

        system_content = structured_ctx.build_system_content(
            enable_caching=self._cfg.enable_caching,
        )

        # History processing pipeline
        history_dicts = history.to_dicts()
        if history_materializer_fn:
            history_dicts = history_materializer_fn(history_dicts)
        # Note: trim_sliding_window was removed here. Its function (round-based
        # drop of old tool results) is fully covered by TokenBudget.trim_history()
        # below, which uses a more granular 4-level priority strategy.

        # The planner owns the decision; the compactor only transforms.
        compact_triggered = False
        compaction_summary: str | None = None
        context_plan: ContextReductionPlan | None = None
        if compactor is not None:
            context_plan = self._planner.plan(
                ContextSnapshot(
                    message_count=len(history_dicts),
                    estimated_tokens=sum(
                        estimate_tokens(str(message.get("content", "")))
                        for message in history_dicts
                    ),
                    step=step,
                    tokens_freed=tokens_freed,
                ),
                ContextBudget(
                    history_tokens=plan.history,
                    enabled=self._cfg.compact_history,
                ),
                stage=ContextPlanningStage.ASSEMBLE,
            )
            if context_plan.includes(ContextReduction.COMPACT):
                from context.compaction import MicroCompactor

                history_dicts = MicroCompactor().compact(history_dicts)
                history_dicts = compactor.compact_history(
                    history_dicts,
                    task_context=compaction_task_context,
                )
                self._planner.record_compaction()
                compact_triggered = True
                compaction_summary = self._find_compaction_summary(
                    history_dicts,
                )
                logger.info("ContextManager: auto-compaction triggered")

        # Final trim
        trimmed_history_dicts = token_budget.trim_history(history_dicts, plan.history)

        # Assemble messages
        messages: list[LLMMessage] = [LLMMessage(role="system", content=system_content)]

        if long_term_context:
            messages.append(LLMMessage(role="user", content=long_term_context))
            messages.append(LLMMessage(
                role="assistant",
                content="Understood. I have the project context and memory index. Proceeding with the task.",
            ))

        for d in trimmed_history_dicts:
            tool_calls = None
            if "tool_calls" in d:
                tool_calls = [
                    ToolCall(name=tc["name"], params=tc["params"], id=tc.get("id"))
                    for tc in d["tool_calls"]
                ]
            messages.append(LLMMessage(
                role=d["role"],
                content=d["content"],
                tool_call_id=d.get("tool_call_id"),
                tool_calls=tool_calls,
            ))

        if task_anchor:
            messages.append(LLMMessage(role="user", content=task_anchor))

        # Measure stats
        stats = self._measure_stats(
            messages=messages,
            system_content=system_content,
            long_term=long_term_context,
            trimmed_history_dicts=trimmed_history_dicts,
            anchor=task_anchor,
            budget_total=plan.total,
            compact_triggered=compact_triggered,
            artifact_store=artifact_store,
            repo_map_text=repo_map_text,
        )
        if context_plan is not None:
            stats.compact_reason = context_plan.reason
        if compact_triggered and compactor is not None:
            compact_result = getattr(
                compactor, "last_compaction_result", None,
            )
            if compact_result is not None:
                stats.compact_method = compact_result.method.value
                stats.compact_truncated = compact_result.truncated
                stats.compact_source_range = compact_result.source_range

        return RequestContext(
            messages=messages,
            stats=stats,
            compact_triggered=compact_triggered,
            compaction_summary=compaction_summary,
        )

    def build_sub_agent_messages(
        self,
        history: ConversationHistory,
        system_content: str,
    ) -> RequestContext:
        """Sub-agent 模式：精简组装，不做裁剪。"""
        from agent.task import ToolCall
        from llm.base import LLMMessage

        history_dicts = history.to_dicts()
        messages: list[LLMMessage] = [LLMMessage(role="system", content=system_content)]

        for d in history_dicts:
            tool_calls = None
            if "tool_calls" in d:
                tool_calls = [
                    ToolCall(name=tc["name"], params=tc["params"], id=tc.get("id"))
                    for tc in d["tool_calls"]
                ]
            messages.append(LLMMessage(
                role=d["role"],
                content=d["content"],
                tool_call_id=d.get("tool_call_id"),
                tool_calls=tool_calls,
            ))

        stats = ContextStats(
            estimated_total_tokens=sum(estimate_tokens(m.content or "") for m in messages),
        )
        return RequestContext(messages=messages, stats=stats)

    def build_inherited_messages(
        self,
        snapshot: ConversationSnapshot,
        history: ConversationHistory,
    ) -> RequestContext:
        """Append fork-local history to an immutable parent request prefix."""
        messages = snapshot.materialize() + history.to_list()
        stats = ContextStats(
            estimated_total_tokens=sum(
                estimate_tokens(message.content or "") for message in messages
            ),
        )
        return RequestContext(messages=messages, stats=stats)

    @staticmethod
    def _find_compaction_summary(messages: list[dict]) -> str | None:
        """Return the generated compact block, never the preserved task head."""
        for message in messages:
            content = str(message.get("content", ""))
            if (
                message.get("kind") == "compaction_boundary"
                or content.startswith("[Earlier conversation summarized")
                or content.startswith("[Conversation compacted")
            ):
                return content
        return None

    def _measure_stats(
        self,
        messages: list["LLMMessage"],
        system_content,
        long_term: str | None,
        trimmed_history_dicts: list[dict],
        anchor: str | None,
        budget_total: int,
        compact_triggered: bool,
        artifact_store: "ArtifactStore | None" = None,
        repo_map_text: str = "",
    ) -> ContextStats:
        """测量 context stats。"""
        from context.token_budget import _estimate_msg_tokens

        system_tokens = estimate_tokens(system_content) if isinstance(system_content, str) else 0
        if isinstance(system_content, list):
            system_tokens = sum(
                estimate_tokens(block.get("text", "") if isinstance(block, dict) else str(block))
                for block in system_content
            )

        memory_tokens = estimate_tokens(long_term) if long_term else 0
        task_tokens = sum(_estimate_msg_tokens(d) for d in trimmed_history_dicts)
        anchor_tokens = estimate_tokens(anchor) if anchor else 0
        repo_map_tokens = estimate_tokens(repo_map_text) if repo_map_text else 0

        total_est = sum(estimate_tokens(m.content or "") for m in messages)

        artifact_summary_tokens = 0
        if artifact_store and artifact_store.count > 0:
            artifact_summary_tokens = artifact_store.total_tokens_stored

        return ContextStats(
            request_budget_tokens=budget_total,
            estimated_total_tokens=total_est,
            system_tokens=system_tokens,
            project_tokens=repo_map_tokens,
            memory_tokens=memory_tokens,
            session_tokens=0,
            task_tokens=task_tokens + anchor_tokens,
            repo_map_tokens=repo_map_tokens,
            artifact_summary_tokens=artifact_summary_tokens,
            omitted_tokens=0,
            compact_triggered=compact_triggered,
        )
