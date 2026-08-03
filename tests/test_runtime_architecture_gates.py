"""G43: Final verification matrix — perf, fault, leak, import contracts.

AC: All new core packages are importable
AC: No forbidden imports across architecture boundaries
AC: Cancellation works under 500ms (fake adapters)
AC: Immutable values don't leak across snapshots
AC: No excessive object retention after GC
"""

import ast
import gc
import os
import time as _time

import pytest

PROJECT_ROOT = os.path.dirname(__file__)


class TestFinalGate:
    """G43: Comprehensive verification matrix."""

    def test_all_packages_importable(self):
        for pkg in ["application", "runtime_core", "hook_core",
                     "eventing", "listeners", "infrastructure",
                     "composition", "core"]:
            import importlib
            mod = importlib.import_module(pkg)
            assert mod is not None

    def test_runtime_no_server_imports(self):
        rt = os.path.join(PROJECT_ROOT, "..", "runtime_core")
        for fn in os.listdir(rt):
            if not fn.endswith(".py") or fn.startswith("__"):
                continue
            with open(os.path.join(rt, fn), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    m = getattr(node, 'module', '') or ''
                    assert not m.startswith("server"), f"runtime_core/{fn} imports {m}"

    def test_cancellation_latency_under_500ms(self):
        from runtime_core.execution import CancellationHandle, RuntimeExecution, ConversationSnapshot
        from runtime_core.model_actions import AssistantText
        from runtime_core.step_loop import StepLoop
        from runtime_core.ports import RuntimePorts, HookGateResult
        from core.eventing.identifiers import SessionId, RunId

        handle = CancellationHandle()
        ctx = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"),
            cancellation=handle, max_steps=10,
            conversation=ConversationSnapshot(messages=({"role": "user", "content": "hi"},)),
        )
        handle.cancel()

        class S:
            def invoke(self, m, t=None, tool_choice=None): return AssistantText(text="ok")
            def stream(self, m, t=None, tool_choice=None):
                async def _s(): return AssistantText(text="ok"); return _s()
            def execute(self, n, p, i=""): return object()
            def check(self, e, i, t=""): return HookGateResult(allowed=True)
            def publish(self, e, p, scope=None): pass
            def now(self): return _time.monotonic()
            def deadline(self, s): return _time.monotonic() + s
            def record(self, r, i, o): pass

        s = S()
        ports = RuntimePorts(llm=s, tools=s, hooks=s, live_events=s,
                             clock=s, token_usage=s)
        loop = StepLoop(ports)
        started = _time.monotonic()
        loop.execute(ctx)
        elapsed = (_time.monotonic() - started) * 1000
        assert elapsed < 500, f"cancel-to-return: {elapsed:.0f}ms > 500ms"

    def test_evidence_snapshot_independent(self):
        from application.evidence.evidence_collector import EvidenceCollector
        ec = EvidenceCollector()
        ec.record_tool("read", True)
        s1 = ec.snapshot()
        ec.record_tool("write", True)
        s2 = ec.snapshot()
        assert len(s1.tool_calls) == 1
        assert len(s2.tool_calls) == 2

    def test_frozen_json_no_dict_share(self):
        from core.json_values import freeze_json
        original = {"a": [1, 2, 3]}
        frozen = freeze_json(original)
        original["a"].append(4)
        assert len(frozen["a"]) == 3, "Frozen must not share mutable state"

    def test_outcome_digest_deterministic(self):
        from runtime_core.outcome import RuntimeOutcome
        from core.eventing.identifiers import RunId
        o1 = RuntimeOutcome.completed(RunId("r1"), steps=5, tokens=100, summary="done")
        o2 = RuntimeOutcome.completed(RunId("r1"), steps=5, tokens=100, summary="done")
        assert o1.digest() == o2.digest()

    def test_no_object_leak_after_scope_churn(self):
        from eventing.scope_tree import ScopeTree
        from core.eventing.identifiers import SessionId
        gc.collect()
        before = len(gc.get_objects())
        tree = ScopeTree()
        for gen in range(100):
            sid = SessionId(f"s-{gen}")
            tree.ensure_session(sid, gen)
            tree.close_session(sid)
        gc.collect()
        after = len(gc.get_objects())
        assert after < before + 5000, f"Possible leak: {before} → {after}"
