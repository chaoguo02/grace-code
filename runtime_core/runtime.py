"""
G16: AgentRuntime — thin wrapper, no DB writes, per-run local state.

Creates StepLoop per run.  No persistent mutable state across runs.
"""

from __future__ import annotations

from runtime_core.execution import RuntimeExecution
from runtime_core.outcome import RuntimeOutcome, RunStatus
from runtime_core.ports import RuntimePorts
from runtime_core.step_loop import StepLoop


class AgentRuntime:
    """Thin runtime wrapper.  No DB, no WebSocket, no SQLite.

    G16: Creates a fresh StepLoop per run() call — no mutable state
    retained between runs.
    """

    def __init__(self, ports: RuntimePorts) -> None:
        self._ports = ports

    def run(self, context: RuntimeExecution) -> RuntimeOutcome:
        """Execute one run.  Pure logic — no side effects.

        G18: Cancellation is checked via context.cancellation at every
        boundary (model, hook, tool execution).
        """
        loop = StepLoop(self._ports)
        outcome = loop.execute(context)

        # Publish live event (non-authoritative)
        self._ports.live_events.publish(
            event_type=f"run.{outcome.status.value}.v1",
            payload=outcome.summary,
        )

        # Record token usage
        if outcome.tokens_used > 0:
            self._ports.token_usage.record(
                context.run_id, outcome.tokens_used, 0,
            )

        return outcome

    @property
    def ports(self) -> RuntimePorts:
        return self._ports
