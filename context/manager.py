"""
context/manager.py

ContextManager — 统一的上下文组装器。

职责：
- 从各层源（system, memory, repo_map, session, history）组装完整 prompt
- 执行分层裁剪（snip → sliding window → compaction → trim）
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
from typing import TYPE_CHECKING

from context.compaction import snip_low_value_turns, trim_sliding_window
from context.history import ConversationHistory, ConversationSnapshot
from context.stats import ContextStats
from context.structured import ContextLayer, ContextPriority, StructuredContext
from context.token_budget import TokenBudget, estimate_tokens

if TYPE_CHECKING:
    from agent.task import ToolCall
    from context.artifacts import ArtifactStore
    from llm.base import LLMMessage

logger = logging.getLogger(__name__)


@dataclass
class ContextManagerConfig:
    """ContextManager 配置。"""
    request_budget_tokens: int = 70_000
    history_max_messages: int = 40
    compact_history: bool = True
    enable_caching: bool = False


@dataclass
class RequestContext:
    """build_request_messages 的返回结果，包含消息列表和观测数据。"""
    messages: list["LLMMessage"] = field(default_factory=list)
    stats: ContextStats = field(default_factory=ContextStats)
    compact_triggered: bool = False


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

    def __init__(self, config: ContextManagerConfig | None = None) -> None:
        self._cfg = config or ContextManagerConfig()

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
        compactor_fn=None,
        should_compact_fn=None,
        history_materializer_fn=None,
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
            compactor_fn: 可选的 compaction 回调 fn(dicts) -> dicts
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
        history_dicts = snip_low_value_turns(history_dicts)
        history_dicts = trim_sliding_window(
            history_dicts,
            token_limit=plan.history,
            keep_recent=3,
        )

        # Compaction check
        compact_triggered = False
        if compactor_fn:
            needs_compact = (
                should_compact_fn(history_dicts, plan.history)
                if should_compact_fn
                else self._should_compact(history_dicts, plan.history)
            )
            if needs_compact:
                history_dicts = compactor_fn(history_dicts)
                compact_triggered = True
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

        return RequestContext(
            messages=messages,
            stats=stats,
            compact_triggered=compact_triggered,
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
    def _should_compact(history_dicts: list[dict], budget: int) -> bool:
        """判断是否需要触发 compaction。"""
        from context.token_budget import _estimate_msg_tokens
        total = sum(_estimate_msg_tokens(d) for d in history_dicts)
        return total > budget * 0.80

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
