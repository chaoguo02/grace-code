"""Session-level trust accumulator — reduce confirmation fatigue.

Phase 2 #4: Tracks approved (tool, path, params_digest) tuples within
a session.  After N explicit user approvals of the same pattern,
auto-approves for the remainder of the session.

CC divergence: CC uses (tool, path) two-tuple.  We add operation-type
differentiation because our permission model lacks CC's post-hoc safety
review layer.  See TOOL_SYSTEM_NORMALIZATION_DESIGN.md, Section 4.2 #4.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

# ── Public API ───────────────────────────────────────────────────────────


class SessionTrustAccumulator:
    """Tracks approved (tool_name, path, digest) within a session.

    Phase 2B: time-based decay — trust fades over time so a single long
    session cannot accumulate unbounded trust (R-C).  Every approval
    interval (default 10 min) the count decays by *decay_rate* (10%).
    Rejections decrease trust immediately.
    """

    def __init__(self, *, threshold: int = 2,
                 decay_interval_s: float = 600.0,
                 decay_rate: float = 0.10) -> None:
        self._approved: dict[tuple[str, str, str], list[float, float]] = {}
        """key → [count (float), last_updated_ts]"""
        self._threshold = threshold
        self._decay_interval_s = decay_interval_s
        self._decay_rate = decay_rate

    def _maybe_decay(self, key: tuple[str, str, str], now: float) -> None:
        entry = self._approved.get(key)
        if entry is None:
            return
        count, last = entry
        if now - last >= self._decay_interval_s:
            periods = int((now - last) / self._decay_interval_s)
            for _ in range(periods):
                count *= (1.0 - self._decay_rate)
            # 原地修改，保持外部 entry 引用一致（勿替换新 list）
            entry[0] = count
            entry[1] = last + periods * self._decay_interval_s

    def record_approval(self, key: tuple[str, str, str],
                        now: float | None = None) -> None:
        """Record one explicit user approval for *key*."""
        now = now if now is not None else time.time()
        entry = self._approved.get(key)
        if entry is None:
            self._approved[key] = [1.0, now]
        else:
            self._maybe_decay(key, now)
            entry[0] += 1.0
            entry[1] = now

    def record_rejection(self, key: tuple[str, str, str],
                         now: float | None = None) -> None:
        """Record one explicit user rejection for *key* (decreases trust).

        Phase 2B: 用户拒绝后该 key 的信任下降一级，防止"一次确认永久信任"。
        """
        now = now if now is not None else time.time()
        entry = self._approved.get(key)
        if entry is None:
            self._approved[key] = [0.0, now]
        else:
            self._maybe_decay(key, now)
            entry[0] = max(0.0, entry[0] - 1.0)
            entry[1] = now

    def is_trusted(self, key: tuple[str, str, str],
                   now: float | None = None) -> bool:
        """Return True if *key* has been approved >= threshold times."""
        entry = self._approved.get(key)
        if entry is None:
            return False
        self._maybe_decay(key, now if now is not None else time.time())
        return entry[0] >= self._threshold

    def trust_score(self, key: tuple[str, str, str],
                    now: float | None = None) -> float:
        """Return the current (decay-adjusted) trust score for *key*."""
        entry = self._approved.get(key)
        if entry is None:
            return 0.0
        self._maybe_decay(key, now if now is not None else time.time())
        return entry[0]

    def clear(self) -> None:
        """Reset all trust (called on session restart)."""
        self._approved.clear()

    @property
    def trusted_count(self) -> int:
        """Number of unique trusted keys (for observability)."""
        return sum(1 for v in self._approved.values() if v[0] >= self._threshold)


# ── Key computation ──────────────────────────────────────────────────────


def compute_trust_key(
    tool_name: str,
    params: dict[str, Any],
) -> tuple[str, str, str]:
    """Compute the trust accumulator key for one tool call.

    Returns (tool_name, path, params_digest) where:
      - tool_name: canonical name (e.g. "Read", "Edit", "Bash")
      - path: the relevant path parameter, or empty string if none
      - params_digest: sha256 of the operation-specific input

    Digest rules (per DDR, Section 4.2 #4):
      Read/Grep/Glob/ViewFile — sha256(path)
      Edit/Write               — sha256(path + "|" + tool_name)
      Bash/Shell                — sha256(first_word + second_word_if_flag)
      WebFetch/WebSearch        — sha256(domain)
      Default                   — sha256(path) if path present, else ""  (fallback)
    """
    path = _extract_path(tool_name, params)
    digest = _compute_digest(tool_name, params, path)
    return (tool_name, path, digest)


def _extract_path(tool_name: str, params: dict[str, Any]) -> str:
    """Extract the relevant path parameter from tool params."""
    # Common path parameter names
    for key in ("path", "file_path", "filepath"):
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _compute_digest(
    tool_name: str,
    params: dict[str, Any],
    path: str,
) -> str:
    """Compute params_digest per operation-type rules."""
    lower_name = tool_name.lower()

    # Read operations — sha256(path)
    if lower_name in ("read", "file_read", "read_file", "fileview", "file_view",
                       "grep", "search_text", "glob", "find_files", "findsymbol",
                       "find_symbol", "gitstatus", "git_status",
                       "memory_read", "memory_list", "memory_search",
                       "list_resources", "read_resource"):
        raw = path or ""

    # Write operations — sha256(path + "|" + tool)
    elif lower_name in ("edit", "file_edit", "write", "file_write",
                         "git_add", "git_commit", "git_push"):
        raw = f"{path}|{tool_name}"

    # Shell — sha256(first_word + second_word_if_flag)
    elif lower_name in ("bash", "shell"):
        cmd = (params.get("command") or params.get("cmd") or "").strip()
        words = cmd.split()
        first = words[0] if words else ""
        second = ""
        if len(words) > 1 and words[1].startswith("-"):
            second = "|" + words[1]
        raw = f"{first}{second}"

    # Network — sha256(domain)
    elif lower_name in ("web_fetch", "webfetch", "web_search", "websearch"):
        url = params.get("url", "")
        # Extract domain from URL (simple heuristic)
        domain = url
        if "://" in domain:
            domain = domain.split("://", 1)[1]
        domain = domain.split("/")[0].split("?")[0]
        raw = domain

    # Default — sha256(path) if path present
    else:
        raw = path or ""

    if not raw:
        return ""

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
