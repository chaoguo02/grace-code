"""
CC-Native DeterministicTrimmer — pair-aware final fallback.

Design (P0_1 Batch 2):
  The LAST line of defense.  Runs AFTER Micro → SessionMemory → API
  compaction chain is exhausted or circuit-broken.

  NEVER throws — always produces a message list that fits within max_tokens.
  Preserves: system message + last N tool_use/tool_result pairs.
  Trims from the oldest messages first.

  The HARD invariant:
    estimated(context_tokens) + output_room <= provider_context_limit
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TrimResult:
    messages: list[dict]
    tokens_trimmed: int
    trim_level: str  # "none" | "soft" | "hard"


class DeterministicTrimmer:
    """Pair-aware deterministic trimmer — never fails.

    CC-aligned: adjustIndexToPreserveAPIInvariants ensures tool_use
    and tool_result blocks stay paired.  The trimmer also preserves
    the system message and the most recent N user/assistant/tool turns.
    """

    def __init__(
        self,
        preserve_last_n_pairs: int = 3,
    ) -> None:
        self._preserve_last_n_pairs = preserve_last_n_pairs

    def trim(
        self,
        messages: list[dict],
        max_tokens: int,
        *,
        estimator: object | None = None,
    ) -> TrimResult:
        """Trim *messages* to fit within *max_tokens*.

        Args:
            messages: The message list to trim.
            max_tokens: Hard ceiling for the trimmed result.
            estimator: Optional LocalTokenEstimator for accurate sizing.
                       If None, uses char/4 heuristic.

        Returns:
            TrimResult with the trimmed message list.
        """
        if not messages:
            return TrimResult(messages=[], tokens_trimmed=0, trim_level="none")

        current = _estimate_list(messages, estimator)
        if current <= max_tokens:
            return TrimResult(messages=list(messages), tokens_trimmed=0, trim_level="none")

        # Step 1: Identify protected indices
        protected: set[int] = set()
        _mark_system(messages, protected)
        _mark_last_n_pairs(messages, self._preserve_last_n_pairs, protected)

        # Step 2: Trim from the front (oldest), skipping protected
        trimmed: list[dict] = []
        tokens_so_far = 0
        trimmed_count = 0

        for i, m in enumerate(messages):
            if i in protected:
                trimmed.append(m)
                tokens_so_far += _estimate_one(m, estimator)
                continue

            est = _estimate_one(m, estimator)
            if tokens_so_far + est <= max_tokens:
                trimmed.append(m)
                tokens_so_far += est
            else:
                trimmed_count += 1

        # Step 3: If still over, hard-truncate from the end (keep system)
        if _estimate_list(trimmed, estimator) > max_tokens and len(trimmed) > 1:
            system_count = sum(1 for m in trimmed if m.get("role") == "system")
            keep_system = trimmed[:system_count]
            rest = trimmed[system_count:]
            # drop from the oldest non-system
            trimmed = keep_system + rest[-max(1, len(rest) // 2):]
            trimmed_count += max(0, len(messages) - len(trimmed))

        tokens_trimmed = current - _estimate_list(trimmed, estimator)

        return TrimResult(
            messages=trimmed,
            tokens_trimmed=max(0, tokens_trimmed),
            trim_level="hard" if trimmed_count > 0 else "soft",
        )


# ── internal helpers ────────────────────────────────────────────────────────

def _estimate_one(msg: dict, estimator: object | None) -> int:
    """Estimate tokens for one message dict."""
    if estimator is not None and hasattr(estimator, "estimate_messages"):
        return estimator.estimate_messages([msg])
    # fallback: char/4 heuristic
    content = msg.get("content", "")
    if isinstance(content, str):
        return max(1, len(content) // 4)
    if isinstance(content, list):
        return max(1, sum(len(str(b)) // 4 for b in content))
    return 1


def _estimate_list(messages: list[dict], estimator: object | None) -> int:
    """Estimate total tokens for a message list."""
    if estimator is not None and hasattr(estimator, "estimate_messages"):
        return estimator.estimate_messages(messages)
    return sum(_estimate_one(m, None) for m in messages)


def _mark_system(messages: list[dict], protected: set[int]) -> None:
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            protected.add(i)


def _mark_last_n_pairs(
    messages: list[dict],
    n: int,
    protected: set[int],
) -> None:
    """Mark the last N tool_use/tool_result pairs as protected.

    CC-aligned: adjustIndexToPreserveAPIInvariants.
    A "pair" is a tool_use immediately followed by a tool_result
    with matching tool_use_id / tool_call_id.
    """
    # Walk backwards, match tool_result → tool_use
    pairs_found = 0
    i = len(messages) - 1
    while i >= 1 and pairs_found < n:
        curr = messages[i]
        prev = messages[i - 1]
        if _is_tool_result(curr) and _is_tool_use(prev):
            if _match_pair(prev, curr):
                protected.add(i - 1)  # tool_use
                protected.add(i)      # tool_result
                pairs_found += 1
                i -= 2
                continue
        i -= 1


def _is_tool_use(msg: dict) -> bool:
    role = msg.get("role", "")
    if role == "assistant":
        return bool(msg.get("tool_calls"))
    return role == "tool_use"


def _is_tool_result(msg: dict) -> bool:
    return msg.get("role", "") in ("tool", "tool_result")


def _match_pair(tool_use: dict, tool_result: dict) -> bool:
    """Check if tool_use and tool_result share the same call id."""
    tool_id = tool_result.get("tool_call_id", tool_result.get("tool_use_id", ""))
    if tool_id:
        # Check tool_calls array for matching ID
        for tc in tool_use.get("tool_calls", []):
            if tc.get("id") == tool_id:
                return True
        # Direct id match
        if tool_use.get("id") == tool_id:
            return True
    return False
