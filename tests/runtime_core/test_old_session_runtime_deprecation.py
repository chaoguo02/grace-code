"""G41: Old SessionRuntime deprecated; new runtime_core is authoritative.

AC: agent/session/runtime.py has DEPRECATED notice
AC: New AgentRuntime (G16) is importable
AC: New StepLoop (G17) has hook/tool pipeline
AC: New CancellationHandle (G18) exists
"""

import ast
import os

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


class TestOldSessionRuntimeDeprecation:
    """G41: Old SessionRuntime deprecated; runtime_core is replacement."""

    def test_old_runtime_deprecated(self):
        path = os.path.join(PROJECT_ROOT, "agent", "session", "runtime.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "DEPRECATED" in source, (
            "G41: agent/session/runtime.py must have DEPRECATED notice"
        )

    def test_new_agent_runtime_importable(self):
        from runtime_core.runtime import AgentRuntime
        assert AgentRuntime is not None

    def test_new_step_loop_has_hook_tool_pipeline(self):
        from runtime_core.step_loop import StepLoop
        assert hasattr(StepLoop, '_process_tool_calls'), (
            "G41: New StepLoop must have _process_tool_calls (G17)"
        )

    def test_new_cancellation_handle_exists(self):
        from runtime_core.execution import CancellationHandle
        h = CancellationHandle()
        assert not h.cancelled
        h.cancel()
        assert h.cancelled
