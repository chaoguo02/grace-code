"""core/provider_governor.py

Provider-level rate limiting and shared backoff (Phase 3).

Per-provider (openai, deepseek, groq, etc.):
  - RPM sliding window
  - TPM token bucket
  - Concurrent call semaphore
  - Shared backoff after 429 (avoids retry storms from independent agents)

Usage:
    pg = ProviderGovernor()
    limiter = pg.get_limiter("deepseek")
    if not limiter.acquire():
        raise ResourceExhausted("provider throttled")
    try:
        response = backend.complete(...)
        pg.record_response("deepseek", status, headers, tokens)
    finally:
        limiter.release(tokens_used)
"""

from __future__ import annotations

import logging
import threading
import time as _time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ProviderRateLimiter
# ---------------------------------------------------------------------------


@dataclass
class ProviderRateLimiter:
    """Per-provider rate limiter: RPM sliding window + TPM token bucket + semaphore."""

    rpm_limit: int = 0           # 0 = unlimited
    tpm_limit: int = 0           # 0 = unlimited
    max_concurrent: int = 0      # 0 = unlimited

    # Internal — post_init initializes
    _semaphore: Optional[threading.BoundedSemaphore] = field(default=None, repr=False)
    _request_times: deque[float] = field(default_factory=deque, repr=False)
    _token_bucket: float = field(default=0.0, repr=False)
    _token_refill_ts: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _shared_backoff_until: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        if self.max_concurrent > 0:
            self._semaphore = threading.BoundedSemaphore(self.max_concurrent)
        self._token_refill_ts = _time.monotonic()
        self._token_bucket = (
            float(self.tpm_limit) if self.tpm_limit > 0 else float("inf")
        )

    # ── Public API ──────────────────────────────────────────────────────

    def acquire(self, tokens: int = 0) -> bool:
        """Try to acquire a request slot. Returns False if throttled."""
        now = _time.monotonic()

        # Shared backoff: if any caller got a 429, everyone waits
        with self._lock:
            if now < self._shared_backoff_until:
                return False

        # Concurrent limit
        semaphore_acquired = False
        if self._semaphore is not None:
            if not self._semaphore.acquire(blocking=False):
                return False
            semaphore_acquired = True

        # RPM check
        with self._lock:
            if self.rpm_limit > 0:
                # Purge old entries outside the 60s window
                cutoff = now - 60.0
                while self._request_times and self._request_times[0] < cutoff:
                    self._request_times.popleft()
                if len(self._request_times) >= self.rpm_limit:
                    if semaphore_acquired:
                        self._semaphore.release()
                    return False

            # TPM check
            if self.tpm_limit > 0:
                self._refill_tokens(now)
                if tokens > self._token_bucket:
                    if semaphore_acquired:
                        self._semaphore.release()
                    return False
                self._token_bucket -= tokens
            if self.rpm_limit > 0:
                self._request_times.append(now)

        return True

    def acquire_wait(
        self,
        tokens: int = 0,
        *,
        timeout_s: float = 60.0,
        cancellation_token: Any = None,
    ) -> bool:
        """Wait boundedly for provider capacity without retry storms."""
        deadline = _time.monotonic() + max(0.0, timeout_s)
        while True:
            if self.acquire(tokens=tokens):
                return True
            if (
                cancellation_token is not None
                and getattr(cancellation_token, "is_cancelled", False)
            ):
                return False
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return False
            _time.sleep(min(0.05, remaining))

    def release(
        self,
        tokens_used: int = 0,
        *,
        reserved_tokens: int = 0,
    ) -> None:
        """Release concurrency and reconcile a prior TPM reservation."""
        if self.tpm_limit > 0 and reserved_tokens > tokens_used:
            with self._lock:
                self._token_bucket = min(
                    float(self.tpm_limit),
                    self._token_bucket + reserved_tokens - tokens_used,
                )
        if self._semaphore is not None:
            try:
                self._semaphore.release()
            except ValueError:
                pass  # already released or never acquired

    def record_429(self, retry_after_s: float = 1.0) -> None:
        """Record a 429 — set shared backoff deadline."""
        deadline = _time.monotonic() + max(1.0, retry_after_s)
        with self._lock:
            self._shared_backoff_until = max(self._shared_backoff_until, deadline)
        logger.warning(
            "Provider rate limited (429) — shared backoff until %.0fs from now",
            max(0, deadline - _time.monotonic()),
        )

    # ── Internal ────────────────────────────────────────────────────────

    def _refill_tokens(self, now: float) -> None:
        """Refill TPM token bucket at a linear rate."""
        if self.tpm_limit <= 0:
            self._token_bucket = float("inf")
            return
        elapsed = now - self._token_refill_ts
        if elapsed >= 60.0:
            self._token_bucket = float(self.tpm_limit)
            self._token_refill_ts = now
        else:
            refill = (elapsed / 60.0) * self.tpm_limit
            self._token_bucket = min(float(self.tpm_limit), self._token_bucket + refill)
            self._token_refill_ts = now


# ---------------------------------------------------------------------------
# ProviderGovernor
# ---------------------------------------------------------------------------


class ProviderGovernor:
    """Manages rate limiters per provider name.

    Thread-safe: all operations are safe to call from any thread.
    """

    def __init__(self, config: object | None = None) -> None:
        self._limiters: dict[str, ProviderRateLimiter] = {}
        self._lock = threading.Lock()
        self._config = config

    def get_limiter(self, provider: str) -> ProviderRateLimiter:
        """Get or create a rate limiter for *provider*."""
        key = provider.lower().strip()
        with self._lock:
            if key not in self._limiters:
                self._limiters[key] = self._create_limiter(key)
            return self._limiters[key]

    def record_response(
        self,
        provider: str,
        status: int,
        tokens: int = 0,
        retry_after: float = 0.0,
    ) -> None:
        """Record a provider response. Triggers 429 backoff if needed."""
        if status == 429:
            limiter = self.get_limiter(provider)
            limiter.record_429(retry_after if retry_after > 0 else 1.0)

    def _create_limiter(self, provider: str) -> ProviderRateLimiter:
        """Create a limiter from config (or defaults)."""
        rpm = 0
        tpm = 0
        concurrent = 0
        if self._config is not None:
            pg_cfg = getattr(self._config, "provider", None)
            if pg_cfg is not None:
                if getattr(pg_cfg, "rate_limit_enabled", False):
                    rpm = max(0, int(getattr(pg_cfg, "rpm", 0)))
                    tpm = max(0, int(getattr(pg_cfg, "tpm", 0)))
                    concurrent = max(
                        0, int(getattr(pg_cfg, "max_concurrent", 0)),
                    )
        return ProviderRateLimiter(
            rpm_limit=rpm,
            tpm_limit=tpm,
            max_concurrent=concurrent if concurrent > 0 else 0,
        )
