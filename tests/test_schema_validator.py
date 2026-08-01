"""P1-2: SchemaValidator — acceptance tests.

AC mappings:
  AC-1  Type coercion: string "123" → int 123
  AC-2  oneOf schema rejects invalid value
  AC-3  pattern constraint enforcement
  AC-4  Backward compat: all 68 existing tests pass
"""

from __future__ import annotations

import pytest

from core.schema_validator import SchemaValidator, ValidationResult, ValidationError


class TestBasicValidation:

    def test_valid_params_pass(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
            "required": ["name"],
        })
        r = v.safe_parse({"name": "test", "count": 42})
        assert r.valid

    def test_missing_required_fails(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        })
        r = v.safe_parse({})
        assert not r.valid
        assert any("required" in e.keyword.lower() or "name" in e.message.lower() for e in r.errors)


class TestTypeCoercion:

    def test_string_to_int(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {"timeout": {"type": "integer"}},
        })
        r = v.safe_parse({"timeout": "30"})
        assert r.valid
        assert r.coerced_params["timeout"] == 30
        assert isinstance(r.coerced_params["timeout"], int)

    def test_string_to_bool_true(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {"verbose": {"type": "boolean"}},
        })
        r = v.safe_parse({"verbose": "true"})
        assert r.valid
        assert r.coerced_params["verbose"] is True

    def test_string_to_bool_false(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {"verbose": {"type": "boolean"}},
        })
        r = v.safe_parse({"verbose": "false"})
        assert r.valid
        assert r.coerced_params["verbose"] is False

    def test_non_coercible_keeps_original(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {"count": {"type": "integer"}},
        })
        r = v.safe_parse({"count": "not_a_number"})
        assert not r.valid  # should fail validation


class TestOneOf:

    def test_one_of_valid(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {
                "result": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "integer"},
                    ]
                }
            },
        })
        r = v.safe_parse({"result": "ok"})
        assert r.valid
        r2 = v.safe_parse({"result": 42})
        assert r2.valid

    def test_one_of_invalid(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {
                "result": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "integer"},
                    ]
                }
            },
        })
        r = v.safe_parse({"result": True})
        assert not r.valid


class TestPattern:

    def test_pattern_valid(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {"email": {"type": "string", "pattern": "^\\S+@\\S+\\.\\S+$"}},
        })
        r = v.safe_parse({"email": "user@example.com"})
        assert r.valid

    def test_pattern_invalid(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {"email": {"type": "string", "pattern": "^\\S+@\\S+\\.\\S+$"}},
        })
        r = v.safe_parse({"email": "not-an-email"})
        assert not r.valid


class TestNestedObjects:

    def test_nested_valid(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer"},
                    },
                    "required": ["host"],
                }
            },
        })
        r = v.safe_parse({"config": {"host": "localhost", "port": 8080}})
        assert r.valid

    def test_nested_invalid(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "properties": {"host": {"type": "string"}},
                    "required": ["host"],
                }
            },
        })
        r = v.safe_parse({"config": {"port": 8080}})
        assert not r.valid


class TestFormatErrors:

    def test_format_for_llm(self):
        v = SchemaValidator({
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        })
        r = v.safe_parse({})
        assert not r.valid
        msg = v.format_errors_for_llm(r.errors)
        assert "Tool call validation failed" in msg
        assert "x" in msg
