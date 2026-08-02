"""
P19-P22: Runtime composition — assembles the new architecture.

Connects coordinator, runtime, event bus, projections, and listeners.
This file is the single assembly point.  Individual modules remain decoupled.
"""

from __future__ import annotations

import os


class RuntimeComposition:
    """Assembles the new architecture for one process.

    LEGACY mode: uses old AgentService + SessionRuntime paths.
    NATIVE mode: uses RunCoordinator + AgentRuntime + OutboxRelay.
    SHADOW mode: runs both, logs mismatches via ShadowRunner.
    """

    def __init__(self, db_path: str, mode: str | None = None) -> None:
        self._mode = mode or os.environ.get("GRACE_RUNTIME_MODE", "LEGACY")
        self._db_path = db_path

    def assemble(self) -> dict:
        """Return a dict of assembled components, keyed by role name."""
        components: dict = {}

        if self._mode in ("NATIVE", "SHADOW"):
            from application.events.schema_registry import SchemaRegistry
            from infrastructure.outbox.sqlite_store import SqliteOutboxStore
            from infrastructure.outbox.relay import OutboxRelay
            from application.coordinators.run_coordinator import RunCoordinator
            from runtime_core.runtime import AgentRuntime
            from runtime_core.ports import RuntimePorts
            from listeners.trace_projection import TraceProjection
            from listeners.projection_runner import ProjectionRunner
            from listeners.ws_gateway import WsGateway
            from listeners.stats_projection import StatsProjection

            registry = SchemaRegistry()
            outbox = SqliteOutboxStore(self._db_path, registry)
            trace = TraceProjection(self._db_path)
            stats = StatsProjection()
            runner = ProjectionRunner([trace, stats])
            ws_gw = WsGateway()
            ports = RuntimePorts()

            def _deliver(record):
                envelope = None  # OutboxRecord → EventEnvelope (mapper in R3.4)
                if envelope:
                    for receipt in runner.handle(envelope):
                        pass

            relay = OutboxRelay(outbox, _deliver)
            runtime = AgentRuntime(ports)
            coordinator = RunCoordinator(runtime, None)  # UoW injected later

            components["registry"] = registry
            components["outbox"] = outbox
            components["relay"] = relay
            components["runtime"] = runtime
            components["coordinator"] = coordinator
            components["trace"] = trace
            components["stats"] = stats
            components["ws_gateway"] = ws_gw
            components["mode"] = self._mode

        return components
