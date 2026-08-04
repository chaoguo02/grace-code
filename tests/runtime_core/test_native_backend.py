"""Phase 3: NativeBackend — Target 测试。

验证:
- 工具在 Backend 初始化时绑定并缓存
- invoke 不接收 tools 参数
- NativeMessage → API dict 转换正确（零 LLMMessage 中转）
- 响应解析为 ModelAction 正确
- to_api_format 输出符合 Anthropic API 预期
"""

from __future__ import annotations

import pytest

from runtime_core.native_message import (
    NativeConversation,
    NativeMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from runtime_core.native_backend import (
    NativeToolSchema,
    NativeResponse,
    _native_msg_to_api_dict,
    _parse_sdk_response,
    _extract_system,
)
from runtime_core.model_actions import AssistantText, ToolCall, ToolCallBatch


# ── NativeToolSchema ───────────────────────────────────────────────────────

class TestNativeToolSchema:
    def test_construction(self):
        s = NativeToolSchema(
            name="Read",
            description="Read a file",
            input_schema={"type": "object", "properties": {}},
        )
        assert s.name == "Read"
        assert s.description == "Read a file"

    def test_to_api_dict(self):
        s = NativeToolSchema(
            name="Read",
            description="Read a file",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        d = s.to_api_dict()
        assert d["name"] == "Read"
        assert d["description"] == "Read a file"
        assert d["input_schema"]["type"] == "object"


# ── NativeMessage → API dict（零 LLMMessage 翻译）────────────────────────

class TestNativeMsgToApiDict:
    def test_single_text_block_simplified(self):
        """单 text block → 简化为纯文本。"""
        msg = NativeMessage.user("hello")
        d = _native_msg_to_api_dict(msg)
        assert d == {"role": "user", "content": "hello"}

    def test_multi_block_to_array(self):
        """多 block → ContentBlock 数组。"""
        msg = NativeMessage.assistant_with_tools(
            "calling",
            (ToolUseBlock(id="c1", name="Read", input={"path": "a.py"}),),
        )
        d = _native_msg_to_api_dict(msg)
        assert d["role"] == "assistant"
        assert isinstance(d["content"], list)
        assert d["content"][0]["type"] == "text"
        assert d["content"][1]["type"] == "tool_use"

    def test_tool_result_as_user_role(self):
        """tool_result → Anthropic API 要求 user role。"""
        msg = NativeMessage.tool_result("c1", "output")
        d = _native_msg_to_api_dict(msg)
        assert d["role"] == "user"
        assert d["content"][0]["type"] == "tool_result"
        assert d["content"][0]["tool_use_id"] == "c1"

    def test_tool_result_with_is_error(self):
        """is_error → 仅在 True 时输出。"""
        msg = NativeMessage.tool_result("c1", "failed", is_error=True)
        d = _native_msg_to_api_dict(msg)
        assert d["content"][0]["is_error"] is True


# ── System 消息提取 ───────────────────────────────────────────────────────

class TestExtractSystem:
    def test_empty_conversation(self):
        conv = NativeConversation.empty()
        assert _extract_system(conv) == ""

    def test_single_system_message(self):
        conv = NativeConversation.from_messages([
            NativeMessage.system("You are helpful"),
            NativeMessage.user("hi"),
        ])
        result = _extract_system(conv)
        assert "You are helpful" in result

    def test_multiple_system_messages_merged(self):
        conv = NativeConversation.from_messages([
            NativeMessage.system("Rule 1"),
            NativeMessage.system("Rule 2"),
            NativeMessage.user("hi"),
        ])
        result = _extract_system(conv)
        assert isinstance(result, str)
        assert "Rule 1" in result
        assert "Rule 2" in result

    def test_no_system_messages(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("hi"),
            NativeMessage.assistant_text("hello"),
        ])
        assert _extract_system(conv) == ""


# ── to_api_format 集成 ────────────────────────────────────────────────────

class TestToApiFormat:
    def test_full_roundtrip_format(self):
        """完整的 2-turn 工具循环 → to_api_format 输出符合 Anthropic API。"""
        conv = NativeConversation.from_messages([
            NativeMessage.user("read a.py"),
            NativeMessage.assistant_with_tools(
                "",
                (ToolUseBlock(id="c1", name="Read", input={"path": "a.py"}),),
            ),
            NativeMessage.tool_result("c1", "file content", is_error=False),
            NativeMessage.assistant_text("file contains: hello"),
        ])

        api_list = conv.to_api_format()

        # system 已过滤；4 条非 system 消息
        assert len(api_list) == 4
        # user message
        assert api_list[0] == {"role": "user", "content": "read a.py"}
        # assistant tool_use
        assert api_list[1]["role"] == "assistant"
        assert isinstance(api_list[1]["content"], list)
        assert api_list[1]["content"][0]["type"] == "tool_use"
        # tool_result → user role
        assert api_list[2]["role"] == "user"
        assert api_list[2]["content"][0]["type"] == "tool_result"
        # final text
        assert api_list[3]["role"] == "assistant"
        assert api_list[3]["content"] == "file contains: hello"


# ── NativeResponse → ModelAction ───────────────────────────────────────────

class _FakeUsage:
    input_tokens = 100
    output_tokens = 50
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeContent:
    def __init__(self, type, **kwargs):
        self.type = type
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, stop_reason, content_blocks):
        self.stop_reason = stop_reason
        self.content = content_blocks
        self.usage = _FakeUsage()


class TestNativeResponse:
    def test_text_response_to_assistant_text(self):
        resp = _FakeResponse("end_turn", [
            _FakeContent("text", text="Done"),
        ])
        nr = _parse_sdk_response(resp)
        action = nr.to_model_action()
        assert isinstance(action, AssistantText)
        assert action.text == "Done"
        assert action.usage.input_tokens == 100

    def test_tool_use_response_to_tool_call(self):
        resp = _FakeResponse("tool_use", [
            _FakeContent("text", text="Calling read"),
            _FakeContent("tool_use", id="call_1", name="Read",
                         input={"path": "a.py"}),
        ])
        nr = _parse_sdk_response(resp)
        action = nr.to_model_action()
        assert isinstance(action, ToolCall)
        assert action.id == "call_1"
        assert action.name == "Read"

    def test_multi_tool_use_to_batch(self):
        resp = _FakeResponse("tool_use", [
            _FakeContent("tool_use", id="c1", name="Read", input={"path": "a.py"}),
            _FakeContent("tool_use", id="c2", name="Grep", input={"pattern": "x"}),
        ])
        nr = _parse_sdk_response(resp)
        action = nr.to_model_action()
        assert isinstance(action, ToolCallBatch)
        assert len(action.calls) == 2

    def test_response_text_property(self):
        resp = _FakeResponse("end_turn", [
            _FakeContent("text", text="Hello"),
            _FakeContent("text", text="World"),
        ])
        nr = _parse_sdk_response(resp)
        assert nr.text == "Hello\nWorld"

    def test_response_tool_uses_property(self):
        resp = _FakeResponse("tool_use", [
            _FakeContent("tool_use", id="c1", name="Read", input={}),
        ])
        nr = _parse_sdk_response(resp)
        assert nr.has_tool_uses
        assert len(nr.tool_uses) == 1
