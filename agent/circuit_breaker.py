"""Runtime-level circuit breaker — enforces stop rules in code, not prompts.

Claude Code pattern: the circuit breaker watches the *rhythm of rejections*,
not what the model says. When thresholds are hit, the Runtime terminates the
agent — no model override, no "解释", no "绕过".

Tracked metrics (all configurable):
- Consecutive tool denials (user/rule rejects same tool repeatedly)
- Cumulative session denials (permission model is structurally broken)
- Consecutive subagent failures (delegation keeps crashing)
- Consecutive tool errors (every tool call in the turn fails repeatedly)
- Consecutive identical tool calls (subagent stuck in a local loop)
- Elapsed time (subagent running too long)
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CircuitBreakerState
# ---------------------------------------------------------------------------

class CircuitBreakerState(str, Enum):
    CLOSED = "closed"        # Normal operation
    HALF_OPEN = "half_open"  # Warning — one more and it trips
    OPEN = "open"            # Circuit tripped — agent must stop


# ---------------------------------------------------------------------------
# CircuitBreakerConfig
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreakerConfig:
    """Thresholds for the circuit breaker. All counters reset on success."""

    max_consecutive_tool_denials: int = 3
    """Trip when user/rule rejects tools this many times in a row."""

    max_session_tool_denials: int = 20
    """Trip when total session denials exceed this (permission model broken)."""

    max_consecutive_subagent_failures: int = 2
    """Trip when subagents crash this many times in a row."""

    max_consecutive_tool_errors: int = 3
    """Trip when every tool in the turn fails this many turns in a row."""

    max_consecutive_same_tool_calls: int = 5
    """Trip when the same tool+params is called this many times in a row.
    This catches subagent-level tool loops (e.g., same file_read 5x)."""

    max_elapsed_seconds: float = 0.0
    """Trip when the agent runs longer than this. 0 = disabled."""

    enabled: bool = True
    """Master switch — set False to disable all breaker checks."""


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

class CircuitBreakerTripped(Exception):
    """Raised in headless mode when the circuit breaker trips."""

    def __init__(self, reason: str, state: CircuitBreakerState) -> None:
        super().__init__(reason)
        self.state = state


@dataclass
class CircuitBreaker:
    """Runtime-level circuit breaker aligned with Claude Code's pattern.

    This is the *code enforcement* layer. Prompt-based rules like
    "after 2 failures stop" are replaced by this class checking counters
    and terminating the agent.

    Integration:
    - PermissionPipeline records denials here
    - AgentTool records subagent failures here
    - ReActAgent.run() calls check() each step
    - When tripped → RunStatus.GAVE_UP (interactive) or raise (headless)
    """

    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)

    # ── Counters ──
    _consecutive_denials: int = 0
    _session_denials: int = 0
    _consecutive_subagent_failures: int = 0
    _consecutive_tool_errors: int = 0
    _consecutive_same_tool_calls: int = 0
    _last_tool_signature: str = ""
    _started_at: float = 0.0
    _state: CircuitBreakerState = CircuitBreakerState.CLOSED
    _trip_reason: str = ""

    def __post_init__(self) -> None:
        self._started_at = _time.time()

    # ── Properties ──

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    @property
    def is_tripped(self) -> bool:
        self.check()  # Sync state from counters before returning
        return self._state == CircuitBreakerState.OPEN

    # ── Recording methods ──

    def record_denial(self) -> None:
        """Record a tool permission denial. Auto-checks thresholds."""
        self._consecutive_denials += 1
        self._session_denials += 1
        logger.debug(
            "CircuitBreaker denial #%d (consecutive=%d, session=%d)",
            self._consecutive_denials, self._consecutive_denials, self._session_denials,
        )
        self.check()

    def record_approval(self) -> None:
        """Record a tool permission approval — resets consecutive denial counter."""
        self._consecutive_denials = 0

    def record_subagent_failure(self) -> None:
        """Record a subagent failure. Auto-checks thresholds."""
        self._consecutive_subagent_failures += 1
        logger.debug(
            "CircuitBreaker subagent failure #%d",
            self._consecutive_subagent_failures,
        )
        self.check()

    def record_subagent_success(self) -> None:
        """Record a subagent success — resets consecutive failure counter."""
        self._consecutive_subagent_failures = 0

    def record_tool_error(self) -> None:
        """Record a turn where ALL tool calls failed. Auto-checks thresholds."""
        self._consecutive_tool_errors += 1
        logger.debug(
            "CircuitBreaker tool error turn #%d",
            self._consecutive_tool_errors,
        )
        self.check()

    def record_tool_success(self) -> None:
        """Record a turn where at least one tool succeeded — resets error counter."""
        self._consecutive_tool_errors = 0

    def record_tool_call(self, tool_name: str, params_hash: str) -> None:
        """Record a tool call for same-tool loop detection.

        Args:
            tool_name: The tool being called (e.g., 'file_read', 'bash').
            params_hash: A stable hash of the parameters (e.g., path + offset).
                Use an empty string to opt out of same-tool tracking for this call.
        """
        if not params_hash:
            return
        signature = f"{tool_name}:{params_hash}"
        if signature == self._last_tool_signature:
            self._consecutive_same_tool_calls += 1
            logger.debug(
                "CircuitBreaker same-tool call #%d: %s",
                self._consecutive_same_tool_calls, signature,
            )
        else:
            self._consecutive_same_tool_calls = 0
            self._last_tool_signature = signature
        self.check()

    @property
    def elapsed_seconds(self) -> float:
        return _time.time() - self._started_at

    # ── Clone for subagent ──

    def clone_for_subagent(self) -> "CircuitBreaker":
        """Create a fresh CircuitBreaker for a subagent.

        Returns a new instance with the same config but zeroed counters.
        Subagent breakers are independent — tripping one doesn't trip the parent.

        The subagent breaker has stricter defaults for same-tool loops
        and optionally a time limit.
        """
        import copy
        cloned = CircuitBreaker(config=copy.copy(self.config))
        # Subagent-specific: stricter same-tool loop detection (4 instead of 5)
        if cloned.config.max_consecutive_same_tool_calls == 5:
            cloned.config.max_consecutive_same_tool_calls = 4
        return cloned

    # ── Check ──

    def check(self) -> bool:
        """Check if the circuit breaker should trip.

        Returns True if the agent must stop NOW.
        Call this at the start of each step in the main loop.
        """
        if not self.config.enabled:
            return False

        # Consecutive denials — agent stuck retrying a blocked action
        if self._consecutive_denials >= self.config.max_consecutive_tool_denials:
            self._trip_state(
                f"Circuit breaker tripped: {self._consecutive_denials} consecutive "
                f"tool denials (threshold: {self.config.max_consecutive_tool_denials})"
            )
            return True

        # Cumulative denials — permission model structurally broken
        if self._session_denials >= self.config.max_session_tool_denials:
            self._trip_state(
                f"Circuit breaker tripped: {self._session_denials} total session "
                f"denials (threshold: {self.config.max_session_tool_denials})"
            )
            return True

        # Consecutive subagent failures — delegation is broken
        if self._consecutive_subagent_failures >= self.config.max_consecutive_subagent_failures:
            self._trip_state(
                f"Circuit breaker tripped: {self._consecutive_subagent_failures} consecutive "
                f"subagent failures (threshold: {self.config.max_consecutive_subagent_failures})"
            )
            return True

        # Consecutive tool errors — environment broken
        if self._consecutive_tool_errors >= self.config.max_consecutive_tool_errors:
            self._trip_state(
                f"Circuit breaker tripped: {self._consecutive_tool_errors} consecutive "
                f"turns with all tools failing (threshold: {self.config.max_consecutive_tool_errors})"
            )
            return True

        # Consecutive same-tool calls — subagent stuck in a local loop
        if self._consecutive_same_tool_calls >= self.config.max_consecutive_same_tool_calls:
            self._trip_state(
                f"Circuit breaker tripped: {self._consecutive_same_tool_calls} consecutive "
                f"identical tool calls ({self._last_tool_signature}) "
                f"(threshold: {self.config.max_consecutive_same_tool_calls})"
            )
            return True

        # Elapsed time — agent running too long
        if self.config.max_elapsed_seconds > 0 and self.elapsed_seconds >= self.config.max_elapsed_seconds:
            self._trip_state(
                f"Circuit breaker tripped: elapsed time {self.elapsed_seconds:.0f}s "
                f"exceeds limit {self.config.max_elapsed_seconds:.0f}s"
            )
            return True

        return False

    def _trip_state(self, reason: str) -> None:
        self._state = CircuitBreakerState.OPEN
        self._trip_reason = reason

    # ── Serialization ──

    def to_summary(self) -> dict:
        """Export breaker state for diagnostics."""
        return {
            "state": self._state.value,
            "consecutive_denials": self._consecutive_denials,
            "session_denials": self._session_denials,
            "consecutive_subagent_failures": self._consecutive_subagent_failures,
            "consecutive_tool_errors": self._consecutive_tool_errors,
            "consecutive_same_tool_calls": self._consecutive_same_tool_calls,
            "last_tool_signature": self._last_tool_signature,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "trip_reason": self._trip_reason,
        }
