"""Phase 11 v2: per-session PermissionPipeline isolation (CC-aligned).

AC:
- Each session gets a FRESH PermissionPipeline (own RLock, own counters)
- Session-A denial counting does NOT leak into Session-B
- Two session pipelines are distinct objects with distinct locks
- hook input carries cwd from RuntimeExecution.workspace
"""

from __future__ import annotations

import pytest


def _assemble(tmp_path):
    """Assemble native object graph with per-session permission support."""
    from composition.runtime_composition import assemble
    return assemble(
        str(tmp_path / "native.db"),
        hook_settings={"permission_rules": {"Write": "ask"}},
        tool_registry=_write_lookup(tmp_path),
    )


def _write_lookup(tmp_path):
    from tools.file_tool import FileWriteTool
    ws = str(tmp_path)
    def _lookup(name):
        if name == "Write":
            return FileWriteTool(workspace_root=ws)
        if name == "Edit":
            from tools.file_edit_tool import FileEditTool
            return FileEditTool(workspace_root=ws)
        return None
    return _lookup


def _hook_input(session_id, tool="Write"):
    from hook_core.inputs import PreToolUseInput
    return PreToolUseInput(
        tool_name=tool, tool_input={"file_path": "test.txt"},
        tool_use_id="t1", session_id=session_id,
    )


def _allow_cb(request):
    from hitl.pipeline import PromptAction, PromptDecision
    return PromptDecision(action=PromptAction.ALLOW_ONCE)


def _deny_cb(request):
    from hitl.pipeline import PromptAction, PromptDecision
    return PromptDecision(action=PromptAction.DENY)


def test_session_pipelines_are_distinct_objects(tmp_path):
    """两 session 各自独立 pipeline 对象 + 独立锁。"""
    comp = _assemble(tmp_path)
    hooks = comp.runtime_ports.hooks

    pA = hooks._pipeline_for("sess-A")
    pB = hooks._pipeline_for("sess-B")

    assert pA is not pB, "两个 session 必须用不同 pipeline 对象"
    assert pA._state_lock is not pB._state_lock, "锁不能共享"


def test_session_a_denials_do_not_leak_to_b(tmp_path):
    """session-A 连续拒绝 → session-B 不受影响（独立 counters）。"""
    comp = _assemble(tmp_path)
    hooks = comp.runtime_ports.hooks

    hooks.register_session_confirm("sess-A", _deny_cb)
    hooks.register_session_confirm("sess-B", _allow_cb)

    # session-A 连续拒绝 3 次
    for _ in range(3):
        r = hooks.check("PreToolUse", _hook_input("sess-A"), tool_name="Write")
        assert r.allowed is False, f"A 应拒绝，got allowed={r.allowed}"

    # session-B 仍放行（counter 独立）
    r = hooks.check("PreToolUse", _hook_input("sess-B"), tool_name="Write")
    assert r.allowed is True, "B 的 counter 应独立于 A"


def test_session_reuses_cached_pipeline(tmp_path):
    """同一 session 复用已构造的 pipeline（不重复创建）。"""
    comp = _assemble(tmp_path)
    hooks = comp.runtime_ports.hooks

    p1 = hooks._pipeline_for("sess-X")
    p2 = hooks._pipeline_for("sess-X")
    assert p1 is p2, "同一 session 应复用同一 pipeline"


def test_register_confirm_invalidates_cache(tmp_path):
    """重新注册 confirm callback → 缓存失效，重建带新回调的 pipeline。"""
    comp = _assemble(tmp_path)
    hooks = comp.runtime_ports.hooks

    hooks.register_session_confirm("sess-Y", _deny_cb)
    p1 = hooks._pipeline_for("sess-Y")
    assert p1._web_confirm_callback is not None

    # 换回调 → 缓存失效 → 重建
    hooks.register_session_confirm("sess-Y", _allow_cb)
    p2 = hooks._pipeline_for("sess-Y")
    assert p1 is not p2, "换回调后应重建 pipeline"


def test_hook_input_carries_cwd(tmp_path):
    """PreToolUse hook input 带 cwd（来自 RuntimeExecution.workspace）。"""
    from runtime_core.execution import RuntimeExecution, ConversationSnapshot
    from runtime_core.native_step_loop import NativeStepLoop, ToolResult
    from runtime_core.ports import RuntimePorts, ToolDenied
    from core.eventing.identifiers import SessionId, RunId
    from runtime_core.model_actions import ToolCall
    from runtime_core.execution import CancellationHandle

    seen_cwd = {}
    class _Hooks:
        def check(self, event_type, hook_input, tool_name=""):
            if event_type == "PreToolUse":
                seen_cwd["cwd"] = getattr(hook_input, "cwd", "MISSING")
            from runtime_core.ports import HookGateResult
            return HookGateResult(allowed=True)

    class _Tools:
        def execute(self, name, params, invocation_id=""):
            from runtime_core.ports import ToolSuccess
            return ToolSuccess(tool_name=name)

    # LLM returns a ToolCall first turn so _process_tool_calls runs
    class _LLM:
        _called = False
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            if not self._called:
                self._called = True
                return ToolCall(id="t1", name="Write", params={"path": "a.txt"})
            from runtime_core.model_actions import AssistantText
            return AssistantText(text="done", stop_reason="end_turn")
        def stream(self, *a, **kw): raise NotImplementedError

    ports = RuntimePorts(
        llm=_LLM(), tools=_Tools(), hooks=_Hooks(),
        live_events=_Events(), clock=_Clock(), token_usage=_Token(),
    )
    loop = NativeStepLoop(ports)
    ctx = RuntimeExecution(
        session_id=SessionId("s1"), run_id=RunId("r1"),
        cancellation=CancellationHandle(), max_steps=5,
        workspace="/repo/workspace",
        conversation=ConversationSnapshot(messages=(
            {"role": "user", "content": "test"},
        )),
    )
    loop.execute(ctx)
    assert seen_cwd.get("cwd") == "/repo/workspace", (
        f"hook input 应带 cwd=/repo/workspace, got {seen_cwd.get('cwd')}"
    )


class _Events:
    def publish(self, *a, **kw): pass

class _Clock:
    import time as _t
    def now(self): return _t.monotonic()
    def deadline(self, s): return _t.monotonic() + s

class _Token:
    def record(self, *a, **kw): pass
