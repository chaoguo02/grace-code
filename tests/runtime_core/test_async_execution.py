"""Phase D: Async tool execution — _atool_calls/_atool_one (CC runTools).

AC:
- read-only + concurrency_safe tools run in parallel (TaskGroup)
- write/serial tools run sequentially
- _atool_one awaits tool.aexecute (async, no to_thread)
- hook deny blocks, permission gate works
"""

from __future__ import annotations

import asyncio

import pytest


def _make_loop(tools_port, scheduler=None):
    """Build NativeStepLoop with fake tools + hooks."""
    from runtime_core.native_step_loop import NativeStepLoop
    from runtime_core.ports import RuntimePorts, HookGateResult
    from runtime_core.tool_scheduler import ToolScheduler

    class _Hooks:
        def check(self, event_type, hook_input, tool_name=""):
            return HookGateResult(allowed=True)

    ports = RuntimePorts(
        llm=object(), tools=tools_port, hooks=_Hooks(),
        live_events=object(), clock=object(), token_usage=object(),
    )
    return NativeStepLoop(ports, scheduler=scheduler or ToolScheduler())


class _AsyncTools:
    """Tool port with async tools. Records execution order."""
    def __init__(self):
        self.executed = []

    async def aexecute(self, name, params, invocation_id=""):
        from runtime_core.ports import ToolSuccess
        self.executed.append(name)
        return ToolSuccess(tool_name=name, output=f"{name}-done")


async def test_atool_calls_parallel_read_tools():
    """read-only + concurrency_safe 工具并行执行。"""
    from runtime_core.model_actions import ToolCall
    from runtime_core.execution import RuntimeExecution, CancellationHandle
    from runtime_core.tool_scheduler import ToolScheduler, ToolMetadata
    from core.eventing.identifiers import SessionId, RunId

    tools = _AsyncTools()
    scheduler = ToolScheduler({
        "Read": ToolMetadata(name="Read", read_only=True, concurrency_safe=True),
        "Grep": ToolMetadata(name="Grep", read_only=True, concurrency_safe=True),
        "Glob": ToolMetadata(name="Glob", read_only=True, concurrency_safe=True),
    })
    loop = _make_loop(tools, scheduler)
    ctx = RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"),
        cancellation=CancellationHandle(), max_steps=5,
    )

    calls = (
        ToolCall(id="r1", name="Read", params={}),
        ToolCall(id="g1", name="Grep", params={}),
        ToolCall(id="b1", name="Glob", params={}),
    )
    results = await loop._atool_calls(calls, ctx)
    assert len(results) == 3
    assert set(tools.executed) == {"Read", "Grep", "Glob"}
    # 全部成功
    assert all(r.hook_allowed for r in results)


async def test_atool_calls_serial_write():
    """write 工具串行执行。"""
    from runtime_core.model_actions import ToolCall
    from runtime_core.execution import RuntimeExecution, CancellationHandle
    from runtime_core.tool_scheduler import ToolScheduler, ToolMetadata
    from core.eventing.identifiers import SessionId, RunId

    tools = _AsyncTools()
    scheduler = ToolScheduler({
        "Write": ToolMetadata(name="Write", read_only=False, concurrency_safe=False),
        "Edit": ToolMetadata(name="Edit", read_only=False, concurrency_safe=False),
    })
    loop = _make_loop(tools, scheduler)
    ctx = RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"),
        cancellation=CancellationHandle(), max_steps=5,
    )

    calls = (
        ToolCall(id="w1", name="Write", params={}),
        ToolCall(id="e1", name="Edit", params={}),
    )
    results = await loop._atool_calls(calls, ctx)
    assert len(results) == 2
    assert tools.executed == ["Write", "Edit"]  # 串行顺序


async def test_atool_one_hook_deny_blocks():
    """hook deny → 工具不执行。"""
    from runtime_core.model_actions import ToolCall
    from runtime_core.execution import RuntimeExecution, CancellationHandle
    from runtime_core.ports import RuntimePorts, HookGateResult
    from runtime_core.native_step_loop import NativeStepLoop
    from core.eventing.identifiers import SessionId, RunId

    class _DenyHooks:
        def check(self, event_type, hook_input, tool_name=""):
            if tool_name == "Danger":
                return HookGateResult(allowed=False, reason="denied by test hook")
            return HookGateResult(allowed=True)

    tools = _AsyncTools()
    ports = RuntimePorts(
        llm=object(), tools=tools, hooks=_DenyHooks(),
        live_events=object(), clock=object(), token_usage=object(),
    )
    loop = NativeStepLoop(ports)
    ctx = RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"),
        cancellation=CancellationHandle(), max_steps=5,
    )

    result = await loop._atool_one(ToolCall(id="d1", name="Danger", params={}), ctx)
    assert not result.hook_allowed
    assert "denied" in result.hook_deny_reason
    assert "Danger" not in tools.executed  # 工具没执行


async def test_atool_one_executes_async_tool():
    """_atool_one await 工具 aexecute (async, 不 to_thread)。"""
    from runtime_core.model_actions import ToolCall
    from runtime_core.execution import RuntimeExecution, CancellationHandle
    from core.eventing.identifiers import SessionId, RunId

    tools = _AsyncTools()
    loop = _make_loop(tools)
    ctx = RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"),
        cancellation=CancellationHandle(), max_steps=5,
    )

    result = await loop._atool_one(ToolCall(id="r1", name="Read", params={}), ctx)
    assert result.hook_allowed
    assert result.outcome is not None
    assert "Read" in tools.executed
