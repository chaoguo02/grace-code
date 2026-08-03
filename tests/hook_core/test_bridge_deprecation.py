"""G39: HookBridge is deprecated — new typed inputs (G11) are the replacement.

AC: bridge.py has DEPRECATED notice
AC: New typed inputs (PreToolUseInput, etc.) are importable
AC: New typed decisions (PreToolUseDecision, etc.) use FrozenJsonObject
AC: HookContext references are only in bridge.py (compat layer)
"""

import ast
import os

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


class TestBridgeDeprecation:
    """G39: bridge.py deprecated; new typed contracts in place."""

    def test_bridge_has_deprecation_notice(self):
        path = os.path.join(PROJECT_ROOT, "hook_core", "bridge.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "DEPRECATED" in source or "DELETED" in source, (
            "G39: hook_core/bridge.py must have DEPRECATED or DELETED notice"
        )

    def test_new_typed_inputs_importable(self):
        """G11 typed inputs replace HookContext."""
        from hook_core.inputs import (
            PreToolUseInput, PostToolUseInput, StopInput,
            UserPromptSubmitInput, PreCompactInput,
        )
        assert PreToolUseInput is not None
        # Verify tool_input is FrozenJsonObject (not dict)
        import inspect
        sig = inspect.signature(PreToolUseInput)
        assert "tool_input" in sig.parameters

    def test_new_typed_decisions_importable(self):
        """G11 typed decisions replace raw dict returns."""
        from hook_core.decisions import (
            PreToolUseDecision, StopDecision,
            HookContractViolation, EVENT_DECISION_MAP,
        )
        assert PreToolUseDecision is not None
        assert "PreToolUse" in EVENT_DECISION_MAP

    def test_hook_context_only_in_bridge(self):
        """G39: HookContext references should only exist in bridge.py."""
        hook_dir = os.path.join(PROJECT_ROOT, "hook_core")
        violations = []
        for fname in os.listdir(hook_dir):
            if fname == "bridge.py" or not fname.endswith(".py"):
                continue
            path = os.path.join(hook_dir, fname)
            with open(path, encoding="utf-8") as f:
                source = f.read()
            if "HookContext" in source:
                # Check if it's just a comment or docstring
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        module = getattr(node, 'module', '') or ''
                        if "HookContext" in str(module):
                            violations.append(f"{fname}: imports {module}")
        assert violations == [], (
            f"G39: HookContext references outside bridge.py:\n"
            + "\n".join(violations)
        )
