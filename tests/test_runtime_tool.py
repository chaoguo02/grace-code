from __future__ import annotations

import asyncio

from runtime import (
    PermissionDecision,
    StreamingToolExecutor,
    ToolCall,
    ToolRegistry,
    ToolResult,
    ToolUseContext,
    build_tool,
    execute_single_tool,
    execute_tool_calls,
    partition_tool_calls,
)
from runtime.tool_executor import Batch, execute_batch


async def _ok_call(input: dict, context: ToolUseContext) -> ToolResult[str]:
    return ToolResult(output=f"ok:{input.get('value', '')}:{context.session_id}")


def _tool(name: str, **kwargs):
    return build_tool(
        name=name,
        input_schema={"type": "object", "properties": {}},
        call_fn=kwargs.pop("call_fn", _ok_call),
        description_text=kwargs.pop("description_text", f"{name} description"),
        **kwargs,
    )


def test_build_tool_fail_closed_defaults():
    tool = _tool("default")

    assert tool.is_concurrency_safe({}) is False
    assert tool.is_read_only({}) is False
    assert tool.is_destructive({}) is False
    assert tool.is_enabled() is True
    assert tool.max_result_size_chars == 100_000


def test_build_tool_description_and_api_definition():
    async def scenario():
        tool = _tool("alpha", description_text="Alpha tool")

        assert await tool.description() == "Alpha tool"
        assert tool.to_api_definition() == {
            "name": "alpha",
            "description": "Alpha tool",
            "input_schema": {"type": "object", "properties": {}},
        }

    asyncio.run(scenario())


def test_registry_skips_disabled_and_sorts_api_definitions():
    registry = ToolRegistry()
    registry.register(_tool("zeta"))
    registry.register(_tool("disabled", is_enabled=lambda: False))
    registry.register(_tool("alpha"))

    assert registry.count == 2
    assert "disabled" not in registry
    assert [tool.name for tool in registry.list_tools()] == ["alpha", "zeta"]
    assert [definition["name"] for definition in registry.get_api_definitions()] == ["alpha", "zeta"]


def test_registry_duplicate_overwrites():
    registry = ToolRegistry()
    first = _tool("same", description_text="first")
    second = _tool("same", description_text="second")

    registry.register(first)
    registry.register(second)

    assert registry.count == 1
    assert registry.get("same") is second


def test_execute_single_tool_validation_failure_skips_call():
    async def scenario():
        called = False

        async def call_fn(input: dict, context: ToolUseContext) -> ToolResult[str]:
            nonlocal called
            called = True
            return ToolResult(output="should not run")

        async def validate(input: dict, context: ToolUseContext):
            return False, "bad input"

        tool = _tool("validate", call_fn=call_fn, validate_input_fn=validate)
        result = await execute_single_tool(tool, ToolCall("1", "validate", {}), ToolUseContext())

        assert result.error == "Input validation failed: bad input"
        assert called is False

    asyncio.run(scenario())


def test_execute_single_tool_permission_denied_skips_call():
    async def scenario():
        called = False

        async def call_fn(input: dict, context: ToolUseContext) -> ToolResult[str]:
            nonlocal called
            called = True
            return ToolResult(output="should not run")

        async def deny(input: dict, context: ToolUseContext):
            return PermissionDecision.DENY

        tool = _tool("deny", call_fn=call_fn, check_permissions_fn=deny)
        result = await execute_single_tool(tool, ToolCall("1", "deny", {}), ToolUseContext())

        assert result.error == "Permission denied"
        assert called is False

    asyncio.run(scenario())


def test_execute_single_tool_permission_ask_skips_call():
    async def scenario():
        called = False

        async def call_fn(input: dict, context: ToolUseContext) -> ToolResult[str]:
            nonlocal called
            called = True
            return ToolResult(output="should not run")

        async def ask(input: dict, context: ToolUseContext):
            return PermissionDecision.ASK

        tool = _tool("ask", call_fn=call_fn, check_permissions_fn=ask)
        result = await execute_single_tool(tool, ToolCall("1", "ask", {}), ToolUseContext())

        assert result.error == "Permission required but no interactive UI available"
        assert called is False

    asyncio.run(scenario())


def test_execute_single_tool_success():
    async def scenario():
        tool = _tool("ok")
        result = await execute_single_tool(
            tool,
            ToolCall("1", "ok", {"value": "x"}),
            ToolUseContext(session_id="s1"),
        )

        assert result.error is None
        assert result.result is not None
        assert result.result.output == "ok:x:s1"
        assert result.duration_ms >= 0

    asyncio.run(scenario())


def test_partition_tool_calls_groups_contiguous_safe_calls():
    registry = ToolRegistry()
    registry.register(_tool("read", is_concurrency_safe=lambda _: True))
    registry.register(_tool("bash", is_concurrency_safe=lambda _: False))
    calls = [
        ToolCall("1", "read", {}),
        ToolCall("2", "read", {}),
        ToolCall("3", "bash", {}),
        ToolCall("4", "read", {}),
        ToolCall("5", "missing", {}),
    ]

    batches = partition_tool_calls(calls, registry)

    assert [batch.is_concurrency_safe for batch in batches] == [True, False, True, False]
    assert [[call.id for call in batch.calls] for batch in batches] == [["1", "2"], ["3"], ["4"], ["5"]]


def test_execute_batch_handles_unknown_tool():
    async def scenario():
        registry = ToolRegistry()
        batch = Batch(is_concurrency_safe=False, calls=[ToolCall("1", "missing", {})])

        results = await execute_batch(batch, registry, ToolUseContext())

        assert len(results) == 1
        assert results[0].error == "Unknown tool: missing"

    asyncio.run(scenario())


def test_execute_tool_calls_runs_all_batches():
    async def scenario():
        registry = ToolRegistry()
        registry.register(_tool("read", is_concurrency_safe=lambda _: True))
        registry.register(_tool("bash", is_concurrency_safe=lambda _: False))

        results = await execute_tool_calls(
            [
                ToolCall("1", "read", {"value": "a"}),
                ToolCall("2", "read", {"value": "b"}),
                ToolCall("3", "bash", {"value": "c"}),
            ],
            registry,
            ToolUseContext(session_id="s"),
        )

        assert [result.call_id for result in results] == ["1", "2", "3"]
        assert [result.result.output for result in results if result.result] == ["ok:a:s", "ok:b:s", "ok:c:s"]

    asyncio.run(scenario())


def test_streaming_tool_executor_accumulates_and_clears_pending_calls():
    async def scenario():
        registry = ToolRegistry()
        registry.register(_tool("read", is_concurrency_safe=lambda _: True))
        executor = StreamingToolExecutor(registry, ToolUseContext(session_id="stream"))

        executor.add_tool(ToolCall("1", "read", {"value": "a"}))
        executor.add_tool(ToolCall("2", "read", {"value": "b"}))

        assert executor.has_pending is True
        results = await executor.execute_all()

        assert executor.has_pending is False
        assert [result.call_id for result in results] == ["1", "2"]
        assert [result.call_id for result in executor.all_results] == ["1", "2"]

    asyncio.run(scenario())
