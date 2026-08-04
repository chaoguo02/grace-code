"""CC-aligned: PreToolUse hooks run FIRST, can override permission rules.

AC:
- A blocking hook denies even when permission would allow (hook = safety floor)
- A hook ALLOW supersedes a permission deny (canUseTool is skipped)
- Hook passes (no decision) → permission gate decides
"""

from __future__ import annotations

import pytest


def _make_hook_input(tool_name="Write", session_id="sess-1"):
    from hook_core.inputs import PreToolUseInput
    return PreToolUseInput(
        tool_name=tool_name,
        tool_input={"file_path": "test.txt"},
        tool_use_id="t1",
        session_id=session_id,
    )


def _lookup(name):
    from tools.file_tool import FileWriteTool
    return FileWriteTool(workspace_root=".") if name in {"Write", "Edit"} else None


def _hook_registry(block=False, allow=False):
    """Registry with one PreToolUse hook that blocks or allows."""
    from hook_core.registry import HookRegistry
    from hook_core.matcher import HookMatcher, HookSelector
    from hook_core.decisions import PreToolUseDecision
    from hitl.pipeline import PermissionDecision

    registry = HookRegistry()
    if block:
        def _h(hook_input):
            return PreToolUseDecision(
                permission=PermissionDecision.DENY, reason="hook block test",
            )
    elif allow:
        def _h(hook_input):
            return PreToolUseDecision(
                permission=PermissionDecision.ALLOW, reason="hook allow test",
            )
    else:
        def _h(hook_input):
            return None  # no decision → pass through

    registry.register(
        name="test-hook", event_type="PreToolUse",
        handler=_h, selector=HookSelector.all_tools(),
    )
    return registry


def test_blocking_hook_denies_even_with_allow_permission():
    """Hook deny wins over permission ALLOW (hook = safety floor, CC)."""
    from composition.runtime_composition import assemble
    from hitl.settings_loader import builtin_native_rules

    comp = assemble(
        "/tmp/hook-order-block.db",
        hook_settings={"permission_rules": {"Write": "allow"}},
        tool_registry=_lookup,
    )
    # Register a blocking hook via dispatcher
    comp.hook_registry.register(
        name="block-hook", event_type="PreToolUse",
        handler=lambda hi: _block_decision(),
        selector=_all_selector(),
    )
    result = comp.runtime_ports.hooks.check(
        "PreToolUse", _make_hook_input(), tool_name="Write",
    )
    assert result.allowed is False, "Hook deny must override permission ALLOW"


def test_hook_allow_supersedes_permission_deny():
    """Hook ALLOW wins over permission DENY (canUseTool skipped, CC)."""
    from composition.runtime_composition import assemble

    comp = assemble(
        "/tmp/hook-order-allow.db",
        hook_settings={"permission_rules": {"Write": "deny"}},
        tool_registry=_lookup,
    )
    comp.hook_registry.register(
        name="allow-hook", event_type="PreToolUse",
        handler=lambda hi: _allow_decision(),
        selector=_all_selector(),
    )
    result = comp.runtime_ports.hooks.check(
        "PreToolUse", _make_hook_input(), tool_name="Write",
    )
    assert result.allowed is True, "Hook ALLOW must supersede permission DENY"


def test_hook_pass_through_permission_gate():
    """Hook no-decision → permission gate decides."""
    from composition.runtime_composition import assemble

    comp = assemble(
        "/tmp/hook-order-pass.db",
        hook_settings={"permission_rules": {"Write": "deny"}},
        tool_registry=_lookup,
    )
    # No hook registered → pass through → permission deny blocks
    result = comp.runtime_ports.hooks.check(
        "PreToolUse", _make_hook_input(), tool_name="Write",
    )
    assert result.allowed is False, "Permission DENY must block when hook passes"


def _block_decision():
    from hook_core.decisions import PreToolUseDecision
    from hitl.pipeline import PermissionDecision
    return PreToolUseDecision(permission=PermissionDecision.DENY, reason="block")


def _allow_decision():
    from hook_core.decisions import PreToolUseDecision
    from hitl.pipeline import PermissionDecision
    return PreToolUseDecision(permission=PermissionDecision.ALLOW, reason="allow")


def _all_selector():
    from hook_core.matcher import HookSelector
    return HookSelector.all_tools()
