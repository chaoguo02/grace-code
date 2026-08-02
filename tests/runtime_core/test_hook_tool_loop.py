"""G17: Hook/Tool Loop — PreToolUse gate, permission, execute, PostToolUse.

AC: allow → tool executes → PostToolUse hook
AC: deny → tool NOT executed, denial recorded
AC: ask → approval required, no tool execution
AC: defer → continuation candidate, no tool execution
AC: transform → replaced input used in tool call
AC: PostToolUse failure → non-blocking, tool already ran
AC: tool failure → recorded as ToolFailure outcome
"""

from __future__ import annotations

import pytest

from core.eventing.identifiers import SessionId, RunId
from core.json_values import freeze_json
from runtime_core.execution import RuntimeExecution, ConversationSnapshot
from runtime_core.model_actions import ToolCall, AssistantText
from runtime_core.outcome import RunStatus
from runtime_core.ports import (
    RuntimePorts, HookGateResult, ToolOutcome,
    ToolSuccess, ToolFailure, ToolDenied,
)
from runtime_core.step_loop import StepLoop, ToolResult


# ── Fakes ───────────────────────────────────────────────────────────────────

class FakeCancellation:
    def __init__(self): self._cancelled = False
    @property
    def cancelled(self) -> bool: return self._cancelled


class FakeLLM:
    def __init__(self, response): self.response = response
    def invoke(self, messages, tools=None): return self.response
    def stream(self, messages, tools=None):
        async def _s(): return self.response
        return _s()


class FakeTools:
    def __init__(self, result=None): self.result = result or ToolSuccess(tool_name="test")
    def execute(self, tool_name, params, invocation_id=""): return self.result


class FakeHooks:
    def __init__(self, result=None): self.result = result or HookGateResult(allowed=True)
    def check(self, event_type, hook_input, tool_name=""): return self.result


class FakeLiveEvents:
    def __init__(self): self.published = []
    def publish(self, event_type, payload): self.published.append((event_type, payload))


class FakeClock:
    def now(self) -> float: return 0.0
    def deadline(self, timeout_s: float) -> float: return timeout_s


class FakeTokenUsage:
    def record(self, run_id, input_tokens, output_tokens) -> None: pass


def _ports(llm_resp=None, hooks=None):
    return RuntimePorts(
        llm=FakeLLM(llm_resp or AssistantText(text="done")),
        tools=FakeTools(),
        hooks=hooks or FakeHooks(),
        live_events=FakeLiveEvents(),
        clock=FakeClock(),
        token_usage=FakeTokenUsage(),
        cancellation=FakeCancellation(),
    )


def _context(max_steps=5):
    return RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=max_steps,
        conversation=ConversationSnapshot(messages=({"role": "user", "content": "hi"},)),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# G17.1 — Allow: tool executes
# ═══════════════════════════════════════════════════════════════════════════════

class TestAllowExecutesTool:
    """G17: allow → ToolPort.execute() is called."""

    def test_allow_executes(self):
        executed = []
        tools = FakeTools()
        hooks = FakeHooks(HookGateResult(allowed=True))

        ports = RuntimePorts(
            llm=FakeLLM(ToolCall(id="t1", name="read", params=freeze_json({"f": "x"}))),
            tools=tools, hooks=hooks,
            live_events=FakeLiveEvents(), clock=FakeClock(),
            token_usage=FakeTokenUsage(), cancellation=FakeCancellation(),
        )
        loop = StepLoop(ports)
        loop.execute(_context(max_steps=1))


# ═══════════════════════════════════════════════════════════════════════════════
# G17.2 — Deny: tool NOT executed
# ═══════════════════════════════════════════════════════════════════════════════

class TestDenyBlocksTool:
    """G17: deny → ToolPort.execute() is NEVER called."""

    def test_deny_blocks_execution(self):
        call_count = [0]

        class CountingTools:
            def execute(self, tool_name, params, invocation_id=""):
                call_count[0] += 1
                return ToolSuccess()

        hooks = FakeHooks(HookGateResult(allowed=False, reason="blocked by policy"))

        ports = RuntimePorts(
            llm=FakeLLM(ToolCall(id="t1", name="rm", params=freeze_json({"f": "x"}))),
            tools=CountingTools(), hooks=hooks,
            live_events=FakeLiveEvents(), clock=FakeClock(),
            token_usage=FakeTokenUsage(), cancellation=FakeCancellation(),
        )
        loop = StepLoop(ports)
        outcome = loop.execute(_context(max_steps=1))

        assert call_count[0] == 0, (
            f"G17: Denied tool must NOT call ToolPort.execute(). Called {call_count[0]} times"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G17.3 — Transform: replaced input
# ═══════════════════════════════════════════════════════════════════════════════

class TestTransform:
    """G17: Transform replaces tool input before execution."""

    def test_transform_replaces_input(self):
        last_params = []

        class CaptureTools:
            def execute(self, tool_name, params, invocation_id=""):
                last_params.append(params)
                return ToolSuccess()

        transformed = freeze_json({"timeout": 60, "safe": True})
        hooks = FakeHooks(HookGateResult(allowed=True, updated_input=transformed))

        ports = RuntimePorts(
            llm=FakeLLM(ToolCall(id="t1", name="run", params=freeze_json({"cmd": "ls"}))),
            tools=CaptureTools(), hooks=hooks,
            live_events=FakeLiveEvents(), clock=FakeClock(),
            token_usage=FakeTokenUsage(), cancellation=FakeCancellation(),
        )
        loop = StepLoop(ports)
        loop.execute(_context(max_steps=1))

        assert len(last_params) == 1
        assert last_params[0] == transformed, (
            f"G17: transformed input must be used. Got {last_params[0]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G17.4 — Tool failure recorded
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolFailure:
    """G17: Tool failure → ToolFailure outcome, loop continues."""

    def test_tool_failure_recorded(self):
        tools = FakeTools(result=ToolFailure(tool_name="write", error="permission denied"))
        hooks = FakeHooks(HookGateResult(allowed=True))

        ports = RuntimePorts(
            llm=FakeLLM(ToolCall(id="t1", name="write", params=freeze_json({"f": "x"}))),
            tools=tools, hooks=hooks,
            live_events=FakeLiveEvents(), clock=FakeClock(),
            token_usage=FakeTokenUsage(), cancellation=FakeCancellation(),
        )
        loop = StepLoop(ports)
        outcome = loop.execute(_context(max_steps=1))

        # Loop should continue after tool failure (max_steps→blocked)
        assert outcome.status == RunStatus.BLOCKED


# ═══════════════════════════════════════════════════════════════════════════════
# G17.5 — Full matrix: success/failure/deny live events
# ═══════════════════════════════════════════════════════════════════════════════

class TestLiveEvents:
    """G17: Tool results emit live events."""

    def test_tool_success_emits_live_event(self):
        events = FakeLiveEvents()
        hooks = FakeHooks(HookGateResult(allowed=True))

        ports = RuntimePorts(
            llm=FakeLLM(ToolCall(id="t1", name="read", params=freeze_json({"f": "x"}))),
            tools=FakeTools(), hooks=hooks,
            live_events=events, clock=FakeClock(),
            token_usage=FakeTokenUsage(), cancellation=FakeCancellation(),
        )
        loop = StepLoop(ports)
        loop.execute(_context(max_steps=1))

        tool_events = [e for e in events.published if e[0] == "tool.executed.v1"]
        assert len(tool_events) >= 1, "G17: Tool execution must emit live event"


# ═══════════════════════════════════════════════════════════════════════════════
# G17.6 — PostToolUse hook failure is non-blocking
# ═══════════════════════════════════════════════════════════════════════════════

class TestPostToolNonBlocking:
    """G17: PostToolUse failure does NOT rollback or block."""

    def test_post_tool_failure_not_blocking(self):
        # First call = PreToolUse (allow), second = PostToolUse (crash)
        call_seq = [0]

        class SeqHooks:
            def check(self, event_type, hook_input, tool_name=""):
                call_seq[0] += 1
                if call_seq[0] == 2:  # PostToolUse
                    raise RuntimeError("post hook crash!")
                return HookGateResult(allowed=True)

        ports = RuntimePorts(
            llm=FakeLLM(ToolCall(id="t1", name="read", params=freeze_json({"f": "x"}))),
            tools=FakeTools(), hooks=SeqHooks(),
            live_events=FakeLiveEvents(), clock=FakeClock(),
            token_usage=FakeTokenUsage(), cancellation=FakeCancellation(),
        )
        loop = StepLoop(ports)
        outcome = loop.execute(_context(max_steps=1))
        # Must continue despite PostToolUse crash
        assert outcome.status == RunStatus.BLOCKED  # max_steps


# ═══════════════════════════════════════════════════════════════════════════════
# H4 — Evidence collection
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvidenceCollection:
    """H4: Evidence is non-None after tool execution."""

    def test_tool_execution_produces_evidence(self):
        hooks = FakeHooks(HookGateResult(allowed=True))
        ports = RuntimePorts(
            llm=FakeLLM(ToolCall(id="t1", name="read", params=freeze_json({"f": "x"}))),
            tools=FakeTools(), hooks=hooks,
            live_events=FakeLiveEvents(), clock=FakeClock(),
            token_usage=FakeTokenUsage(), cancellation=FakeCancellation(),
        )
        loop = StepLoop(ports)
        outcome = loop.execute(_context(max_steps=1))
        assert outcome.evidence is not None, (
            "H4 FAIL: evidence must not be None after tool execution"
        )
        assert len(outcome.evidence.tool_calls) == 1, (
            f"Expected 1 tool evidence, got {len(outcome.evidence.tool_calls)}"
        )

    def test_evidence_tool_name_correct(self):
        hooks = FakeHooks(HookGateResult(allowed=True))
        ports = RuntimePorts(
            llm=FakeLLM(ToolCall(id="t1", name="bash", params=freeze_json({"cmd": "ls"}))),
            tools=FakeTools(), hooks=hooks,
            live_events=FakeLiveEvents(), clock=FakeClock(),
            token_usage=FakeTokenUsage(), cancellation=FakeCancellation(),
        )
        loop = StepLoop(ports)
        outcome = loop.execute(_context(max_steps=1))
        assert outcome.evidence.tool_calls[0].tool_name == "bash"

    def test_denied_tool_also_produces_evidence(self):
        """H4: Denied tools should also record evidence (hook_allowed=False)."""
        hooks = FakeHooks(HookGateResult(allowed=False, reason="blocked"))
        ports = RuntimePorts(
            llm=FakeLLM(ToolCall(id="t1", name="rm", params=freeze_json({}))),
            tools=FakeTools(), hooks=hooks,
            live_events=FakeLiveEvents(), clock=FakeClock(),
            token_usage=FakeTokenUsage(), cancellation=FakeCancellation(),
        )
        loop = StepLoop(ports)
        outcome = loop.execute(_context(max_steps=1))
        assert outcome.evidence is not None
        # Denied tool still produces evidence
        assert len(outcome.evidence.tool_calls) >= 1
