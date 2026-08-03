"""Phase 4: Backend ContentBlock 保真 — Target 测试。

验证 _to_anthropic_messages 精确产出 CC 数组：
assistant(tool_calls) → text + tool_use blocks；role=tool → tool_result block
（含 is_error 透传）。OpenAI backend 配对由 _sanitize_tool_pairs 保护。
"""

from __future__ import annotations

import pytest

from core.types import ToolCall
from llm.anthropic_backend import _to_anthropic_messages
from llm.base import LLMMessage


def test_anthropic_assistant_tool_use_blocks():
    """assistant + tool_calls → [text, tool_use] content blocks。"""
    result = _to_anthropic_messages([LLMMessage(
        role="assistant", content="calling",
        tool_calls=[ToolCall(id="c1", name="Read", params={"path": "a.py"})],
    )])
    assert result[0]["role"] == "assistant"
    blocks = result[0]["content"]
    assert blocks[0] == {"type": "text", "text": "calling"}
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["id"] == "c1"
    assert blocks[1]["name"] == "Read"
    assert blocks[1]["input"] == {"path": "a.py"}


def test_anthropic_tool_result_with_is_error():
    """role=tool + is_error → tool_result block + is_error: true。"""
    result = _to_anthropic_messages([LLMMessage(
        role="tool", tool_call_id="c1", content="boom", is_error=True,
    )])
    assert result[0]["role"] == "user"
    block = result[0]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "c1"
    assert block["content"] == "boom"
    assert block["is_error"] is True


def test_anthropic_tool_result_no_is_error_when_false():
    """is_error=False → 不设置 is_error 字段。"""
    result = _to_anthropic_messages([LLMMessage(
        role="tool", tool_call_id="c2", content="ok",
    )])
    block = result[0]["content"][0]
    assert "is_error" not in block


def test_anthropic_assistant_content_list_split():
    """assistant content 为 ContentBlock list → 原样拆块 + tool_use。"""
    result = _to_anthropic_messages([LLMMessage(
        role="assistant",
        content=[{"type": "text", "text": "pre"}],
        tool_calls=[ToolCall(id="c3", name="Grep", params={"pattern": "x"})],
    )])
    blocks = result[0]["content"]
    assert blocks[0] == {"type": "text", "text": "pre"}
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["id"] == "c3"


def test_anthropic_plain_user_message():
    result = _to_anthropic_messages([LLMMessage(role="user", content="hi")])
    assert result[0] == {"role": "user", "content": "hi"}
