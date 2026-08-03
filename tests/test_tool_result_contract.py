"""Tests for normalized tool result and observation contract."""

from core.base import ToolResult
from core.errors import ToolErrorType
from core.types import ToolOutcome
from observability.models import build_replay_tool_execution, build_tool_output


def test_permission_denied_maps_to_blocked():
    result = ToolResult.from_error(ToolErrorType.PERMISSION_DENIED, "nope")
    assert result.normalized_outcome() is ToolOutcome.BLOCKED
    obs = result.to_observation("Read")
    assert obs.outcome is ToolOutcome.BLOCKED
    assert obs.error


def test_unavailable_maps_to_blocked():
    result = ToolResult.from_error(ToolErrorType.UNAVAILABLE, "blocked")
    assert result.normalized_outcome() is ToolOutcome.BLOCKED


def test_invalid_params_maps_to_blocked():
    result = ToolResult.from_error(ToolErrorType.INVALID_PARAMS, "bad params")
    assert result.normalized_outcome() is ToolOutcome.BLOCKED
    obs = result.to_observation("Read")
    assert obs.outcome is ToolOutcome.BLOCKED


def test_timeout_maps_to_failed():
    result = ToolResult.from_error(ToolErrorType.TIMEOUT, "timeout")
    assert result.normalized_outcome() is ToolOutcome.FAILED


def test_empty_success_maps_to_empty():
    result = ToolResult(success=True, output="")
    assert result.normalized_outcome() is ToolOutcome.EMPTY
    assert result.to_observation("Read").outcome is ToolOutcome.EMPTY


def test_whitespace_only_success_maps_to_empty():
    result = ToolResult(success=True, output="   \n  ")
    assert result.normalized_outcome() is ToolOutcome.EMPTY


def test_explicit_partial_outcome_survives():
    result = ToolResult(success=True, output="partial", outcome=ToolOutcome.PARTIAL)
    assert result.normalized_outcome() is ToolOutcome.PARTIAL
    assert result.to_observation("Read").outcome is ToolOutcome.PARTIAL


def test_explicit_blocked_outcome_survives():
    result = ToolResult(success=False, output="", outcome=ToolOutcome.BLOCKED)
    assert result.normalized_outcome() is ToolOutcome.BLOCKED
    obs = result.to_observation("Read")
    assert obs.outcome is ToolOutcome.BLOCKED


def test_explicit_skipped_outcome_survives():
    result = ToolResult(success=False, output="", outcome=ToolOutcome.SKIPPED)
    assert result.normalized_outcome() is ToolOutcome.SKIPPED


def test_generic_failure_without_tool_error_maps_to_failed():
    result = ToolResult(success=False, output="", error="something broke")
    assert result.normalized_outcome() is ToolOutcome.FAILED


def test_tool_output_snapshot_carries_outcome():
    result = ToolResult(success=True, output="hello", outcome=ToolOutcome.NONE)
    payload = build_tool_output(result, capture_tool_outputs=True)
    assert payload["success"] is True
    assert payload["outcome"] == ToolOutcome.NONE.value
    assert payload["output"] == "hello"


def test_tool_output_snapshot_blocked():
    result = ToolResult.from_error(ToolErrorType.PERMISSION_DENIED, "denied")
    payload = build_tool_output(result, capture_tool_outputs=False)
    assert payload["success"] is False
    assert payload["outcome"] == ToolOutcome.BLOCKED.value


def test_replay_tool_execution_carries_outcome():
    result = ToolResult(success=True, output="hello", outcome=ToolOutcome.PARTIAL)
    obs = result.to_observation("Read")
    snapshot = build_replay_tool_execution(obs, tool_call_id="abc", params={"path": "x"})
    assert snapshot.outcome == ToolOutcome.PARTIAL.value
    assert snapshot.success is True


# ── Validation rejection → observation contract ──


def test_unknown_tool_validation_result_maps_to_blocked_observation():
    """validate_tool_calls rejects unknown tool → synthetic observation carries BLOCKED."""
    from llm.tool_call_validator import validate_tool_calls
    from core.base import ToolErrorType as _TE
    from core.base import ToolRetryDirective
    from agent.task import ToolCall

    # Build a schema that has "Read" but not "FakeTool"
    from llm.base import LLMToolSchema
    schemas = [
        LLMToolSchema(
            name="Read",
            description="Read a file",
            parameters={"type": "object", "properties": {}, "required": []},
        ),
    ]
    tool_calls = [ToolCall(id="1", name="FakeTool", params={})]
    validation = validate_tool_calls(tool_calls, schemas)
    assert not validation.valid
    assert validation.error_type == "unknown_tool"

    # This is what core.py does on validation failure
    fake_result = ToolResult.from_error(
        error_type=_TE.INVALID_PARAMS,
        retry=ToolRetryDirective.RETRY,
        detail=validation.error_message,
    )
    obs = fake_result.to_observation(validation.offending_tool or "unknown")
    assert obs.outcome is ToolOutcome.BLOCKED
    assert not obs.is_success()


def test_missing_required_param_validation_maps_to_blocked():
    """validate_tool_calls rejects missing required param → BLOCKED observation."""
    from llm.tool_call_validator import validate_tool_calls
    from core.base import ToolErrorType as _TE
    from core.base import ToolRetryDirective
    from agent.task import ToolCall

    from llm.base import LLMToolSchema
    schemas = [
        LLMToolSchema(
            name="Write",
            description="Write a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
    ]
    tool_calls = [ToolCall(id="1", name="Write", params={})]  # missing "path"
    validation = validate_tool_calls(tool_calls, schemas)
    assert not validation.valid
    # M2: required-field enforcement now goes through the single SchemaValidator
    # authority, so missing-required maps to the generic invalid_params type.
    assert validation.error_type == "invalid_params"
    assert "Missing required parameter 'path'" in validation.error_message

    fake_result = ToolResult.from_error(
        error_type=_TE.INVALID_PARAMS,
        retry=ToolRetryDirective.RETRY,
        detail=validation.error_message,
    )
    obs = fake_result.to_observation(validation.offending_tool or "unknown")
    assert obs.outcome is ToolOutcome.BLOCKED


def test_duplicate_call_validation_maps_to_blocked():
    """validate_tool_calls rejects duplicate calls → BLOCKED observation."""
    from llm.tool_call_validator import validate_tool_calls
    from core.base import ToolErrorType as _TE
    from core.base import ToolRetryDirective
    from agent.task import ToolCall

    from llm.base import LLMToolSchema
    schemas = [
        LLMToolSchema(
            name="Read",
            description="Read a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
    ]
    tool_calls = [
        ToolCall(id="1", name="Read", params={"path": "a.txt"}),
        ToolCall(id="2", name="Read", params={"path": "a.txt"}),  # duplicate
    ]
    validation = validate_tool_calls(tool_calls, schemas)
    assert not validation.valid
    assert validation.error_type == "duplicate_call"

    fake_result = ToolResult.from_error(
        error_type=_TE.INVALID_PARAMS,
        retry=ToolRetryDirective.RETRY,
        detail=validation.error_message,
    )
    obs = fake_result.to_observation(validation.offending_tool or "unknown")
    assert obs.outcome is ToolOutcome.BLOCKED


def test_invalid_param_type_validation_maps_to_blocked():
    """validate_tool_calls rejects wrong param type → BLOCKED observation."""
    from llm.tool_call_validator import validate_tool_calls
    from core.base import ToolErrorType as _TE
    from core.base import ToolRetryDirective
    from agent.task import ToolCall

    from llm.base import LLMToolSchema
    schemas = [
        LLMToolSchema(
            name="Read",
            description="Read a file",
            parameters={
                "type": "object",
                "properties": {"offset": {"type": "integer"}},
                "required": [],
            },
        ),
    ]
    tool_calls = [ToolCall(id="1", name="Read", params={"offset": "not_a_number"})]
    validation = validate_tool_calls(tool_calls, schemas)
    assert not validation.valid
    assert validation.error_type == "invalid_params"

    fake_result = ToolResult.from_error(
        error_type=_TE.INVALID_PARAMS,
        retry=ToolRetryDirective.RETRY,
        detail=validation.error_message,
    )
    obs = fake_result.to_observation(validation.offending_tool or "unknown")
    assert obs.outcome is ToolOutcome.BLOCKED


def test_nested_schema_validation_rejects_invalid_array_item_and_enum():
    from agent.task import ToolCall
    from llm.base import LLMToolSchema
    from llm.tool_call_validator import validate_tool_calls

    schema = LLMToolSchema(
        name="Batch",
        description="batch",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["read", "write"],
                            },
                        },
                        "required": ["mode"],
                    },
                },
            },
            "required": ["items"],
        },
    )

    validation = validate_tool_calls(
        [ToolCall(name="Batch", params={"items": [{"mode": "delete"}]})],
        [schema],
    )

    assert validation.valid is False
    assert validation.error_type == "invalid_params"
    assert "params.items[0].mode" in validation.error_message


# ── Policy blocked → observation contract ──


def test_policy_blocked_tool_result_carries_blocked_outcome():
    """PolicyAwareToolRegistry blocked call → observation carries BLOCKED."""
    result = ToolResult(success=False, output="", error="blocked by policy", outcome=ToolOutcome.BLOCKED)
    obs = result.to_observation("Write")
    assert obs.outcome is ToolOutcome.BLOCKED
    assert not obs.is_success()
    assert obs.error == "blocked by policy"
