"""
G12: Hook Registry — copy-on-write, thread-safe snapshots, decision binding.

- register() binds (event_type, allowed_decision_class) from EVENT_DECISION_MAP.
- Snapshot is immutable; tasks bind to a revision.
- unregister() does NOT affect already-bound RuntimeExecutions.
- Concurrency-safe: 100 concurrent writers/readers produce valid snapshots.
- Stable order: priority asc, then registration sequence.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from hook_core.decisions import EVENT_DECISION_MAP
from hook_core.matcher import HookSelector


class HookAlreadyRegisteredError(ValueError):
    """Hook with this name is already registered."""


class HookNotFoundError(ValueError):
    """No hook registered with this name."""


class InvalidEventTypeError(ValueError):
    """Event type not in EVENT_DECISION_MAP — no policy defined."""


@dataclass(frozen=True, slots=True)
class HookRegistration:
    name: str
    event_type: str
    handler: object
    selector: HookSelector = field(default_factory=HookSelector.all_tools)
    priority: int = 100
    allowed_decision_class: type | None = None  # G12: bound at registration
    _sequence: int = 0  # stable ordering within same priority


@dataclass(frozen=True)
class RegistrySnapshot:
    """Immutable snapshot of registered hooks at a point in time."""
    revision: int
    hooks: tuple[HookRegistration, ...]


class HookRegistry:
    """Copy-on-write, thread-safe hook registry.

    Registration creates a new revision.  Tasks bind to a snapshot
    and are immune to subsequent registrations (or unregistrations).
    """

    def __init__(self) -> None:
        self._hooks: dict[str, HookRegistration] = {}
        self._revision: int = 0
        self._sequence: int = 0
        self._lock = threading.Lock()  # G12: concurrency safety

    def register(self, name: str, event_type: str,
                 handler: object,
                 selector: HookSelector | None = None,
                 priority: int = 100) -> None:
        """Register a hook.  Binds decision class from EVENT_DECISION_MAP.

        Raises:
            HookAlreadyRegisteredError: name already registered.
            InvalidEventTypeError: event_type has no policy defined.
        """
        # G12: Validate event_type has a policy
        if event_type not in EVENT_DECISION_MAP:
            raise InvalidEventTypeError(
                f"Unknown event type '{event_type}'. "
                f"Must be one of: {sorted(EVENT_DECISION_MAP.keys())}"
            )

        decision_class = EVENT_DECISION_MAP[event_type]

        with self._lock:
            if name in self._hooks:
                existing = self._hooks[name]
                raise HookAlreadyRegisteredError(
                    f"Hook '{name}' already registered for "
                    f"'{existing.event_type}'"
                )
            self._sequence += 1
            self._hooks[name] = HookRegistration(
                name=name, event_type=event_type,
                handler=handler,
                selector=selector or HookSelector.all_tools(),
                priority=priority,
                allowed_decision_class=decision_class,
                _sequence=self._sequence,
            )
            self._revision += 1

    def unregister(self, name: str) -> None:
        """Remove a hook.  Does NOT affect tasks already bound to a snapshot."""
        with self._lock:
            if name not in self._hooks:
                raise HookNotFoundError(f"Hook '{name}' not registered")
            del self._hooks[name]
            self._revision += 1

    def snapshot(self) -> RegistrySnapshot:
        """Capture current revision.  Thread-safe."""
        with self._lock:
            return RegistrySnapshot(
                revision=self._revision,
                hooks=tuple(self._hooks.values()),
            )

    def get_hooks(self, snapshot: RegistrySnapshot | None,
                  event_type: str, tool_name: str = "") -> list[HookRegistration]:
        """Get hooks for *event_type*, sorted stable: priority asc, sequence asc.

        If snapshot is None, creates a snapshot of current state.
        Filters by selector if tool_name is provided.
        """
        hooks = (
            snapshot.hooks if snapshot is not None
            else self.snapshot().hooks
        )
        matching = [
            h for h in hooks
            if h.event_type == event_type
            and (not tool_name or h.selector.selects(tool_name))
        ]
        # G12: stable sort: priority asc, then registration sequence
        matching.sort(key=lambda h: (h.priority, h._sequence))
        return matching

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._hooks)
