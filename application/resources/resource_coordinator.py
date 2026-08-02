"""
G36: Resource Coordinator — direct-call acquire/release, fact only on success.

- Resource lifecycle managed by Coordinator, not EventBus.
- Fact published only AFTER successful state transition.
- No EventBus-driven resource commands.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum


class ResourceState(StrEnum):
    FREE = "free"
    ACQUIRED = "acquired"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ResourceLease:
    resource_id: str
    owner_run_id: str
    acquired_at: float = 0.0


class ResourceCoordinator:
    """Direct-call resource management.  Facts emitted only on success.

    G36: Not EventBus-driven.  Coordinator owns resource state authority.
    """

    def __init__(self) -> None:
        self._resources: dict[str, ResourceLease] = {}
        self._lock = threading.Lock()
        self._acquisition_log: list[dict] = []

    def acquire(self, resource_id: str, run_id: str) -> ResourceLease | None:
        """Try to acquire a resource.  Returns None if already held.

        G36: Fact is recorded in _acquisition_log only on success.
        """
        with self._lock:
            existing = self._resources.get(resource_id)
            if existing is not None:
                return None  # already held

            lease = ResourceLease(
                resource_id=resource_id, owner_run_id=run_id,
            )
            self._resources[resource_id] = lease
            # Fact only on success
            self._acquisition_log.append({
                "action": "acquired", "resource_id": resource_id,
                "run_id": run_id,
            })
            return lease

    def release(self, resource_id: str, run_id: str) -> bool:
        """Release a resource.  Returns True if this run owned it."""
        with self._lock:
            existing = self._resources.get(resource_id)
            if existing and existing.owner_run_id == run_id:
                del self._resources[resource_id]
                self._acquisition_log.append({
                    "action": "released", "resource_id": resource_id,
                    "run_id": run_id,
                })
                return True
            return False

    def release_all(self, run_id: str) -> int:
        """Release all resources held by a run.  Returns count."""
        with self._lock:
            keys = [k for k, v in self._resources.items()
                    if v.owner_run_id == run_id]
            for k in keys:
                del self._resources[k]
                self._acquisition_log.append({
                    "action": "released", "resource_id": k, "run_id": run_id,
                })
            return len(keys)

    def is_acquired(self, resource_id: str) -> bool:
        with self._lock:
            return resource_id in self._resources

    @property
    def held_count(self) -> int:
        with self._lock:
            return len(self._resources)

    @property
    def acquisition_facts(self) -> list[dict]:
        """Facts emitted on successful acquire/release (read-only log)."""
        return list(self._acquisition_log)
