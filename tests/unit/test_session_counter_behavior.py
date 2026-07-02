"""
# REQUIREMENT CONTRACT: Session Counter Behavior (v1.0)
--------------------------------------------
## [R1] RESET TRIGGER
- Condition: `run_consolidation()` returns successfully without raising.
- Behavior: `.sessions-since-dream` must reset to `0`.
- Irrelevant factors:
  - File change status (`changed=True` and `changed=False` both reset).
  - Session content validity (empty or valid data both reset after success).

## [R2] PERSISTENCE TRIGGER
- Condition: `run_consolidation()` raises any exception.
- Behavior: `.sessions-since-dream` must preserve its original value.
- Irrelevant factors:
  - Exception cause (I/O, logic, or external service failure).
  - Number of sessions already processed before the failure.

## [R3] SOURCE OF TRUTH
- Sole state source: `.sessions-since-dream` file content.
- Forbidden behavior:
  - Reading the counter from memory or cache instead of persisted state.
  - Inferring counter state from any other file.
--------------------------------------------
Changing this test changes the requirement contract and requires team review.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from memory.consolidation import record_session_end, run_consolidation


COUNTER_FILE = ".sessions-since-dream"


class DummyStore:
    """Minimal public-shape store object for run_consolidation()."""

    def __init__(self, store_dir: Path) -> None:
        self.store_dir = store_dir


class FailingRunner:
    """Public DreamRunner-compatible failure injector."""

    allowed_bash = "read-only"

    def __init__(self, memory_dir: Path) -> None:
        self.allowed_write_root = memory_dir

    def run(self, *, memory_dir: Path, prompt: str, log_dir: str | None = None) -> bool:
        raise RuntimeError("simulated dream failure")


def _read_counter(memory_dir: Path) -> int:
    return int((memory_dir / COUNTER_FILE).read_text(encoding="utf-8").strip() or "0")


def _record_sessions(memory_dir: Path, count: int) -> None:
    for index in range(count):
        # Write realistic per-session trace files, then record session end via public API.
        (memory_dir / f"session_{index}.md").write_text(
            f"Session data {index}",
            encoding="utf-8",
        )
        record_session_end(memory_dir)


def test_counter_resets_after_successful_consolidation() -> None:
    """[R1] Success resets the counter to 0 regardless of changed=False."""
    with tempfile.TemporaryDirectory(prefix="counter_success_") as tmp:
        memory_dir = Path(tmp)
        store = DummyStore(memory_dir)

        _record_sessions(memory_dir, 5)
        assert _read_counter(memory_dir) == 5

        # Existing MEMORY.md already satisfies hard limits, so RuleDreamRunner has no changes.
        (memory_dir / "MEMORY.md").write_text(
            "# Memory Index\n\n- [stable](stable.md) — stable memory (project)\n",
            encoding="utf-8",
        )
        (memory_dir / "stable.md").write_text(
            "---\nname: stable\ndescription: stable memory\ntype: project\n---\n\nUNCHANGED_CONTENT\n",
            encoding="utf-8",
        )

        result = run_consolidation(store, async_run=False)
        assert result is False, "RuleDreamRunner should report no file changes in this scenario"

        current_count = _read_counter(memory_dir)
        assert current_count == 0, (
            f"Violates [R1]: Dream succeeded but counter={current_count}; expected 0 even when no files changed"
        )
        print("OK successful consolidation resets counter even with no file changes")


def test_counter_persists_on_consolidation_failure() -> None:
    """[R2] Failure preserves the accumulated counter."""
    with tempfile.TemporaryDirectory(prefix="counter_failure_") as tmp:
        memory_dir = Path(tmp)
        store = DummyStore(memory_dir)

        _record_sessions(memory_dir, 5)
        assert _read_counter(memory_dir) == 5

        try:
            run_consolidation(store, async_run=False, runner=FailingRunner(memory_dir))
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected simulated dream failure")

        current_count = _read_counter(memory_dir)
        assert current_count == 5, (
            f"Violates [R2]: Dream failed but counter={current_count}; expected accumulated value to remain 5"
        )
        print("OK failed consolidation preserves accumulated counter")


if __name__ == "__main__":
    test_counter_resets_after_successful_consolidation()
    test_counter_persists_on_consolidation_failure()
    print("ALL SESSION COUNTER BEHAVIOR CHECKS PASSED")
