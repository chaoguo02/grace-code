"""
P13: Runtime skeleton — thin wrapper, no DB writes.

Creates StepLoop, runs execution, publishes outcome via ports.
"""

from __future__ import annotations

from runtime_core.execution import RuntimeExecution
from runtime_core.outcome import RuntimeOutcome
from runtime_core.ports import RuntimePorts
from runtime_core.step_loop import StepLoop


class AgentRuntime:
    """Thin runtime wrapper.  No DB, no WebSocket, no SQLite."""

    def __init__(self, ports: RuntimePorts) -> None:
        self._ports = ports

    def run(self, context: RuntimeExecution) -> RuntimeOutcome:
        loop = StepLoop(self._ports)
        outcome = loop.execute(context)

        # Publish outcome via event port
        if self._ports.events is not None:
            self._ports.events.publish(outcome)

        return outcome

    @property
    def ports(self) -> RuntimePorts:
        return self._ports
