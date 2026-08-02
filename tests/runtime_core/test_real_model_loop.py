"""G16: Real Model Loop — no hardcoded response, typed actions, correct outcomes.

AC: No hardcoded {"role":"assistant","content":"ok"} in step loop
AC: AssistantText → completed outcome
AC: ModelStop → completed outcome
AC: ToolCall → tool batch stored, loop continues (G17 hook/tool)
AC: ModelRefusal → blocked outcome
AC: ModelFailure (non-retryable) → failed outcome
AC: ModelFailure (retryable) → continue loop
AC: max_steps → blocked outcome (NOT silently completed)
AC: deterministic: same input → same outcome (fake adapter)
"""

from __future__ import annotations

import ast
import os

import pytest

from core.eventing.identifiers import SessionId, RunId
from runtime_core.execution import RuntimeExecution, ConversationSnapshot
from runtime_core.model_actions import (
    AssistantText, ToolCall, ToolCallBatch,
    ModelStop, ModelRefusal, ModelFailure,
)
from runtime_core.outcome import RuntimeOutcome, RunStatus
from runtime_core.ports import (
    RuntimePorts, LLMPort, ToolPort, HookGatePort,
    LiveEventPort, ClockPort, TokenUsagePort, CancellationPort,
    HookGateResult, ToolSuccess,
)
from runtime_core.step_loop import StepLoop, StepResult


# ── Fake ports with controllable model response ────────────────────────────

class FakeCancellation:
    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


class FakeLLM:
    """LLM port that returns a pre-configured ModelAction."""
    def __init__(self, response: ModelAction | None = None) -> None:
        self.response = response or AssistantText(text="ok")
        self.call_count = 0

    def invoke(self, messages, tools=None) -> ModelAction:
        self.call_count += 1
        return self.response

    def stream(self, messages, tools=None):
        self.call_count += 1
        async def _s():
            return self.response
        return _s()


class FakeTools:
    def execute(self, tool_name, params, invocation_id="") -> ToolSuccess:
        return ToolSuccess(tool_name=tool_name)


class FakeHooks:
    def check(self, event_type, hook_input, tool_name="") -> HookGateResult:
        return HookGateResult(allowed=True)


class FakeLiveEvents:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event_type, payload) -> None:
        self.published.append((event_type, payload))


class FakeClock:
    def now(self) -> float:
        return 0.0

    def deadline(self, timeout_s: float) -> float:
        return timeout_s


class FakeTokenUsage:
    def __init__(self) -> None:
        self.records: list = []

    def record(self, run_id, input_tokens, output_tokens) -> None:
        self.records.append((run_id, input_tokens, output_tokens))


def _make_ports(llm_response: ModelAction | None = None):
    return RuntimePorts(
        llm=FakeLLM(llm_response),
        tools=FakeTools(),
        hooks=FakeHooks(),
        live_events=FakeLiveEvents(),
        clock=FakeClock(),
        token_usage=FakeTokenUsage(),
        cancellation=FakeCancellation(),
    )


def _make_context(max_steps: int = 10) -> RuntimeExecution:
    return RuntimeExecution(
        session_id=SessionId("s1"),
        run_id=RunId("r1"),
        max_steps=max_steps,
        conversation=ConversationSnapshot(messages=(
            {"role": "user", "content": "hello"},
        )),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# G16.1 — No hardcoded response
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoHardcodedResponse:
    """G16: step_loop.py has no hardcoded model response."""

    def test_no_hardcoded_ok_in_step_loop(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "runtime_core", "step_loop.py",
        )
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert '"ok"' not in source or 'content' not in source, (
            "G16: step_loop.py must not contain hardcoded model response"
        )
        assert '{"role": "assistant"' not in source, (
            "G16: step_loop.py must not hardcode assistant messages"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G16.2 — AssistantText → completed
# ═══════════════════════════════════════════════════════════════════════════════

class TestAssistantTextCompleted:
    """G16: AssistantText terminates the loop with completed outcome."""

    def test_assistant_text_completes(self):
        ports = _make_ports(AssistantText(text="done!", stop_reason="end_turn"))
        loop = StepLoop(ports)
        outcome = loop.execute(_make_context())

        assert outcome.status == RunStatus.COMPLETED, (
            f"Expected COMPLETED, got {outcome.status}"
        )
        assert outcome.summary == "done!"

    def test_model_stop_completes(self):
        ports = _make_ports(ModelStop(stop_reason="end_turn", text="all good"))
        loop = StepLoop(ports)
        outcome = loop.execute(_make_context())
        assert outcome.status == RunStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# G16.3 — ToolCall → deferred
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolCallDeferred:
    """G16: ToolCall is stored as a batch for G17, not executed."""

    def test_tool_call_stored_not_executed(self):
        from core.json_values import freeze_json
        tc = ToolCall(id="t1", name="read", params=freeze_json({"file": "x"}))
        ports = _make_ports(tc)
        loop = StepLoop(ports)
        outcome = loop.execute(_make_context(max_steps=1))

        # ToolCall should NOT complete — loop continues until max_steps
        assert outcome.status == RunStatus.BLOCKED, (
            f"G16: ToolCall should continue loop, got {outcome.status}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G16.4 — ModelRefusal → blocked
# ═══════════════════════════════════════════════════════════════════════════════

class TestModelRefusalBlocked:
    """G16: ModelRefusal produces blocked outcome."""

    def test_refusal_blocked(self):
        ports = _make_ports(ModelRefusal(reason="content policy"))
        loop = StepLoop(ports)
        outcome = loop.execute(_make_context())

        assert outcome.status == RunStatus.BLOCKED
        assert "content policy" in outcome.error


# ═══════════════════════════════════════════════════════════════════════════════
# G16.5 — ModelFailure handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestModelFailure:
    """G16: Non-retryable → failed; retryable → continue."""

    def test_non_retryable_failure(self):
        ports = _make_ports(ModelFailure(error="auth error", retryable=False))
        loop = StepLoop(ports)
        outcome = loop.execute(_make_context())
        assert outcome.status == RunStatus.FAILED

    def test_retryable_failure_continues(self):
        ports = _make_ports(ModelFailure(error="rate limited", retryable=True))
        loop = StepLoop(ports)
        outcome = loop.execute(_make_context(max_steps=1))
        # Should hit max_steps (blocked) — it retried but hit the limit
        assert outcome.status == RunStatus.BLOCKED


# ═══════════════════════════════════════════════════════════════════════════════
# G16.6 — Max steps → blocked
# ═══════════════════════════════════════════════════════════════════════════════

class TestMaxSteps:
    """G16: max_steps reached → blocked, not silently completed."""

    def test_max_steps_blocked(self):
        # Retryable failure that keeps looping until max_steps
        ports = _make_ports(ModelFailure(error="slow", retryable=True))
        loop = StepLoop(ports)
        outcome = loop.execute(_make_context(max_steps=3))

        assert outcome.status == RunStatus.BLOCKED, (
            f"G16: max_steps must BLOCK, not complete. Got {outcome.status}"
        )
        assert "max_steps" in outcome.summary


# ═══════════════════════════════════════════════════════════════════════════════
# G16.7 — Deterministic: same input → same outcome
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeterminism:
    """G16: Same context + same fake adapter → same outcome digest."""

    def test_same_input_same_outcome(self):
        ctx = _make_context()

        ports1 = _make_ports(AssistantText(text="hello"))
        ports2 = _make_ports(AssistantText(text="hello"))

        outcome1 = StepLoop(ports1).execute(ctx)
        outcome2 = StepLoop(ports2).execute(ctx)

        assert outcome1.status == outcome2.status
        assert outcome1.summary == outcome2.summary
        assert outcome1.steps_taken == outcome2.steps_taken
