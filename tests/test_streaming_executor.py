from __future__ import annotations

import asyncio

from runtime.streaming_executor import StreamingToolExecutor, ToolStatus
from runtime.tool import ToolCall, ToolExecutionResult, ToolResult


def test_aggressive_mode_returns_results_in_tool_call_order():
    async def scenario():
        executor = StreamingToolExecutor()
        completion_order: list[str] = []

        async def execute_fn(tool_call: ToolCall) -> ToolExecutionResult:
            if tool_call.id == "slow":
                await asyncio.sleep(0.02)
            completion_order.append(tool_call.id)
            return ToolExecutionResult(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                result=ToolResult(output=f"ok:{tool_call.id}"),
            )

        executor.add_tool(ToolCall("slow", "read", {}), is_concurrency_safe=True, execute_fn=execute_fn)
        executor.add_tool(ToolCall("fast", "read", {}), is_concurrency_safe=True, execute_fn=execute_fn)

        results = await executor.get_remaining_results()

        assert completion_order == ["fast", "slow"]
        assert [result.call_id for result in results] == ["slow", "fast"]
        assert all(tool.status == ToolStatus.COMPLETED for tool in executor.tools)

    asyncio.run(scenario())


def test_aggressive_mode_unsafe_tool_barriers_later_safe_tools():
    async def scenario():
        executor = StreamingToolExecutor()
        started: list[str] = []
        finished: list[str] = []

        async def execute_fn(tool_call: ToolCall) -> ToolExecutionResult:
            started.append(tool_call.id)
            await asyncio.sleep(0.01)
            finished.append(tool_call.id)
            return ToolExecutionResult(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                result=ToolResult(output=f"ok:{tool_call.id}"),
            )

        executor.add_tool(ToolCall("a", "read", {}), is_concurrency_safe=True, execute_fn=execute_fn)
        executor.add_tool(ToolCall("b", "read", {}), is_concurrency_safe=True, execute_fn=execute_fn)
        executor.add_tool(ToolCall("c", "bash", {}), is_concurrency_safe=False, execute_fn=execute_fn)
        executor.add_tool(ToolCall("d", "read", {}), is_concurrency_safe=True, execute_fn=execute_fn)

        results = await executor.get_remaining_results()

        assert [result.call_id for result in results] == ["a", "b", "c", "d"]
        assert started[:2] == ["a", "b"]
        assert started.index("c") > finished.index("a")
        assert started.index("c") > finished.index("b")
        assert started.index("d") > finished.index("c")

    asyncio.run(scenario())


def test_concurrency_safe_callable_is_fail_closed_on_exception():
    async def scenario():
        executor = StreamingToolExecutor()

        async def execute_fn(tool_call: ToolCall) -> ToolExecutionResult:
            return ToolExecutionResult(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                result=ToolResult(output="ok"),
            )

        def bad_check(_input: dict) -> bool:
            raise RuntimeError("intentional crash")

        executor.add_tool(
            ToolCall("x", "read", {}),
            is_concurrency_safe=bad_check,
            execute_fn=execute_fn,
        )
        results = await executor.get_remaining_results()

        assert executor.tools[0].is_concurrency_safe is False
        assert results[0].call_id == "x"
        assert results[0].error is None

    asyncio.run(scenario())


def test_concurrency_safe_callable_rejects_non_dict_input():
    async def scenario():
        executor = StreamingToolExecutor()

        async def execute_fn(tool_call: dict) -> ToolExecutionResult:
            return ToolExecutionResult(
                call_id=tool_call["id"],
                tool_name=tool_call["name"],
                result=ToolResult(output="ok"),
            )

        executor.add_tool(
            {"id": "x", "name": "read", "input": "not-a-dict"},
            is_concurrency_safe=lambda _input: True,
            execute_fn=execute_fn,
        )
        results = await executor.get_remaining_results()

        assert executor.tools[0].is_concurrency_safe is False
        assert results[0].call_id == "x"

    asyncio.run(scenario())


def test_context_modifiers_are_applied_in_tool_call_order():
    async def scenario():
        executor = StreamingToolExecutor()

        async def execute_fn(tool_call: ToolCall) -> dict:
            return {
                "call_id": tool_call.id,
                "tool_name": tool_call.name,
                "context_modifier": lambda ctx, value=tool_call.id: [*ctx, value],
            }

        executor.add_tool(ToolCall("first", "read", {}), is_concurrency_safe=True, execute_fn=execute_fn)
        executor.add_tool(ToolCall("second", "read", {}), is_concurrency_safe=True, execute_fn=execute_fn)
        await executor.get_remaining_results()

        assert executor.apply_context_modifiers([]) == ["first", "second"]

    asyncio.run(scenario())
