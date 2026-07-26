"""Thread-safe, expiring ownership leases used by the shared task board."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
import uuid
from typing import Callable


@dataclass(frozen=True)
class Lease:
    resource_id: str
    owner_id: str
    token: str
    acquired_at: float
    expires_at: float

    def valid_at(self, now: float) -> bool:
        return now < self.expires_at


class LeaseManager:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._leases: dict[str, Lease] = {}
        self._lock = threading.RLock()

    def acquire(self, resource_id: str, owner_id: str, ttl_seconds: float) -> Lease | None:
        if not resource_id or not owner_id:
            raise ValueError("resource_id and owner_id are required")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        with self._lock:
            now = self._clock()
            current = self._leases.get(resource_id)
            if current is not None and current.valid_at(now):
                return current if current.owner_id == owner_id else None
            lease = Lease(
                resource_id=resource_id,
                owner_id=owner_id,
                token=uuid.uuid4().hex,
                acquired_at=now,
                expires_at=now + ttl_seconds,
            )
            self._leases[resource_id] = lease
            return lease

    def renew(self, token: str, ttl_seconds: float) -> Lease | None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        with self._lock:
            now = self._clock()
            current = next(
                (lease for lease in self._leases.values() if lease.token == token),
                None,
            )
            if current is None or not current.valid_at(now):
                return None
            renewed = Lease(
                resource_id=current.resource_id,
                owner_id=current.owner_id,
                token=current.token,
                acquired_at=current.acquired_at,
                expires_at=now + ttl_seconds,
            )
            self._leases[current.resource_id] = renewed
            return renewed

    def release(self, token: str) -> bool:
        with self._lock:
            resource = next(
                (
                    resource_id
                    for resource_id, lease in self._leases.items()
                    if lease.token == token
                ),
                None,
            )
            if resource is None:
                return False
            del self._leases[resource]
            return True

    def get(self, resource_id: str) -> Lease | None:
        with self._lock:
            lease = self._leases.get(resource_id)
            if lease is not None and not lease.valid_at(self._clock()):
                del self._leases[resource_id]
                return None
            return lease

