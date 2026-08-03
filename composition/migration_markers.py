"""G42: DEPRECATED — Native paths are now authoritative (G28-G31).

Migration complete.  This file is retained for audit trail only.
GRACE_RUNTIME_MODE is no longer checked in production code paths.
"""
  - When (after which verification gate)
  - How to roll back
"""

MIGRATION_PLAN = {
    "P23": {
        "target": "server/services/agent_service.py",
        "action": "Split into AgentService (thin API) + AgentCoordinator (business logic)",
        "gate": "NATIVE mode passes 100% of existing integration tests",
        "rollback": "Restore agent_service.py from git",
    },
    "P24": {
        "target": "server/services/agent_service.py (remainder)",
        "action": "Extract memory maintenance, compaction, session context injection",
        "gate": "All extracted modules have independent unit tests",
        "rollback": "Inline the extracted code back",
    },
    "P25": {
        "target": "server/services/event_bus.py",
        "action": "Delete EventBus class; replace with ScopedEventBus + LiveEventSink",
        "gate": "Zero publish_raw callers in production code",
        "rollback": "Restore event_bus.py; clients use old import path",
    },
    "P26": {
        "target": "hooks/dispatcher.py, hooks/registry.py (old)",
        "action": "Delete old hooks; replace with hook_core/",
        "gate": "hook_core passes all existing hook tests",
        "rollback": "Restore old hooks/ directory",
    },
    "P27": {
        "target": "agent/session/runtime.py (old)",
        "action": "Delete old Runtime; replace with runtime_core/ + coordinators",
        "gate": "NATIVE mode runs 100% of Scenario/Batch/Multi-Agent tests",
        "rollback": "Restore runtime.py; set GRACE_RUNTIME_MODE=LEGACY",
    },
    "P28": {
        "target": "All migration flags + compatibility code",
        "action": "Delete GRACE_RUNTIME_MODE, GRACE_LEGACY_*, ShadowRunner; final cleanup",
        "gate": "One full release cycle on NATIVE with zero LEGACY fallback incidents",
        "rollback": "Restore deleted files from git; re-add flags",
    },
}
