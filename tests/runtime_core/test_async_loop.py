"""Phase E: aiterate async generator (CC query).

AC:
- aiterate is an async generator yielding events
- text-only model → completed event
- tool-use model → tool_result events → next turn → completed
- streaming backend yields text_delta events
"""

from __future__ import annotations

import asyncio

import pytest


def _make_ports(llm, tools=None):
    """Build RuntimePorts with fake llm + tools + hooks."""
    from runtime_core.ports import RuntimePorts, HookGateResult

    class _Hooks:
        def check(self, event_type, hook_input, tool_name=""):
            return HookGateResult(allowed=True)

    class _Events:
        def publish(self, *a, **kw): pass
    class _Clock:
        import time as _t
        def now(self): return _t.monotonic()
        def deadline(self, s): return _t.monotonic() + s
    class _Token:
        def record(self, *a, **kw): pass

    return RuntimePorts(
        llm=llm, tools=tools or _AsyncTools(), hooks=_Hooks(),
        live_events=_Events(), clock=_Clock(), token_usage=_Token(),
    )


class _AsyncTools:
    """Tool port with async tools."""
    async def aexecute(self, name, params, invocation_id=""):
        from runtime_core.ports import ToolSuccess
        return ToolSuccess(tool_name=name, output=f"{name}-done")


async def test_aiterate_text_only_completes():
    """纯文本模型 → completed 事件。"""
    from runtime_core.native_step_loop import NativeStepLoop
    from runtime_core.model_actions import AssistantText
    from runtime_core.execution import RuntimeExecution, ConversationSnapshot, CancellationHandle
    from core.eventing.identifiers import SessionId, RunId

    class _TextLLM:
        async def ainvoke(self, conversation, *, tool_choice=None, cancellation=None):
            return AssistantText(text="all done", stop_reason="end_turn")
        # 无 astream_iter → aiterate 走 ainvoke 分支

    loop = NativeStepLoop(_make_ports(_TextLLM()))
    ctx = RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"),
        cancellation=CancellationHandle(), max_steps=5,
        conversation=ConversationSnapshot(messages=(
            {"role": "user", "content": "hi"},
        )),
    )
    events = [ev async for ev in loop.aiterate(ctx)]
    assert events[-1]["type"] == "completed"
    assert events[-1]["outcome"].summary == "all done"


async def test_aiterate_tool_loop():
    """工具模型 → tool_result 事件 → 下一轮文本 → completed。"""
    from runtime_core.native_step_loop import NativeStepLoop
    from runtime_core.model_actions import ToolCall, AssistantText
    from runtime_core.execution import RuntimeExecution, ConversationSnapshot, CancellationHandle
    from core.eventing.identifiers import SessionId, RunId

    class _ToolThenTextLLM:
        _called = False
        async def ainvoke(self, conversation, *, tool_choice=None, cancellation=None):
            if not self._called:
                self._called = True
                return ToolCall(id="t1", name="Read", params={"path": "a.py"})
            return AssistantText(text="read the file", stop_reason="end_turn")
        # 无 astream_iter → aiterate 走 ainvoke 分支

    loop = NativeStepLoop(_make_ports(_ToolThenTextLLM()))
    ctx = RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"),
        cancellation=CancellationHandle(), max_steps=5,
        conversation=ConversationSnapshot(messages=(
            {"role": "user", "content": "read"},
        )),
    )
    events = [ev async for ev in loop.aiterate(ctx)]
    types = [ev["type"] for ev in events]
    assert "tool_result" in types
    assert types[-1] == "completed"


async def test_aiterate_streaming_text_deltas():
    """流式后端 → text_delta 事件。"""
    from runtime_core.native_step_loop import NativeStepLoop
    from runtime_core.execution import RuntimeExecution, ConversationSnapshot, CancellationHandle
    from core.eventing.identifiers import SessionId, RunId
    from llm.base import StreamEvent, StreamEventKind

    class _StreamLLM:
        async def astream_iter(self, conversation, *, tool_choice=None, model=""):
            yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, text="hel")
            yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, text="lo")
            yield StreamEvent(kind=StreamEventKind.FINISH, text="hello")

    loop = NativeStepLoop(_make_ports(_StreamLLM()))
    ctx = RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"),
        cancellation=CancellationHandle(), max_steps=5,
        conversation=ConversationSnapshot(messages=(
            {"role": "user", "content": "hi"},
        )),
    )
    events = [ev async for ev in loop.aiterate(ctx)]
    deltas = [ev["text"] for ev in events if ev["type"] == "text_delta"]
    assert "".join(deltas) == "hello"
    assert events[-1]["type"] == "completed"
