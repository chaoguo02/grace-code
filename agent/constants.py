"""
agent/constants.py

Agent-loop configuration constants extracted from agent/core.py (Phase 5 A3).

Each constant replaces a bare numeric or string literal whose semantic
meaning was previously implicit.  All values are identical to the
pre-extraction defaults — no behavioural change.
"""

# ── Budget thresholds ───────────────────────────────────────────────────────
DEFAULT_REQUEST_BUDGET_TOKENS: int = 200_000
"""Fallback token budget when request_budget_tokens is not configured."""
DEFAULT_HISTORY_BUDGET_TOKENS: int = 110_000
"""Fallback history budget for the collapse layer."""
DEFAULT_MAX_OUTPUT_TOKENS: int = 32_000
"""Default max_tokens for LLM responses."""
TRUNCATION_BUFFER_TOKENS: int = 100
"""Buffer subtracted from max_tokens when detecting output truncation."""

# ── Budget monitoring ───────────────────────────────────────────────────────
BUDGET_WARNING_PCT: int = 80
"""Trigger a budget warning when total_tokens exceeds this percentage."""
BUDGET_COMPACT_PCT: int = 100
"""Trigger auto-compact when total_tokens exceeds this percentage."""

# ── Display truncation ──────────────────────────────────────────────────────
DIFF_PREVIEW_MAX_CHARS: int = 3_000
"""Maximum characters of git diff included in the completion record."""
SUMMARY_TRUNCATION_CHARS: int = 2_000
"""Truncation length for history extraction and session memory summaries."""
TOOL_EXTRACT_CHARS: int = 500
"""Maximum characters of individual tool output extracted for summary."""
FINDING_DESC_CHARS: int = 200
"""Maximum characters of a finding description in post-compaction recovery."""
DEFAULT_TRUNCATE_OUTPUT_CHARS: int = 8_000
"""Default max_chars for _truncate_output()."""

# ── Loop control ────────────────────────────────────────────────────────────
COMPLETION_BLOCK_THRESHOLD: int = 3
"""Number of same-reason completion blocks before forcing give_up."""
TEST_FAILURE_REFLECTION_LIMIT: int = 3
"""Number of test-failure reflections before forcing give_up."""
RECENT_FILES_WINDOW: int = 20
"""Number of recently-accessed files passed to session memory extraction."""
SESSION_MEMORY_MSG_WINDOW: int = 20
"""Number of most-recent messages used for session memory context."""
RECOVERY_MAX_FINDINGS: int = 10
"""Maximum accumulated findings re-injected after compaction."""
MAX_TOOL_RESULTS_EXTRACT: int = 5
"""Maximum tool results extracted when building the max_steps summary."""

# ── Sentinels ───────────────────────────────────────────────────────────────
NO_THOUGHT_SENTINEL: str = "(no thought)"
"""Magic sentinel emitted by some LLM backends for empty reasoning blocks."""
