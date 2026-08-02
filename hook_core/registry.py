"""
P10: Immutable Hook Registry — copy-on-write revision.

Task binding captures a revision snapshot; subsequent registrations
do not affect already-bound tasks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from hook_core.decisions import (
    PreToolUseDecision, PostToolUseDecision, UserPromptSubmitDecision,
    StopDecision, PreCompactDecision,
)
from hook_core.inputs import (
    PreToolUseInput, PostToolUseInput, UserPromptSubmitInput,
    StopInput, SubagentStopInput, PreCompactInput,
)
from hook_core.matcher import HookSelector


class HookAlreadyRegisteredError(ValueError):
    """Hook with this name is already registered."""


class HookNotFoundError(ValueError):
    """No hook registered with this name."""


@dataclass(frozen=True, slots=True)
class HookRegistration:
    name: str
    event_type: str  # "PreToolUse" | "PostToolUse" | ...
    selector: HookSelector
    handler: object   # callable(input) -> decision


@dataclass(frozen=True)
class RegistrySnapshot:
    """Immutable snapshot of registered hooks at a point in time."""
    revision: int
    hooks: tuple[HookRegistration, ...]


class HookRegistry:
    """Copy-on-write hook registry.

    Registration creates a new revision.  Tasks bind to a revision
    snapshot and are immune to subsequent registrations.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, HookRegistration] = {}
        self._revision: int = 0

    def register(self, name: str, event_type: str,
                 handler: object,
                 selector: HookSelector | None = None) -> None:
        if name in self._hooks:
            raise HookAlreadyRegisteredError(
                f"Hook '{name}' already registered for '{self._hooks[name].event_type}'"
            )
        self._hooks[name] = HookRegistration(
            name=name, event_type=event_type,
            selector=selector or HookSelector.all_tools(),
            handler=handler,
        )
        self._revision += 1

    def unregister(self, name: str) -> None:
        if name not in self._hooks:
            raise HookNotFoundError(f"Hook '{name}' not registered")
        del self._hooks[name]
        self._revision += 1

    def snapshot(self) -> RegistrySnapshot:
        """Capture current revision.  Task binds to this snapshot."""
        return RegistrySnapshot(
            revision=self._revision,
            hooks=tuple(self._hooks.values()),
        )

    def get_hooks(self, snapshot: RegistrySnapshot | None,
                  event_type: str, tool_name: str = "") -> list[HookRegistration]:
        """Get hooks for *event_type* from *snapshot*.

        If snapshot is None, uses the current state.
        Filters by selector if tool_name is provided.
        """
        hooks = (
            snapshot.hooks if snapshot is not None
            else tuple(self._hooks.values())
        )
        matching = [
            h for h in hooks
            if h.event_type == event_type
            and (not tool_name or h.selector.selects(tool_name))
        ]
        return matching

    @property
    def revision(self) -> int:
        return self._revision
