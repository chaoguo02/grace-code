"""Shared wall-clock interval helpers."""

from __future__ import annotations

import time


def elapsed_seconds(started_at: float, completed_at: float = 0.0) -> float:
    if started_at == 0:
        return 0.0
    end = completed_at if completed_at > 0 else time.time()
    return end - started_at
