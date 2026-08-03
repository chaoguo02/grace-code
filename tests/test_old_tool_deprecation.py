"""T23: Old tool execution path marked DEPRECATED."""

import ast, os

def test_tool_execution_deprecated():
    path = os.path.join(os.path.dirname(__file__), "..", "core", "tool_execution.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    assert "DEPRECATED" in source, "T23: core/tool_execution.py must have DEPRECATED notice"

def test_native_tool_path_importable():
    from runtime_core.step_loop import StepLoop
    assert hasattr(StepLoop, '_process_tool_calls'), "T23: Native tool path must exist"
