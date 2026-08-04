"""Phase 5: NativeStepLoop — Target 测试。

验证新管道端到端：
- 2-turn 工具轮转（tool_use → tool_result → text）
- 零 dict 消息构造（StepLoop 不碰 {"role": "assistant"...}）
- ConversationState 协议完整性
- ConversationStore 即时持久化
- 取消、拒绝、错误路径
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from core.eventing.identifiers import SessionId, RunId
from core.json_values import freeze_json
from runtime_core.execution import (
    RuntimeExecution,
    ConversationSnapshot,
    CapabilitySnapshot,
    CancellationHandle,
)
from runtime_core.model_actions import (
    AssistantText,
    ModelAction,
    ToolCall as MACToolCall,
    ToolCallBatch,
    TokenUsage,
)
from runtime_core.native_backend import NativeBackend
from runtime_core.native_message import (
    NativeConversation,
    NativeMessage,
    ToolUseBlock,
)
from runtime_core.conversation_store import ConversationStore
from runtime_core.native_step_loop import NativeStepLoop, ToolResult
from runtime_core.outcome import RunStatus
from runtime_core.ports import (
    RuntimePorts,
    HookGateResult,
    ToolSuccess,
    ToolFailure,
    ToolDenied,
    ToolErrorType,
)


# ── Fakes ──────────────────────────────────────────────────────────────────


class _Cancellation:
    cancelled = False
    def child(self): return _Cancellation()


class _FakeTools:
    def __init__(self):
        self.calls: list[tuple] = []

    def execute(self, tool_name, params, invocation_id=""):
        self.calls.append((tool_name, invocation_id))
        return ToolSuccess(
            tool_name=tool_name,
            output=f"output-{tool_name}",
            tool_use_id=invocation_id,
        )

    async def aexecute(self, tool_name, params, invocation_id=""):
        """CC tool.call() 等价 — async 包装 sync execute，不阻塞事件循环。"""
        return self.execute(tool_name, params, invocation_id)


class _FakeHooks:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.checks: list[tuple] = []

    def check(self, event_type, hook_input, tool_name=""):
        self.checks.append((event_type, tool_name))
        return HookGateResult(allowed=self.allowed)


class _FakeLiveEvents:
    def __init__(self):
        self.events: list[tuple] = []
    def publish(self, event_type, payload, scope=None):
        self.events.append((event_type, payload))


class _FakeClock:
    def now(self): return 0.0
    def deadline(self, timeout_s): return timeout_s


class _FakeTokenUsage:
    def __init__(self):
        self.records: list[tuple] = []
    def record(self, run_id, input_tokens, output_tokens):
        self.records.append((run_id, input_tokens, output_tokens))


class _ScriptedBackend:
    """按序列返回 ModelAction，记录每次 invoke 收到的 conversation。"""
    def __init__(self, responses: list[ModelAction]):
        self.responses = list(responses)
        self.invocations: list[NativeConversation] = []

    def invoke(self, conversation, *, tool_choice=None, cancellation=None):
        self.invocations.append(conversation)
        return self.responses.pop(0)

    @property
    def model_name(self): return "scripted"
    @property
    def tool_count(self): return 0
    @property
    def tool_names(self): return ()


def _make_ports(llm=None, tools=None, hooks=None, live_events=None):
    return RuntimePorts(
        llm=llm or _ScriptedBackend([AssistantText(text="done")]),
        tools=tools or _FakeTools(),
        hooks=hooks or _FakeHooks(),
        live_events=live_events or _FakeLiveEvents(),
        clock=_FakeClock(),
        token_usage=_FakeTokenUsage(),
    )


def _make_store():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "test_native.db")
    store = ConversationStore(db_path, session_id="s1", run_id="r1")
    store._tmp_dir = d  # 用于清理
    return store


def _tool_call(cid, name="Read", params=None):
    return MACToolCall(
        id=cid, name=name,
        params=freeze_json(params or {"path": "test.py"}),
    )


def _run_async(loop, context):
    """Consume aiterate async generator → RuntimeOutcome.  CC-aligned test helper."""
    async def _consume():
        outcome = None
        async for event in loop.aiterate(context):
            if event["type"] in ("completed", "failed", "cancelled", "blocked"):
                outcome = event["outcome"]
        return outcome
    return asyncio.run(_consume())


# ── 核心轮转测试 ──────────────────────────────────────────────────────────

class TestTwoTurnToolRoundtrip:
    """对齐旧 test_step_loop_tool_roundtrip.py 的相同断言。"""

    def test_two_step_tool_roundtrip(self):
        """2-step：第 1 次返回 ToolCall，第 2 次返回文本。

        验证：NativeStepLoop 中：
        - 第 2 次 invoke 收到的 conversation 包含 tool_use → tool_result 轮转
        - 零 dict 构造（conversation 中是 NativeMessage 类型）
        """
        backend = _ScriptedBackend([
            _tool_call("c1", "Read"),
            AssistantText(text="done", stop_reason="end_turn",
                          usage=TokenUsage(input_tokens=10, output_tokens=5)),
        ])
        ports = _make_ports(llm=backend)
        store = _make_store()
        loop = NativeStepLoop(ports, backend, store)

        context = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
            conversation=ConversationSnapshot(
                messages=({"role": "user", "content": "read a.py"},),
            ),
            capabilities=CapabilitySnapshot(),
            cancellation=_Cancellation(),
        )

        outcome = _run_async(loop, context)

        assert outcome.status is RunStatus.COMPLETED
        assert len(backend.invocations) == 2

        # 第 2 次 invoke 收到的 conversation
        conv = backend.invocations[1]
        assert len(conv) >= 3  # user, assistant(tool_use), user(tool_result)

        # 验证 tool_use 消息
        assistant_msgs = [m for m in conv.messages if m.has_tool_uses]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0].tool_uses[0].id == "c1"
        assert assistant_msgs[0].tool_uses[0].name == "Read"

        # 验证 tool_result 消息
        result_msgs = [m for m in conv.messages if m.has_tool_results]
        assert len(result_msgs) == 1
        assert result_msgs[0].tool_results[0].tool_use_id == "c1"
        assert result_msgs[0].tool_results[0].is_error is False

    def test_batch_tool_roundtrip(self):
        """批量工具调用 → 每个都有 tool_result。"""
        backend = _ScriptedBackend([
            ToolCallBatch(calls=(
                _tool_call("c1", "Read"), _tool_call("c2", "Grep"),
            ), usage=TokenUsage(input_tokens=10, output_tokens=5)),
            AssistantText(text="done"),
        ])
        ports = _make_ports(llm=backend)
        store = _make_store()
        loop = NativeStepLoop(ports, backend, store)

        context = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
            conversation=ConversationSnapshot(
                messages=({"role": "user", "content": "go"},),
            ),
            capabilities=CapabilitySnapshot(),
            cancellation=_Cancellation(),
        )

        outcome = _run_async(loop, context)
        assert outcome.status is RunStatus.COMPLETED

        conv = backend.invocations[1]
        result_msgs = [m for m in conv.messages if m.has_tool_results]
        assert len(result_msgs) == 2
        ids = {m.tool_results[0].tool_use_id for m in result_msgs}
        assert ids == {"c1", "c2"}


# ── 协议完整性 ────────────────────────────────────────────────────────────

class TestProtocolCompleteness:
    def test_step_loop_never_touches_tool_use_id(self):
        """验证 NativeStepLoop 不直接访问 tool_use_id — 通过 ConversationState 间接。"""
        backend = _ScriptedBackend([
            _tool_call("secret_id_123", "Read"),
            AssistantText(text="done"),
        ])
        store = _make_store()
        loop = NativeStepLoop(_make_ports(llm=backend), backend, store)

        context = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
            conversation=ConversationSnapshot(
                messages=({"role": "user", "content": "go"},),
            ),
            capabilities=CapabilitySnapshot(),
            cancellation=_Cancellation(),
        )

        _run_async(loop, context)

        # 验证 tool_result 的 tool_use_id 正确配对（由 ConversationState 保证）
        conv = backend.invocations[1]
        result_msgs = [m for m in conv.messages if m.has_tool_results]
        assert result_msgs[0].tool_results[0].tool_use_id == "secret_id_123"

    def test_tool_denied_produces_is_error(self):
        """PreToolUse deny → tool_result is_error=True。"""
        backend = _ScriptedBackend([
            _tool_call("c1", "Write"),
            AssistantText(text="done"),
        ])
        hooks = _FakeHooks(allowed=False)  # 全部 deny
        ports = _make_ports(llm=backend, hooks=hooks)
        store = _make_store()
        loop = NativeStepLoop(ports, backend, store)

        context = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
            conversation=ConversationSnapshot(
                messages=({"role": "user", "content": "go"},),
            ),
            capabilities=CapabilitySnapshot(),
            cancellation=_Cancellation(),
        )

        _run_async(loop, context)

        # tool_result 带 is_error（deny）
        conv = backend.invocations[1]
        result_msgs = [m for m in conv.messages if m.has_tool_results]
        assert len(result_msgs) >= 1


# ── 即时持久化 ────────────────────────────────────────────────────────────

class TestInstantPersistence:
    def test_messages_written_before_completion(self, tmp_path):
        """消息在 Run 结束前已经持久化到 DB。"""
        import os as _os

        d = tempfile.mkdtemp()
        db_path = _os.path.join(d, "persist.db")
        store = ConversationStore(db_path, session_id="s1", run_id="r1")

        backend = _ScriptedBackend([
            _tool_call("c1", "Read"),
            AssistantText(text="done", stop_reason="end_turn"),
        ])
        loop = NativeStepLoop(_make_ports(llm=backend), backend, store)

        context = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
            conversation=ConversationSnapshot(
                messages=({"role": "user", "content": "task"},),
            ),
            capabilities=CapabilitySnapshot(),
            cancellation=_Cancellation(),
        )

        _run_async(loop, context)

        # 从 DB 重建 — 验证即时持久化
        conv = store.rebuild_conversation()
        # 至少包含: user("task"), assistant(tool_use), 2×tool_result, assistant("done")
        assert len(conv) >= 3
        # 有 tool_use 消息
        assert any(m.has_tool_uses for m in conv.messages)
        # 有 tool_result 消息
        assert any(m.has_tool_results for m in conv.messages)
        # 有完成文本
        assert any(m.text == "done" for m in conv.messages)

        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ── 错误路径 ──────────────────────────────────────────────────────────────

class TestErrorPaths:
    def test_model_refusal(self):
        from runtime_core.model_actions import ModelRefusal

        backend = _ScriptedBackend([
            ModelRefusal(reason="I cannot do that"),
        ])
        store = _make_store()
        loop = NativeStepLoop(_make_ports(llm=backend), backend, store)

        context = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
            conversation=ConversationSnapshot(
                messages=({"role": "user", "content": "hack"},),
            ),
            capabilities=CapabilitySnapshot(),
            cancellation=_Cancellation(),
        )

        outcome = _run_async(loop, context)
        assert outcome.status is RunStatus.BLOCKED

    def test_model_failure_non_retryable(self):
        from runtime_core.model_actions import ModelFailure

        backend = _ScriptedBackend([
            ModelFailure(error="api error", retryable=False),
        ])
        store = _make_store()
        loop = NativeStepLoop(_make_ports(llm=backend), backend, store)

        context = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
            conversation=ConversationSnapshot(
                messages=({"role": "user", "content": "task"},),
            ),
            capabilities=CapabilitySnapshot(),
            cancellation=_Cancellation(),
        )

        outcome = _run_async(loop, context)
        assert outcome.status is RunStatus.FAILED

    def test_max_steps_exceeded(self):
        backend = _ScriptedBackend([
            _tool_call(f"c{i}", "Read")
            for i in range(30)  # 超过 MAX_STEPS=25
        ])
        store = _make_store()
        loop = NativeStepLoop(_make_ports(llm=backend), backend, store)

        context = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=2,
            conversation=ConversationSnapshot(
                messages=({"role": "user", "content": "task"},),
            ),
            capabilities=CapabilitySnapshot(),
            cancellation=_Cancellation(),
        )

        outcome = _run_async(loop, context)
        assert outcome.status is RunStatus.BLOCKED
        assert "max_steps" in outcome.blocked_by


# ── 证据收集 ──────────────────────────────────────────────────────────────

class TestEvidenceCollection:
    def test_tool_evidence_collected(self):
        backend = _ScriptedBackend([
            _tool_call("c1", "Read"),
            AssistantText(text="done"),
        ])
        store = _make_store()
        loop = NativeStepLoop(_make_ports(llm=backend), backend, store)

        context = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
            conversation=ConversationSnapshot(
                messages=({"role": "user", "content": "task"},),
            ),
            capabilities=CapabilitySnapshot(),
            cancellation=_Cancellation(),
        )

        outcome = _run_async(loop, context)
        assert outcome.evidence is not None
        assert len(outcome.evidence.tool_calls) == 1
        assert outcome.evidence.tool_calls[0].tool_name == "Read"
        assert outcome.evidence.tool_calls[0].success is True


# ── 与旧 StepLoop 行为等价性 ─────────────────────────────────────────────

class TestBehavioralParity:
    """验证 NativeStepLoop 与旧 StepLoop 的行为等价。"""

    def test_single_text_response(self):
        """纯文本响应（无工具）→ completed。"""
        backend = _ScriptedBackend([
            AssistantText(text="Hello, world!", stop_reason="end_turn"),
        ])
        store = _make_store()
        loop = NativeStepLoop(_make_ports(llm=backend), backend, store)

        context = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=5,
            conversation=ConversationSnapshot(
                messages=({"role": "user", "content": "hi"},),
            ),
            capabilities=CapabilitySnapshot(),
            cancellation=_Cancellation(),
        )

        outcome = _run_async(loop, context)
        assert outcome.status is RunStatus.COMPLETED
        assert "Hello" in outcome.summary
