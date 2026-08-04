"""P0: message_validator — Target 测试。"""

import pytest
from runtime_core.message_validator import validate_messages, ValidationResult
from runtime_core.native_message import (
    NativeConversation, NativeMessage, TextBlock, ToolUseBlock, ToolResultBlock,
)


class TestValidConversations:
    def test_simple_user_assistant(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("hi"),
            NativeMessage.assistant_text("hello"),
        ])
        result = validate_messages(conv)
        assert result.valid

    def test_tool_roundtrip(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("read a.py"),
            NativeMessage.assistant_with_tools(
                "", (ToolUseBlock(id="c1", name="Read", input={"path": "a.py"}),),
            ),
            NativeMessage.tool_result("c1", "content"),
            NativeMessage.assistant_text("done"),
        ])
        result = validate_messages(conv)
        assert result.valid


class TestSystemPosition:
    def test_system_must_be_first(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("hi"),
            NativeMessage.system("late system"),
        ])
        result = validate_messages(conv)
        assert not result.valid
        assert any("must be at the beginning" in e for e in result.errors)

    def test_system_with_tool_use_rejected(self):
        conv = NativeConversation.from_messages([
            NativeMessage.assistant_with_tools(
                "", (ToolUseBlock(id="c1", name="Read"),),
            ),
        ])
        # Put a system message with tool_use — system role can't have tool_use
        msg = NativeMessage(
            role="system",
            content=(ToolUseBlock(id="bad", name="Test"),),
        )
        conv2 = NativeConversation.from_messages([msg])
        result = validate_messages(conv2)
        assert not result.valid


class TestAlternatingRoles:
    def test_consecutive_assistant_rejected(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("hi"),
            NativeMessage.assistant_text("a1"),
            NativeMessage.assistant_text("a2"),  # consecutive!
        ])
        result = validate_messages(conv)
        assert not result.valid
        assert any("consecutive assistant" in e for e in result.errors)

    def test_tool_result_after_user_is_ok(self):
        """user → tool_result(user role) 是合法的（Anthropic 允许）。"""
        conv = NativeConversation.from_messages([
            NativeMessage.user("hi"),
            NativeMessage.tool_result("c1", "output"),
        ])
        result = validate_messages(conv)
        assert result.valid


class TestBlockValidation:
    def test_empty_content_rejected(self):
        conv = NativeConversation.from_messages([
            NativeMessage(role="user", content=()),
        ])
        result = validate_messages(conv)
        assert not result.valid
        assert any("must not be empty" in e for e in result.errors)

    def test_tool_result_empty_array_rejected(self):
        msg = NativeMessage(
            role="user",
            content=(ToolResultBlock(tool_use_id="c1", content=(), is_error=False),),
        )
        conv = NativeConversation.from_messages([msg])
        result = validate_messages(conv)
        assert not result.valid
        assert any("empty content array" in e for e in result.errors)

    def test_tool_use_in_user_role_rejected(self):
        msg = NativeMessage(
            role="user",
            content=(ToolUseBlock(id="c1", name="Read"),),
        )
        conv = NativeConversation.from_messages([msg])
        result = validate_messages(conv)
        assert not result.valid
        assert any("must be in assistant" in e for e in result.errors)

    def test_tool_use_empty_id_rejected(self):
        conv = NativeConversation.from_messages([
            NativeMessage.assistant_with_tools(
                "", (ToolUseBlock(id="", name="Read"),),
            ),
        ])
        result = validate_messages(conv)
        assert not result.valid
        assert any("empty id" in e for e in result.errors)

    def test_tool_use_empty_name_rejected(self):
        conv = NativeConversation.from_messages([
            NativeMessage.assistant_with_tools(
                "", (ToolUseBlock(id="c1", name=""),),
            ),
        ])
        result = validate_messages(conv)
        assert not result.valid
        assert any("empty name" in e for e in result.errors)


class TestToolPairingWarning:
    def test_unmatched_tool_use_warns(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("hi"),
            NativeMessage.assistant_with_tools(
                "", (ToolUseBlock(id="orphan_1", name="Read"),),
            ),
            # 没有 tool_result!
            NativeMessage.assistant_text("done"),
        ])
        result = validate_messages(conv)
        # 有警告但对话本身合法（可能还在进行中）
        assert any("Unmatched tool_use" in w for w in result.warnings)


class TestRaiseIfInvalid:
    def test_valid_does_not_raise(self):
        conv = NativeConversation.from_messages([
            NativeMessage.user("hi"),
            NativeMessage.assistant_text("hello"),
        ])
        result = validate_messages(conv)
        result.raise_if_invalid()  # 不抛异常

    def test_invalid_raises(self):
        conv = NativeConversation.from_messages([
            NativeMessage.assistant_text("a1"),
            NativeMessage.assistant_text("a2"),
        ])
        result = validate_messages(conv)
        with pytest.raises(ValueError, match="Message validation failed"):
            result.raise_if_invalid()


class TestEmptyConversation:
    def test_empty_conversation_rejected(self):
        conv = NativeConversation.empty()
        result = validate_messages(conv)
        assert not result.valid
