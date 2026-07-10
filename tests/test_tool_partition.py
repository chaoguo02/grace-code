from __future__ import annotations

from runtime.tool import ToolCall
from runtime.tool_partition import partition_tool_calls


def test_empty_tool_calls_returns_empty_batches():
    assert partition_tool_calls([], {"read"}) == []


def test_contiguous_same_safety_tools_are_grouped():
    calls = [
        ToolCall(id="1", name="read", input={"path": "a.ts"}),
        ToolCall(id="2", name="read", input={"path": "b.ts"}),
        ToolCall(id="3", name="bash", input={"cmd": "rm"}),
        ToolCall(id="4", name="read", input={"path": "c.ts"}),
    ]

    batches = partition_tool_calls(calls, {"read"})

    assert [batch.is_concurrency_safe for batch in batches] == [True, False, True]
    assert [[tool_call.id for tool_call in batch.tool_calls] for batch in batches] == [["1", "2"], ["3"], ["4"]]


def test_unknown_tools_are_unsafe():
    calls = [
        ToolCall(id="1", name="unknown", input={}),
        ToolCall(id="2", name="read", input={}),
    ]

    batches = partition_tool_calls(calls, {"read"})

    assert [batch.is_concurrency_safe for batch in batches] == [False, True]
    assert [[tool_call.id for tool_call in batch.tool_calls] for batch in batches] == [["1"], ["2"]]
