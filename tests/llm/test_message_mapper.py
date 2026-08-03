"""Phase 1: message_mapper 保真映射 — Target 测试。

对齐 CC List[ContentBlock]：tool_calls / tool_call_id / content blocks
从 dict 到 LLMMessage 全程保真，不再被扁平化为纯文本。
"""

from __future__ import annotations

import pytest

from llm.message_mapper import (
    messages_to_llm, tool_dicts_to_schemas,
)


def test_user_message_preserved():
    msgs = messages_to_llm([{"role": "user", "content": "hello"}])
    assert len(msgs) == 1
    assert msgs[0].role == "user"
    assert msgs[0].content == "hello"


def test_assistant_tool_calls_preserved():
    msgs = messages_to_llm([{
        "role": "assistant",
        "content": "calling tools",
        "tool_calls": [
            {"id": "c1", "name": "Read", "params": {"path": "a.py"}},
            {"id": "c2", "name": "Grep", "params": {"pattern": "x"}},
        ],
    }])
    assert len(msgs) == 1
    assert msgs[0].role == "assistant"
    assert len(msgs[0].tool_calls) == 2
    assert msgs[0].tool_calls[0].id == "c1"
    assert msgs[0].tool_calls[0].name == "Read"
    assert msgs[0].tool_calls[0].params == {"path": "a.py"}
    assert msgs[0].tool_calls[1].id == "c2"


def test_role_tool_tool_call_id_preserved():
    msgs = messages_to_llm([{
        "role": "tool", "tool_call_id": "c1",
        "content": "file content", "is_error": False,
    }])
    assert len(msgs) == 1
    assert msgs[0].role == "tool"
    assert msgs[0].tool_call_id == "c1"
    assert msgs[0].content == "file content"
    assert msgs[0].is_error is False


def test_bare_tool_result_block_to_role_tool():
    """裸 tool_result block（CC 格式，无 role 键）→ role=tool + tool_use_id。"""
    msgs = messages_to_llm([{
        "type": "tool_result", "tool_use_id": "c9",
        "content": "result", "is_error": True,
    }])
    assert len(msgs) == 1
    assert msgs[0].role == "tool"
    assert msgs[0].tool_call_id == "c9"
    assert msgs[0].is_error is True


def test_content_blocks_preserved():
    """content 为 list[dict]（ContentBlock）→ 原样直传。"""
    blocks = [{"type": "text", "text": "hi"}, {"type": "image", "source": {"type": "base64"}}]
    msgs = messages_to_llm([{"role": "user", "content": blocks}])
    assert msgs[0].content == blocks
    assert isinstance(msgs[0].content, list)


def test_frozen_json_wrapper_unpacked():
    """FrozenJsonObject wrapper {"messages": [...]} → 解包。"""
    from core.json_values import freeze_json
    wrapped = freeze_json({"messages": [{"role": "user", "content": "x"}]})
    msgs = messages_to_llm(wrapped)
    assert len(msgs) == 1
    assert msgs[0].content == "x"


def test_tool_dicts_to_schemas_preserves_parameters():
    schemas = tool_dicts_to_schemas([{
        "name": "Read",
        "description": "read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }])
    assert len(schemas) == 1
    assert schemas[0].name == "Read"
    assert schemas[0].parameters["properties"]["path"]["type"] == "string"


def test_tool_dicts_none_returns_empty():
    assert tool_dicts_to_schemas(None) == []
    assert tool_dicts_to_schemas([]) == []
