"""G15: ModelAction + Ports — typed, no raw dict, non-Optional.

AC: ModelAction sum type covers all model outputs
AC: ToolCall.params is FrozenJsonObject (not dict)
AC: LLMPort.invoke returns ModelAction (not object)
AC: ToolPort.execute returns ToolOutcome (not object)
AC: RuntimePorts has all 7 ports non-Optional
AC: No web_mode or UI concerns in ports.py
AC: Fake ports must provide all 7 interfaces
"""

from __future__ import annotations

import ast
import os

import pytest

from core.json_values import freeze_json, FrozenJsonObject
from core.eventing.identifiers import RunId
from runtime_core.model_actions import (
    ModelAction,
    AssistantText,
    ToolCall,
    ToolCallBatch,
    ModelStop,
    ModelRefusal,
    ModelFailure,
)
from runtime_core.ports import (
    RuntimePorts,
    LLMPort,
    ToolPort,
    HookGatePort,
    LiveEventPort,
    ClockPort,
    TokenUsagePort,
    CancellationPort,
    ToolOutcome,
    ToolSuccess,
    ToolFailure,
    ToolDenied,
    HookGateResult,
)


# ═══════════════════════════════════════════════════════════════════════════════
# G15.1 — ModelAction types
# ═══════════════════════════════════════════════════════════════════════════════

class TestModelActions:
    """G15: ModelAction sum type covers all model outputs."""

    def test_assistant_text(self):
        a = AssistantText(text="hello", stop_reason="end_turn")
        assert isinstance(a, ModelAction)
        assert a.text == "hello"

    def test_tool_call_params_is_frozen(self):
        p = freeze_json({"file": "test.py"})
        tc = ToolCall(id="tc1", name="read", params=p)
        assert isinstance(tc.params, FrozenJsonObject)

    def test_tool_call_batch(self):
        p1 = freeze_json({"a": 1})
        p2 = freeze_json({"b": 2})
        tc1 = ToolCall(id="1", name="read", params=p1)
        tc2 = ToolCall(id="2", name="write", params=p2)
        batch = ToolCallBatch(calls=(tc1, tc2))
        assert len(batch.calls) == 2

    def test_model_stop(self):
        s = ModelStop(stop_reason="end_turn", text="done")
        assert s.stop_reason == "end_turn"

    def test_model_refusal(self):
        r = ModelRefusal(reason="content policy")
        assert isinstance(r, ModelAction)

    def test_model_failure(self):
        f = ModelFailure(error="rate limited", retryable=True)
        assert f.retryable is True

    def test_all_are_frozen(self):
        a = AssistantText(text="hi")
        with pytest.raises(Exception):
            a.text = "other"  # type: ignore


# ═══════════════════════════════════════════════════════════════════════════════
# G15.2 — ToolOutcome
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolOutcome:
    """G15: ToolOutcome sum type."""

    def test_tool_success(self):
        ts = ToolSuccess(tool_name="read", output="file content", duration_ms=12.5)
        assert ts.tool_name == "read"
        assert not isinstance(ts, ToolFailure)

    def test_tool_failure(self):
        tf = ToolFailure(tool_name="write", error="permission denied")
        assert isinstance(tf, ToolOutcome)

    def test_tool_denied(self):
        td = ToolDenied(tool_name="delete", reason="blocked by hook")
        assert td.reason == "blocked by hook"


# ═══════════════════════════════════════════════════════════════════════════════
# G15.3 — HookGate
# ═══════════════════════════════════════════════════════════════════════════════

class TestHookGate:
    """G15: HookGatePort + HookGateResult."""

    def test_hook_gate_result_allowed(self):
        r = HookGateResult(allowed=True)
        assert r.allowed

    def test_hook_gate_result_denied(self):
        r = HookGateResult(allowed=False, reason="blocked by policy")
        assert not r.allowed
        assert "blocked" in r.reason

    def test_hook_gate_result_with_updated_input(self):
        updated = freeze_json({"timeout": 60})
        r = HookGateResult(allowed=True, updated_input=updated)
        assert isinstance(r.updated_input, FrozenJsonObject)


# ═══════════════════════════════════════════════════════════════════════════════
# G15.4 — RuntimePorts: all seven non-Optional
# ═══════════════════════════════════════════════════════════════════════════════

class FakeLLM:
    def invoke(self, messages, tools=None) -> ModelAction:
        return AssistantText(text="fake")

    def stream(self, messages, tools=None):
        async def _s():
            return AssistantText(text="fake stream")
        return _s()


class FakeTools:
    def execute(self, tool_name, params, invocation_id="") -> ToolOutcome:
        return ToolSuccess(tool_name=tool_name)


class FakeHooks:
    def check(self, event_type, hook_input, tool_name="") -> HookGateResult:
        return HookGateResult(allowed=True)


class FakeLiveEvents:
    def publish(self, event_type, payload) -> None:
        pass


class FakeClock:
    def now(self) -> float:
        return 0.0

    def deadline(self, timeout_s: float) -> float:
        return timeout_s


class FakeTokenUsage:
    def record(self, run_id, input_tokens, output_tokens) -> None:
        pass


class FakeCancellation:
    @property
    def cancelled(self) -> bool:
        return False


class TestRuntimePorts:
    """G15: RuntimePorts requires all 7 ports; no Optional."""

    def test_all_ports_non_optional(self):
        ports = RuntimePorts(
            llm=FakeLLM(),
            tools=FakeTools(),
            hooks=FakeHooks(),
            live_events=FakeLiveEvents(),
            clock=FakeClock(),
            token_usage=FakeTokenUsage(),
            cancellation=FakeCancellation(),
        )
        assert ports.llm is not None
        assert ports.tools is not None
        assert ports.hooks is not None
        assert ports.live_events is not None
        assert ports.clock is not None
        assert ports.token_usage is not None
        assert ports.cancellation is not None

    def test_runtime_ports_is_frozen(self):
        ports = RuntimePorts(
            llm=FakeLLM(), tools=FakeTools(), hooks=FakeHooks(),
            live_events=FakeLiveEvents(), clock=FakeClock(),
            token_usage=FakeTokenUsage(), cancellation=FakeCancellation(),
        )
        with pytest.raises(Exception):
            ports.llm = FakeLLM()  # type: ignore

    def test_no_web_mode_field(self):
        """G15: RuntimePorts must not have web_mode or other UI concerns."""
        fields = {f.name for f in RuntimePorts.__dataclass_fields__.values()}
        assert "web_mode" not in fields, "G15: web_mode must be removed"
        expected = {"llm", "tools", "hooks", "live_events", "clock",
                     "token_usage", "cancellation"}
        assert fields == expected, f"Unexpected fields: {fields - expected}"


# ═══════════════════════════════════════════════════════════════════════════════
# G15.5 — Static gate: no object/raw dict in ports.py
# ═══════════════════════════════════════════════════════════════════════════════

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "runtime_core")


class TestStaticGate:
    """G15: ports.py uses typed interfaces, not object/dict."""

    def test_ports_no_raw_object_return(self):
        path = os.path.join(RUNTIME_DIR, "ports.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.returns:
                    ret = ast.unparse(node.returns)
                    # Protocol methods should return typed values, not bare 'object'
                    if ret.strip() == "object":
                        pytest.fail(
                            f"ports.py:{node.lineno}: {node.name} returns bare 'object'"
                        )

    def test_ports_no_raw_dict_param(self):
        path = os.path.join(RUNTIME_DIR, "ports.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for arg in node.args.args + node.args.posonlyargs:
                    if arg.annotation:
                        ann = ast.unparse(arg.annotation)
                        if ann.strip() == "dict" or ann.strip().startswith("dict["):
                            pytest.fail(
                                f"ports.py:{node.lineno}: {node.name} uses {ann}"
                            )
