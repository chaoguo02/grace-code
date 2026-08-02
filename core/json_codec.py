"""
G2: Canonical JSON codec — encode/decode with deterministic ordering.

Produces identical bytes for semantically identical FrozenJsonObject values
regardless of insertion order.  Used for event envelope identity (digest)
and schema round-trip verification.
"""

from __future__ import annotations

import hashlib
import json as _json

from core.json_values import (
    FrozenJsonObject,
    JsonValue,
    freeze_json,
    thaw_json,
)


# ── Canonical JSON bytes (sorted keys, compact) ────────────────────────────

def canonical_dumps(value: JsonValue) -> bytes:
    """Serialize a JsonValue to canonical JSON bytes.

    Keys are sorted, no trailing whitespace, compact representation.
    Two semantically identical FrozenJsonObjects produce identical bytes.
    """
    raw = _canonical_to_raw(value)
    return _json.dumps(raw, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_digest(value: JsonValue) -> str:
    """SHA-256 hex digest of canonical JSON bytes.

    Used for event envelope identity (source + event_id + digest).
    Two identical payloads produce identical digests.
    """
    data = canonical_dumps(value)
    return hashlib.sha256(data).hexdigest()


def canonical_dumps_string(value: JsonValue) -> str:
    """Serialize to canonical JSON string (for human-readable output)."""
    raw = _canonical_to_raw(value)
    return _json.dumps(raw, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"))


# ── Encode / Decode ────────────────────────────────────────────────────────

def encode(value: JsonValue) -> bytes:
    """Encode a JsonValue to standard JSON bytes.  Keys are sorted."""
    return canonical_dumps(value)


def decode(data: bytes) -> JsonValue:
    """Decode JSON bytes into a frozen JsonValue.

    Raises TypeError/ValueError if the JSON contains invalid values.
    """
    raw = _json.loads(data)
    return freeze_json(raw)


def decode_string(s: str) -> JsonValue:
    """Decode a JSON string into a frozen JsonValue."""
    raw = _json.loads(s)
    return freeze_json(raw)


def round_trip(value: JsonValue) -> JsonValue:
    """Encode then decode — must produce an equal value."""
    return decode(encode(value))


def digest_equals(a: JsonValue, b: JsonValue) -> bool:
    """Compare two JsonValues by canonical digest."""
    return canonical_digest(a) == canonical_digest(b)


# ── Internal helpers ───────────────────────────────────────────────────────

def _canonical_to_raw(value: JsonValue):
    """Convert a JsonValue to a plain Python structure for json.dumps.

    Object keys are iterated in sorted order (guaranteed by FrozenJsonObject),
    arrays remain in tuple order.
    """
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, tuple):
        return [_canonical_to_raw(item) for item in value]
    if isinstance(value, FrozenJsonObject):
        # items are already sorted by key in FrozenJsonObject.__post_init__
        return {k: _canonical_to_raw(v) for k, v in value.items}
    raise TypeError(f"Cannot serialize {type(value).__name__} to JSON")
