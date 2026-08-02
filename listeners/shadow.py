"""P18: Shadow composition — run new path alongside old, compare results."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ShadowRunner:
    """Run a new handler alongside an old one. Log mismatches, never fail."""

    def __init__(self, old_handler, new_handler) -> None:
        self._old = old_handler
        self._new = new_handler
        self._mismatches = 0

    def __call__(self, *args, **kwargs):
        old_result = None
        new_result = None
        try:
            old_result = self._old(*args, **kwargs)
        except Exception as exc:
            logger.debug("Shadow: old handler failed: %s", exc)
        try:
            new_result = self._new(*args, **kwargs)
        except Exception as exc:
            logger.debug("Shadow: new handler failed: %s", exc)

        if old_result != new_result:
            self._mismatches += 1
            logger.info("Shadow mismatch #%d: old=%s new=%s", self._mismatches, old_result, new_result)

        return old_result  # old path is authoritative

    @property
    def mismatches(self) -> int:
        return self._mismatches
