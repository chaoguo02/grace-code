"""
Bridge: old poll-based CancellationToken → new push-based CancellationHandle.

This adapter lets existing code (agent loop, LLM invoker, HITL) continue
using the old CancellationToken unchanged, while new infrastructure
(CancellationHandle, ProcessRegistry) receives the push-based signal.

Zero changes required to old code.  Remove this file when CancellationToken
is fully replaced by CancellationHandle.
"""

from __future__ import annotations

import threading
import time

from core.cancellation import CancellationHandle


def adapt_cancellation_token(
    old_token: object,
    *,
    poll_interval: float = 0.1,
) -> CancellationHandle:
    """Create a CancellationHandle that mirrors an old CancellationToken.

    The old token is polled every *poll_interval* seconds.  When it fires,
    the cancellation is PUSHED to the returned handle (callbacks run).

    If *old_token* is already cancelled, the returned handle is cancelled
    immediately.

    Args:
        old_token: Any object with ``is_cancelled`` (bool), ``reason``, and
                   ``detail`` attributes (the existing CancellationToken).

    Returns:
        A CancellationHandle that propagates the old token's cancel signal.
    """
    handle = CancellationHandle()

    # Already cancelled?  Propagate immediately.
    if getattr(old_token, "is_cancelled", False):
        reason = _safe_attr(old_token, "reason", "")
        detail = _safe_attr(old_token, "detail", "")
        handle.cancel(
            reason=str(reason) if reason else "",
            detail=str(detail) if detail else "",
        )
        return handle

    # Start background poller — daemon so it never blocks shutdown.
    def _poll() -> None:
        while not getattr(old_token, "is_cancelled", False):
            time.sleep(poll_interval)

        reason = _safe_attr(old_token, "reason", "")
        detail = _safe_attr(old_token, "detail", "")
        handle.cancel(
            reason=str(reason) if reason else "",
            detail=str(detail) if detail else "",
        )

    poller = threading.Thread(target=_poll, daemon=True, name="cancel-adapter")
    poller.start()
    return handle


def _safe_attr(obj: object, attr: str, default: object) -> object:
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default
