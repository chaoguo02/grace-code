"""
G2: Immutable JSON Value — frozen, validated, canonical boundary types.

Replaces `dict[str, Any]` across core boundaries (Hook inputs/decisions,
Event payloads, Runtime ports).  Every value is immutable and deeply validated.

Constraints enforced at freeze():
  - No NaN / Infinity floats
  - No bytes, datetime, callable, custom objects, circular references
  - String keys only (for objects)
  - Max depth, max keys per object, max scalar string bytes

Canonical ordering: object keys sorted; array order preserved in tuple.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# ── Constants (fixed thresholds) ────────────────────────────────────────────

MAX_DEPTH = 32
MAX_KEYS = 256
MAX_STRING_BYTES = 1_048_576  # 1 MiB


# ── Type aliases ────────────────────────────────────────────────────────────

JsonScalar = str | int | float | bool | None
"""Leaf JSON values.  float excludes NaN and Infinity (checked at freeze)."""

# Forward reference for recursive type: JsonValue = scalar | tuple | FrozenJsonObject
# Declared at module level for use in isinstance checks and type annotations.


# ── FrozenJsonObject ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FrozenJsonObject:
    """Immutable JSON object with canonical (sorted) key ordering.

    Keys are sorted alphabetically at construction.  Items are stored as
    a tuple of (key, JsonValue) pairs — never a mutable dict.
    """

    items: tuple[tuple[str, "JsonValue"], ...]

    def __post_init__(self) -> None:
        # Ensure canonical ordering (sorted by key)
        if list(self.items) != sorted(self.items, key=lambda kv: kv[0]):
            sorted_items = tuple(sorted(self.items, key=lambda kv: kv[0]))
            object.__setattr__(self, "items", sorted_items)
        if len(self.items) > MAX_KEYS:
            raise ValueError(
                f"Object has {len(self.items)} keys, max is {MAX_KEYS}"
            )
        seen: set[str] = set()
        for k, _v in self.items:
            if not isinstance(k, str):
                raise TypeError(
                    f"FrozenJsonObject keys must be str, got {type(k).__name__}"
                )
            if k in seen:
                raise ValueError(f"Duplicate key in FrozenJsonObject: {k!r}")
            seen.add(k)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FrozenJsonObject):
            return self.items == other.items
        if isinstance(other, dict):
            return {k: v for k, v in self.items} == other
        return NotImplemented

    def __getitem__(self, key: str) -> "JsonValue":
        for k, v in self.items:
            if k == key:
                return v
        raise KeyError(key)

    def get(self, key: str, default: "JsonValue | None" = None) -> "JsonValue | None":
        for k, v in self.items:
            if k == key:
                return v
        return default

    def __contains__(self, key: str) -> bool:
        return any(k == key for k, _v in self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(k for k, _v in self.items)

    def keys(self) -> tuple[str, ...]:
        return tuple(k for k, _v in self.items)

    def values(self) -> tuple["JsonValue", ...]:
        return tuple(v for _k, v in self.items)

    def __repr__(self) -> str:
        inner = ", ".join(f"{k!r}: {v!r}" for k, v in self.items)
        return f"{{{inner}}}"


# Recursive type — defined after FrozenJsonObject so the class is available
JsonValue = JsonScalar | tuple["JsonValue", ...] | FrozenJsonObject
"""Complete JSON value type: scalar, array (as tuple), or object."""


# ── freeze / thaw ───────────────────────────────────────────────────────────

def freeze_json(value: Any, *, _depth: int = 0) -> JsonValue:
    """Deep-freeze a Python value into an immutable JsonValue.

    Raises TypeError/ValueError for unsupported types, NaN, Infinity,
    bytes, datetime, callable, custom objects, circular references,
    depth overflow, or key count overflow.
    """
    if _depth > MAX_DEPTH:
        raise ValueError(f"JSON nesting depth {_depth} exceeds max {MAX_DEPTH}")

    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if math.isnan(value):
            raise ValueError("NaN is not valid JSON")
        if math.isinf(value):
            raise ValueError("Infinity is not valid JSON")
        return value

    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_STRING_BYTES:
            raise ValueError(
                f"String too long: {len(value.encode('utf-8'))} bytes "
                f"(max {MAX_STRING_BYTES})"
            )
        return value

    if isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"bytes/bytearray/memoryview is not valid JSON")

    if isinstance(value, dict):
        if len(value) > MAX_KEYS:
            raise ValueError(f"Object has {len(value)} keys, max is {MAX_KEYS}")
        pairs: list[tuple[str, JsonValue]] = []
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(
                    f"JSON object keys must be str, got {type(k).__name__}"
                )
            frozen_v = freeze_json(v, _depth=_depth + 1)
            pairs.append((k, frozen_v))
        return FrozenJsonObject(items=tuple(sorted(pairs, key=lambda kv: kv[0])))

    if isinstance(value, FrozenJsonObject):
        # Already frozen — recursively validate children
        return FrozenJsonObject(items=tuple(
            (k, freeze_json(v, _depth=_depth + 1)) for k, v in value.items
        ))

    if isinstance(value, (list, tuple)):
        items = tuple(freeze_json(item, _depth=_depth + 1) for item in value)
        return items

    # Reject known bad types
    if isinstance(value, (set, frozenset)):
        raise TypeError("set/frozenset is not valid JSON")

    from datetime import date as _date, time as _time, datetime as _dt, timedelta as _td

    if isinstance(value, (_dt, _date, _time, _td)):
        raise TypeError(
            f"{type(value).__name__} is not valid JSON — "
            f"serialize to ISO-8601 string first"
        )

    if callable(value):
        raise TypeError(f"callable is not valid JSON: {value!r}")

    # Generic catch-all for other unsupported types
    raise TypeError(
        f"Cannot freeze {type(value).__name__} — not a valid JSON type"
    )


def thaw_json(value: JsonValue) -> Any:
    """Convert a frozen JsonValue back to mutable Python objects (dict/list).

    Returns a NEW mutable object every call — the caller owns it.
    Used only at adapter boundaries (e.g. tool parameter passing to SDK).
    """
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    if isinstance(value, FrozenJsonObject):
        return {k: thaw_json(v) for k, v in value.items}
    raise TypeError(f"Cannot thaw {type(value).__name__}")


# ── Helpers ─────────────────────────────────────────────────────────────────

def json_value_repr(value: JsonValue) -> str:
    """Compact repr string for a JsonValue (for debugging/logging)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, tuple):
        inner = ", ".join(json_value_repr(v) for v in value)
        return f"[{inner}]"
    if isinstance(value, FrozenJsonObject):
        inner = ", ".join(f"{json_value_repr(k)}: {json_value_repr(v)}"
                          for k, v in value.items)
        return f"{{{inner}}}"
    return repr(value)
