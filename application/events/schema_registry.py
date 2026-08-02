"""
P3: Schema Registry — whitelist of payload classes by (event_type, version).

Duplicate registration = ValueError at startup.
Only registered payload classes can be wrapped in EventEnvelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from application.events.run_facts import (
    RunSubmittedV1, RunStartedV1, RunCompletedV1, RunFailedV1,
    RunCancelledV1, RunBlockedV1, RunGaveUpV1,
)
from application.events.tool_facts import ToolExecutedV1
from application.events.delegation_facts import (
    DelegationCreatedV1, DelegationCompletedV1,
    ChildTaskStartedV1, ChildTaskCompletedV1,
)


@dataclass(frozen=True, slots=True)
class SchemaEntry:
    event_type: str       # e.g. "run.completed.v1"
    version: int
    payload_class: type
    description: str = ""


class SchemaRegistry:
    """Immutable after construction. Duplicate keys → ValueError."""

    def __init__(self) -> None:
        self._entries: dict[str, SchemaEntry] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        defaults: list[SchemaEntry] = [
            # Run facts
            SchemaEntry("run.submitted.v1", 1, RunSubmittedV1),
            SchemaEntry("run.started.v1", 1, RunStartedV1),
            SchemaEntry("run.completed.v1", 1, RunCompletedV1),
            SchemaEntry("run.failed.v1", 1, RunFailedV1),
            SchemaEntry("run.cancelled.v1", 1, RunCancelledV1),
            SchemaEntry("run.blocked.v1", 1, RunBlockedV1),
            SchemaEntry("run.gave_up.v1", 1, RunGaveUpV1),
            # Tool facts
            SchemaEntry("tool.executed.v1", 1, ToolExecutedV1),
            # Delegation facts
            SchemaEntry("delegation.created.v1", 1, DelegationCreatedV1),
            SchemaEntry("delegation.completed.v1", 1, DelegationCompletedV1),
            SchemaEntry("child_task.started.v1", 1, ChildTaskStartedV1),
            SchemaEntry("child_task.completed.v1", 1, ChildTaskCompletedV1),
        ]
        for entry in defaults:
            self.register(entry)

    def register(self, entry: SchemaEntry) -> None:
        key = entry.event_type
        if key in self._entries:
            existing = self._entries[key]
            if existing.payload_class is not entry.payload_class:
                raise ValueError(
                    f"Duplicate schema key '{key}': "
                    f"existing={existing.payload_class.__name__}, "
                    f"new={entry.payload_class.__name__}"
                )
        self._entries[key] = entry

    def get(self, event_type: str) -> SchemaEntry | None:
        return self._entries.get(event_type)

    def has(self, event_type: str) -> bool:
        return event_type in self._entries

    def validate_payload(self, event_type: str, payload: Any) -> bool:
        """True if *payload* is an instance of the registered class."""
        entry = self._entries.get(event_type)
        if entry is None:
            return False
        return isinstance(payload, entry.payload_class)

    @property
    def registered_types(self) -> list[str]:
        return sorted(self._entries.keys())
