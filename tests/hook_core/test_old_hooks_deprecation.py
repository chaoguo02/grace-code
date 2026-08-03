"""G40: Old hooks/ dispatcher + registry are deprecated.

AC: hooks/dispatcher.py has DEPRECATED notice
AC: hooks/registry.py has DEPRECATED notice
AC: New hook_core (G11-G14) is the authoritative replacement
AC: New HookRegistry supports snapshot isolation (G12)
AC: New HookDispatcher supports async dispatch (G14)
"""

import ast
import os

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


class TestOldHooksDeprecation:
    """G40: Old hooks/ modules deprecated; new hook_core/ is authoritative."""

    def test_old_dispatcher_deprecated(self):
        path = os.path.join(PROJECT_ROOT, "hooks", "dispatcher.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "DEPRECATED" in source, (
            "G40: hooks/dispatcher.py must have DEPRECATED notice"
        )

    def test_old_registry_deprecated(self):
        path = os.path.join(PROJECT_ROOT, "hooks", "registry.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "DEPRECATED" in source, (
            "G40: hooks/registry.py must have DEPRECATED notice"
        )

    def test_new_registry_supports_snapshot(self):
        """G12: New HookRegistry has snapshot isolation."""
        from hook_core.registry import HookRegistry, RegistrySnapshot
        reg = HookRegistry()
        snap = reg.snapshot()
        assert isinstance(snap, RegistrySnapshot)

    def test_new_dispatcher_supports_async(self):
        """G14: New HookDispatcher has dispatch_async."""
        from hook_core.dispatcher import HookDispatcher
        from hook_core.registry import HookRegistry
        reg = HookRegistry()
        disp = HookDispatcher(reg)
        assert hasattr(disp, 'dispatch_async'), (
            "G40: New HookDispatcher must have dispatch_async (G14)"
        )
