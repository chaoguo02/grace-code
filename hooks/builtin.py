"""G36M-1: DEPRECATED — replaced by hook_core (G11-G14).

Built-in hooks — framework-level safety nets registered at startup.
Kept for backward compat.  New code must use HookRegistry.register() from hook_core.
"""

from __future__ import annotations
from typing import Any

from hooks.events import HookContext, HookEvent


def _register_builtin_stop_hook(registry) -> None:
    """Register the built-in Stop hook: verify changes before allowing finish."""
    from hooks.registry import HookMatcher, InternalHook

    def _verify_changes(context: HookContext) -> None:
        """Built-in stop hook: inject verification message if applicable."""
        # This is called by the dispatcher; exit code 0 = pass through.
        # The actual blocking is done by returning a decision, but since
        # InternalHook doesn't support decisions directly, we inject a
        # verification message via additional_context.
        pass  # The real verification happens in the agent loop context

    registry.register_internal(
        HookEvent.STOP,
        InternalHook(callback=_verify_changes, matcher=HookMatcher()),
    )
