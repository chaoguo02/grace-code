"""Phase 2: ConversationState — Target 测试。

验证:
- 协议完整性：tool_use → tool_result 自动配对
- StepLoop 视角：只传 outcome + tool_call，不碰 tool_use_id
- 错误兜底：未匹配 tool_use 自动生成 is_error 占位
- 恢复：从 NativeConversation 重建状态
- to_api_format 直接输出 Anthropic API 格式
"""

from __future__ import annotations

import pytest

from core.json_values import freeze_json
from runtime_core.model_actions import (
    AssistantText,
    ModelFailure,
    ModelRefusal,
    ModelStop,
    ToolCall,
    ToolCallBatch,
)
from runtime_core.native_message import (
    NativeConversation,
    NativeMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from runtime_core.ports import ToolDenied, ToolFailure, ToolSuccess, ToolErrorType
from runtime_core.conversation_state import ConversationState


# ── Helpers ────────────────────────────────────────────────────────────────

def _tool_call(call_id: str, name: str = "Read", params: dict | None = None) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        params=freeze_json(params or {"path": "test.py"}),
    )


def _success(call_id: str = "c1") -> ToolSuccess:
    return ToolSuccess(
        tool_name="Read",
        output="file content",
        tool_use_id=call_id,
    )


def _failure(call_id: str = "c1") -> ToolFailure:
    return ToolFailure(
        tool_name="Read",
        error="file not found",
        error_type=ToolErrorType.EXECUTION_ERROR,
    )


# ── 基础消息操作 ──────────────────────────────────────────────────────────

class TestBasicMessageOperations:
    def test_add_user_message(self):
        state = ConversationState()
        state.add_user_message("hello")
        assert len(state.messages) == 1
        assert state.messages[0].role == "user"
        assert state.messages[0].text == "hello"

    def test_add_system_message(self):
        state = ConversationState()
        state.add_system_message("sys prompt")
        assert state.messages[0].role == "system"

    def test_add_assistant_text(self):
        state = ConversationState()
        state.add_assistant_message(AssistantText(text="done", stop_reason="end_turn"))
        assert state.messages[0].role == "assistant"
        assert state.messages[0].text == "done"

    def test_add_model_stop(self):
        state = ConversationState()
        state.add_assistant_message(ModelStop(text="stopped", stop_reason="max_tokens"))
        assert state.messages[0].role == "assistant"
        assert "stopped" in state.messages[0].text

    def test_add_model_refusal(self):
        state = ConversationState()
        state.add_assistant_message(ModelRefusal(reason="content policy"))
        assert "content policy" in state.messages[0].text

    def test_add_model_failure_non_retryable(self):
        """非重试失败 → 追加错误消息。"""
        state = ConversationState()
        state.add_assistant_message(ModelFailure(error="api error", retryable=False))
        assert len(state.messages) == 1
        assert "api error" in state.messages[0].text

    def test_add_model_failure_retryable_skipped(self):
        """可重试失败 → 不追加消息（等待重试）。"""
        state = ConversationState()
        state.add_assistant_message(ModelFailure(error="timeout", retryable=True))
        assert len(state.messages) == 0


# ── tool_use → tool_result 配对 ───────────────────────────────────────────

class TestToolUseToolResultPairing:
    def test_single_tool_roundtrip(self):
        """单工具调用 → tool_result 自动配对。"""
        state = ConversationState()
        tc = _tool_call("call_123", "Read")

        # Step 1: 记录 assistant 的 tool_use
        state.add_assistant_message(tc)
        assert state.pending_tool_use_count == 1
        assert state.has_pending_tool_uses()

        # assistant 消息验证
        msg = state.messages[0]
        assert msg.role == "assistant"
        assert len(msg.tool_uses) == 1
        assert msg.tool_uses[0].id == "call_123"
        assert msg.tool_uses[0].name == "Read"

        # Step 2: StepLoop 报告工具结果 — 不知道 tool_use_id
        state.add_tool_result(_success("call_123"), tc)
        assert state.pending_tool_use_count == 0

        # tool_result 消息验证
        result_msg = state.messages[1]
        assert result_msg.role == "user"  # Anthropic: tool_result as user
        assert len(result_msg.tool_results) == 1
        assert result_msg.tool_results[0].tool_use_id == "call_123"
        assert result_msg.tool_results[0].is_error is False

    def test_batch_tool_roundtrip(self):
        """批量工具调用 → 多个 tool_result 一一匹配。"""
        state = ConversationState()
        tc1 = _tool_call("c1", "Read")
        tc2 = _tool_call("c2", "Write")
        batch = ToolCallBatch(calls=(tc1, tc2))

        state.add_assistant_message(batch)
        assert state.pending_tool_use_count == 2

        # assistant 消息包含两个 tool_use
        msg = state.messages[0]
        assert len(msg.tool_uses) == 2
        assert {tu.id for tu in msg.tool_uses} == {"c1", "c2"}

        # 逐一报告结果
        state.add_tool_result(_success("c1"), tc1)
        assert state.pending_tool_use_count == 1
        state.add_tool_result(_failure("c2"), tc2)
        assert state.pending_tool_use_count == 0

        # 验证三条消息：assistant + tool_result(c1) + tool_result(c2)
        assert len(state.messages) == 3
        assert state.messages[1].tool_results[0].tool_use_id == "c1"
        assert state.messages[1].tool_results[0].is_error is False
        assert state.messages[2].tool_results[0].tool_use_id == "c2"
        assert state.messages[2].tool_results[0].is_error is True

    def test_tool_use_id_is_opaque_to_caller(self):
        """StepLoop 视角：只传 outcome + tool_call，不知道 tool_use_id 格式。"""
        state = ConversationState()
        tc = _tool_call("tk_abc_123", "Bash")

        state.add_assistant_message(tc)
        # 调用者不需要知道 "tk_abc_123" 是什么
        state.add_tool_result(_success(), tc)

        # 协议自动保证配对
        assert state.pending_tool_use_count == 0
        assert state.messages[1].tool_results[0].tool_use_id == "tk_abc_123"


# ── 错误语义 ──────────────────────────────────────────────────────────────

class TestErrorSemantics:
    def test_failure_produces_is_error_true(self):
        state = ConversationState()
        tc = _tool_call("c1")
        state.add_assistant_message(tc)
        state.add_tool_result(_failure("c1"), tc)

        result_block = state.messages[1].tool_results[0]
        assert result_block.is_error is True
        assert "file not found" in str(result_block.content)

    def test_denied_produces_is_error_true(self):
        state = ConversationState()
        tc = _tool_call("c1")
        state.add_assistant_message(tc)
        state.add_tool_result(
            ToolDenied(tool_name="Write", reason="denied by rule"), tc
        )

        result_block = state.messages[1].tool_results[0]
        assert result_block.is_error is True
        assert "denied" in str(result_block.content).lower()

    def test_drain_pending_as_errors(self):
        """未匹配的 tool_use → drain 生成 is_error 占位。"""
        state = ConversationState()
        tc1 = _tool_call("orphan_1")
        tc2 = _tool_call("orphan_2")
        state.add_assistant_message(ToolCallBatch(calls=(tc1, tc2)))

        # 未报告 tool_result → drain
        assert state.pending_tool_use_count == 2
        drained = state.drain_pending_as_errors()

        assert len(drained) == 2
        assert state.pending_tool_use_count == 0
        # 验证占位内容
        assert all(b.is_error for m in drained for b in m.tool_results)
        assert all("interrupted" in str(b.content).lower()
                   for m in drained for b in m.tool_results)


# ── 导出 ──────────────────────────────────────────────────────────────────

class TestConversationExport:
    def test_to_conversation(self):
        state = ConversationState()
        state.add_user_message("hi")
        state.add_assistant_message(AssistantText(text="hello"))

        conv = state.to_conversation()
        assert isinstance(conv, NativeConversation)
        assert len(conv) == 2

    def test_to_api_format(self):
        """to_api_format 直接输出 Anthropic API 格式 — 绕过 LLMMessage。"""
        state = ConversationState()
        state.add_user_message("hi")
        state.add_assistant_message(AssistantText(text="hello"))

        api = state.to_api_format()
        assert len(api) == 2
        assert api[0] == {"role": "user", "content": "hi"}
        assert api[1] == {"role": "assistant", "content": "hello"}

    def test_to_api_format_with_tools(self):
        """带 tool_use 的对话输出包含 content block 数组。"""
        state = ConversationState()
        tc = _tool_call("c1", "Read")
        state.add_assistant_message(tc)
        state.add_tool_result(_success("c1"), tc)

        api = state.to_api_format()
        # assistant 消息含 content block 数组
        assert isinstance(api[0]["content"], list)
        # tool_result 消息
        assert api[1]["role"] == "user"


# ── 恢复 ──────────────────────────────────────────────────────────────────

class TestRecovery:
    def test_rebuild_from_conversation(self):
        """从 NativeConversation 重建，pending_tool_uses 索引恢复。"""
        state = ConversationState()
        tc = _tool_call("recover_1")
        state.add_assistant_message(tc)
        # 没有 tool_result → pending

        conv = state.to_conversation()
        rebuilt = ConversationState.rebuild_from(conv)

        assert rebuilt.pending_tool_use_count == 1
        assert rebuilt.has_pending_tool_uses()

    def test_rebuild_after_complete_roundtrip(self):
        """完整往返后重建 → pending 为零。"""
        state = ConversationState()
        tc = _tool_call("c1")
        state.add_assistant_message(tc)
        state.add_tool_result(_success("c1"), tc)

        conv = state.to_conversation()
        rebuilt = ConversationState.rebuild_from(conv)

        assert rebuilt.pending_tool_use_count == 0
        assert len(rebuilt.messages) == 2

    def test_rebuild_multi_turn(self):
        """多轮重建后消息顺序保持。"""
        state = ConversationState()
        state.add_user_message("task")
        state.add_assistant_message(AssistantText(text="thinking..."))

        tc = _tool_call("c1")
        state.add_assistant_message(tc)
        state.add_tool_result(_success("c1"), tc)

        state.add_assistant_message(AssistantText(text="done"))

        conv = state.to_conversation()
        rebuilt = ConversationState.rebuild_from(conv)

        assert len(rebuilt.messages) == 5
        assert rebuilt.messages[0].role == "user"
        assert rebuilt.messages[1].role == "assistant"
        assert rebuilt.messages[2].role == "assistant"  # tool_use
        assert rebuilt.messages[3].role == "user"       # tool_result
        assert rebuilt.messages[4].role == "assistant"  # done


# ── 集成：模拟 StepLoop 的使用模式 ───────────────────────────────────────

class TestStepLoopIntegration:
    def test_simulated_two_turn_tool_loop(self):
        """模拟 2-turn 工具循环：第 1 轮 tool_use，第 2 轮文本。"""
        state = ConversationState()
        state.add_user_message("read file a.py")

        # Turn 1: 模型返回 tool_use
        tc = _tool_call("call_1", "Read", {"path": "a.py"})
        state.add_assistant_message(tc)
        state.add_tool_result(
            ToolSuccess(tool_name="Read", output="content of a.py", tool_use_id="call_1"),
            tc,
        )

        # Turn 2: 模型返回文本
        state.add_assistant_message(AssistantText(text="file content is: hello world"))

        # 验证完整对话 — 可直接传给 Backend.invoke
        conv = state.to_conversation()
        assert len(conv) == 4  # user, assistant(tool_use), user(tool_result), assistant(text)

        # 验证消息类型
        assert conv.messages[0].role == "user"
        assert conv.messages[1].has_tool_uses
        assert conv.messages[2].has_tool_results
        assert conv.messages[3].text == "file content is: hello world"

    def test_model_refusal_ends_conversation(self):
        """模型拒绝 → 追加拒绝消息后对话结束。"""
        state = ConversationState()
        state.add_user_message("hack the planet")
        state.add_assistant_message(ModelRefusal(reason="I cannot assist with that"))

        conv = state.to_conversation()
        assert len(conv) == 2
        assert "cannot assist" in conv.messages[1].text.lower()
