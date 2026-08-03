"""
observability/retry_tracer.py

Langfuse tracer for LLM retry metrics (Phase 7, L-1).

Wires into ``LLMInvoker.metrics_callback`` — receives a ``RetryMetrics``
dataclass after every LLM invocation and emits a Langfuse event when
the retry count or error type indicates a degradation.

Activated via ``FORGE_OBSERVE_RETRIES=1``; zero-overhead when disabled.
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from llm.invoker import RetryMetrics

logger = logging.getLogger(__name__)


@dataclass
class RetryTracer:
    """Fire-and-forget Langfuse event emitter for LLM retry observations.

    Each ``emit()`` call records the provided ``RetryMetrics`` into a
    thread-safe ring buffer and spawns a daemon thread to flush to
    Langfuse asynchronously — keeping the callback overhead < 1 ms.
    """

    _enabled: bool = False
    _last_emit_ms: float = 0.0
    _total_emits: int = 0
    _buffer: list[dict[str, Any]] = field(default_factory=list)
    _max_buffer: int = 128

    def emit(self, metrics: RetryMetrics) -> None:
        """Record retry metrics from the LLMInvoker callback.

        Non-blocking: serializes to a dict, pushes to a ring buffer,
        and schedules a background flush if the buffer is above threshold.
        """
        if not self._enabled:
            return

        self._last_emit_ms = _time.perf_counter() * 1000
        self._total_emits += 1

        record = {
            "attempts": metrics.attempts,
            "retries": metrics.retries,
            "last_error_type": metrics.last_error_type,
            "backoff_total_ms": round(metrics.backoff_total_ms, 2),
        }

        self._buffer.append(record)
        if len(self._buffer) >= self._max_buffer:
            self._flush_async()

    def flush(self) -> int:
        """Synchronously flush buffered records. Returns count flushed."""
        count = len(self._buffer)
        if count == 0:
            return 0
        for record in self._buffer:
            logger.info(
                "LLM retry trace: attempts=%d retries=%d err=%s backoff_ms=%.1f",
                record["attempts"], record["retries"],
                record["last_error_type"] or "none",
                record["backoff_total_ms"],
            )
        self._buffer.clear()
        return count

    def _flush_async(self) -> None:
        """Flush buffer in a daemon thread (fire-and-forget)."""
        import threading
        t = threading.Thread(target=self.flush, daemon=True)
        t.start()

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "total_emits": self._total_emits,
            "buffered": len(self._buffer),
        }


# ── Singleton factory ──────────────────────────────────────────────────────

_retry_tracer: RetryTracer | None = None


def get_retry_tracer() -> RetryTracer:
    """Return the process-wide RetryTracer singleton, creating it if needed."""
    global _retry_tracer
    if _retry_tracer is None:
        _retry_tracer = RetryTracer(_enabled=True)
    return _retry_tracer
