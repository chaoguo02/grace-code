"""
ApprovalBroker — synchronous blocking approval for headless Web mode.

This is the web-transport equivalent of Claude Code's headless
``control_request`` / ``control_response`` NDJSON protocol.

CC headless::

    # Agent thread blocks on stdin waiting for a control_response
    stdout: {"type":"control_request","request_id":"...","tool":"Edit",...}
    stdin:  {"type":"control_response","request_id":"...","decision":"allow"}

Forge Web::

    # Agent thread blocks on threading.Event, frontend wakes it via HTTP
    WS push:    {"type":"approval_required","request_id":"...","tool_name":"Edit",...}
    HTTP POST:  /api/sessions/{id}/tool-approve {"request_id":"...","decision":"allow"}
    Event.set() → Agent thread continues

The mechanism differs (stdin vs threading.Event) because the transport
differs (terminal vs Web), but the fundamental pattern is identical:
**the agent thread blocks synchronously until an external decision arrives**.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from hitl.pipeline import PromptAction, PromptDecision

logger = logging.getLogger(__name__)

# ── Default timeout matching CC's ~60s headless timeout ──────────────────
_DEFAULT_TIMEOUT = 60.0


@dataclass
class PendingApproval:
    """One tool call waiting for human approval.

    The ``event`` is the synchronisation primitive: the agent thread calls
    ``event.wait(timeout)`` and the HTTP handler calls ``event.set()``.
    """

    request_id: str
    tool_name: str
    params: dict[str, Any]
    thought: str
    event: threading.Event = field(default_factory=threading.Event)
    decision: PromptDecision | None = None
    created_at: float = field(default_factory=time.time)


class ApprovalBroker:
    """Per-session, thread-safe approval queue.

    One broker instance lives for the lifetime of a session.  The agent
    thread (background) pushes a request and blocks; the WebSocket / HTTP
    handler (main event loop) resolves it.

    Usage::

        broker = ApprovalBroker("abc123")

        # Agent thread
        decision = broker.wait_for_decision(request)   # blocks ≤ timeout

        # HTTP handler (different thread)
        broker.resolve(request_id, PromptDecision(ALLOW_ONCE))
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._pending: dict[str, PendingApproval] = {}
        self._lock = threading.Lock()
        self._timeout = _DEFAULT_TIMEOUT

    # ── Agent thread ─────────────────────────────────────────────────────

    def wait_for_decision(
        self,
        request: "ApprovalRequest",
        *,
        timeout: float | None = None,
        on_pending: "Callable[[str], None] | None" = None,
    ) -> PromptDecision:
        """Block the calling thread until a decision arrives or timeout.

        Called from the agent background thread.  This is the exact
        equivalent of CC's blocking ``stdin.readline()`` wait.

        The optional *on_pending* callback fires AFTER the request_id is
        assigned but BEFORE the thread blocks.  Use it to push the
        ``approval_required`` WS event (CC's ``control_request`` equivalent).

        Returns:
            PromptDecision with action ALLOW_ONCE or DENY.
        """
        req_id = uuid.uuid4().hex[:12]
        timeout = timeout if timeout is not None else self._timeout

        pending = PendingApproval(
            request_id=req_id,
            tool_name=request.tool_name,
            params=dict(request.params),
            thought=request.thought or "",
        )

        with self._lock:
            self._pending[req_id] = pending

        # Let the caller push the WS event BEFORE we block.
        request._request_id = req_id

        # CC control_request equivalent: push the event to the frontend
        if on_pending is not None:
            try:
                on_pending(req_id)
            except Exception:
                logger.debug("on_pending callback failed for %s", req_id, exc_info=True)

        logger.info(
            "ApprovalBroker waiting — id=%s tool=%s timeout=%.0fs",
            req_id, request.tool_name, timeout,
        )

        signaled = pending.event.wait(timeout=timeout)

        with self._lock:
            self._pending.pop(req_id, None)

        if not signaled:
            logger.warning("ApprovalBroker timeout — id=%s tool=%s", req_id, request.tool_name)
            return PromptDecision(
                action=PromptAction.DENY,
                note=f"Approval timed out after {timeout:.0f}s",
            )

        logger.info(
            "ApprovalBroker resolved — id=%s decision=%s",
            req_id,
            pending.decision.action.value if pending.decision else "?",
        )
        return pending.decision or PromptDecision(action=PromptAction.DENY)

    # ── HTTP handler (main thread / event loop) ──────────────────────────

    def resolve(self, request_id: str, decision: PromptDecision) -> bool:
        """Signal a pending approval with the user's decision.

        Called from the HTTP handler (or any thread).  This wakes the
        agent thread that is blocked in ``wait_for_decision()``.

        Returns:
            True if a matching pending request was found and resolved.
        """
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            logger.debug("ApprovalBroker resolve miss — id=%s", request_id)
            return False
        pending.decision = decision
        pending.event.set()
        return True

    # ── Introspection ────────────────────────────────────────────────────

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def session_id(self) -> str:
        return self._session_id


# ── Request wrapper (avoids coupling broker to pipeline internals) ────────


class ApprovalRequest:
    """Lightweight value object passed from pipeline to broker.

    Mirrors CC's ``control_request`` fields.
    """

    def __init__(
        self,
        tool_name: str,
        params: dict[str, Any],
        thought: str = "",
    ) -> None:
        self.tool_name = tool_name
        self.params = params
        self.thought = thought
        # Set by broker.wait_for_decision() before blocking
        self._request_id: str = ""

    @property
    def request_id(self) -> str:
        return self._request_id
