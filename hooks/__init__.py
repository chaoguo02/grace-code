"""
hooks/ — Event-driven hook system for lifecycle extensibility.

Core components:
- HookEvent: lifecycle event types (PreToolUse, PostToolUse, Stop, etc.)
- HookContext: event context passed to hooks
- HookDispatcher: central event dispatch (match → execute → decide)
- HookRegistry: stores external (command) and internal (callable) hooks
"""

from hooks.events import (
    BLOCKABLE_EVENTS, HookContext, HookEvent, SessionStartSource,
)
from hooks.protocol import DispatchResult, ExitCode, HookOutput, HookResult
from hooks.matcher import HookMatcher
from hooks.registry import ExternalHookConfig, HookRegistry, InternalHook
from hooks.dispatcher import HookDispatcher

__all__ = [
    "HookEvent",
    "HookContext",
    "SessionStartSource",
    "BLOCKABLE_EVENTS",
    "ExitCode",
    "HookOutput",
    "HookResult",
    "DispatchResult",
    "HookMatcher",
    "ExternalHookConfig",
    "InternalHook",
    "HookRegistry",
    "HookDispatcher",
]
