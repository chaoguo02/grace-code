"""
tests/test_native_tool_use.py

Native tool_use architecture 验证：
- LLMMessage 的 tool_calls 字段序列化/反序列化
- Backend converters 正确生成 native 格式
- Agent core dual-path 行为验证
- Token budget 和 compaction 兼容两种格式
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.task import Action, ActionType, ToolCall, Observation, ObservationStatus
from context.history import ConversationHistory
from context.token_budget import TokenBudget, _estimate_msg_tokens, _get_content_str
from context.compaction import ConversationCompactor
from llm.base import LLMMessage, LLMResponse, MockBackend


# ---------------------------------------------------------------------------
# Phase 1: Data Model Tests
# ---------------------------------------------------------------------------


class TestToolCallId:
    """ToolCall.id 字段测试。"""

    def test_tool_call_with_id(self):
        tc = ToolCall(name="shell", params={"cmd": "ls"}, id="toolu_abc123")
        assert tc.id == "toolu_abc123"
        d = tc.to_dict()
        assert d == {"name": "shell", "params": {"cmd": "ls"}, "id": "toolu_abc123"}

    def test_tool_call_without_id(self):
        tc = ToolCall(name="shell", params={"cmd": "ls"})
        assert tc.id is None
        d = tc.to_dict()
        assert d == {"name": "shell", "params": {"cmd": "ls"}}
        assert "id" not in d

    def test_mock_backend_assigns_id(self):
        script = [
            Action(ActionType.TOOL_CALL, "thinking", [ToolCall("shell", {"cmd": "ls"})]),
        ]
        backend = MockBackend(script)
        result = backend.complete([], [])
        assert result.action.tool_calls[0].id is not None
        assert result.action.tool_calls[0].id.startswith("mock_")


class TestLLMMessageToolCalls:
    """LLMMessage.tool_calls 字段测试。"""

    def test_message_with_tool_calls(self):
        tc = ToolCall(name="shell", params={"cmd": "ls"}, id="toolu_abc")
        msg = LLMMessage(role="assistant", content="Let me check", tool_calls=[tc])
        assert msg.tool_calls == [tc]
        assert msg.tool_call_id is None

    def test_message_with_tool_call_id(self):
        msg = LLMMessage(role="tool", content="output here", tool_call_id="toolu_abc")
        assert msg.tool_call_id == "toolu_abc"
        assert msg.tool_calls is None

    def test_message_plain_text(self):
        msg = LLMMessage(role="user", content="hello")
        assert msg.tool_calls is None
        assert msg.tool_call_id is None


class TestHistorySerialization:
    """ConversationHistory 序列化新字段。"""

    def test_to_dicts_includes_tool_calls(self):
        h = ConversationHistory()
        tc = ToolCall(name="shell", params={"cmd": "ls"}, id="toolu_abc")
        h.add(LLMMessage(role="assistant", content="thinking", tool_calls=[tc]))
        h.add(LLMMessage(role="tool", content="file.py", tool_call_id="toolu_abc"))

        dicts = h.to_dicts()
        assert dicts[0]["tool_calls"] == [{"name": "shell", "params": {"cmd": "ls"}, "id": "toolu_abc"}]
        assert dicts[1]["tool_call_id"] == "toolu_abc"

    def test_to_dicts_no_extra_fields_for_plain(self):
        h = ConversationHistory()
        h.add(LLMMessage(role="user", content="hello"))
        dicts = h.to_dicts()
        assert "tool_calls" not in dicts[0]
        assert "tool_call_id" not in dicts[0]

    def test_from_dicts_roundtrip(self):
        original = [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "I'll check",
             "tool_calls": [{"name": "shell", "params": {"cmd": "ls"}, "id": "tc_1"}]},
            {"role": "tool", "content": "a.py\nb.py", "tool_call_id": "tc_1"},
            {"role": "assistant", "content": "Found it"},
        ]
        h = ConversationHistory.from_dicts(original)
        result = h.to_dicts()

        assert result[1]["tool_calls"] == [{"name": "shell", "params": {"cmd": "ls"}, "id": "tc_1"}]
        assert result[2]["tool_call_id"] == "tc_1"
        assert "tool_calls" not in result[0]
        assert "tool_calls" not in result[3]


# ---------------------------------------------------------------------------
# Phase 3: Backend Emit Tests
# ---------------------------------------------------------------------------


class TestAnthropicEmit:
    """_to_anthropic_messages 正确生成 native content blocks。"""

    def test_native_tool_call_message(self):
        from llm.anthropic_backend import _to_anthropic_messages

        tc = ToolCall(name="shell", params={"cmd": "ls"}, id="toolu_abc")
        msg = LLMMessage(role="assistant", content="Let me check", tool_calls=[tc])
        result = _to_anthropic_messages([msg])

        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        blocks = result[0]["content"]
        assert blocks[0] == {"type": "text", "text": "Let me check"}
        assert blocks[1] == {"type": "tool_use", "id": "toolu_abc", "name": "shell", "input": {"cmd": "ls"}}

    def test_native_tool_result_message(self):
        from llm.anthropic_backend import _to_anthropic_messages

        msg = LLMMessage(role="tool", content="file.py\ntest.py", tool_call_id="toolu_abc")
        result = _to_anthropic_messages([msg])

        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"] == [
            {"type": "tool_result", "tool_use_id": "toolu_abc", "content": "file.py\ntest.py"}
        ]

    def test_plain_text_message_unchanged(self):
        from llm.anthropic_backend import _to_anthropic_messages

        msg = LLMMessage(role="user", content="hello")
        result = _to_anthropic_messages([msg])
        assert result == [{"role": "user", "content": "hello"}]

    def test_empty_thought_omitted(self):
        from llm.anthropic_backend import _to_anthropic_messages

        tc = ToolCall(name="shell", params={"cmd": "ls"}, id="toolu_abc")
        msg = LLMMessage(role="assistant", content="", tool_calls=[tc])
        result = _to_anthropic_messages([msg])

        blocks = result[0]["content"]
        assert len(blocks) == 1
        assert blocks[0]["type"] == "tool_use"


class TestOpenAIEmit:
    """_to_openai_messages 正确生成 native tool_calls 格式。"""

    def test_native_tool_call_message(self):
        from llm.openai_backend import _to_openai_messages

        tc = ToolCall(name="shell", params={"cmd": "ls"}, id="call_abc")
        msg = LLMMessage(role="assistant", content="Let me check", tool_calls=[tc])
        result = _to_openai_messages([msg])

        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Let me check"
        assert result[0]["tool_calls"] == [{
            "id": "call_abc",
            "type": "function",
            "function": {"name": "shell", "arguments": '{"cmd": "ls"}'},
        }]

    def test_native_tool_result_message(self):
        from llm.openai_backend import _to_openai_messages

        msg = LLMMessage(role="tool", content="file.py\ntest.py", tool_call_id="call_abc")
        result = _to_openai_messages([msg])

        assert result == [{"role": "tool", "tool_call_id": "call_abc", "content": "file.py\ntest.py"}]

    def test_plain_text_message_unchanged(self):
        from llm.openai_backend import _to_openai_messages

        msg = LLMMessage(role="user", content="hello")
        result = _to_openai_messages([msg])
        assert result == [{"role": "user", "content": "hello"}]


# ---------------------------------------------------------------------------
# Phase 5: Token Budget Tests
# ---------------------------------------------------------------------------


class TestNativePairAtomicity:
    """Native tool_use/tool_result 配对原子裁剪测试。"""

    def test_build_native_pairs(self):
        messages = [
            {"role": "user", "content": "fix bug"},
            {"role": "assistant", "content": "thinking",
             "tool_calls": [{"name": "shell", "params": {"cmd": "ls"}, "id": "tc1"}]},
            {"role": "tool", "content": "a.py", "tool_call_id": "tc1"},
            {"role": "assistant", "content": "found it"},
        ]
        pairs = TokenBudget._build_native_pairs(messages)
        assert pairs[1] == 2
        assert pairs[2] == 1
        assert 0 not in pairs
        assert 3 not in pairs

    def test_trim_by_priority_drops_pair_together(self):
        """丢弃 tool result 时必须同时丢弃对应的 tool call。"""
        budget = TokenBudget(total=2000)
        messages = [
            {"role": "user", "content": "fix bug"},
            {"role": "assistant", "content": "a" * 200,
             "tool_calls": [{"name": "shell", "params": {"cmd": "ls"}, "id": "tc1"}]},
            {"role": "tool", "content": "b" * 200, "tool_call_id": "tc1"},
            {"role": "assistant", "content": "c" * 200,
             "tool_calls": [{"name": "shell", "params": {"cmd": "cat"}, "id": "tc2"}]},
            {"role": "tool", "content": "d" * 200, "tool_call_id": "tc2"},
            {"role": "user", "content": "thanks"},
        ]
        # 严格限制 token，迫使裁剪
        trimmed = budget.trim_history(messages, 300)

        # 验证：不应该出现孤立的 tool_call 没有配对 tool_result
        for i, msg in enumerate(trimmed):
            if msg.get("tool_calls"):
                tc_id = msg["tool_calls"][0].get("id")
                if tc_id:
                    # 必须能找到配对的 tool result
                    has_result = any(
                        m.get("tool_call_id") == tc_id for m in trimmed
                    )
                    assert has_result, (
                        f"tool_call id={tc_id} at index {i} has no paired tool_result"
                    )

    def test_trim_results_only_drops_pair(self):
        """_trim_results_only 丢弃 native tool_result 时同时丢弃 tool_call。"""
        messages = [
            {"role": "user", "content": "fix bug"},
            {"role": "assistant", "content": "x" * 100,
             "tool_calls": [{"name": "shell", "params": {"cmd": "ls"}, "id": "tc1"}]},
            {"role": "tool", "content": "y" * 500, "tool_call_id": "tc1"},
            {"role": "assistant", "content": "done"},
        ]
        token_counts = [_estimate_msg_tokens(m) for m in messages]
        # 设置 limit 使得整对放不下
        tight_limit = token_counts[0] + token_counts[3] + 50

        result = TokenBudget._trim_results_only(messages, token_counts, tight_limit)
        if result is not None:
            # 没有孤立的 tool_call
            for msg in result:
                if msg.get("tool_calls"):
                    tc_id = msg["tool_calls"][0].get("id")
                    if tc_id:
                        has_result = any(m.get("tool_call_id") == tc_id for m in result)
                        assert has_result

    def test_text_mode_unaffected(self):
        """Text 模式的消息不受配对逻辑影响。"""
        messages = [
            {"role": "user", "content": "fix bug"},
            {"role": "assistant", "content": "Thought: checking\nAction: shell\nParams: {}"},
            {"role": "user", "content": "[Tool: shell | SUCCESS]\n" + "x" * 500},
            {"role": "assistant", "content": "done"},
        ]
        pairs = TokenBudget._build_native_pairs(messages)
        assert len(pairs) == 0


class TestTokenBudgetNativeFormat:
    """TokenBudget 兼容 native tool_use 格式。"""

    def test_estimate_msg_tokens_native_tool_call(self):
        msg = {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [{"name": "shell", "params": {"cmd": "ls"}, "id": "tc1"}],
        }
        tokens = _estimate_msg_tokens(msg)
        assert tokens > 0

    def test_estimate_msg_tokens_plain_text(self):
        msg = {"role": "user", "content": "hello world"}
        tokens = _estimate_msg_tokens(msg)
        assert tokens > 0

    def test_get_content_str_none(self):
        msg = {"role": "assistant", "content": None}
        assert _get_content_str(msg) == ""

    def test_message_priority_native_tool_call(self):
        msg = {"role": "assistant", "content": "thinking", "tool_calls": [{"name": "shell"}]}
        priority = TokenBudget._message_priority(msg, 1, 10)
        assert priority == 2

    def test_message_priority_native_tool_result(self):
        msg = {"role": "tool", "content": "output", "tool_call_id": "tc1"}
        priority = TokenBudget._message_priority(msg, 1, 10)
        assert priority == 1

    def test_message_priority_text_tool_call(self):
        msg = {"role": "assistant", "content": "Thought: x\nAction: shell\nParams: {}"}
        priority = TokenBudget._message_priority(msg, 1, 10)
        assert priority == 2

    def test_message_priority_text_tool_result(self):
        msg = {"role": "user", "content": "[Tool: shell | SUCCESS]\noutput"}
        priority = TokenBudget._message_priority(msg, 1, 10)
        assert priority == 1

    def test_trim_history_with_native_messages(self):
        budget = TokenBudget(total=2000)
        messages = [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "thinking",
             "tool_calls": [{"name": "shell", "params": {"cmd": "ls"}, "id": "tc1"}]},
            {"role": "tool", "content": "a" * 5000, "tool_call_id": "tc1"},
            {"role": "assistant", "content": "found it"},
        ]
        trimmed = budget.trim_history(messages, 500)
        assert len(trimmed) <= len(messages)
        assert trimmed[0]["content"] == "fix the bug"

    def test_extract_thought_native(self):
        msg = {"role": "assistant", "content": "I need to check the file",
               "tool_calls": [{"name": "file_read"}]}
        thought = TokenBudget._extract_thought(msg)
        assert thought == "I need to check the file"

    def test_extract_thought_text_mode(self):
        msg = {"role": "assistant", "content": "Thought: checking\nAction: shell\nParams: {}"}
        thought = TokenBudget._extract_thought(msg)
        assert thought == "Thought: checking"


# ---------------------------------------------------------------------------
# Phase 5: Compaction Tests
# ---------------------------------------------------------------------------


class TestCompactionNativeFormat:
    """Compaction 兼容 native tool_use 格式。"""

    def test_extract_from_assistant_native(self):
        compactor = ConversationCompactor()
        result = compactor._extract_from_assistant(
            "I need to run tests",
            tool_calls=[{"name": "shell", "params": {"cmd": "pytest"}}],
        )
        assert "→ I need to run tests" in result
        assert "🛠 shell" in result
        assert "cmd=pytest" in result

    def test_extract_from_assistant_text_fallback(self):
        compactor = ConversationCompactor()
        result = compactor._extract_from_assistant(
            "Thought: checking\nAction: shell\nParams: {\"cmd\": \"ls\"}"
        )
        assert "→ checking" in result
        assert "🛠 shell" in result

    def test_extract_native_tool_result(self):
        compactor = ConversationCompactor()
        result = compactor._extract_native_tool_result("a.py\nb.py\nc.py")
        assert "✓" in result
        assert "a.py" in result

    def test_format_messages_for_llm_native(self):
        compactor = ConversationCompactor()
        messages = [
            {"role": "assistant", "content": "let me check",
             "tool_calls": [{"name": "shell", "params": {"cmd": "ls"}}]},
            {"role": "tool", "content": "file.py", "tool_call_id": "tc1"},
        ]
        result = compactor._format_messages_for_llm(messages)
        assert "Called: shell" in result
        assert "Tool result" in result

    def test_format_messages_for_llm_text(self):
        compactor = ConversationCompactor()
        messages = [
            {"role": "assistant", "content": "Thought: x\nAction: shell\nParams: {}"},
            {"role": "user", "content": "[Tool: shell | SUCCESS]\noutput"},
        ]
        result = compactor._format_messages_for_llm(messages)
        assert "Thought:" in result
        assert "[Tool:" in result

    def test_summarize_with_regex_mixed_formats(self):
        compactor = ConversationCompactor()
        messages = [
            # Native format
            {"role": "assistant", "content": "checking files",
             "tool_calls": [{"name": "shell", "params": {"cmd": "ls"}}]},
            {"role": "tool", "content": "a.py\nb.py", "tool_call_id": "tc1"},
            # Text format
            {"role": "assistant", "content": "Thought: now reading\nAction: file_read\nParams: {\"path\": \"a.py\"}"},
            {"role": "user", "content": "[Tool: file_read | SUCCESS]\ndef foo(): pass"},
        ]
        result = compactor._summarize_with_regex(messages, 2000)
        assert "shell" in result
        assert "file_read" in result


# ---------------------------------------------------------------------------
# Integration: Agent Core Dual-Path
# ---------------------------------------------------------------------------


class TestAgentCoreDualPath:
    """验证 agent core 根据 supports_function_calling 切换路径。"""

    def test_mock_backend_native_path(self, tmp_path):
        """MockBackend supports_function_calling=True → native messages in history."""
        from agent.core import ReActAgent, AgentConfig
        from agent.task import Task
        from agent.event_log import EventLog
        from tools.base import ToolRegistry, NoopTool

        registry = ToolRegistry()
        registry.register(NoopTool("echo", output="hi"))

        script = [
            Action(ActionType.TOOL_CALL, "let me echo", [ToolCall("echo", {"text": "hi"})]),
            Action(ActionType.FINISH, "done", message="All done"),
        ]
        backend = MockBackend(script)
        agent = ReActAgent(backend, registry, AgentConfig(max_steps=5))

        task = Task(description="test", repo_path=str(tmp_path), max_steps=5)
        with EventLog.create(task, log_dir=str(tmp_path)) as log:
            agent.run(task, log)

        # Verify: second complete() call should receive native-format messages
        assert backend.call_count == 2
        second_messages = backend.received_messages[1]

        # Find the assistant message with tool_calls
        tool_call_msgs = [m for m in second_messages if m.tool_calls]
        assert len(tool_call_msgs) == 1
        assert tool_call_msgs[0].tool_calls[0].name == "echo"
        assert tool_call_msgs[0].content == "let me echo"

        # Find the tool result message
        tool_result_msgs = [m for m in second_messages if m.tool_call_id]
        assert len(tool_result_msgs) == 1
        assert tool_result_msgs[0].role == "tool"
        assert "hi" in tool_result_msgs[0].content
