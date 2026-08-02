"""
G8: DeliveryOutcome — typed sum type for projection delivery results.

- Delivered: all required projections acknowledged.
- RetryableDeliveryFailure: transient error, reschedule.
- PermanentDeliveryFailure: unrecoverable, move to DLQ.

Each ProjectionReceipt records an individual projection's outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ── Projection receipt ─────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ProjectionReceipt:
    """Individual projection delivery result."""
    projection_name: str
    event_id: str
    success: bool
    error: str = ""


@dataclass(frozen=True, slots=True)
class Delivered:
    """All required projections succeeded.  Best-effort errors recorded."""
    receipts: tuple[ProjectionReceipt, ...] = ()
    best_effort_errors: int = 0


@dataclass(frozen=True, slots=True)
class RetryableDeliveryFailure:
    """At least one required projection failed transiently."""
    reason: str
    retry_after_ms: int = 1000
    receipts: tuple[ProjectionReceipt, ...] = ()
    failed_projection: str = ""


@dataclass(frozen=True, slots=True)
class PermanentDeliveryFailure:
    """Unrecoverable — unknown schema, poison payload, or max retries."""
    reason: str
    receipts: tuple[ProjectionReceipt, ...] = ()


DeliveryOutcome = Delivered | RetryableDeliveryFailure | PermanentDeliveryFailure


# ── Helpers ─────────────────────────────────────────────────────────────────

def merge_receipts(
    receipts: list[ProjectionReceipt],
    required_names: set[str],
    best_effort_names: set[str],
) -> DeliveryOutcome:
    """Merge individual receipts into a DeliveryOutcome.

    - Any required projection failure → RetryableDeliveryFailure
    - All required OK → Delivered (best-effort errors counted)
    - Permanent errors in required → PermanentDeliveryFailure
    """
    best_effort_errors = 0
    for r in receipts:
        if not r.success:
            if r.projection_name in required_names:
                if "permanent" in r.error.lower() or "unknown schema" in r.error.lower():
                    return PermanentDeliveryFailure(
                        reason=f"Required projection {r.projection_name} permanent failure: {r.error}",
                        receipts=tuple(receipts),
                    )
                return RetryableDeliveryFailure(
                    reason=f"Required projection {r.projection_name} failed: {r.error}",
                    receipts=tuple(receipts),
                    failed_projection=r.projection_name,
                )
            elif r.projection_name in best_effort_names:
                best_effort_errors += 1

    return Delivered(
        receipts=tuple(receipts),
        best_effort_errors=best_effort_errors,
    )
