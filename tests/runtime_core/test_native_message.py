"""Phase 1: NativeMessage 类型 — Target 测试。

验证:
- ContentBlock 类型不可变、字段正确
- NativeMessage 不接受 str content（始终是 tuple[ContentBlock, ...]）
- NativeConversation 不可变
- to_api_dict / from_api_dict 往返保真
- 零 str|list 联合类型（有 str content 的测试不应编译通过）
"""

from __future__ import annotations

import pytest

from runtime_core.native_message import (
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    NativeMessage,
    NativeConversation,
)


# ── ContentBlock 类型 ──────────────────────────────────────────────────────

class TestTextBlock:
    def test_construction(self):
        b = TextBlock(text="hello")
        assert b.type == "text"
        assert b.text == "hello"
        assert b.cache_control is None

    def test_immutable(self):
        b = TextBlock(text="hello")
        with pytest.raises(Exception):
            b.text = "world"  # type: ignore

    def test_to_api_dict(self):
        b = TextBlock(text="hello")
        assert b.to_api_dict() == {"type": "text", "text": "hello"}

    def test_to_api_dict_with_cache_control(self):
        b = TextBlock(text="ephemeral", cache_control={"type": "ephemeral"})
        assert b.to_api_dict() == {
            "type": "text", "text": "ephemeral",
            "cache_control": {"type": "ephemeral"},
        }

    def test_equality(self):
        a = TextBlock(text="x")
        b = TextBlock(text="x")
        c = TextBlock(text="y")
        assert a == b
        assert a != c


class TestToolUseBlock:
    def test_construction(self):
        b = ToolUseBlock(id="call_1", name="Read", input={"path": "a.py"})
        assert b.type == "tool_use"
        assert b.id == "call_1"
        assert b.name == "Read"
        assert b.input == {"path": "a.py"}

    def test_defaults(self):
        b = ToolUseBlock()
        assert b.id == ""
        assert b.name == ""
        assert b.input == {}

    def test_immutable(self):
        b = ToolUseBlock(id="c1", name="R", input={})
        with pytest.raises(Exception):
            b.id = "c2"  # type: ignore

    def test_to_api_dict(self):
        b = ToolUseBlock(id="call_1", name="Read", input={"path": "a.py"})
        assert b.to_api_dict() == {
            "type": "tool_use",
            "id": "call_1",
            "name": "Read",
            "input": {"path": "a.py"},
        }


class TestToolResultBlock:
    def test_success_factory(self):
        b = ToolResultBlock.from_success("call_1", "file content")
        assert b.type == "tool_result"
        assert b.tool_use_id == "call_1"
        assert b.content == "file content"
        assert b.is_error is False

    def test_error_factory(self):
        b = ToolResultBlock.from_error("call_1", "command not found")
        assert b.is_error is True
        assert b.content == "command not found"

    def test_denied_factory(self):
        b = ToolResultBlock.from_denied("call_1", "permission denied")
        assert b.is_error is True
        assert "denied" in str(b.content).lower()

    def test_to_api_dict_success(self):
        b = ToolResultBlock.from_success("call_1", "result")
        d = b.to_api_dict()
        assert d["type"] == "tool_result"
        assert d["tool_use_id"] == "call_1"
        assert d["content"] == "result"
        assert "is_error" not in d or d["is_error"] is False

    def test_to_api_dict_error(self):
        b = ToolResultBlock.from_error("call_1", "fail")
        d = b.to_api_dict()
        assert d["is_error"] is True

    def test_immutable(self):
        b = ToolResultBlock.from_success("c1", "ok")
        with pytest.raises(Exception):
            b.is_error = True  # type: ignore


# ── NativeMessage ──────────────────────────────────────────────────────────

class TestNativeMessage:
    def test_user_factory(self):
        msg = NativeMessage.user("hello")
        assert msg.role == "user"
        assert len(msg.content) == 1
        assert isinstance(msg.content[0], TextBlock)
        assert msg.content[0].text == "hello"

    def test_system_factory(self):
        msg = NativeMessage.system("you are helpful")
        assert msg.role == "system"
        assert msg.content[0].text == "you are helpful"

    def test_assistant_text_factory(self):
        msg = NativeMessage.assistant_text("done")
        assert msg.role == "assistant"
        assert len(msg.content) == 1
        assert msg.content[0].text == "done"

    def test_assistant_with_tools(self):
        tool_uses = (
            ToolUseBlock(id="c1", name="Read", input={"path": "a.py"}),
            ToolUseBlock(id="c2", name="Grep", input={"pattern": "x"}),
        )
        msg = NativeMessage.assistant_with_tools("calling tools", tool_uses)
        assert msg.role == "assistant"
        assert len(msg.content) == 3  # text + 2 tool_use
        assert msg.content[0].text == "calling tools"
        assert msg.content[1].id == "c1"
        assert msg.content[2].id == "c2"

    def test_assistant_with_tools_empty_text(self):
        tool_uses = (ToolUseBlock(id="c1", name="Read"),)
        msg = NativeMessage.assistant_with_tools("", tool_uses)
        assert len(msg.content) == 1  # 无 text，仅 tool_use
        assert msg.content[0].id == "c1"

    def test_tool_result_message(self):
        msg = NativeMessage.tool_result("call_1", "output", is_error=False)
        assert msg.role == "user"  # Anthropic API: tool_result as user
        assert len(msg.content) == 1
        assert isinstance(msg.content[0], ToolResultBlock)
        assert msg.content[0].tool_use_id == "call_1"

    def test_immutable(self):
        msg = NativeMessage.user("hello")
        with pytest.raises(Exception):
            msg.role = "system"  # type: ignore
        with pytest.raises(Exception):
            msg.content = ()  # type: ignore

    def test_text_property(self):
        msg = NativeMessage.assistant_with_tools(
            "thought", (ToolUseBlock(id="c1", name="R"),)
        )
        assert msg.text == "thought"

    def test_tool_uses_property(self):
        tu = ToolUseBlock(id="c1", name="Read")
        msg = NativeMessage.assistant_with_tools("", (tu,))
        assert msg.tool_uses == (tu,)

    def test_tool_uses_property_empty(self):
        msg = NativeMessage.user("hello")
        assert msg.tool_uses == ()

    def test_tool_results_property(self):
        msg = NativeMessage.tool_result("c1", "out")
        assert len(msg.tool_results) == 1
        assert msg.tool_results[0].tool_use_id == "c1"

    def test_has_tool_uses(self):
        msg = NativeMessage.assistant_with_tools("", (ToolUseBlock(id="c1", name="R"),))
        assert msg.has_tool_uses is True
        assert NativeMessage.user("hello").has_tool_uses is False

    def test_has_tool_results(self):
        assert NativeMessage.tool_result("c1").has_tool_results is True
        assert NativeMessage.user("hello").has_tool_results is False

    # ── to_api_dict / from_api_dict 往返 ──────────────────────────────

    def test_to_api_dict_single_text(self):
        msg = NativeMessage.user("hello")
        d = msg.to_api_dict()
        assert d == {"role": "user", "content": "hello"}

    def test_to_api_dict_multi_block(self):
        msg = NativeMessage.assistant_with_tools(
            "calling",
            (ToolUseBlock(id="c1", name="Read", input={"path": "a.py"}),),
        )
        d = msg.to_api_dict()
        assert d["role"] == "assistant"
        assert isinstance(d["content"], list)
        assert len(d["content"]) == 2

    def test_to_api_dict_empty(self):
        msg = NativeMessage(role="user", content=())
        d = msg.to_api_dict()
        assert d == {"role": "user", "content": ""}

    def test_roundtrip_user_text(self):
        original = NativeMessage.user("hello world")
        restored = NativeMessage.from_api_dict(original.to_api_dict())
        assert restored.role == original.role
        assert restored.text == "hello world"

    def test_roundtrip_assistant_with_tools(self):
        original = NativeMessage.assistant_with_tools(
            "Using tools",
            (
                ToolUseBlock(id="call_1", name="Read", input={"path": "f.py"}),
                ToolUseBlock(id="call_2", name="Write", input={"path": "f.py", "content": "x"}),
            ),
        )
        restored = NativeMessage.from_api_dict(original.to_api_dict())
        assert restored.role == "assistant"
        assert len(restored.tool_uses) == 2
        assert restored.tool_uses[0].id == "call_1"
        assert restored.tool_uses[1].name == "Write"

    def test_roundtrip_tool_result(self):
        original = NativeMessage.tool_result("call_1", "output text", is_error=False)
        restored = NativeMessage.from_api_dict(original.to_api_dict())
        assert restored.role == "user"
        assert len(restored.tool_results) == 1
        assert restored.tool_results[0].tool_use_id == "call_1"
        assert restored.tool_results[0].is_error is False

    def test_roundtrip_tool_result_error(self):
        original = NativeMessage.tool_result("call_x", "failed", is_error=True)
        restored = NativeMessage.from_api_dict(original.to_api_dict())
        assert restored.tool_results[0].is_error is True

    def test_from_api_dict_string_content(self):
        restored = NativeMessage.from_api_dict({"role": "user", "content": "hello"})
        assert restored.role == "user"
        assert restored.text == "hello"

    def test_from_api_dict_list_content(self):
        restored = NativeMessage.from_api_dict({
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "c1", "name": "Read", "input": {}},
            ],
        })
        assert restored.role == "assistant"
        assert restored.content[0].text == "ok"
        assert restored.content[1].id == "c1"

    def test_equality(self):
        a = NativeMessage.user("hello")
        b = NativeMessage.user("hello")
        c = NativeMessage.user("world")
        assert a == b
        assert a != c


# ── NativeConversation ─────────────────────────────────────────────────────

class TestNativeConversation:
    def test_empty(self):
        conv = NativeConversation.empty()
        assert conv.is_empty
        assert len(conv) == 0
        assert conv.messages == ()

    def test_from_messages(self):
        msgs = [NativeMessage.user("hi"), NativeMessage.assistant_text("hello")]
        conv = NativeConversation.from_messages(msgs)
        assert len(conv) == 2

    def test_with_message_immutable(self):
        conv = NativeConversation.empty()
        conv2 = conv.with_message(NativeMessage.user("hi"))
        assert conv.is_empty  # 原实例不变
        assert len(conv2) == 1
        assert conv2.messages[0].text == "hi"

    def test_with_messages(self):
        conv = NativeConversation.empty()
        conv2 = conv.with_messages((
            NativeMessage.user("a"),
            NativeMessage.assistant_text("b"),
        ))
        assert len(conv2) == 2

    def test_to_api_format_filters_system(self):
        conv = NativeConversation.from_messages([
            NativeMessage.system("sys prompt"),
            NativeMessage.user("hi"),
            NativeMessage.assistant_text("hello"),
        ])
        api = conv.to_api_format()
        # system 消息被排除（Anthropic API 单独处理）
        assert len(api) == 2
        assert api[0]["role"] == "user"
        assert api[1]["role"] == "assistant"

    def test_system_messages(self):
        conv = NativeConversation.from_messages([
            NativeMessage.system("s1"),
            NativeMessage.user("hi"),
            NativeMessage.system("s2"),
        ])
        systems = conv.system_messages
        assert len(systems) == 2
        assert all(m.role == "system" for m in systems)

    def test_non_system_messages(self):
        conv = NativeConversation.from_messages([
            NativeMessage.system("s"),
            NativeMessage.user("hi"),
        ])
        assert len(conv.non_system_messages) == 1
        assert conv.non_system_messages[0].role == "user"

    def test_last_message(self):
        conv = NativeConversation.empty()
        assert conv.last_message is None
        conv2 = conv.with_message(NativeMessage.user("hi"))
        assert conv2.last_message.role == "user"

    def test_immutable(self):
        conv = NativeConversation.from_messages([NativeMessage.user("hi")])
        with pytest.raises(Exception):
            conv.messages = ()  # type: ignore
