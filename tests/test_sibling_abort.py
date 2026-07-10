from __future__ import annotations

import asyncio

from runtime.sibling_abort import StreamingToolExecutor, ToolStatus
from runtime.tool import ToolCall, ToolExecutionResult, ToolResult


def test_sibling_abort_on_serial_error_cancels_remaining_sibling():
    async def scenario():
        executor = StreamingToolExecutor()
        executor.add_tool(ToolCall(id="t1", name="bash", input={"cmd": "fail"}), is_concurrency_safe=False)
        executor.add_tool(ToolCall(id="t2", name="bash", input={"cmd": "ok"}), is_concurrency_safe=False)

        async def execute_fn(tool_call: ToolCall) -> ToolExecutionResult:
            if tool_call.id == "t1":
                raise RuntimeError("command failed")
            return ToolExecutionResult(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                result=ToolResult(output="ok"),
            )

        results = await executor.execute_all(execute_fn)

        assert executor.has_errored is True
        assert executor.tools[0].status == ToolStatus.ERRORED
        assert executor.tools[1].status == ToolStatus.CANCELLED
        assert results[0].error == "Error: command failed"
        assert "[cancelled]" in (results[1].error or "")

    asyncio.run(scenario())


def test_concurrent_safe_tools_all_succeed():
    async def scenario():
        executor = StreamingToolExecutor()
        executor.add_tool(ToolCall(id="t1", name="read", input={"path": "a.py"}), is_concurrency_safe=True)
        executor.add_tool(ToolCall(id="t2", name="read", input={"path": "b.py"}), is_concurrency_safe=True)

        async def execute_fn(tool_call: ToolCall) -> ToolExecutionResult:
            return ToolExecutionResult(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                result=ToolResult(output=f"content of {tool_call.input['path']}"),
            )

        results = await executor.execute_all(execute_fn)

        assert executor.has_errored is False
        assert len(results) == 2
        assert all(tool.status == ToolStatus.COMPLETED for tool in executor.tools)
        assert [result.result.output for result in results if result.result] == ["content of a.py", "content of b.py"]

    asyncio.run(scenario())


def test_concurrent_error_cancels_slow_sibling():
    async def scenario():
        executor = StreamingToolExecutor()
        executor.add_tool(ToolCall(id="fast", name="read", input={}), is_concurrency_safe=True)
        executor.add_tool(ToolCall(id="slow", name="read", input={}), is_concurrency_safe=True)

        async def execute_fn(tool_call: ToolCall) -> ToolExecutionResult:
            if tool_call.id == "fast":
                raise RuntimeError("fast failed")
            await asyncio.sleep(1)
            return ToolExecutionResult(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                result=ToolResult(output="slow ok"),
            )

        results = await executor.execute_all(execute_fn)

        assert executor.has_errored is True
        assert executor.tools[0].status == ToolStatus.ERRORED
        assert executor.tools[1].status == ToolStatus.CANCELLED
        assert results[0].error == "Error: fast failed"
        assert "[cancelled]" in (results[1].error or "")

    asyncio.run(scenario())
