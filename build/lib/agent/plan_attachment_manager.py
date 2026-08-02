"""Plan mode attachment throttling — CC-aligned progressive reminder strategy.

Strategy (CC-aligned):
  Turn 1:  Full instructions (~200 lines)
  Turn 2-4: Nothing (trust short-term memory)
  Turn 5:  Sparse reminder (~8 lines)
  Turn 6-9: Nothing
  Turn 10: Sparse reminder
  ...
  Every 5th injection: Full instructions again (every ~25 turns)
  Every 25th turn: Full instructions (long-session refresh)

Constants:
  TURNS_BETWEEN_ATTACHMENTS = 5
  FULL_REMINDER_EVERY_N_ATTACHMENTS = 5  # every 5th injection = full

Designed to work with the existing ``runtime_message_source`` callback
pattern in agent/session/runtime.py — receives the current step number.
"""

from __future__ import annotations

TURNS_BETWEEN_ATTACHMENTS = 5
FULL_REMINDER_EVERY_N_ATTACHMENTS = 5  # every 5th injection = full refresh
FULL_REMINDER_EVERY_N_TURNS = 25       # also full refresh every 25 turns


class PlanModeAttachmentManager:
    """Manage progressive plan mode prompt injection to save tokens.

    Estimated token savings: ~10,000 tokens per 15-turn plan session
    compared to injecting the full instructions every turn.
    """

    def __init__(self) -> None:
        self._turn_counter: int = 0
        self._injection_counter: int = 0

    def set_turn(self, step: int) -> None:
        """Sync to the agent loop's current step number."""
        self._turn_counter = step

    def reset_on_compaction(self) -> None:
        """Force full refresh after context compaction — model lost context."""
        self._turn_counter = 1  # Next injection will be turn 1 → full

    def current_turn(self) -> int:
        return self._turn_counter

    def should_inject(self) -> bool:
        """Return True if an attachment should be injected this turn.

        Turn 1 is skipped — build_runtime_messages() already injects the full
        instructions via runtime_prompt_builder when the session starts."""
        if self._turn_counter == 1:
            return False  # build_runtime_messages already injected on turn 1
        return self._turn_counter % TURNS_BETWEEN_ATTACHMENTS == 0

    def get_attachment(self) -> str | None:
        """Return the appropriate attachment text, or None if no injection needed."""
        if not self.should_inject():
            return None

        self._injection_counter += 1

        from prompts.builder import (
            get_plan_mode_injection,
            get_plan_mode_sparse_reminder,
        )

        # Full refresh every N injections or every 25 turns
        if (
            self._injection_counter % FULL_REMINDER_EVERY_N_ATTACHMENTS == 0
            or self._turn_counter % FULL_REMINDER_EVERY_N_TURNS == 0
        ):
            return get_plan_mode_injection()  # Full instructions
        return get_plan_mode_sparse_reminder()  # Sparse reminder
