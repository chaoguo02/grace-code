"""Runtime-owned availability facts for registered tools.

The guard is intentionally a small blocklist. Discovery/connection code
marks a tool unavailable; execution code consumes that typed fact. It does
not guess recovery timing, count model retries, or implement a second circuit
breaker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolAvailabilityState(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class InterceptDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True)
class ToolAvailability:
    name: str
    state: ToolAvailabilityState = ToolAvailabilityState.AVAILABLE
    reason: str = ""


@dataclass(frozen=True)
class InterceptResult:
    decision: InterceptDecision
    feedback: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolAvailabilityGuard:
    """Declarative tool blocklist populated from objective Runtime facts."""

    _availability: dict[str, ToolAvailability] = field(default_factory=dict)

    def register(self, name: str) -> ToolAvailability:
        availability = ToolAvailability(name=name)
        self._availability[name] = availability
        return availability

    def register_bulk(self, names: set[str] | frozenset[str] | list[str]) -> None:
        for name in names:
            self.register(name)

    def mark_unavailable(self, name: str, reason: str) -> None:
        self._availability[name] = ToolAvailability(
            name=name,
            state=ToolAvailabilityState.UNAVAILABLE,
            reason=reason,
        )

    def mark_available(self, name: str) -> None:
        self._availability[name] = ToolAvailability(name=name)

    def state_for(self, name: str) -> ToolAvailabilityState:
        availability = self._availability.get(name)
        return availability.state if availability else ToolAvailabilityState.AVAILABLE

    def get_reason(self, name: str) -> str:
        availability = self._availability.get(name)
        return availability.reason if availability else ""

    def intercept(self, name: str, session_id: str = "") -> InterceptResult:
        del session_id  # availability is a Runtime fact, not model-retry state
        availability = self._availability.get(name)
        if availability is None or availability.state is ToolAvailabilityState.AVAILABLE:
            return InterceptResult(decision=InterceptDecision.ALLOW)
        return InterceptResult(
            decision=InterceptDecision.BLOCK,
            feedback={
                "status": availability.state.value,
                "tool": name,
                "reason": availability.reason,
                "retry": "do_not_retry",
            },
        )

    def get_active_tool_names(self) -> set[str]:
        return {
            availability.name
            for availability in self._availability.values()
            if availability.state is ToolAvailabilityState.AVAILABLE
        }

    def get_unavailable_summary(self) -> list[dict[str, str]]:
        return [
            {
                "name": availability.name,
                "reason": availability.reason,
                "state": availability.state.value,
            }
            for availability in self._availability.values()
            if availability.state is ToolAvailabilityState.UNAVAILABLE
        ]

    def to_summary(self) -> dict[str, Any]:
        unavailable = self.get_unavailable_summary()
        return {
            "total": len(self._availability),
            "active": len(self.get_active_tool_names()),
            "unavailable": len(unavailable),
            "capabilities": {
                name: {
                    "state": availability.state.value,
                    "reason": availability.reason,
                }
                for name, availability in sorted(self._availability.items())
            },
        }
