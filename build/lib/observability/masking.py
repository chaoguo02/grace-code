from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_DEFAULT_MAX_DEPTH = 4
_DEFAULT_MAX_ITEMS = 20
_DEFAULT_MAX_STRING_LENGTH = 4_000

_MASK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)(['\"]?)[^\s,'\"]+\2"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(secret\s*[=:]\s*)(['\"]?)[^\s,'\"]+\2"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(token\s*[=:]\s*)(['\"]?)[^\s,'\"]+\2"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password\s*[=:]\s*)(['\"]?)[^\s,'\"]+\2"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(sk|pk)-lf-[A-Za-z0-9_-]+\b"), "[REDACTED_LANGFUSE_KEY]"),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[REDACTED_EMAIL]"),
]


def truncate_text(text: str, max_length: int = _DEFAULT_MAX_STRING_LENGTH) -> str:
    if len(text) <= max_length:
        return text
    head = max_length // 2
    tail = max_length - head
    return f"{text[:head]}\n...[truncated]...\n{text[-tail:]}"


def mask_text(text: str, max_length: int = _DEFAULT_MAX_STRING_LENGTH) -> str:
    masked = text
    for pattern, replacement in _MASK_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return truncate_text(masked, max_length=max_length)


def sanitize_for_langfuse(
    value: Any,
    *,
    mask_sensitive_data: bool = True,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    max_items: int = _DEFAULT_MAX_ITEMS,
    max_string_length: int = _DEFAULT_MAX_STRING_LENGTH,
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, Path):
        value = str(value)

    if isinstance(value, str):
        if mask_sensitive_data:
            return mask_text(value, max_length=max_string_length)
        return truncate_text(value, max_length=max_string_length)

    if max_depth <= 0:
        return f"<{type(value).__name__}>"

    if isinstance(value, dict):
        items = list(value.items())
        sanitized: dict[str, Any] = {}
        for key, item in items[:max_items]:
            sanitized[str(key)] = sanitize_for_langfuse(
                item,
                mask_sensitive_data=mask_sensitive_data,
                max_depth=max_depth - 1,
                max_items=max_items,
                max_string_length=max_string_length,
            )
        if len(items) > max_items:
            sanitized["_truncated_items"] = len(items) - max_items
        return sanitized

    if isinstance(value, (list, tuple, set, frozenset)):
        seq = list(value)
        sanitized_list = [
            sanitize_for_langfuse(
                item,
                mask_sensitive_data=mask_sensitive_data,
                max_depth=max_depth - 1,
                max_items=max_items,
                max_string_length=max_string_length,
            )
            for item in seq[:max_items]
        ]
        if len(seq) > max_items:
            sanitized_list.append(f"... ({len(seq) - max_items} more items)")
        return sanitized_list

    return sanitize_for_langfuse(
        repr(value),
        mask_sensitive_data=mask_sensitive_data,
        max_depth=max_depth - 1,
        max_items=max_items,
        max_string_length=max_string_length,
    )
