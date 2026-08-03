"""G36M-1: DEPRECATED — replaced by hook_core (G11-G14).

hooks/ — Event-driven hook system for lifecycle extensibility.
Kept for backward compat.  New code must use hook_core.registry + hook_core.dispatcher.
"""

from hooks.events import (
    BLOCKABLE_EVENTS, HookContext, HookEvent, SessionStartSource,
)
from hooks.protocol import (
    DispatchResult, ExitCode, HookDecision, HookOutput, HookResult,
)
from hooks.matcher import HookMatcher
from hooks.registry import (
    ExternalHookConfig,
    HookDataAuthority,
    HookDecisionAuthority,
    HookFailurePolicy,
    HookRegistry,
    HookScheduling,
    InternalHook,
)
from hooks.dispatcher import HookDispatcher

__all__ = [
    "HookEvent",
    "HookContext",
    "SessionStartSource",
    "BLOCKABLE_EVENTS",
    "ExitCode",
    "HookOutput",
    "HookDecision",
    "HookResult",
    "DispatchResult",
    "HookMatcher",
    "ExternalHookConfig",
    "InternalHook",
    "HookScheduling",
    "HookDecisionAuthority",
    "HookDataAuthority",
    "HookFailurePolicy",
    "HookRegistry",
    "HookDispatcher",
]
