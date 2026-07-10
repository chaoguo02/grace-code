"""Tests for the minimal runtime query loop."""

from __future__ import annotations

import asyncio

import pytest

from runtime import (
    MaxTurnsExceededError,
    RuntimeMessage,
    RuntimeModelResponse,
    ToolCall,
    ToolRegistry,
    ToolResult,
    build_tool,
    query_loop,
)
from runtime.query_loop import LoopExitReason, LoopTerminalEvent, ToolResultEvent


def _make_registry() -> ToolRegistry:
    """Create a registry with add and multiply example tools."""

    async def add_fn(input: dict, _ctx) -> ToolResult[str]:
        return ToolResult(output=str(input["a"] + input["b"]))

    async def multiply_fn(input: dict, _ctx) -> ToolResult[str]:
        return ToolResult(output=str(input["a"] * input["b"]))

    add_tool = build_tool(
        name="add",
        description_text="Add two numbers",
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
        is_concurrency_safe=lambda _: True,
        call_fn=add_fn,
    )

    multiply_tool = build_tool(
        name="multiply",
        description_text="Multiply two numbers",
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
        is_concurrency_safe=lambda _: True,
        call_fn=multiply_fn,
    )

    registry = ToolRegistry()
    registry.register(add_tool)
    registry.register(multiply_tool)
    return registry


class TestQueryLoopNoToolCalls:
    def test_returns_text_immediately(self):
        messages = [RuntimeMessage.user("Hello")]
        call_count = 0

        async def fake_model(_msgs, _registry):
            nonlocal call_count
            call_count += 1
            return RuntimeModelResponse(text="Hi there!")

        result = asyncio.run(query_loop(messages, model_fn=fake_model, registry=ToolRegistry()))

        assert result == "Hi there!"
        assert call_count == 1


class TestQueryLoopWithToolCalls:
    def test_two_tools_then_final_text(self):
        registry = _make_registry()
        messages = [RuntimeMessage.user("What is 2+3 and 4*5?")]

        async def fake_model(msgs, _registry):
            if len(msgs) == 1:
                return RuntimeModelResponse(
                    text="",
                    tool_calls=(
                        ToolCall(id="call_1", name="add", input={"a": 2, "b": 3}),
                        ToolCall(id="call_2", name="multiply", input={"a": 4, "b": 5}),
                    ),
                )
            tool_results = [message for message in msgs if message.role == "tool_result"]
            assert len(tool_results) == 2
            return RuntimeModelResponse(text="2+3=5, 4*5=20")

        result = asyncio.run(query_loop(messages, model_fn=fake_model, registry=registry))

        assert result == "2+3=5, 4*5=20"

    def test_messages_accumulate_correctly(self):
        registry = _make_registry()
        messages = [RuntimeMessage.user("Compute 1+1")]

        async def fake_model(msgs, _registry):
            if len(msgs) == 1:
                return RuntimeModelResponse(
                    text="",
                    tool_calls=(ToolCall(id="call_add", name="add", input={"a": 1, "b": 1}),),
                )

            assert len(msgs) == 3
            assert msgs[0].role == "user"
            assert msgs[1].role == "assistant"
            assert msgs[1].tool_calls[0].name == "add"
            assert msgs[2].role == "tool_result"
            assert msgs[2].tool_call_id == "call_add"
            assert msgs[2].content == "2"
            return RuntimeModelResponse(text="1+1=2")

        result = asyncio.run(query_loop(messages, model_fn=fake_model, registry=registry))

        assert result == "1+1=2"


class TestQueryLoopMaxTurns:
    def test_raises_on_exceed(self):
        registry = _make_registry()
        messages = [RuntimeMessage.user("loop forever")]

        async def always_call_tool(_msgs, _registry):
            return RuntimeModelResponse(
                text="",
                tool_calls=(ToolCall(id="call_loop", name="add", input={"a": 1, "b": 1}),),
            )

        with pytest.raises(MaxTurnsExceededError) as exc_info:
            asyncio.run(query_loop(messages, model_fn=always_call_tool, registry=registry, max_turns=3))

        assert exc_info.value.max_turns == 3


class TestQueryLoopSiblingAbort:
    def test_tool_error_and_cancelled_result_are_returned_to_model(self):
        async def fail_fn(_input: dict, _ctx) -> ToolResult[str]:
            raise RuntimeError("boom")

        async def slow_fn(_input: dict, _ctx) -> ToolResult[str]:
            await asyncio.sleep(1)
            return ToolResult(output="slow ok")

        fail_tool = build_tool(
            name="fail",
            description_text="Failing tool",
            input_schema={"type": "object", "properties": {}},
            is_concurrency_safe=lambda _: True,
            call_fn=fail_fn,
        )
        slow_tool = build_tool(
            name="slow",
            description_text="Slow tool",
            input_schema={"type": "object", "properties": {}},
            is_concurrency_safe=lambda _: True,
            call_fn=slow_fn,
        )
        registry = ToolRegistry()
        registry.register(fail_tool)
        registry.register(slow_tool)
        messages = [RuntimeMessage.user("run both")]

        async def fake_model(msgs, _registry):
            if len(msgs) == 1:
                return RuntimeModelResponse(
                    text="",
                    tool_calls=(
                        ToolCall(id="t1", name="fail", input={}),
                        ToolCall(id="t2", name="slow", input={}),
                    ),
                )
            tool_results = [message.content for message in msgs if message.role == "tool_result"]
            assert any("boom" in result for result in tool_results)
            assert any("[cancelled]" in result for result in tool_results)
            return RuntimeModelResponse(text="saw failure")

        result = asyncio.run(query_loop(messages, model_fn=fake_model, registry=registry))

        assert result == "saw failure"


class TestQueryLoopMultiTurn:
    def test_chained_tool_calls(self):
        registry = _make_registry()
        messages = [RuntimeMessage.user("Add 2+3, then multiply result by 10")]
        turn = 0

        async def fake_model(msgs, _registry):
            nonlocal turn
            turn += 1

            if turn == 1:
                return RuntimeModelResponse(
                    text="",
                    tool_calls=(ToolCall(id="call_1", name="add", input={"a": 2, "b": 3}),),
                )
            if turn == 2:
                last_result = [message for message in msgs if message.role == "tool_result"][-1]
                assert last_result.content == "5"
                return RuntimeModelResponse(
                    text="",
                    tool_calls=(ToolCall(id="call_2", name="multiply", input={"a": 5, "b": 10}),),
                )

            last_result = [message for message in msgs if message.role == "tool_result"][-1]
            assert last_result.content == "50"
            return RuntimeModelResponse(text="Result is 50")

        result = asyncio.run(query_loop(messages, model_fn=fake_model, registry=registry))

        assert result == "Result is 50"
        assert turn == 3
