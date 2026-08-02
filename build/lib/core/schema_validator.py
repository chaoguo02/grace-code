"""
CC-Native JSON Schema validator (P1-2).

Uses jsonschema library for standard-compliant validation.
Implements the Zod safeParse pattern: never throws, returns structured result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationError:
    """Structured validation error — path + message + schema context."""
    path: str          # JSON pointer to the invalid field, e.g. "/params/timeout"
    message: str       # Human-readable error
    schema_path: str   # JSON pointer to the failing schema constraint
    keyword: str       # The JSON Schema keyword that failed ("type","required","enum",...)

    def to_llm_feedback(self) -> str:
        return f"  - {self.path}: {self.message}"


@dataclass
class ValidationResult:
    """CC-aligned safeParse result — never throws."""
    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    coerced_params: dict[str, Any] | None = None


class SchemaValidator:
    """CC-aligned JSON Schema validator using jsonschema library.

    Supports JSON Schema draft-07 as baseline (compatible with most LLM
    function-calling schemas).

    Key behaviors:
    - safe_parse(): validate + coerce, never throws
    - format_errors_for_llm(): structured feedback for model self-correction
    - Type coercion: string "123" → int 123, string "true" → bool True
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        self._schema = schema
        self._required = schema.get("required", []) or []
        self._properties = schema.get("properties", {}) or {}

    def safe_parse(self, params: dict[str, Any]) -> ValidationResult:
        """Validate *params* against the schema. Never throws.

        Returns ValidationResult with:
        - valid=True, coerced_params=coerced dict on success
        - valid=False, errors=list on failure
        """
        import jsonschema

        errors: list[ValidationError] = []
        coerced: dict[str, Any] = dict(params)

        # Step 1: type coercion (string→int, string→bool, string→null)
        for key, value in list(coerced.items()):
            prop_schema = self._properties.get(key, {})
            coerced[key] = self._coerce_value(value, prop_schema)

        # Step 2: jsonschema validation
        try:
            validator = jsonschema.Draft7Validator(self._schema)
            validation_errors = list(validator.iter_errors(coerced))
        except jsonschema.SchemaError as exc:
            return ValidationResult(
                valid=False,
                errors=[ValidationError(
                    path="/",
                    message=f"Schema error: {exc.message}",
                    schema_path="/",
                    keyword="schema",
                )],
            )

        if not validation_errors:
            return ValidationResult(valid=True, coerced_params=coerced)

        # Step 3: convert jsonschema errors to structured format
        for err in validation_errors:
            path = "/" + "/".join(str(p) for p in err.absolute_path) if err.absolute_path else "/"
            errors.append(ValidationError(
                path=path,
                message=err.message,
                schema_path="/" + "/".join(str(p) for p in err.absolute_schema_path),
                keyword=err.validator,
            ))

        return ValidationResult(valid=False, errors=errors)

    def format_errors_for_llm(self, errors: list[ValidationError]) -> str:
        """Format validation errors as LLM-readable feedback.

        CC pattern: structured errors help the model self-correct on the next turn.
        """
        lines = ["Tool call validation failed:"]
        for e in errors:
            lines.append(e.to_llm_feedback())
        return "\n".join(lines)

    @staticmethod
    def _coerce_value(value: Any, prop_schema: dict[str, Any]) -> Any:
        """Coerce string values to the expected type when safe.

        Handles common LLM mistakes: sending numbers as strings,
        booleans as strings, null as string "null".
        """
        prop_type = prop_schema.get("type", "")
        if not isinstance(value, str):
            return value

        if prop_type == "integer":
            try:
                return int(value)
            except (ValueError, TypeError):
                return value

        if prop_type == "number":
            try:
                return float(value)
            except (ValueError, TypeError):
                return value

        if prop_type == "boolean":
            if value.lower() in ("true", "1", "yes"):
                return True
            if value.lower() in ("false", "0", "no", ""):
                return False
            return value

        if prop_type == "null" and value.lower() in ("null", "none", ""):
            return None

        # Const coercion: if schema has "const", try matching
        if "const" in prop_schema:
            const_val = prop_schema["const"]
            if isinstance(const_val, int):
                try:
                    return int(value)
                except (ValueError, TypeError):
                    pass
            elif isinstance(const_val, float):
                try:
                    return float(value)
                except (ValueError, TypeError):
                    pass

        return value
