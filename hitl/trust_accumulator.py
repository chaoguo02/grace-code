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
from typing import Any

# ── Public API ───────────────────────────────────────────────────────────


class SessionTrustAccumulator:
    """Tracks approved (tool_name, path, digest) within a session."""

    def __init__(self, *, threshold: int = 2) -> None:
        self._approved: dict[tuple[str, str, str], int] = {}
        self._threshold = threshold

    def record_approval(self, key: tuple[str, str, str]) -> None:
        """Record one explicit user approval for *key*."""
        self._approved[key] = self._approved.get(key, 0) + 1

    def is_trusted(self, key: tuple[str, str, str]) -> bool:
        """Return True if *key* has been approved >= threshold times."""
        return self._approved.get(key, 0) >= self._threshold

    def clear(self) -> None:
        """Reset all trust (called on session restart)."""
        self._approved.clear()

    @property
    def trusted_count(self) -> int:
        """Number of unique trusted keys (for observability)."""
        return sum(1 for v in self._approved.values() if v >= self._threshold)


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
