from __future__ import annotations

import asyncio

from runtime.query_loop import LoopExitReason, LoopTerminalEvent, ToolResultEvent, query_loop
from runtime.tool import ToolExecutionResult, ToolResult


def test_streaming_query_loop_executes_tool_use_and_continues():
    async def scenario():
        calls = 0

        async def call_model(messages):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield {"type": "text_delta", "text": "checking"}
                yield {"type": "tool_use", "id": "t1", "name": "read", "input": {}}
            else:
                assert any(
                    isinstance(message, dict)
                    and message.get("content", [{}])[0].get("type") == "tool_result"
                    for message in messages
                    if isinstance(message.get("content", None), list)
                )
                yield {"type": "text_delta", "text": "done"}

        async def execute_tool(tool_call):
            return ToolExecutionResult(
                call_id=tool_call["id"],
                tool_name=tool_call["name"],
                result=ToolResult(output="file content"),
            )

        events = []
        async for event in query_loop(
            [{"role": "user", "content": "read file"}],
            call_model=call_model,
            execute_tool=execute_tool,
            get_concurrency_safe=lambda _tool_call: True,
            max_turns=3,
        ):
            events.append(event)

        assert any(isinstance(event, ToolResultEvent) and event.output == "file content" for event in events)
        assert isinstance(events[-1], LoopTerminalEvent)
        assert events[-1].reason == LoopExitReason.COMPLETED
        assert calls == 2

    asyncio.run(scenario())


def test_streaming_query_loop_applies_tool_result_budget_before_next_turn():
    async def scenario():
        calls = 0

        async def call_model(messages):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield {"type": "tool_use", "id": "t1", "name": "shell", "input": {}}
            else:
                tool_results = [
                    block
                    for message in messages
                    if isinstance(message, dict) and isinstance(message.get("content"), list)
                    for block in message["content"]
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                ]
                assert tool_results
                assert "truncated" in tool_results[0]["content"]
                yield {"type": "text_delta", "text": "done"}

        async def execute_tool(tool_call):
            return ToolExecutionResult(
                call_id=tool_call["id"],
                tool_name=tool_call["name"],
                result=ToolResult(output="x" * 40_000),
            )

        events = []
        async for event in query_loop(
            [{"role": "user", "content": "run shell"}],
            call_model=call_model,
            execute_tool=execute_tool,
            get_concurrency_safe=lambda _tool_call: True,
            max_turns=3,
        ):
            events.append(event)

        assert isinstance(events[-1], LoopTerminalEvent)
        assert events[-1].reason == LoopExitReason.COMPLETED
        assert calls == 2

    asyncio.run(scenario())


def test_streaming_query_loop_returns_blocking_limit_when_compression_fails():
    async def scenario():
        async def call_model(messages):
            yield {"type": "text_delta", "text": "should not run"}

        async def execute_tool(_tool_call):
            raise AssertionError("tool should not run")

        events = []
        async for event in query_loop(
            [{"role": "user", "content": "x" * 100}],
            call_model=call_model,
            execute_tool=execute_tool,
            get_concurrency_safe=lambda _tool_call: True,
            context_window=10,
        ):
            events.append(event)

        assert events == [LoopTerminalEvent(reason=LoopExitReason.BLOCKING_LIMIT)]

    asyncio.run(scenario())


def test_streaming_query_loop_stop_hook_blocks_then_continues():
    async def scenario():
        calls = 0
        hook_calls = 0

        async def call_model(messages):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield {"type": "text_delta", "text": "done too early"}
            else:
                assert any(
                    isinstance(message, dict) and "Stop hook blocked" in str(message.get("content", ""))
                    for message in messages
                )
                yield {"type": "text_delta", "text": "fixed"}

        async def execute_tool(_tool_call):
            raise AssertionError("tool should not run")

        async def on_stop_hook(messages):
            nonlocal hook_calls
            hook_calls += 1
            if hook_calls == 1:
                return [{"role": "user", "content": "[Stop hook blocked completion]\ntests failed"}]
            return None

        events = []
        async for event in query_loop(
            [{"role": "user", "content": "finish only after checks pass"}],
            call_model=call_model,
            execute_tool=execute_tool,
            get_concurrency_safe=lambda _tool_call: True,
            on_stop_hook=on_stop_hook,
            max_turns=5,
        ):
            events.append(event)

        assert isinstance(events[-1], LoopTerminalEvent)
        assert events[-1].reason == LoopExitReason.COMPLETED
        assert calls == 2
        assert hook_calls == 2

    asyncio.run(scenario())


def test_streaming_query_loop_stop_hook_retry_limit_errors():
    async def scenario():
        calls = 0
        hook_calls = 0

        async def call_model(messages):
            nonlocal calls
            calls += 1
            yield {"type": "text_delta", "text": "done"}

        async def execute_tool(_tool_call):
            raise AssertionError("tool should not run")

        async def on_stop_hook(messages):
            nonlocal hook_calls
            hook_calls += 1
            return [{"role": "user", "content": "[Stop hook blocked completion]\nstill failing"}]

        events = []
        async for event in query_loop(
            [{"role": "user", "content": "finish"}],
            call_model=call_model,
            execute_tool=execute_tool,
            get_concurrency_safe=lambda _tool_call: True,
            on_stop_hook=on_stop_hook,
            max_turns=10,
        ):
            events.append(event)

        assert isinstance(events[-1], LoopTerminalEvent)
        assert events[-1].reason == LoopExitReason.ERROR
        assert hook_calls == 4

    asyncio.run(scenario())
