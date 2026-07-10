"""Write discipline for automatic memory extraction."""

from __future__ import annotations


class WriteDiscipline:
    """Tracks explicit memory writes so auto-extraction can skip the turn."""

    def __init__(self) -> None:
        self._explicit_write_this_turn = False

    def notify_explicit_memory_write(self) -> None:
        self._explicit_write_this_turn = True

    def should_skip_auto_extract(self) -> bool:
        return self._explicit_write_this_turn

    def reset_turn(self) -> None:
        self._explicit_write_this_turn = False
