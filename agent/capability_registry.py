"""Dynamic Capability Registry — Runtime-enforced tool availability.

Claude Code philosophy: "Context as Code." The model should never see tools it
cannot use. State belongs to the Runtime, not the model.

Four key design decisions (P1-6 corrections):
1. HALF_OPEN + exponential backoff — retry_at timestamps, probe-then-promote
2. Error dedup + hard blocking — structured first error, minimal repeats, N→abort
3. CapabilityKey scoping — process/user/session isolation, no cross-contamination
4. Graceful degradation — Runtime provides alternatives, model doesn't guess
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── CapabilityState (with HALF_OPEN) ──

class CapabilityState(str, Enum):
    ACTIVE = "active"            # Tool is working — appears in function definitions
    HALF_OPEN = "half_open"      # Probing — allow ONE execution to test recovery
    CIRCUIT_OPEN = "circuit_open"  # Tripped — blocked, retry_at governs re-probe
    UNAVAILABLE = "unavailable"  # Permanent — missing dependency, won't recover


# ── CapabilityKey (scoped isolation) ──

@dataclass(frozen=True)
class CapabilityKey:
    """Scoped key for a capability. Prevents cross-session contamination.

    scope_type="process" → global (MCP server down → all sessions affected)
    scope_type="user"    → per-user (OAuth expiry → one user affected)
    scope_type="session" → per-session (transient network hiccup)
    """

    capability: str
    scope_type: str = "process"
    scope_id: str = ""   # user_id or session_id

    def __str__(self) -> str:
        if self.scope_id:
            return f"{self.scope_type}:{self.scope_id}:{self.capability}"
        return f"{self.scope_type}:{self.capability}"


# ── CapabilityFallback (graceful degradation) ──

@dataclass(frozen=True)
class CapabilityFallback:
    """Alternative when a tool is unavailable."""

    tool: str       # alternative tool name
    usage: str = "" # brief instruction: "Use `cat` for read-only access"


# ── Capability ──

@dataclass
class Capability:
    """Metadata about a single tool's availability."""

    key: CapabilityKey
    state: CapabilityState = CapabilityState.ACTIVE
    reason: str = ""              # Why unavailable
    source: str = "builtin"       # builtin | mcp | dynamic
    risk_level: str = "low"
    fallbacks: tuple[CapabilityFallback, ...] = ()  # alternatives when blocked
    retry_at: float = 0.0         # epoch seconds — when HALF_OPEN probe is allowed
    backoff_count: int = 0        # number of consecutive failures (for 2^n backoff)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.key.capability,
            "scope": f"{self.key.scope_type}:{self.key.scope_id}" if self.key.scope_id else self.key.scope_type,
            "state": self.state.value,
            "reason": self.reason,
            "source": self.source,
            "backoff_count": self.backoff_count,
            "retry_in_s": max(0.0, self.retry_at - _time.time()) if self.retry_at else 0.0,
        }


# ── InterceptResult (structured feedback, not silent drop) ──

@dataclass
class InterceptResult:
    """Result of a capability interception check.

    If blocked=True, the caller MUST NOT execute the tool. The feedback
    dict is injected into the conversation as structured context.
    """

    blocked: bool
    feedback: dict[str, Any] = field(default_factory=dict)
    """Structured JSON for the model — never silent, never verbose on repeat."""


# ── CapabilityRegistry ──

# Maximum intercepts per (key, session) before hard-blocking
_MAX_INTERCEPTS_BEFORE_HARD_BLOCK = 5
# Base backoff in seconds (multiplied by 2^backoff_count)
_BASE_BACKOFF_SECONDS = 2.0


@dataclass
class CapabilityRegistry:
    """Runtime authority for tool availability.

    Corrected P1-6 architecture:
    - HALF_OPEN probes: exponential backoff, automatic promotion on success
    - Error dedup: full feedback first time, minimal on repeat, hard block at N
    - Scope isolation: process/user/session keys prevent cross-contamination
    - Graceful degradation: registered fallbacks guide the model
    """

    _capabilities: dict[CapabilityKey, Capability] = field(default_factory=dict)
    _intercept_counts: dict[tuple[CapabilityKey, str], int] = field(default_factory=dict)
    """Per-(key, session_id) intercept counter for dedup + hard blocking."""

    # ── Registration ──

    def register(
        self,
        key: CapabilityKey | str,
        source: str = "builtin",
        risk_level: str = "low",
        fallbacks: tuple[CapabilityFallback, ...] = (),
    ) -> Capability:
        """Register a tool as ACTIVE. Accepts str or CapabilityKey for convenience."""
        if isinstance(key, str):
            key = CapabilityKey(capability=key, scope_type="process")
        cap = Capability(
            key=key,
            state=CapabilityState.ACTIVE,
            source=source,
            risk_level=risk_level,
            fallbacks=fallbacks,
        )
        self._capabilities[key] = cap
        return cap

    def register_bulk(
        self,
        names: set[str] | frozenset[str],
        source: str = "builtin",
    ) -> None:
        for name in names:
            self.register(CapabilityKey(capability=name, scope_type="process"), source=source)

    # ── State mutations (with exponential backoff) ──

    def mark_unavailable(self, key: CapabilityKey | str, reason: str, permanent: bool = False) -> None:
        """Mark a tool as unavailable.

        If permanent=False (default), uses CIRCUIT_OPEN with exponential backoff.
        If permanent=True (missing dependency), uses UNAVAILABLE (no auto-recovery).
        """
        if isinstance(key, str):
            key = CapabilityKey(capability=key, scope_type="process")
        cap = self._capabilities.get(key)
        if cap is None:
            cap = self.register(key, source="mcp")

        if permanent:
            cap.state = CapabilityState.UNAVAILABLE
        else:
            cap.state = CapabilityState.CIRCUIT_OPEN
            cap.backoff_count += 1
            # Exponential backoff: 2^count * base
            delay = _BASE_BACKOFF_SECONDS * (2 ** (cap.backoff_count - 1))
            cap.retry_at = _time.time() + delay

        cap.reason = reason
        logger.warning(
            "Capability %s marked %s (backoff=%d, retry_in=%.1fs): %s",
            key, cap.state.value, cap.backoff_count,
            max(0.0, cap.retry_at - _time.time()), reason,
        )

    def mark_circuit_open(self, key: CapabilityKey | str, reason: str) -> None:
        """Explicitly block a capability with CIRCUIT_OPEN + exponential backoff."""
        self.mark_unavailable(key, reason, permanent=False)

    def mark_available(self, key: CapabilityKey | str) -> None:
        """Restore a tool to ACTIVE. Resets backoff."""
        if isinstance(key, str):
            key = CapabilityKey(capability=key, scope_type="process")
        cap = self._capabilities.get(key)
        if cap is None:
            cap = self.register(key)
        cap.state = CapabilityState.ACTIVE
        cap.reason = ""
        cap.backoff_count = 0
        cap.retry_at = 0.0
        logger.info("Capability %s restored to ACTIVE", key)

    # ── HALF_OPEN probing ──

    def try_probe(self, key: CapabilityKey | str) -> bool:
        """Check if we should attempt a HALF_OPEN probe.

        Returns True if: the tool is CIRCUIT_OPEN AND retry_at has passed.
        Caller should attempt a real execution — if it succeeds, call
        mark_available(); if it fails, call mark_unavailable() again.
        """
        if isinstance(key, str):
            key = CapabilityKey(capability=key, scope_type="process")
        cap = self._capabilities.get(key)
        if cap is None:
            return False
        if cap.state != CapabilityState.CIRCUIT_OPEN:
            return False
        if cap.retry_at > 0 and _time.time() < cap.retry_at:
            return False  # backoff hasn't expired yet

        # Transition to HALF_OPEN — allow one probe
        cap.state = CapabilityState.HALF_OPEN
        logger.info("Capability %s → HALF_OPEN (probing)", key)
        return True

    def confirm_probe_failed(self, key: CapabilityKey | str, reason: str) -> None:
        """Called when a HALF_OPEN probe failed. Returns to CIRCUIT_OPEN."""
        if isinstance(key, str):
            key = CapabilityKey(capability=key, scope_type="process")
        cap = self._capabilities.get(key)
        if cap is None:
            return
        cap.state = CapabilityState.CIRCUIT_OPEN
        cap.backoff_count += 1
        delay = _BASE_BACKOFF_SECONDS * (2 ** (cap.backoff_count - 1))
        cap.retry_at = _time.time() + delay
        cap.reason = reason
        logger.warning(
            "Capability %s HALF_OPEN probe failed (backoff=%d, retry_in=%.1fs): %s",
            key, cap.backoff_count, delay, reason,
        )

    # ── Interception with dedup (structured feedback, never silent) ──

    def intercept(
        self,
        key: CapabilityKey | str,
        session_id: str = "",
    ) -> InterceptResult:
        """Check if a tool call should be blocked. Returns structured feedback.

        Dedup logic:
        - First intercept per (key, session): full structured feedback with reasons + alternatives
        - Repeat intercept: minimal feedback to avoid context bloat
        - After _MAX_INTERCEPTS_BEFORE_HARD_BLOCK: raises InterceptHardBlock
        """
        if isinstance(key, str):
            key = CapabilityKey(capability=key, scope_type="process")

        cap = self._capabilities.get(key)

        # Unknown tool — not in registry
        if cap is None:
            return InterceptResult(blocked=False)

        # ACTIVE — let it through
        if cap.state == CapabilityState.ACTIVE:
            return InterceptResult(blocked=False)

        # HALF_OPEN — let the probe through
        if cap.state == CapabilityState.HALF_OPEN:
            return InterceptResult(blocked=False)

        # ── Blocked — build structured feedback ──
        count_key = (key, session_id)
        count = self._intercept_counts.get(count_key, 0) + 1
        self._intercept_counts[count_key] = count

        # Hard block after N intercepts (>= so it triggers at exactly N)
        if count >= _MAX_INTERCEPTS_BEFORE_HARD_BLOCK:
            logger.error(
                "Capability %s hard-blocked after %d intercepts in session %s",
                key, count, session_id,
            )
            raise InterceptHardBlock(
                f"Tool '{key.capability}' has been blocked {count} times. "
                f"Session cannot continue. Reason: {cap.reason}"
            )

        # First intercept: full structured feedback
        if count == 1:
            feedback = {
                "status": "unavailable",
                "tool": key.capability,
                "state": cap.state.value,
                "reason": cap.reason,
                "retryable": cap.state != CapabilityState.UNAVAILABLE,
            }
            if cap.fallbacks:
                feedback["alternatives"] = [
                    {"tool": f.tool, "usage": f.usage} for f in cap.fallbacks
                ]
            logger.debug("Capability %s first intercept (session=%s): %s", key, session_id, cap.reason)
        else:
            # Repeat intercept: minimal feedback — avoid context bloat
            feedback = {
                "status": "blocked",
                "tool": key.capability,
                "reported_before": True,
                "count": count,
            }

        return InterceptResult(blocked=True, feedback=feedback)

    def reset_intercept_count(self, key: CapabilityKey | str, session_id: str) -> None:
        """Reset the intercept counter (e.g., after a tool becomes available again)."""
        if isinstance(key, str):
            key = CapabilityKey(capability=key, scope_type="process")
        self._intercept_counts.pop((key, session_id), None)

    # ── Queries ──

    def is_available(self, key: CapabilityKey | str) -> bool:
        """Check if a tool is currently callable (ACTIVE or HALF_OPEN).

        IMPORTANT: Unknown keys (never registered) default to AVAILABLE.
        The registry is a blocklist — only explicitly blocked tools are unavailable.
        This prevents unregistered subagent types from being filtered out.
        """
        if isinstance(key, str):
            key = CapabilityKey(capability=key, scope_type="process")
        cap = self._capabilities.get(key)
        if cap is None:
            return True  # not registered = not blocked
        return cap.state in (CapabilityState.ACTIVE, CapabilityState.HALF_OPEN)

    def get_state(self, key: CapabilityKey | str) -> CapabilityState | None:
        if isinstance(key, str):
            key = CapabilityKey(capability=key, scope_type="process")
        cap = self._capabilities.get(key)
        return cap.state if cap else None

    def get_reason(self, key: CapabilityKey | str) -> str:
        if isinstance(key, str):
            key = CapabilityKey(capability=key, scope_type="process")
        cap = self._capabilities.get(key)
        return cap.reason if cap else ""

    def get_active_tool_names(self) -> set[str]:
        """Return tool names that are ACTIVE or HALF_OPEN (visible to model)."""
        return {
            cap.key.capability
            for cap in self._capabilities.values()
            if cap.state in (CapabilityState.ACTIVE, CapabilityState.HALF_OPEN)
        }

    def get_unavailable_summary(self) -> list[dict[str, str]]:
        return [
            {"name": str(cap.key), "reason": cap.reason, "state": cap.state.value}
            for cap in self._capabilities.values()
            if cap.state not in (CapabilityState.ACTIVE, CapabilityState.HALF_OPEN)
        ]

    # ── Serialization ──

    def to_summary(self) -> dict[str, Any]:
        return {
            "total": len(self._capabilities),
            "active": len(self.get_active_tool_names()),
            "unavailable": len(self.get_unavailable_summary()),
            "capabilities": {
                str(cap.key): cap.to_dict()
                for cap in sorted(self._capabilities.values(), key=lambda c: str(c.key))
            },
        }


# ── InterceptHardBlock ──

class InterceptHardBlock(Exception):
    """Raised when a tool has been intercepted too many times in a session.

    This is NOT caught by the normal tool execution flow — it propagates
    up to the main loop, which should terminate the agent run.
    """
