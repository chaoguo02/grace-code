"""Phase 2: StepLoop 工具轮转保真 — Before/Target 测试。

对齐 CC List[ContentBlock]：tool 执行后，下一轮 LLM 收到的对话必须包含
配对的 assistant(tool_calls) + role=tool(tool_call_id) 消息，tool_use_id
与 tool_call.id 一一对应，而非扁平化的纯文本。

Before（实现前）：本文件 FAIL —— 当前 step_loop 只回填裸 tool_result block
（无 role、无 assistant tool_use 回填）。
Target（实现后）：全部 PASS。
"""

from __future__ import annotations

import pytest

from core.eventing.identifiers import SessionId, RunId
from core.json_values import freeze_json
from runtime_core.execution import RuntimeExecution, ConversationSnapshot, CapabilitySnapshot
from runtime_core.model_actions import (
    AssistantText, ToolCall, ToolCallBatch,
)
from runtime_core.outcome import RunStatus
from runtime_core.ports import (
    RuntimePorts, ToolSuccess, HookGateResult,
)
from runtime_core.step_loop import StepLoop


class _Cancellation:
    cancelled = False
    def child(self): return _Cancellation()


class _SequencedLLM:
    """按序列返回响应，记录每次 invoke 收到的 messages。"""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.tools_seen: list = []
    def invoke(self, messages, tools=None, tool_choice=None):
        self.calls.append(messages)
        self.tools_seen.append(tools)
        return self.responses.pop(0)
    def stream(self, messages, tools=None, tool_choice=None):
        async def _s(): return self.responses[0]
        return _s()


class _FakeTools:
    def execute(self, tool_name, params, invocation_id=""):
        return ToolSuccess(tool_name=tool_name, output=f"out-{tool_name}", tool_use_id=invocation_id)


class _FakeHooks:
    def check(self, event_type, hook_input, tool_name=""):
        return HookGateResult(allowed=True)


class _FakeLiveEvents:
    def publish(self, event_type, payload, scope=None): pass


class _FakeClock:
    def now(self): return 0.0
    def deadline(self, timeout_s): return timeout_s


class _FakeTokenUsage:
    def record(self, run_id, input_tokens, output_tokens): pass


def _make_ports(llm):
    return RuntimePorts(
        llm=llm, tools=_FakeTools(), hooks=_FakeHooks(),
        live_events=_FakeLiveEvents(), clock=_FakeClock(),
        token_usage=_FakeTokenUsage(),
    )


def _tool_call(cid, name):
    return ToolCall(id=cid, name=name, params=freeze_json({"k": "v"}))


def test_two_step_tool_roundtrip_pairs_ids():
    """2-step：第 1 次返回 ToolCallBatch(c1,c2)，第 2 次返回文本。
    断言第 2 次 invoke 收到 assistant(tool_calls=[c1,c2]) + 2 条 role=tool。"""
    llm = _SequencedLLM([
        ToolCallBatch(calls=(_tool_call("c1", "Read"), _tool_call("c2", "Grep"))),
        AssistantText(text="done", stop_reason="end_turn"),
    ])
    loop = StepLoop(_make_ports(llm))
    context = RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
        conversation=ConversationSnapshot(messages=({"role": "user", "content": "hi"},)),
        capabilities=CapabilitySnapshot(),
        cancellation=_Cancellation(),
    )

    outcome = loop.execute(context)

    assert outcome.status is RunStatus.COMPLETED
    assert len(llm.calls) == 2, f"应发生 2 次 LLM 调用，got {len(llm.calls)}"

    # 第 2 次调用收到的消息
    msgs = llm.calls[1]
    # assistant + tool_calls 配对
    assistant = [m for m in msgs
                 if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant) == 1, (
        f"必须回填 assistant(tool_calls) 消息，got {msgs}"
    )
    assert [tc["id"] for tc in assistant[0]["tool_calls"]] == ["c1", "c2"], (
        f"tool_calls id 必须保真 c1/c2，got {assistant[0]['tool_calls']}"
    )
    # role=tool + tool_call_id 配对
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 2, (
        f"必须回填 2 条 role=tool 消息，got {msgs}"
    )
    assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}, (
        f"tool_call_id 必须与 tool_call.id 一一对应，got {tool_msgs}"
    )


def test_parallel_tool_use_id_matches_call_id():
    """并行 2-call 时 tool_use_id 与 tool_call.id 一一对应（对齐 T20）。"""
    llm = _SequencedLLM([
        ToolCallBatch(calls=(_tool_call("p1", "Read"), _tool_call("p2", "Write"))),
        AssistantText(text="done", stop_reason="end_turn"),
    ])
    loop = StepLoop(_make_ports(llm))
    context = RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
        conversation=ConversationSnapshot(messages=({"role": "user", "content": "go"},)),
        capabilities=CapabilitySnapshot(),
        cancellation=_Cancellation(),
    )

    outcome = loop.execute(context)
    assert outcome.status is RunStatus.COMPLETED
    msgs = llm.calls[1]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"p1", "p2"}


def test_tools_passed_to_llm():
    """StepLoop 传 tools 给 invoke（非空、含 schemas）。"""
    llm = _SequencedLLM([AssistantText(text="done")])
    loop = StepLoop(_make_ports(llm))
    context = RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
        conversation=ConversationSnapshot(messages=({"role": "user", "content": "go"},)),
        capabilities=CapabilitySnapshot(tool_schemas=(
            {"name": "Read", "description": "d", "parameters": {}},
        )),
        cancellation=_Cancellation(),
    )
    loop.execute(context)
    # 第 1 次 invoke 应收到 tools
    assert llm.tools_seen[0] is not None
    assert any(t.get("name") == "Read" for t in llm.tools_seen[0])
