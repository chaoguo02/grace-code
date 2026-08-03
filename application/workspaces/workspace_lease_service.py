"""
G34: Workspace Lease Service — acquire/release/expire, conflict serialization.

- Coordinator owns lease lifecycle; Runtime uses WorkspacePort only.
- Write conflicts serialized by lease key (same key + write → blocked).
- Read leases shared; write lease blocks all others.
- release_all() cleans up all leases for a terminated run.
- Lease expiry prevents zombie leases from crashed runs.
"""

from __future__ import annotations

import threading
import time as _time
from dataclasses import dataclass
from enum import StrEnum


class LeaseMode(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    """Immutable lease record."""
    key: str
    owner_run_id: str
    mode: LeaseMode = LeaseMode.READ
    acquired_at: float = 0.0
    expires_at: float = 0.0

    @property
    def expired(self) -> bool:
        return _time.monotonic() > self.expires_at


class WorkspaceLeaseService:
    """Manages workspace leases with conflict detection and expiry.

    G34: Coordinator manages leases; Runtime only sees WorkspacePort.
         Write conflicts → serialized execution.
         Crashed run leases expire and can be taken over.
    """

    DEFAULT_LEASE_S = 300.0  # 5 minutes

    def __init__(self) -> None:
        # key → list of active leases (multiple readers share same key)
        self._leases: dict[str, list[WorkspaceLease]] = {}
        self._lock = threading.Lock()

    # ── Acquire ───────────────────────────────────────────────────────

    def acquire(self, key: str, run_id: str,
                mode: str = "read",
                lease_s: float = DEFAULT_LEASE_S) -> WorkspaceLease | None:
        """Try to acquire a lease.  Returns None on conflict.

        Rules:
          - No existing lease → acquire succeeds.
          - Existing read lease + new read → shared (both granted).
          - Existing write lease + anything → conflict (return None).
          - Existing read lease + new write → conflict (return None).
          - Expired lease → takeover (replace with new lease).
        """
        mode_enum = LeaseMode(mode)
        now = _time.monotonic()

        with self._lock:
            existing_list = self._leases.get(key, [])

            # Check for conflicts with non-expired leases
            for existing in existing_list:
                if existing.expired:
                    continue
                if existing.mode == LeaseMode.WRITE:
                    return None  # write blocks everything
                if mode_enum == LeaseMode.WRITE:
                    return None  # can't upgrade to write while readers exist

            lease = WorkspaceLease(
                key=key, owner_run_id=run_id, mode=mode_enum,
                acquired_at=now, expires_at=now + lease_s,
            )
            if key not in self._leases:
                self._leases[key] = []
            self._leases[key].append(lease)
            return lease

    def acquire_many(self, keys: list[str], run_id: str,
                     mode: str = "read") -> list[WorkspaceLease]:
        """Acquire leases for multiple keys.  Returns only successful leases."""
        results = []
        for key in keys:
            lease = self.acquire(key, run_id, mode=mode)
            if lease is not None:
                results.append(lease)
        return results

    # ── Release ───────────────────────────────────────────────────────

    def release(self, key: str, run_id: str) -> bool:
        """Release a specific lease.  Returns True if found and released."""
        with self._lock:
            existing_list = self._leases.get(key, [])
            for i, lease in enumerate(existing_list):
                if lease.owner_run_id == run_id:
                    existing_list.pop(i)
                    if not existing_list:
                        del self._leases[key]
                    return True
            return False

    def release_all(self, run_id: str) -> int:
        """Release all leases held by *run_id*.  Returns count released."""
        count = 0
        with self._lock:
            for key in list(self._leases.keys()):
                before = len(self._leases[key])
                self._leases[key] = [
                    v for v in self._leases[key]
                    if v.owner_run_id != run_id
                ]
                removed = before - len(self._leases[key])
                count += removed
                if not self._leases[key]:
                    del self._leases[key]
        return count

    # ── Query ─────────────────────────────────────────────────────────

    def list_for_run(self, run_id: str) -> list[WorkspaceLease]:
        """All active leases for a run."""
        with self._lock:
            result = []
            for lease_list in self._leases.values():
                for lease in lease_list:
                    if lease.owner_run_id == run_id:
                        result.append(lease)
            return result

    def check_conflict(self, key: str, mode: str = "write") -> bool:
        """True if acquiring *key* with *mode* would conflict."""
        mode_enum = LeaseMode(mode)
        with self._lock:
            for existing in self._leases.get(key, []):
                if existing.expired:
                    continue
                if existing.mode == LeaseMode.WRITE:
                    return True
                if mode_enum == LeaseMode.WRITE:
                    return True
            return False

    def expire_stale(self) -> int:
        """Remove all expired leases.  Returns count cleaned."""
        count = 0
        with self._lock:
            for key in list(self._leases.keys()):
                before = len(self._leases[key])
                self._leases[key] = [
                    v for v in self._leases[key] if not v.expired
                ]
                removed = before - len(self._leases[key])
                count += removed
                if not self._leases[key]:
                    del self._leases[key]
        return count

    @property
    def active_lease_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._leases.values())
