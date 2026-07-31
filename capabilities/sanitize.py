"""Sanitization helpers for prompt-facing capability context."""

from __future__ import annotations

import re

_REDACTIONS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s,;]+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+\-/]+=*)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)(\s*[:=]\s*)([^\s,;]+)"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)([?&](?:token|key|api_key|secret|password)=)([^\s&#]+)"), r"\1[REDACTED]"),
    # Prefixed token forms commonly found in env vars and connection errors
    (re.compile(r"\b(sk-[A-Za-z0-9._\-]+)"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(gh[ps]_[A-Za-z0-9._\-]+)"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\b(xox[bps]-[A-Za-z0-9._\-]+)"), "[REDACTED_SLACK_TOKEN]"),
    # Bare env-var assignment with secret-like values
    (re.compile(r"(?i)([A-Z_]{3,30}(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*)([^\s,;]+)"), r"\1[REDACTED]"),
)


def sanitize_text(text: str, *, limit: int = 240) -> str:
    """Return single-line prompt-safe text with common secret forms redacted."""
    value = " ".join(str(text or "").split())
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    if len(value) > limit:
        return value[: max(0, limit - 1)].rstrip() + "…"
    return value


def sanitize_error(text: str, limit: int = 240) -> str:
    return sanitize_text(text, limit=limit)
