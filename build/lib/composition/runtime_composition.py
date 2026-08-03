"""
P19-P22: Runtime composition — assembles the new architecture.

Connects coordinator, runtime, event bus, projections, and listeners.
This file is the single assembly point.  Individual modules remain decoupled.

G0: Sealed false ACK (projection failures propagate to Relay) and added
    process-level owner guard against dual relay startup.
"""

from __future__ import annotations

import os
import threading


# ── G0: Process-level owner guard ────────────────────────────────────────
# Replaced by durable lease in G10.  For G0 this is a per-process lock
# keyed by DB path to prevent dual relay on the same database.
_owner_lock = threading.Lock()
_active_owners: dict[str, int] = {}  # db_path -> os.getpid()


def _acquire_owner(db_path: str) -> None:
    """Acquire process-level ownership of the outbox relay for *db_path*.

    Raises RuntimeError if another pipeline already owns this DB path.
    """
    db_path = os.path.abspath(db_path)
    with _owner_lock:
        if db_path in _active_owners:
            raise RuntimeError(
                f"Relay owner already active for DB {db_path} "
                f"(pid={_active_owners[db_path]}). "
                f"Only one relay owner per database is permitted."
            )
        _active_owners[db_path] = os.getpid()


def _release_owner(db_path: str) -> None:
    """Release process-level ownership for *db_path*."""
    db_path = os.path.abspath(db_path)
    with _owner_lock:
        _active_owners.pop(db_path, None)


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
        """Return a dict of assembled components, keyed by role name.

        When mode is NATIVE or SHADOW, the returned dict includes a fully
        wired pipeline: OutboxRelay → Projections (direct, not via bus).
        """
        components: dict = {"mode": self._mode}

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
            from eventing.scoped_bus import ScopedEventBus

            registry = SchemaRegistry()
            outbox = SqliteOutboxStore(self._db_path, registry)
            bus = ScopedEventBus()

            # ── Projections ──────────────────────────────────────────
            trace = TraceProjection(self._db_path)
            stats = StatsProjection()
            ws_gw = WsGateway()

            # Subscribe projections to the bus for live (non-durable) path.
            # Durable delivery goes through _deliver directly — see below.
            bus.subscribe("run.submitted.v1", trace.on_event, "trace")
            bus.subscribe("run.started.v1", trace.on_event, "trace")
            bus.subscribe("run.completed.v1", trace.on_event, "trace")
            bus.subscribe("run.failed.v1", trace.on_event, "trace")
            bus.subscribe("run.cancelled.v1", trace.on_event, "trace")
            bus.subscribe("run.blocked.v1", trace.on_event, "trace")
            bus.subscribe("run.gave_up.v1", trace.on_event, "trace")

            for et in registry.registered_types:
                if et.startswith("run."):
                    bus.subscribe(et, stats.on_event, "stats")
                    bus.subscribe(et, ws_gw.on_event, "ws_gateway")

            # ── G0: Durable delivery — direct projection calls ────────
            # bus.publish() silently swallows handler exceptions (P5 sync bus),
            # so we bypass it for durable delivery.  Projection failures MUST
            # propagate to the Relay so it can reschedule/DLQ.
            # G8 replaces this with a typed ProjectionDispatcher.
            def _deliver(record):
                """Durable delivery: decode → deliver to all projections directly.

                Exceptions propagate so the Relay reschedules instead of
                falsely marking the event as delivered (false ACK sealed).
                """
                envelope = registry.decode(record.payload_json)
                # Ensure session scope exists before delivering
                sid = envelope.scope.session_id
                if sid is not None:
                    bus.ensure_session(sid)
                # Direct projection calls — failures propagate (no silent swallow)
                trace.on_event(envelope)
                stats.on_event(envelope)
                ws_gw.on_event(envelope)

            relay = OutboxRelay(outbox, _deliver)
            runtime = AgentRuntime(RuntimePorts())
            coordinator = RunCoordinator(runtime, None)  # UoW injected per-request

            for k, v in [
                ("registry", registry), ("outbox", outbox), ("relay", relay),
                ("bus", bus), ("runtime", runtime), ("coordinator", coordinator),
                ("trace", trace), ("stats", stats), ("ws_gateway", ws_gw),
            ]:
                components[k] = v

        return components


def start_native_pipeline(db_path: str) -> dict:
    """Start the native event pipeline: OutboxRelay → Projections.

    G0: Acquires process-level owner guard.  Only one pipeline per DB path.
         Projection failures propagate to Relay (no false ACK).

    Call this once at server startup when GRACE_RUNTIME_MODE=NATIVE.
    Returns a dict with {'relay', 'bus', 'shutdown'} — call shutdown() to stop.
    """
    import logging
    logger = logging.getLogger(__name__)

    # G0: Acquire process-level owner guard (replaced by durable lease in G10)
    _acquire_owner(db_path)

    from application.events.schema_registry import SchemaRegistry
    from infrastructure.outbox.sqlite_store import SqliteOutboxStore
    from infrastructure.outbox.relay import OutboxRelay
    from listeners.trace_projection import TraceProjection
    from listeners.stats_projection import StatsProjection
    from listeners.ws_gateway import WsGateway
    from eventing.scoped_bus import ScopedEventBus

    registry = SchemaRegistry()
    outbox = SqliteOutboxStore(db_path, registry)
    bus = ScopedEventBus()

    # Install outbox DDL
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        SqliteOutboxStore.install(conn)
        conn.commit()
    finally:
        conn.close()

    # Projections
    trace = TraceProjection(db_path)
    stats = StatsProjection()
    ws_gw = WsGateway()

    # Subscribe projections to run events (live path — non-durable subscribers)
    for et in registry.registered_types:
        if et.startswith("run."):
            bus.subscribe(et, trace.on_event, "trace")
            bus.subscribe(et, stats.on_event, "stats")
            bus.subscribe(et, ws_gw.on_event, "ws_gateway")

    # G0: Durable delivery — direct projection calls.
    # bus.publish() silently swallows handler exceptions (P5 sync bus design),
    # so we bypass it for durable delivery.  Projection failures propagate
    # to the Relay for reschedule/DLQ instead of false ACK.
    # G8 replaces this with a typed ProjectionDispatcher.
    def _deliver(record):
        envelope = registry.decode(record.payload_json)
        sid = envelope.scope.session_id
        if sid is not None:
            bus.ensure_session(sid)
        # Direct projection calls — failures propagate (no silent swallow)
        trace.on_event(envelope)
        stats.on_event(envelope)
        ws_gw.on_event(envelope)

    relay = OutboxRelay(outbox, _deliver)
    relay.start()
    logger.info("Native event pipeline started (relay=%s, db=%s)",
                relay._worker_id, db_path)

    def shutdown():
        try:
            relay.stop()
            logger.info("Native event pipeline stopped (db=%s)", db_path)
        finally:
            # G0: Release owner guard so a new pipeline can start after shutdown
            _release_owner(db_path)

    return {"relay": relay, "bus": bus, "trace": trace, "stats": stats,
            "ws_gateway": ws_gw, "shutdown": shutdown}
