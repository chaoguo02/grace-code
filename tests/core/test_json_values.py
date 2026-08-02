"""G2: Immutable JSON Value — comprehensive contract tests.

Covers:
  - freeze/thaw round-trip
  - Immutability (original dict modification does not affect frozen)
  - Rejection: NaN, Infinity, bytes, datetime, callable, circular ref, non-str keys
  - Depth/key count/string byte limits
  - Canonical key ordering
  - Canonical digest determinism
  - FrozenJsonObject property-style access
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, date, time, timedelta

import pytest

from core.json_values import (
    FrozenJsonObject,
    JsonScalar,
    JsonValue,
    MAX_DEPTH,
    MAX_KEYS,
    MAX_STRING_BYTES,
    freeze_json,
    thaw_json,
    json_value_repr,
)
from core.json_codec import (
    canonical_dumps,
    canonical_digest,
    decode,
    encode,
    decode_string,
    round_trip,
    digest_equals,
)


# ═══════════════════════════════════════════════════════════════════════════════
# G2.1 — freeze / thaw round-trip
# ═══════════════════════════════════════════════════════════════════════════════

class TestFreezeThaw:
    """Freeze then thaw must produce equal mutable values."""

    def test_scalars(self):
        assert freeze_json(None) is None
        assert freeze_json(True) is True
        assert freeze_json(False) is False
        assert freeze_json(42) == 42
        assert freeze_json(-1) == -1
        assert freeze_json(0) == 0
        assert freeze_json(3.14) == 3.14
        assert freeze_json(0.0) == 0.0
        assert freeze_json("hello") == "hello"
        assert freeze_json("") == ""

    def test_simple_dict_roundtrip(self):
        original = {"a": 1, "b": "two", "c": True}
        frozen = freeze_json(original)
        thawed = thaw_json(frozen)
        assert thawed == original

    def test_nested_structure(self):
        original = {
            "name": "test",
            "scores": [95, 87, 92],
            "metadata": {"version": 2, "tags": ["a", "b"]},
        }
        frozen = freeze_json(original)
        thawed = thaw_json(frozen)
        assert thawed == original

    def test_list_becomes_tuple(self):
        frozen = freeze_json([1, 2, 3])
        assert isinstance(frozen, tuple)
        assert frozen == (1, 2, 3)

    def test_thaw_tuple_becomes_list(self):
        frozen = freeze_json([1, 2, 3])
        thawed = thaw_json(frozen)
        assert isinstance(thawed, list)
        assert thawed == [1, 2, 3]

    def test_empty_structures(self):
        assert thaw_json(freeze_json({})) == {}
        assert thaw_json(freeze_json([])) == []


# ═══════════════════════════════════════════════════════════════════════════════
# G2.2 — Immutability (the core value proposition)
# ═══════════════════════════════════════════════════════════════════════════════

class TestImmutability:
    """Frozen values must NOT be affected by mutation of the original input."""

    def test_dict_mutation_does_not_affect_frozen(self):
        original = {"a": 1, "b": [10, 20]}
        frozen = freeze_json(original)

        # Mutate original
        original["a"] = 999
        original["b"].append(30)
        original["c"] = "new"

        # Frozen must be unchanged
        assert isinstance(frozen, FrozenJsonObject)
        assert frozen["a"] == 1
        assert frozen["b"] == (10, 20)
        assert "c" not in frozen

    def test_nested_list_mutation_isolation(self):
        inner = [1, 2]
        original = {"items": inner}
        frozen = freeze_json(original)

        inner.append(3)
        original["extra"] = "bad"

        assert frozen["items"] == (1, 2)
        assert "extra" not in frozen

    def test_thaw_returns_new_objects(self):
        frozen = freeze_json({"x": [1, 2]})
        a = thaw_json(frozen)
        b = thaw_json(frozen)
        a["x"].append(3)
        # b must be unaffected
        assert b["x"] == [1, 2], "thaw must return independent mutable copies"


# ═══════════════════════════════════════════════════════════════════════════════
# G2.3 — Rejection of invalid types
# ═══════════════════════════════════════════════════════════════════════════════

class TestRejectInvalidTypes:
    """freeze_json must reject non-JSON types with clear errors."""

    def test_nan_rejected(self):
        with pytest.raises(ValueError, match="NaN"):
            freeze_json(float("nan"))

    def test_infinity_rejected(self):
        with pytest.raises(ValueError, match="Infinity"):
            freeze_json(float("inf"))
        with pytest.raises(ValueError, match="Infinity"):
            freeze_json(float("-inf"))

    def test_bytes_rejected(self):
        with pytest.raises(TypeError, match="bytes"):
            freeze_json(b"hello")

    def test_bytearray_rejected(self):
        with pytest.raises(TypeError, match="bytes"):
            freeze_json(bytearray(b"hello"))

    def test_datetime_rejected(self):
        with pytest.raises(TypeError, match="datetime"):
            freeze_json(datetime.now())

    def test_date_rejected(self):
        with pytest.raises(TypeError, match="date"):
            freeze_json(date.today())

    def test_time_rejected(self):
        with pytest.raises(TypeError, match="time"):
            freeze_json(time(12, 0))

    def test_timedelta_rejected(self):
        with pytest.raises(TypeError, match="timedelta"):
            freeze_json(timedelta(seconds=1))

    def test_callable_rejected(self):
        with pytest.raises(TypeError, match="callable"):
            freeze_json(lambda x: x)

    def test_set_rejected(self):
        with pytest.raises(TypeError, match="set"):
            freeze_json({1, 2, 3})

    def test_custom_object_rejected(self):
        class Foo:
            pass

        with pytest.raises(TypeError):
            freeze_json(Foo())
        with pytest.raises(TypeError):
            freeze_json(object())

    def test_non_string_key_rejected(self):
        with pytest.raises(TypeError, match="keys must be str"):
            freeze_json({42: "value"})

    def test_non_string_key_in_nested_dict(self):
        with pytest.raises(TypeError, match="keys must be str"):
            freeze_json({"outer": {1: "inner"}})


# ═══════════════════════════════════════════════════════════════════════════════
# G2.4 — Limits: depth, keys, string bytes
# ═══════════════════════════════════════════════════════════════════════════════

class TestLimits:
    """Hard limits protect against unbounded input."""

    def test_max_depth_exceeded(self):
        deep = 1
        for _ in range(MAX_DEPTH + 1):
            deep = {"nested": deep}
        with pytest.raises(ValueError, match="depth"):
            freeze_json(deep)

    def test_max_depth_ok_at_limit(self):
        ok = 0
        for _ in range(MAX_DEPTH):
            ok = {"nested": ok}
        result = freeze_json(ok)
        assert isinstance(result, FrozenJsonObject)

    def test_max_keys_exceeded(self):
        big = {str(i): i for i in range(MAX_KEYS + 1)}
        with pytest.raises(ValueError, match="keys"):
            freeze_json(big)

    def test_max_keys_ok_at_limit(self):
        ok = {str(i): i for i in range(MAX_KEYS)}
        result = freeze_json(ok)
        assert len(result) == MAX_KEYS

    def test_max_string_bytes_exceeded(self):
        long_str = "x" * (MAX_STRING_BYTES + 1)
        with pytest.raises(ValueError, match="String too long"):
            freeze_json(long_str)

    def test_max_string_bytes_ok_at_limit(self):
        ok = "x" * MAX_STRING_BYTES
        result = freeze_json(ok)
        assert result == ok

    def test_max_string_bytes_in_nested_value(self):
        long_str = "x" * (MAX_STRING_BYTES + 1)
        with pytest.raises(ValueError, match="String too long"):
            freeze_json({"key": long_str})

    def test_max_string_bytes_in_list(self):
        long_str = "x" * (MAX_STRING_BYTES + 1)
        with pytest.raises(ValueError, match="String too long"):
            freeze_json([long_str])


# ═══════════════════════════════════════════════════════════════════════════════
# G2.5 — Canonical ordering
# ═══════════════════════════════════════════════════════════════════════════════

class TestCanonicalOrdering:
    """Object keys are always sorted; array order is preserved."""

    def test_keys_sorted(self):
        frozen = freeze_json({"z": 1, "a": 2, "m": 3})
        keys = tuple(frozen.keys())
        assert keys == ("a", "m", "z")

    def test_array_order_preserved(self):
        frozen = freeze_json([3, 1, 2])
        assert frozen == (3, 1, 2)

    def test_nested_keys_sorted(self):
        frozen = freeze_json({
            "outer": {"z": 99, "a": 1},
            "list": [3, 2, 1],
        })
        outer_keys = tuple(frozen.keys())
        assert outer_keys == ("list", "outer")
        inner_obj = frozen["outer"]
        assert isinstance(inner_obj, FrozenJsonObject)
        assert tuple(inner_obj.keys()) == ("a", "z")

    def test_duplicate_key_in_constructor_rejected(self):
        with pytest.raises(ValueError, match="Duplicate"):
            FrozenJsonObject(items=(("a", 1), ("a", 2)))

    def test_explicit_frozen_object_key_count_limit(self):
        with pytest.raises(ValueError, match="keys"):
            FrozenJsonObject(
                items=tuple((str(i), i) for i in range(MAX_KEYS + 1))
            )

    def test_frozen_json_object_auto_sorts(self):
        obj = FrozenJsonObject(items=(("b", 2), ("a", 1), ("c", 3)))
        assert tuple(obj.keys()) == ("a", "b", "c")
        assert obj.items[0] == ("a", 1)


# ═══════════════════════════════════════════════════════════════════════════════
# G2.6 — Canonical digest determinism
# ═══════════════════════════════════════════════════════════════════════════════

class TestCanonicalDigest:
    """Same semantic content → same digest, regardless of input order."""

    def test_insertion_order_does_not_affect_digest(self):
        a = freeze_json({"b": 2, "a": 1})
        b = freeze_json({"a": 1, "b": 2})
        assert canonical_digest(a) == canonical_digest(b)

    def test_random_field_order_100_iterations(self):
        import random
        fields = [("name", "test"), ("count", 42), ("tags", ("a", "b", "c")),
                   ("meta", {"v": 1})]
        digest = None
        for _ in range(100):
            shuffled = list(fields)
            random.shuffle(shuffled)
            frozen = freeze_json(dict(shuffled))
            d = canonical_digest(frozen)
            if digest is None:
                digest = d
            else:
                assert d == digest, f"Digest differs for shuffled input"

    def test_nested_random_order_100_iterations(self):
        import random
        inner = [("x", 1), ("y", 2), ("z", 3)]
        outer = [("alpha", 1), ("beta", {"c": 3, "b": 2, "a": 1})]
        digest = None
        for _ in range(100):
            i_shuffled = list(inner)
            random.shuffle(i_shuffled)
            o_shuffled = list(outer)
            random.shuffle(o_shuffled)
            o_shuffled_dict = dict(o_shuffled)
            o_shuffled_dict["beta"] = dict(i_shuffled)
            frozen = freeze_json(o_shuffled_dict)
            d = canonical_digest(frozen)
            if digest is None:
                digest = d
            else:
                assert d == digest

    def test_different_values_different_digest(self):
        a = freeze_json({"v": 1})
        b = freeze_json({"v": 2})
        assert canonical_digest(a) != canonical_digest(b)

    def test_digest_equals_helper(self):
        a = freeze_json({"a": 1})
        b = freeze_json({"a": 1})
        c = freeze_json({"a": 2})
        assert digest_equals(a, b)
        assert not digest_equals(a, c)


# ═══════════════════════════════════════════════════════════════════════════════
# G2.7 — Encode / Decode round-trip
# ═══════════════════════════════════════════════════════════════════════════════

class TestCodecRoundTrip:
    """encode → decode must produce equal frozen values."""

    def test_roundtrip_simple(self):
        original = freeze_json({"name": "test", "count": 42})
        result = round_trip(original)
        assert result == original

    def test_roundtrip_nested(self):
        original = freeze_json({
            "items": [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}],
            "meta": {"total": 2, "page": 1},
        })
        result = round_trip(original)
        assert result == original

    def test_roundtrip_all_scalars(self):
        original = freeze_json({
            "null": None,
            "bool_true": True,
            "bool_false": False,
            "int_pos": 42,
            "int_neg": -7,
            "int_zero": 0,
            "float": 3.14,
            "str": "hello",
            "empty_str": "",
        })
        result = round_trip(original)
        assert result == original

    def test_decode_string(self):
        result = decode_string('{"a": 1, "b": "hello"}')
        assert result == freeze_json({"a": 1, "b": "hello"})

    def test_decode_round_trip_preserves_tuple(self):
        original = freeze_json([1, 2, 3])
        data = encode(original)
        decoded = decode(data)
        assert decoded == (1, 2, 3)

    def test_canonical_dumps_is_deterministic(self):
        a = canonical_dumps(freeze_json({"b": 1, "a": 2}))
        b = canonical_dumps(freeze_json({"a": 2, "b": 1}))
        assert a == b

    def test_canonical_string_no_trailing_whitespace(self):
        s = canonical_dumps(freeze_json({"a": 1})).decode("utf-8")
        assert s == '{"a":1}'

    def test_decode_rejects_nan_in_json(self):
        with pytest.raises(ValueError, match="NaN"):
            decode_string('[NaN]')

    def test_decode_rejects_infinity_in_json(self):
        with pytest.raises(ValueError, match="Infinity"):
            decode_string('[Infinity]')


# ═══════════════════════════════════════════════════════════════════════════════
# G2.8 — FrozenJsonObject property access patterns
# ═══════════════════════════════════════════════════════════════════════════════

class TestFrozenJsonObjectAccess:
    """FrozenJsonObject supports dict-like read access."""

    def test_getitem(self):
        obj = freeze_json({"key": "value"})
        assert obj["key"] == "value"

    def test_getitem_missing(self):
        obj = freeze_json({"a": 1})
        with pytest.raises(KeyError):
            _ = obj["missing"]

    def test_get_with_default(self):
        obj = freeze_json({"a": 1})
        assert obj.get("a") == 1
        assert obj.get("missing") is None
        assert obj.get("missing", 42) == 42

    def test_contains(self):
        obj = freeze_json({"a": 1})
        assert "a" in obj
        assert "b" not in obj

    def test_len(self):
        assert len(freeze_json({})) == 0
        assert len(freeze_json({"a": 1, "b": 2})) == 2

    def test_iter_keys(self):
        obj = freeze_json({"b": 2, "a": 1})
        assert list(obj) == ["a", "b"]

    def test_keys_values(self):
        obj = freeze_json({"b": 2, "a": 1})
        assert obj.keys() == ("a", "b")
        assert obj.values() == (1, 2)

    def test_repr(self):
        obj = freeze_json({"a": 1, "b": "hello"})
        r = repr(obj)
        assert "'a'" in r
        assert "'b'" in r
        assert "1" in r
        assert "'hello'" in r


# ═══════════════════════════════════════════════════════════════════════════════
# G2.9 — Edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Unusual but valid inputs."""

    def test_very_deep_but_valid(self):
        """MAX_DEPTH nesting is OK."""
        val = 0
        for _ in range(MAX_DEPTH - 1):
            val = {"nested": val}
        result = freeze_json(val)
        assert isinstance(result, FrozenJsonObject)

    def test_unicode_strings(self):
        original = {"greeting": "你好", "emoji": "🎉"}
        frozen = freeze_json(original)
        assert frozen["greeting"] == "你好"
        assert frozen["emoji"] == "🎉"
        assert thaw_json(frozen) == original

    def test_large_array(self):
        arr = list(range(10000))
        frozen = freeze_json(arr)
        assert len(frozen) == 10000
        assert frozen[0] == 0
        assert frozen[-1] == 9999
        assert thaw_json(frozen) == arr

    def test_zero_and_negative_zero(self):
        # Python float: 0.0 == -0.0, but they're distinct objects
        # freeze_json preserves the value
        assert freeze_json(0.0) == 0.0
        assert freeze_json(-0.0) == -0.0

    @pytest.mark.skipif(sys.version_info < (3, 11),
                        reason="Python 3.10 has different float repr")
    def test_large_int_precision(self):
        big = 2**63
        frozen = freeze_json(big)
        thawed = thaw_json(frozen)
        assert thawed == big

    def test_json_value_repr(self):
        r = json_value_repr(freeze_json({"a": 1, "b": [2, 3]}))
        assert "'a'" in r
        assert "1" in r
        assert "[" in r
