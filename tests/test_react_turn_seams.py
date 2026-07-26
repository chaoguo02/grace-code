from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.loop.turns import (
    ActionContractStatus,
    CompletionOutcome,
    CompletionFacts,
    CompletionRetrySource,
    PreStepOutcome,
    PostObservationOutcome,
    ProviderErrorOutcome,
    ProviderTurn,
    OutputRecoveryOutcome,
    ToolResultAnalysis,
    analyze_tool_result,
    build_action_history,
    complete_run,
    evaluate_completion,
    evaluate_early_step_gate,
    evaluate_observation_batch,
    evaluate_output_recovery,
    evaluate_post_observation,
    evaluate_provider_error,
    evaluate_runtime_step_gate,
    execute_action,
    invoke_provider_turn,
    prepare_provider_request,
    prepare_turn,
    validate_action_contract,
)
from agent.core import ReActAgent
from agent.recovery import AgentTurnState
from context.history import ConversationSnapshotError
from agent.completion_guard import CompletionCheckResult
from agent.loop.types import CompletionBlockTracker
from agent.runtime_controller import StepAction, StepDecision
from agent.session.task_state_machine import GuardResult
from agent.task import (
    Action,
    ActionType,
    RunStatus,
    TerminationReason,
    ToolCall,
    VerificationReason,
    VerificationStatus,
)
from core.base import (
    ToolEffect,
    ToolErrorType,
    ToolMetadata,
    ToolResult,
    ToolRole,
)
from llm.base import (
    CacheStats,
    LLMMessage,
    LLMResponse,
    LLMToolSchema,
    MessageKind,
)


def test_prepare_turn_freezes_provider_inputs():
    registry = MagicMock()
    bound_registry = object()
    registry.with_run_context.return_value = bound_registry
    messages = [LLMMessage(role="user", content="hello")]
    context = object()

    prepared = prepare_turn(messages, [], registry, context)
    messages.append(LLMMessage(role="user", content="later mutation"))

    assert len(prepared.messages) == 1
    assert prepared.tools == ()
    assert prepared.execution_registry is bound_registry
    registry.with_run_context.assert_called_once_with(context)


@dataclass(frozen=True)
class _RequestContext:
    spawn_context: object | None = None
    delegation_width: int = 1


def test_prepare_provider_request_filters_new_spawns_during_child_phase():
    registry = MagicMock()
    registry.get_schemas.return_value = [
        LLMToolSchema("Agent", "spawn", {}),
        LLMToolSchema("Read", "read", {}),
    ]
    registry.metadata_for.side_effect = lambda name: (
        ToolMetadata(roles=frozenset({ToolRole.DELEGATE}))
        if name == "Agent"
        else ToolMetadata()
    )
    state = MagicMock()
    updated_state = object()
    state.with_updates.return_value = updated_state

    request = prepare_provider_request(
        messages=[LLMMessage(role="user", content="work")],
        history_messages=[LLMMessage(role="user", content="work")],
        registry=registry,
        execution_context=_RequestContext(),
        state=state,
        step=2,
        total_tokens=10,
        strip_tools=False,
        child_phase_active=True,
        parent_session_id="session",
        parent_agent_name="primary",
        repo_path=".",
        model_name="model",
    )

    assert [tool.name for tool in request.turn.tools] == ["Read"]
    assert len(request.turn.messages) == 2
    assert request.turn.messages[-1].role == "system"
    assert request.turn.messages[-1].kind is MessageKind.RUNTIME_NOTICE
    assert "Agent tool is intentionally unavailable" in (
        request.turn.messages[-1].content
    )
    assert request.state is updated_state
    assert request.spawn_context is None
    state_messages = state.with_updates.call_args.kwargs["messages"]
    assert len(state_messages) == 1


def test_prepare_provider_request_binds_delegation_snapshot():
    schema = LLMToolSchema("Delegate", "delegate", {})
    registry = MagicMock()
    registry.get_schemas.return_value = [schema]
    registry.metadata_for.return_value = ToolMetadata(
        roles=frozenset({ToolRole.DELEGATE}),
    )
    state = MagicMock()
    snapshot = object()

    request = prepare_provider_request(
        messages=[LLMMessage(role="user", content="work")],
        history_messages=[LLMMessage(role="user", content="work")],
        registry=registry,
        execution_context=_RequestContext(),
        state=state,
        step=1,
        total_tokens=0,
        strip_tools=False,
        child_phase_active=False,
        parent_session_id="session",
        parent_agent_name="primary",
        repo_path=".",
        model_name="model",
        spawn_context_factory=lambda **kwargs: snapshot,
    )

    assert request.spawn_context is snapshot
    bound_context = registry.with_run_context.call_args.args[0]
    assert bound_context.spawn_context is snapshot


def test_prepare_provider_request_falls_back_when_snapshot_is_invalid():
    registry = MagicMock()
    registry.get_schemas.return_value = [
        LLMToolSchema("Delegate", "delegate", {}),
    ]
    registry.metadata_for.return_value = ToolMetadata(
        roles=frozenset({ToolRole.DELEGATE}),
    )

    def _fail_snapshot(**kwargs):
        raise ConversationSnapshotError("broken tool pairing")

    request = prepare_provider_request(
        messages=[LLMMessage(role="user", content="work")],
        history_messages=[LLMMessage(role="user", content="work")],
        registry=registry,
        execution_context=_RequestContext(),
        state=MagicMock(),
        step=1,
        total_tokens=0,
        strip_tools=False,
        child_phase_active=False,
        parent_session_id="session",
        parent_agent_name="primary",
        repo_path=".",
        model_name="model",
        spawn_context_factory=_fail_snapshot,
    )

    assert request.spawn_context is None
    bound_context = registry.with_run_context.call_args.args[0]
    assert bound_context.spawn_context is None


def test_early_step_gate_prioritizes_cancellation():
    result = evaluate_early_step_gate(
        step=4,
        cancellation_requested=True,
        cancellation_detail="user stopped",
        permission_circuit_tripped=True,
    )

    assert result.outcome is PreStepOutcome.TERMINATE
    assert result.status is RunStatus.CANCELLED
    assert result.steps_taken == 3
    assert result.cancelled


def test_early_step_gate_stops_on_permission_circuit():
    result = evaluate_early_step_gate(
        step=2,
        cancellation_requested=False,
        cancellation_detail="",
        permission_circuit_tripped=True,
    )

    assert result.outcome is PreStepOutcome.TERMINATE
    assert result.status is RunStatus.GAVE_UP
    assert "permission circuit breaker" in result.summary


def test_runtime_step_gate_short_circuits_guard_on_controller_termination():
    guard = MagicMock()
    result = evaluate_runtime_step_gate(
        step=3,
        controller_check=lambda: StepDecision(
            action=StepAction.TERMINATE,
            terminate_status=RunStatus.MAX_STEPS,
            terminate_summary="step limit",
            terminate_reason=TerminationReason.MAX_STEPS,
            terminate_detail="step limit",
        ),
        guard_check=guard,
    )

    assert result.outcome is PreStepOutcome.TERMINATE
    assert result.status is RunStatus.MAX_STEPS
    assert result.termination_reason is TerminationReason.MAX_STEPS
    guard.assert_not_called()


def test_runtime_step_gate_converts_terminating_guard():
    decision = StepDecision(
        action=StepAction.INJECT_MESSAGE,
        inject_message="budget warning",
        strip_tools=True,
    )
    result = evaluate_runtime_step_gate(
        step=3,
        controller_check=lambda: decision,
        guard_check=lambda: GuardResult(
            passed=False,
            reason="guard rejected",
            terminate=True,
        ),
    )

    assert result.outcome is PreStepOutcome.TERMINATE
    assert result.decision is decision
    assert result.termination_reason is TerminationReason.GUARD_REJECTED


def test_invoke_provider_turn_normalizes_classic_token_accounting():
    action = Action(ActionType.FINISH, "done", message="complete")
    response = LLMResponse(
        action=action,
        raw_content="complete",
        input_tokens=100,
        output_tokens=20,
        cache_stats=CacheStats(cache_read_tokens=40),
    )
    prepared = prepare_turn([], [], MagicMock(), object())

    turn = invoke_provider_turn(
        prepared,
        streaming=False,
        stream_call=MagicMock(),
        complete_call=lambda messages, tools: response,
    )

    assert turn.action is action
    assert turn.response is response
    assert turn.billable_tokens == 80
    assert turn.cache_stats is response.cache_stats


def test_invoke_provider_turn_prepares_streaming_executor():
    action = Action(ActionType.FINISH, "done", message="complete")
    prepared = prepare_turn(
        [LLMMessage(role="user", content="hello")],
        [],
        MagicMock(),
        object(),
    )
    complete_call = MagicMock()

    def _stream(messages, tools, executor):
        assert messages[0].content == "hello"
        assert tools == []
        assert executor is not None
        return action

    turn = invoke_provider_turn(
        prepared,
        streaming=True,
        stream_call=_stream,
        complete_call=complete_call,
    )

    assert turn.action is action
    assert turn.response is None
    assert turn.streaming_executor is not None
    assert turn.billable_tokens > 0
    complete_call.assert_not_called()


def test_provider_error_prefers_runtime_recovery():
    state = AgentTurnState()
    recovered_state = state.with_updates(turn_count=2)

    result = evaluate_provider_error(
        RuntimeError("context length exceeded"),
        state=state,
        streaming=False,
        recover=lambda error, current: (recovered_state, True),
    )

    assert result.outcome is ProviderErrorOutcome.RETRY
    assert result.state is recovered_state


def test_provider_error_classifies_classic_context_overflow():
    result = evaluate_provider_error(
        RuntimeError("HTTP 413: prompt too long"),
        state=AgentTurnState(),
        streaming=False,
        recover=lambda error, current: None,
    )

    assert result.outcome is ProviderErrorOutcome.TERMINATE
    assert result.termination_reason is TerminationReason.PROMPT_TOO_LONG


def test_output_recovery_escalates_truncated_finish():
    state = AgentTurnState()
    response = LLMResponse(
        action=Action(ActionType.FINISH, "partial", message="partial"),
        raw_content="partial",
        output_tokens=8192,
        finish_reason="length",
    )
    provider_turn = ProviderTurn(
        action=response.action,
        response=response,
        billable_tokens=8192,
    )

    result = evaluate_output_recovery(
        provider_turn,
        state=state,
        current_max_tokens=8192,
        escalated_max_tokens=64000,
        truncation_buffer_tokens=128,
    )

    assert result.outcome is OutputRecoveryOutcome.RETRY
    assert result.max_tokens == 64000
    assert result.state.recovery.escalation_applied


def test_validate_action_contract_normalizes_ids_and_rejects_invalid_call():
    action = Action(
        ActionType.TOOL_CALL,
        "read",
        [ToolCall("Read", {})],
    )
    schema = LLMToolSchema(
        name="Read",
        description="Read a file",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )

    result = validate_action_contract(
        action,
        [schema],
        task_id="task-1",
        step=2,
    )

    assert action.tool_calls[0].id.startswith("runtime_call_")
    assert result.status is ActionContractStatus.INVALID
    assert result.observation is not None
    assert "requires parameter 'path'" in result.observation.error


def test_validate_action_contract_treats_empty_schema_set_as_authority_boundary():
    action = Action(
        ActionType.TOOL_CALL,
        "try anyway",
        [ToolCall("Read", {"path": "a.txt"})],
    )

    result = validate_action_contract(
        action,
        [],
        task_id="task-1",
        step=1,
    )

    assert result.status is ActionContractStatus.TOOLS_DISABLED


def test_build_action_history_preserves_native_tool_call_pairing():
    call = ToolCall("Read", {"path": "a.txt"}, id="call-1")
    action = Action(ActionType.TOOL_CALL, "inspect", [call])
    observation = ToolResult(success=True, output="content").to_observation("Read")

    messages = build_action_history(
        action,
        [observation],
        supports_function_calling=True,
        render_action=lambda value: value.thought,
        render_observations=lambda values: str(len(values)),
        render_tool_result=lambda value: value.output,
    )

    assert [message.role for message in messages] == ["assistant", "tool"]
    assert messages[0].tool_calls == [call]
    assert messages[1].tool_call_id == "call-1"
    assert messages[1].content == "content"


def test_analyze_tool_result_derives_success_facts():
    result = ToolResult(
        success=True,
        output="done",
        subagent_tokens_used=25,
        structured_findings=({"title": "finding"},),
        metadata={"plan_contract": {"goal": "ship"}},
    )
    metadata = ToolMetadata(
        effects=frozenset({
            ToolEffect.READ_WORKSPACE,
            ToolEffect.WRITE_WORKSPACE,
            ToolEffect.TEST,
        }),
        path_parameter="path",
        roles=frozenset({
            ToolRole.DELEGATE,
            ToolRole.PERSIST_MEMORY,
        }),
    )

    analysis = analyze_tool_result(
        tool_name="Composite",
        params={"path": "src/app.py"},
        metadata=metadata,
        result=result,
        delegation_block_prefix="BLOCKED_BY_DELEGATION_POLICY:",
    )

    assert analysis.observation.is_success()
    assert analysis.persisted_memory
    assert analysis.delegated_tokens == 25
    assert analysis.read_path == "src/app.py"
    assert analysis.write_path == "src/app.py"
    assert analysis.verification_ok
    assert analysis.plan_contract == {"goal": "ship"}


def test_analyze_tool_result_exposes_typed_environment_block():
    result = ToolResult.from_error(
        ToolErrorType.ENVIRONMENT_UNAVAILABLE,
        detail="docker is unavailable",
        alternative="start docker",
    )

    analysis = analyze_tool_result(
        tool_name="shell",
        params={"cmd": "docker ps"},
        metadata=ToolMetadata(effects=frozenset({ToolEffect.EXECUTE})),
        result=result,
        delegation_block_prefix="BLOCKED_BY_DELEGATION_POLICY:",
    )

    assert analysis.environment_block is not None
    assert analysis.environment_block.detail == "docker is unavailable"
    assert analysis.environment_block.alternative == "start docker"


def test_observation_batch_treats_expected_blocks_as_breaker_success():
    observation = ToolResult.from_error(
        ToolErrorType.PERMISSION_DENIED,
        detail="expected policy block",
    ).to_observation("Delegate")
    observation.metadata["expected_block"] = True
    record_error = MagicMock()
    record_success = MagicMock()

    evaluation = evaluate_observation_batch(
        [observation],
        record_error=record_error,
        record_success=record_success,
        get_consecutive_failures=lambda: 0,
        max_consecutive_failures=3,
        description_limit=80,
    )

    assert not evaluation.recorded_error
    record_error.assert_not_called()
    record_success.assert_called_once_with()


def test_observation_batch_reports_consecutive_failure_termination():
    observation = ToolResult.from_error(
        ToolErrorType.INTERNAL,
        detail="tool crashed",
    ).to_observation("shell")

    evaluation = evaluate_observation_batch(
        [observation],
        record_error=lambda: None,
        record_success=lambda: None,
        get_consecutive_failures=lambda: 3,
        max_consecutive_failures=3,
        description_limit=80,
    )

    assert evaluation.recorded_error
    assert "3 consecutive tool failures" in evaluation.give_up_reason
    assert "tool crashed" in evaluation.give_up_reason


def test_post_observation_completes_after_missing_target_followups():
    evaluation = evaluate_post_observation(
        step=3,
        any_test_failed=False,
        missing_target_message="requested test is absent",
        missing_followups=1,
        missing_detected_step=1,
        confirmation_search=True,
        test_failure_count=0,
        test_failure_limit=3,
        task_anchor=" anchor",
        missing_reflection=MagicMock(),
        test_failure_reflection=MagicMock(),
    )

    assert evaluation.outcome is PostObservationOutcome.COMPLETE
    assert evaluation.missing_followups == 0
    assert evaluation.summary == "requested test is absent"


def test_post_observation_reflects_then_gives_up_on_repeated_test_failure():
    first = evaluate_post_observation(
        step=1,
        any_test_failed=True,
        missing_target_message=None,
        missing_followups=None,
        missing_detected_step=None,
        confirmation_search=False,
        test_failure_count=1,
        test_failure_limit=3,
        task_anchor=" anchor",
        missing_reflection=MagicMock(),
        test_failure_reflection=lambda: "fix tests",
    )
    final = evaluate_post_observation(
        step=2,
        any_test_failed=True,
        missing_target_message=None,
        missing_followups=None,
        missing_detected_step=None,
        confirmation_search=False,
        test_failure_count=first.reflection_count,
        test_failure_limit=3,
        task_anchor=" anchor",
        missing_reflection=MagicMock(),
        test_failure_reflection=lambda: "fix tests",
    )

    assert first.outcome is PostObservationOutcome.REFLECT
    assert first.reflection_prompt == "fix tests anchor"
    assert final.outcome is PostObservationOutcome.GIVE_UP
    assert "3 times" in final.summary


def test_react_applies_typed_tool_facts_to_run_owned_state():
    agent = object.__new__(ReActAgent)
    agent._explicit_memory_write_this_run = False
    agent._invalidate_ltc = MagicMock()
    agent._accumulated_structured_findings = []
    agent._accumulated_plan_contract = None
    agent._accessed_files = set()
    agent._mark_stale_for_written_file = MagicMock()
    completion_context = MagicMock()
    execution_budget = SimpleNamespace(
        consume=MagicMock(),
        token_used=25,
    )
    result = ToolResult(success=True, output="done")
    observation = result.to_observation("Composite")
    analysis = ToolResultAnalysis(
        observation=observation,
        persisted_memory=True,
        delegated_tokens=25,
        structured_findings=({"title": "finding"},),
        plan_contract={"goal": "ship"},
        tool_path="src/app.py",
        read_path="src/app.py",
    )

    applied = agent._apply_tool_result_analysis(
        analysis,
        tool_name="Composite",
        metadata=ToolMetadata(),
        result=result,
        completion_context=completion_context,
        execution_budget=execution_budget,
        task=SimpleNamespace(repo_path="."),
        git_state=object(),
    )

    assert applied is observation
    assert agent._explicit_memory_write_this_run
    agent._invalidate_ltc.assert_called_once_with()
    execution_budget.consume.assert_called_once_with(25)
    assert agent._accumulated_structured_findings == [
        {"title": "finding"},
    ]
    assert agent._accumulated_plan_contract == {"goal": "ship"}
    assert "src/app.py" in agent._accessed_files
    completion_context.record_tool_result.assert_called_once()


@dataclass(frozen=True)
class _Context:
    delegation_width: int = 1


class _Executor:
    def __init__(self):
        self.calls = []

    def enqueue(self, call):
        self.calls.append(call)

    def dispatch(self):
        pass

    def collect(self):
        return [ToolResult(success=True, output=call.name) for call in self.calls]


def test_execute_action_deduplicates_and_preserves_order(monkeypatch):
    monkeypatch.setattr(
        "agent.loop.turns.partition_tool_calls",
        lambda calls, registry: [list(calls)],
    )
    registry = MagicMock()
    executor = _Executor()
    calls = [
        ToolCall(name="Read", params={"path": "a"}, id="1"),
        ToolCall(name="Read", params={"path": "a"}, id="2"),
        ToolCall(name="Grep", params={"pattern": "x"}, id="3"),
    ]

    executed = execute_action(
        calls,
        registry,
        _Context(),
        streaming_executor=executor,
    )

    assert [call.id for call in executed.tool_calls] == ["1", "3"]
    assert [result.output for result in executed.results] == ["Read", "Grep"]


@pytest.mark.parametrize(
    ("facts", "status", "reason"),
    [
        (
            CompletionFacts(True, True, True, True, True, True),
            VerificationStatus.VERIFIED,
            VerificationReason.NONE,
        ),
        (
            CompletionFacts(True, False, True, True, True, True),
            VerificationStatus.FAILED,
            VerificationReason.TEST_FAILED,
        ),
        (
            CompletionFacts(True, False, False, False, True, True),
            VerificationStatus.UNAVAILABLE,
            VerificationReason.NO_TEST_ENVIRONMENT,
        ),
        (
            CompletionFacts(False, False, False, True, True, False),
            VerificationStatus.UNAVAILABLE,
            VerificationReason.NO_VERSION_CONTROL,
        ),
        (
            CompletionFacts(False, False, False, True, False, True),
            VerificationStatus.NOT_APPLICABLE,
            VerificationReason.NONE,
        ),
    ],
)
def test_complete_run_classifies_observed_facts(facts, status, reason):
    decision = complete_run(facts)

    assert decision.status is status
    assert decision.reason is reason


def test_evaluate_completion_stop_hook_retries_before_workspace_checks():
    refresh = MagicMock()
    guard = MagicMock()

    result = evaluate_completion(
        stop_message="address hook feedback",
        stop_hook_count=0,
        max_stop_hook_retries=3,
        checks=(),
        refresh_workspace=refresh,
        guard_check=guard,
        block_tracker=CompletionBlockTracker(),
        block_threshold=3,
        facts_factory=MagicMock(),
    )

    assert result.outcome is CompletionOutcome.RETRY
    assert result.retry_source is CompletionRetrySource.STOP_HOOK
    assert result.stop_hook_count == 1
    refresh.assert_not_called()
    guard.assert_not_called()


def test_evaluate_completion_abort_short_circuits_git_guard():
    guard = MagicMock()

    result = evaluate_completion(
        stop_message=None,
        stop_hook_count=0,
        max_stop_hook_retries=3,
        checks=(lambda: CompletionCheckResult.abort("cannot verify"),),
        refresh_workspace=MagicMock(),
        guard_check=guard,
        block_tracker=CompletionBlockTracker(),
        block_threshold=3,
        facts_factory=MagicMock(),
    )

    assert result.outcome is CompletionOutcome.GIVE_UP
    assert result.check_aborted
    assert result.reason == "cannot verify"
    guard.assert_not_called()


def test_evaluate_completion_guard_retries_then_gives_up():
    tracker = CompletionBlockTracker(threshold=2)

    def _evaluate():
        return evaluate_completion(
            stop_message=None,
            stop_hook_count=0,
            max_stop_hook_retries=3,
            checks=(),
            refresh_workspace=lambda: None,
            guard_check=lambda: CompletionCheckResult.retry(
                "make a change",
                reason="no diff",
            ),
            block_tracker=tracker,
            block_threshold=2,
            facts_factory=MagicMock(),
        )

    first = _evaluate()
    second = _evaluate()

    assert first.outcome is CompletionOutcome.RETRY
    assert first.completion_blocked_increment == 1
    assert second.outcome is CompletionOutcome.GIVE_UP
    assert "blocked 2 times" in second.reason


def test_evaluate_completion_classifies_verified_finish():
    result = evaluate_completion(
        stop_message=None,
        stop_hook_count=1,
        max_stop_hook_retries=3,
        checks=(CompletionCheckResult.done,),
        refresh_workspace=lambda: None,
        guard_check=CompletionCheckResult.done,
        block_tracker=CompletionBlockTracker(),
        block_threshold=3,
        facts_factory=lambda: CompletionFacts(
            has_changes=True,
            verification_ok=True,
            test_was_run=True,
            pytest_available=True,
            had_any_write=True,
            is_git_repo=True,
        ),
    )

    assert result.outcome is CompletionOutcome.COMPLETE
    assert result.stop_hook_count == 0
    assert result.verification.status is VerificationStatus.VERIFIED
