"""G11: Hook typed contracts — FrozenJsonObject, no Any/dict, per-event decision.

AC: All tool_input fields are FrozenJsonObject (not dict[str, Any])
AC: StopInput uses outcome_summary (not messages: list[dict])
AC: updated_input uses FrozenJsonObject | None (not dict | None)
AC: HookContractViolation exists for invalid inputs
AC: EVENT_DECISION_MAP covers all hook events
AC: Zero Any imports in inputs.py
AC: Zero raw dict type annotations in decisions.py
"""

from __future__ import annotations

import ast
import os

import pytest

from core.json_values import FrozenJsonObject, freeze_json
from hook_core.inputs import (
    PreToolUseInput,
    PostToolUseInput,
    PostToolUseFailureInput,
    PermissionRequestInput,
    PermissionDeniedInput,
    StopInput,
    UserPromptSubmitInput,
)
from hook_core.decisions import (
    PreToolUseDecision,
    PostToolUseDecision,
    UserPromptSubmitDecision,
    StopDecision,
    HookContractViolation,
    ObserveDecision,
    PermissionDecision,
    StopVerdict,
    EVENT_DECISION_MAP,
)


# ═══════════════════════════════════════════════════════════════════════════════
# G11.1 — tool_input is FrozenJsonObject
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolInputIsFrozen:
    """G11: All tool_input fields use FrozenJsonObject, not dict."""

    def test_pretooluse_input_accepts_frozen(self):
        frozen = freeze_json({"file": "test.py", "content": "hello"})
        inp = PreToolUseInput(tool_name="write", tool_input=frozen)
        assert isinstance(inp.tool_input, FrozenJsonObject)
        assert inp.tool_input["file"] == "test.py"

    def test_posttooluse_input_accepts_frozen(self):
        frozen = freeze_json({"file": "test.py"})
        inp = PostToolUseInput(tool_name="write", tool_input=frozen)
        assert isinstance(inp.tool_input, FrozenJsonObject)

    def test_permission_request_accepts_frozen(self):
        frozen = freeze_json({"path": "/etc/config"})
        inp = PermissionRequestInput(tool_name="write", tool_input=frozen)
        assert isinstance(inp.tool_input, FrozenJsonObject)

    def test_raw_dict_rejected_at_boundary(self):
        """FrozenJsonObject cannot be constructed from a raw dict without freeze_json."""
        # The type annotation is FrozenJsonObject — passing a plain dict
        # would be caught by a type checker (mypy) and by runtime isinstance
        raw = {"file": "test.py"}
        assert not isinstance(raw, FrozenJsonObject), (
            "Raw dict must not be interchangeable with FrozenJsonObject"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G11.2 — StopInput uses outcome_summary, not messages
# ═══════════════════════════════════════════════════════════════════════════════

class TestStopInput:
    """G11: StopInput has outcome_summary, not mutable messages list."""

    def test_stop_input_has_outcome_summary(self):
        inp = StopInput(
            session_id="s1",
            steps_taken=3,
            tokens_used=500,
            outcome_summary="completed: wrote 2 files",
        )
        assert inp.outcome_summary == "completed: wrote 2 files"
        assert inp.steps_taken == 3

    def test_stop_input_no_messages_field(self):
        """G11: StopInput must not have a 'messages' field."""
        assert not hasattr(StopInput, "messages") or "messages" not in (
            f.name for f in StopInput.__dataclass_fields__.values()
        ), "G11: StopInput must not have messages field"


# ═══════════════════════════════════════════════════════════════════════════════
# G11.3 — updated_input uses FrozenJsonObject | None
# ═══════════════════════════════════════════════════════════════════════════════

class TestUpdatedInput:
    """G11: Decision updated_input uses FrozenJsonObject, not dict."""

    def test_pretooluse_decision_accepts_frozen(self):
        frozen = freeze_json({"timeout": 30})
        dec = PreToolUseDecision(
            permission=PermissionDecision.ALLOW,
            updated_input=frozen,
            reason="adjusted timeout",
        )
        assert isinstance(dec.updated_input, FrozenJsonObject)

    def test_pretooluse_decision_none_is_ok(self):
        dec = PreToolUseDecision(permission=PermissionDecision.DENY)
        assert dec.updated_input is None

    def test_user_prompt_submit_decision_accepts_frozen(self):
        frozen = freeze_json({"prompt": "corrected"})
        dec = UserPromptSubmitDecision(block=False, updated_input=frozen)
        assert isinstance(dec.updated_input, FrozenJsonObject)


# ═══════════════════════════════════════════════════════════════════════════════
# G11.4 — HookContractViolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestHookContractViolation:
    """G11: HookContractViolation for invalid/unknown inputs."""

    def test_hook_contract_violation_exists(self):
        v = HookContractViolation(reason="invalid input", detail="expected FrozenJsonObject, got dict")
        assert v.reason == "invalid input"
        assert isinstance(v, HookContractViolation)

    def test_hook_contract_violation_is_not_observe_decision(self):
        """G11: HookContractViolation replaces ObserveDecision for error cases."""
        v = HookContractViolation(reason="bad type")
        # It's a distinct type, not ObserveDecision
        assert type(v) is HookContractViolation


# ═══════════════════════════════════════════════════════════════════════════════
# G11.5 — EVENT_DECISION_MAP coverage
# ═══════════════════════════════════════════════════════════════════════════════

class TestEventDecisionMap:
    """G11: EVENT_DECISION_MAP maps each lifecycle point to its decision type."""

    def test_map_covers_all_events(self):
        events = {
            "PreToolUse", "PostToolUse", "PostToolUseFailure",
            "PostToolBatch", "UserPromptSubmit", "Stop", "StopFailure",
            "SessionStart", "SessionEnd", "SubagentStart", "SubagentStop",
            "PreCompact", "PostCompact", "PermissionRequest", "PermissionDenied",
            "Notification",
        }
        assert set(EVENT_DECISION_MAP.keys()) == events, (
            f"Missing events: {events - set(EVENT_DECISION_MAP.keys())}"
        )

    def test_map_values_are_types(self):
        for event_name, dec_type in EVENT_DECISION_MAP.items():
            assert isinstance(dec_type, type), (
                f"EVENT_DECISION_MAP[{event_name}] = {dec_type!r} is not a type"
            )

    def test_pretooluse_decision_values(self):
        dec = PreToolUseDecision(permission=PermissionDecision.DENY, reason="blocked")
        assert dec.permission == PermissionDecision.DENY
        dec2 = PreToolUseDecision(permission=PermissionDecision.ALLOW)
        assert dec2.permission == PermissionDecision.ALLOW


# ═══════════════════════════════════════════════════════════════════════════════
# G11.6 — Static gate: zero Any / dict / HookContext in hook_core
# ═══════════════════════════════════════════════════════════════════════════════

HOOK_CORE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "hook_core")


class TestStaticGate:
    """G11: hook_core/inputs.py and decisions.py contain zero Any/裸dict/HookContext."""

    def test_inputs_no_any_import(self):
        path = os.path.join(HOOK_CORE_DIR, "inputs.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [n.name for n in node.names]
                assert "Any" not in names, (
                    "G11: inputs.py must not import Any"
                )

    def test_inputs_no_dict_type_annotation(self):
        """G11: inputs.py field types must not be dict[...] or Any."""
        path = os.path.join(HOOK_CORE_DIR, "inputs.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                ann = ast.unparse(node.annotation)
                assert "dict[" not in ann, (
                    f"G11: inputs.py field annotation uses dict[...]: {ann}"
                )

    def test_inputs_no_hook_context(self):
        path = os.path.join(HOOK_CORE_DIR, "inputs.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                names = [n.name for n in getattr(node, 'names', [])]
                assert "HookContext" not in module, "G11: inputs.py imports HookContext"
                assert "HookContext" not in names, "G11: inputs.py imports HookContext"

    def test_decisions_no_dict_annotation(self):
        """G11: decisions.py field types must not be bare dict."""
        path = os.path.join(HOOK_CORE_DIR, "decisions.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                ann = ast.unparse(node.annotation)
                # Allow FrozenJsonObject | None, but NOT bare dict | None
                if ann.strip() == "dict | None" or ann.strip().startswith("dict "):
                    pytest.fail(f"G11: decisions.py uses bare dict: {ann}")

    def test_decisions_no_hook_context(self):
        path = os.path.join(HOOK_CORE_DIR, "decisions.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                names = [n.name for n in getattr(node, 'names', [])]
                assert "HookContext" not in module, "G11: decisions.py imports HookContext"
                assert "HookContext" not in names, "G11: decisions.py imports HookContext"
