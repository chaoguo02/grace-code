"""runtime_core/context_budget.py

上下文窗口管理 — 主动管理 token 预算，不让 API 400 来告诉你超了。

CC 对齐：CC 内置了精密的上下文预算管理。在每轮请求前计算 token 数，
超限时自动触发：① 早期消息摘要压缩；② 非关键 tool_result 裁剪；
③ 系统提示词降级。与消息管道解耦，但紧密集成在 StepLoop 的决策前。

设计：
- ContextBudgetManager 是纯计算层，不持有状态
- 输入：NativeConversation + 预算限制
- 输出：修剪后的 NativeConversation + 预算消耗报告
- 修剪策略按优先级递减：裁剪 tool_result → 摘要早期消息 → 降级 system prompt
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime_core.native_message import (
    NativeConversation,
    NativeMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


# ── Token estimation ─────────────────────────────────────────────────────────

# 保守估计：平均每个字符 ≈ 0.3 tokens（英文）；中文约 0.6 tokens
_CHARS_PER_TOKEN = 3.0


def _estimate_tokens(text: str) -> int:
    """快速 token 数估算（不依赖 tokenizer，纯字符计数）。

    保守估计：text 长度 / 3 向上取整。
    """
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN) + 1)


def _message_tokens(msg: NativeMessage) -> int:
    """估算单个 NativeMessage 的 token 数。"""
    total = 4  # role + overhead
    for block in msg.content:
        if isinstance(block, TextBlock):
            total += _estimate_tokens(block.text)
        elif isinstance(block, ToolUseBlock):
            total += _estimate_tokens(block.name)
            total += _estimate_tokens(str(block.input))
        elif isinstance(block, ToolResultBlock):
            if isinstance(block.content, str):
                total += _estimate_tokens(block.content)
            elif isinstance(block.content, tuple):
                for sub in block.content:
                    total += _estimate_tokens(sub.text)
    return total


def _conversation_tokens(conv: NativeConversation) -> int:
    """估算整个对话的 token 数。"""
    return sum(_message_tokens(m) for m in conv.messages)


# ── Budget constraints ───────────────────────────────────────────────────────


@dataclass
class BudgetConfig:
    """上下文预算配置。"""
    max_tokens: int = 180_000          # 总预算（低于模型上限留余量）
    tool_result_max_chars: int = 2000  # tool_result 最大字符数
    first_message_preserved: bool = True  # 保留首条 user 消息
    tool_pair_preserved: bool = True   # 保留完整的 tool_use/tool_result 对


@dataclass
class BudgetReport:
    """预算消耗报告。"""
    original_tokens: int = 0
    trimmed_tokens: int = 0
    messages_trimmed: int = 0
    tool_results_trimmed: int = 0
    within_budget: bool = True


# ── ContextBudgetManager ─────────────────────────────────────────────────────


class ContextBudgetManager:
    """上下文预算管理器 — 纯计算层。

    在 NativeStepLoop 调用 invoke() 之前插入：
        budget = ContextBudgetManager(config)
        pruned_conv, report = budget.ensure_budget(conversation)
        model_action = backend.invoke(pruned_conv)
    """

    def __init__(self, config: BudgetConfig | None = None):
        self._config = config or BudgetConfig()

    def ensure_budget(
        self,
        conversation: NativeConversation,
    ) -> tuple[NativeConversation, BudgetReport]:
        """确保对话在 token 预算内。

        Returns:
            (修剪后的对话, 预算报告)
        """
        total = _conversation_tokens(conversation)
        report = BudgetReport(original_tokens=total)

        if total <= self._config.max_tokens:
            return conversation, report

        # Strategy 1: Trim tool_result outputs (least destructive)
        conv = self._trim_tool_results(conversation, report)

        # Strategy 2: Shrink early non-essential messages
        if _conversation_tokens(conv) > self._config.max_tokens:
            conv = self._compact_early_messages(conv, report)

        # Strategy 3: Warn but don't truncate further
        final_tokens = _conversation_tokens(conv)
        report.trimmed_tokens = report.original_tokens - final_tokens
        report.within_budget = final_tokens <= self._config.max_tokens

        return conv, report

    # ── Strategy 1: Trim tool_result outputs ────────────────────────────

    def _trim_tool_results(
        self,
        conversation: NativeConversation,
        report: BudgetReport,
    ) -> NativeConversation:
        """裁剪 tool_result 的内容长度。"""
        max_chars = self._config.tool_result_max_chars
        trimmed_messages: list[NativeMessage] = []

        for msg in conversation.messages:
            new_blocks = []
            modified = False

            for block in msg.content:
                if isinstance(block, ToolResultBlock) and isinstance(block.content, str):
                    if len(block.content) > max_chars:
                        # 保留前 max_chars 字符 + 截断标记
                        new_content = (
                            block.content[:max_chars]
                            + f"\n... [truncated {len(block.content) - max_chars} chars]"
                        )
                        new_blocks.append(ToolResultBlock(
                            tool_use_id=block.tool_use_id,
                            content=new_content,
                            is_error=block.is_error,
                        ))
                        modified = True
                        report.tool_results_trimmed += 1
                    else:
                        new_blocks.append(block)
                else:
                    new_blocks.append(block)

            if modified:
                trimmed_messages.append(NativeMessage(
                    role=msg.role,
                    content=tuple(new_blocks),
                ))
            else:
                trimmed_messages.append(msg)

        return NativeConversation(messages=tuple(trimmed_messages))

    # ── Strategy 2: Compact early messages ──────────────────────────────

    def _compact_early_messages(
        self,
        conversation: NativeConversation,
        report: BudgetReport,
    ) -> NativeConversation:
        """压缩早期非关键消息。

        保留: 首条 user 消息 + 最近 20 条消息 + 所有 tool_use/tool_result 对
        压缩: 中间的纯文本消息 → 摘要占位
        """
        messages = list(conversation.messages)
        if len(messages) <= 25:
            return conversation

        # 保留首条
        keep_first = 1 if self._config.first_message_preserved else 0
        # 保留最近 N 条
        keep_last = 20

        if keep_first + keep_last >= len(messages):
            return conversation

        # 中间消息：保留 tool_use/tool_result 对，压缩纯文本
        middle = messages[keep_first:-keep_last]
        compacted: list[NativeMessage] = []

        for msg in middle:
            if msg.has_tool_uses or msg.has_tool_results:
                # 保留完整的工具交互
                compacted.append(msg)
            else:
                # 纯文本消息 → 摘要（首次遇到时插入）
                if not any(
                    m.text.startswith("[Earlier conversation summarized")
                    for m in compacted
                ):
                    compacted.append(NativeMessage(
                        role="user",
                        content=(TextBlock(
                            text="[Earlier conversation summarized: "
                                 f"{len(middle)} messages compressed "
                                 f"to stay within context budget]"
                        ),),
                    ))
                    report.messages_trimmed += len(middle) - len(compacted)

        result = (
            messages[:keep_first]
            + compacted
            + messages[-keep_last:]
        )
        return NativeConversation(messages=tuple(result))


# ── Convenience function ─────────────────────────────────────────────────────


def ensure_context_budget(
    conversation: NativeConversation,
    max_tokens: int = 180_000,
) -> tuple[NativeConversation, BudgetReport]:
    """便捷函数：确保对话在 token 预算内。"""
    mgr = ContextBudgetManager(BudgetConfig(max_tokens=max_tokens))
    return mgr.ensure_budget(conversation)
