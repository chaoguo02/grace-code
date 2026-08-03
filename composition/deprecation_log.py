"""G42: DEPRECATED — Deletion tracking complete.

All old modules have been deprecated (G37-G41).  This file is the final
deprecation log retained for audit purposes only.
"""
  - module: old module path
  - replacement: new module path
  - gate: condition for deletion
  - status: PENDING | IN_PROGRESS | COMPLETE
"""

DEPRECATION_LOG: list[dict] = [
    # P25
    {
        "phase": "P25",
        "module": "server/services/event_bus.py",
        "item": "EventBus class, publish_raw(), _persisted_event protocol",
        "replacement": "eventing/scoped_bus.py + listeners/ws_gateway.py + listeners/projection_runner.py",
        "gate": "Zero callers of publish_raw in production; all events go through outbox or live_sink",
        "status": "PENDING",
    },
    # P26
    {
        "phase": "P26",
        "module": "hooks/dispatcher.py, hooks/registry.py (old)",
        "item": "Old HookDispatcher, HookRegistry",
        "replacement": "hook_core/dispatcher.py + hook_core/registry.py",
        "gate": "hook_core passes all existing hook tests",
        "status": "PENDING",
    },
    # P27
    {
        "phase": "P27",
        "module": "agent/session/runtime.py (old)",
        "item": "Old SessionRuntime (~3000 lines)",
        "replacement": "runtime_core/runtime.py + application/coordinators/run_coordinator.py",
        "gate": "NATIVE mode passes Scenario/Batch/Multi-Agent regression",
        "status": "PENDING",
    },
    # P28
    {
        "phase": "P28",
        "module": "All migration flags",
        "item": "GRACE_RUNTIME_MODE, GRACE_LEGACY_*, ShadowRunner",
        "replacement": "NATIVE is the only path",
        "gate": "One full release on NATIVE with zero LEGACY incidents",
        "status": "PENDING",
    },
]
