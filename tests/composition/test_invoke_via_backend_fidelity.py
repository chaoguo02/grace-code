"""Phase 1: _invoke_via_backend 保真 — Target 测试。

验证 _invoke_via_backend 不再扁平化：传给 backend.complete 的
LLMMessage 保留 tool_calls / tool_call_id，且 tools 透传。
"""

from __future__ import annotations

import pytest


class _RecordingBackend:
    """记录收到的 messages/tools，返回 finish。"""

    def __init__(self):
        self.received_messages = None
        self.received_tools = None

    def complete(self, messages, tools):
        self.received_messages = list(messages)
        self.received_tools = list(tools)
        from llm.base import LLMResponse
        from core.types import Action, ActionType
        return LLMResponse(
            action=Action(action_type=ActionType.FINISH, thought="done"),
            raw_content="done",
            input_tokens=1,
            output_tokens=1,
        )


def test_invoke_via_backend_preserves_tool_calls():
    from composition.runtime_composition import _invoke_via_backend

    backend = _RecordingBackend()
    _invoke_via_backend(
        backend,
        [
            {"role": "assistant", "content": "calling",
             "tool_calls": [{"id": "c1", "name": "Read", "params": {"path": "a.py"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ],
        tools=[{"name": "Read", "description": "d",
                "parameters": {"type": "object", "properties": {}}}],
    )

    # 第 0 条：assistant + tool_calls 保真
    m0 = backend.received_messages[0]
    assert m0.role == "assistant"
    assert m0.tool_calls[0].id == "c1"
    assert m0.tool_calls[0].params == {"path": "a.py"}
    # 第 1 条：role=tool + tool_call_id 保真
    m1 = backend.received_messages[1]
    assert m1.role == "tool"
    assert m1.tool_call_id == "c1"
    assert m1.content == "result"
    # tools 透传
    assert len(backend.received_tools) == 1
    assert backend.received_tools[0].name == "Read"


def test_invoke_via_backend_preserves_bare_tool_result():
    """裸 tool_result block → role=tool，不再被扁平化为 user 文本。"""
    from composition.runtime_composition import _invoke_via_backend

    backend = _RecordingBackend()
    _invoke_via_backend(
        backend,
        [{"type": "tool_result", "tool_use_id": "c9", "content": "out", "is_error": True}],
    )

    m0 = backend.received_messages[0]
    assert m0.role == "tool", f"裸 block 应映射为 role=tool，got role={m0.role}"
    assert m0.tool_call_id == "c9"
    assert m0.is_error is True


def test_invoke_via_backend_tools_none():
    from composition.runtime_composition import _invoke_via_backend

    backend = _RecordingBackend()
    _invoke_via_backend(backend, [{"role": "user", "content": "hi"}])
    assert backend.received_tools == []


def test_invoke_via_backend_fake_mode_unchanged():
    """backend=None → H1 fake 响应不变。"""
    from composition.runtime_composition import _invoke_via_backend
    from runtime_core.model_actions import AssistantText

    result = _invoke_via_backend(None, [{"role": "user", "content": "x"}])
    assert isinstance(result, AssistantText)
    assert result.text == "H1 fake response"
