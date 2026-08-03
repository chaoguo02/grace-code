"""Runtime-owned availability facts for registered tools.

The registry is intentionally a small blocklist. Discovery/connection code
declares a tool unavailable; execution code consumes that typed fact. It does
not guess recovery timing, count model retries, or implement a second circuit
breaker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CapabilityState(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class InterceptDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True)
class Capability:
    name: str
    state: CapabilityState = CapabilityState.AVAILABLE
    reason: str = ""


@dataclass(frozen=True)
class InterceptResult:
    decision: InterceptDecision
    feedback: dict[str, Any] = field(default_factory=dict)


@dataclass
class CapabilityRegistry:
    """Declarative tool blocklist populated from objective Runtime facts."""

    _capabilities: dict[str, Capability] = field(default_factory=dict)

    def register(self, name: str) -> Capability:
        capability = Capability(name=name)
        self._capabilities[name] = capability
        return capability

    def register_bulk(self, names: set[str] | frozenset[str]) -> None:
        for name in names:
            self.register(name)

    def mark_unavailable(self, name: str, reason: str) -> None:
        self._capabilities[name] = Capability(
            name=name,
            state=CapabilityState.UNAVAILABLE,
            reason=reason,
        )

    def mark_available(self, name: str) -> None:
        self._capabilities[name] = Capability(name=name)

    def state_for(self, name: str) -> CapabilityState:
        capability = self._capabilities.get(name)
        return capability.state if capability else CapabilityState.AVAILABLE

    def get_reason(self, name: str) -> str:
        capability = self._capabilities.get(name)
        return capability.reason if capability else ""

    def intercept(self, name: str, session_id: str = "") -> InterceptResult:
        del session_id  # availability is a Runtime fact, not model-retry state
        capability = self._capabilities.get(name)
        if capability is None or capability.state is CapabilityState.AVAILABLE:
            return InterceptResult(decision=InterceptDecision.ALLOW)
        return InterceptResult(
            decision=InterceptDecision.BLOCK,
            feedback={
                "status": capability.state.value,
                "tool": name,
                "reason": capability.reason,
                "retry": "do_not_retry",
            },
        )

    def get_active_tool_names(self) -> set[str]:
        return {
            capability.name
            for capability in self._capabilities.values()
            if capability.state is CapabilityState.AVAILABLE
        }

    def get_unavailable_summary(self) -> list[dict[str, str]]:
        return [
            {
                "name": capability.name,
                "reason": capability.reason,
                "state": capability.state.value,
            }
            for capability in self._capabilities.values()
            if capability.state is CapabilityState.UNAVAILABLE
        ]

    def to_summary(self) -> dict[str, Any]:
        unavailable = self.get_unavailable_summary()
        return {
            "total": len(self._capabilities),
            "active": len(self.get_active_tool_names()),
            "unavailable": len(unavailable),
            "capabilities": {
                name: {
                    "state": capability.state.value,
                    "reason": capability.reason,
                }
                for name, capability in sorted(self._capabilities.items())
            },
        }
