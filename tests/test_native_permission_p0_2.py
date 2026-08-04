"""P0-2: Native permission acceptEdits — Write/Edit auto-approve, dangerous denied.

AC:
- Write/Edit tools are NOT in ASK rules → acceptEdits mode auto-approves
- Dangerous shell (docker/rm/git push) still ASK → headless auto-deny
- _BLOCKED_PATTERNS still DENY (safety floor)
- Read-only tools still ALLOW
"""

from __future__ import annotations

import pytest


def _make_pipeline():
    """Native object graph permission pipeline: acceptEdits + builtin_native_rules."""
    from hitl.settings_loader import builtin_native_rules
    from hitl.pipeline import PermissionPipeline, ToolApprovalMode

    pipeline = PermissionPipeline(
        rules=builtin_native_rules(),
        approval_mode=ToolApprovalMode.AUTO,
    )
    pipeline.set_permission_mode("acceptEdits")
    return pipeline


def _tool(name: str):
    from core.base import BaseTool, ToolMetadata

    _readonly = name in {"Read", "Grep", "Glob", "file_view"}
    _name = name

    class _T(BaseTool):
        name = _name
        description = "test"
        parameters_schema = {"type": "object", "properties": {}}
        metadata = ToolMetadata()
        def isReadOnly(self, params=None):
            return _readonly
        def execute(self, params):
            from core.base import ToolResult
            return ToolResult(success=True, output="ok")

    return _T()


def test_write_auto_approved_under_accept_edits():
    """Write not in ask rules → acceptEdits auto-approve (CC coding agent)."""
    from hitl.pipeline import PermissionDecision
    pipeline = _make_pipeline()
    result = pipeline.check(_tool("Write"), {"file_path": "test.txt"})
    assert result.decision is PermissionDecision.ALLOW


def test_edit_auto_approved_under_accept_edits():
    """Edit not in ask rules → acceptEdits auto-approve."""
    from hitl.pipeline import PermissionDecision
    pipeline = _make_pipeline()
    result = pipeline.check(_tool("Edit"), {"file_path": "test.txt"})
    assert result.decision is PermissionDecision.ALLOW


def test_dangerous_shell_denied_headless():
    """docker in ask rules → headless (no callback) → DENY (fail closed)."""
    from hitl.pipeline import PermissionDecision
    from core.base import BaseTool, ToolMetadata

    class _Bash(BaseTool):
        name = "Bash"
        description = "test"
        parameters_schema = {"type": "object", "properties": {"command": {"type": "string"}}}
        metadata = ToolMetadata()
        def isReadOnly(self, params=None):
            return False
        def execute(self, params):
            from core.base import ToolResult
            return ToolResult(success=True, output="ok")

    pipeline = _make_pipeline()
    result = pipeline.check(_Bash(), {"command": "docker run nginx"})
    assert result.decision is PermissionDecision.DENY


def test_readonly_tool_allowed():
    """Read-only tools in allow rules → ALLOW."""
    from hitl.pipeline import PermissionDecision
    pipeline = _make_pipeline()
    result = pipeline.check(_tool("Read"), {"file_path": "a.py"})
    assert result.decision is PermissionDecision.ALLOW


def test_safe_shell_auto_allowed():
    """ls in allow rules → AUTO mode ALLOW (headless)."""
    from hitl.pipeline import PermissionDecision
    from core.base import BaseTool, ToolMetadata

    class _Bash(BaseTool):
        name = "Bash"
        description = "test"
        parameters_schema = {"type": "object", "properties": {"command": {"type": "string"}}}
        metadata = ToolMetadata()
        def isReadOnly(self, params=None):
            return False
        def execute(self, params):
            from core.base import ToolResult
            return ToolResult(success=True, output="ok")

    pipeline = _make_pipeline()
    result = pipeline.check(_Bash(), {"command": "ls -la"})
    assert result.decision is PermissionDecision.ALLOW
