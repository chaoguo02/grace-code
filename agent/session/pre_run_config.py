"""Pre-run configuration staging for session execution.

``SessionPreRunConfig`` unifies 11 separate pre-run staging dicts
that previously lived as separate instance attributes on
``SessionRuntime``.  It is consumed once per run, then discarded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SessionPreRunConfig:
    """All pre-run staging state for one session.

    **Serializability contract**: ``created_at`` uses ``time.perf_counter()``
    (monotonic clock for interval measurement, relative to an arbitrary
    origin).  Perf-counter values are NOT wall-clock timestamps and are
    NOT serializable.  Never pickle/JSON-serialize this field — it exists
    purely for staleness detection within a single process lifetime.  If
    cross-process transfer is ever needed, replace with ``time.time_ns()``
    + independent staleness math.

    Do NOT add ``__getstate__`` / ``__setstate__`` to work around this —
    cross-process transfer of pre-run staging state is a design error, not
    a serialization gap.  Pre-run configs that leave the creating process
    should fail loudly, not silently carry a meaningless timestamp.
    """

    created_at: float = 0.0  # time.perf_counter() — NOT serializable, NOT wall-clock
    web_confirm_callback: Callable | None = None
    stream_callback: Callable | None = None
    text_lifecycle_callback: Callable | None = None
    text_delta_callback: Callable | None = None
    pending_skill_activations: list[dict[str, object]] = field(default_factory=list)
    permission_mode: str | None = None
    injected_rules: tuple = ()
    model_switch: str | None = None
    model_provider: str = ""
    effort: str | None = None
    thinking: bool | None = None
    skill_modifier: Any = None

    _STALENESS_SECONDS: float = 30.0

    @property
    def is_stale(self) -> bool:
        """Config that was set but never consumed may carry stale state.

        Returns True if this config has existed for more than the
        staleness threshold without being consumed.
        """
        return self.created_at > 0 and (
            time.perf_counter() - self.created_at > self._STALENESS_SECONDS
        )
