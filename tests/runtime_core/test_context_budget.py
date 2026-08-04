"""P1: ContextBudgetManager — Target 测试。"""

import pytest
from runtime_core.context_budget import (
    ContextBudgetManager, BudgetConfig, ensure_context_budget,
    _estimate_tokens, _conversation_tokens,
)
from runtime_core.native_message import (
    NativeConversation, NativeMessage, TextBlock, ToolUseBlock, ToolResultBlock,
)


class TestTokenEstimation:
    def test_estimate_empty(self):
        assert _estimate_tokens("") == 0

    def test_estimate_short(self):
        assert _estimate_tokens("hello") > 0

    def test_conversation_tokens_nonzero(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("hello world"),
            NativeMessage.assistant_text("hi there"),
        ])
        tokens = _conversation_tokens(conv)
        assert tokens > 0


class TestWithinBudget:
    def test_small_conversation_passes(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("hi"),
            NativeMessage.assistant_text("hello"),
        ])
        mgr = ContextBudgetManager(BudgetConfig(max_tokens=100_000))
        pruned, report = mgr.ensure_budget(conv)
        assert report.within_budget
        assert report.messages_trimmed == 0

    def test_returns_same_conversation_when_within_budget(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("task"),
        ])
        mgr = ContextBudgetManager(BudgetConfig(max_tokens=100_000))
        pruned, _ = mgr.ensure_budget(conv)
        assert len(pruned) == len(conv)


class TestToolResultTrimming:
    def test_long_tool_result_trimmed(self):
        """长 tool_result 被裁剪到 max_chars（强制超预算触发 trim）。"""
        long_output = "x" * 5000
        conv = NativeConversation.from_messages([
            NativeMessage.user("read big file"),
            NativeMessage.assistant_with_tools(
                "", (ToolUseBlock(id="c1", name="Read"),),
            ),
            NativeMessage.tool_result("c1", long_output),
        ])
        # 设置极低预算强制触发 trim
        mgr = ContextBudgetManager(BudgetConfig(
            max_tokens=50, tool_result_max_chars=200,
        ))
        pruned, report = mgr.ensure_budget(conv)
        # tool_result 被裁剪
        result_block = pruned.messages[2].tool_results[0]
        content = result_block.content
        assert isinstance(content, str)
        assert len(content) < len(long_output)
        assert "truncated" in content.lower()
        assert report.tool_results_trimmed >= 1

    def test_short_tool_result_not_trimmed(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("read"),
            NativeMessage.tool_result("c1", "short"),
        ])
        mgr = ContextBudgetManager(BudgetConfig(
            max_tokens=100_000, tool_result_max_chars=2000,
        ))
        pruned, report = mgr.ensure_budget(conv)
        assert report.tool_results_trimmed == 0


class TestConvenience:
    def test_ensure_context_budget(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("hi"),
            NativeMessage.assistant_text("hello"),
        ])
        pruned, report = ensure_context_budget(conv, max_tokens=100_000)
        assert report.within_budget
