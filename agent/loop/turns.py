"""Explicit prepare, execute, and completion seams for the ReAct loop."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Sequence

from agent.constants import NO_THOUGHT_SENTINEL
from agent.recovery import Transition
from agent.runtime_controller import StepAction, StepDecision
from agent.task import (
    Action,
    ActionType,
    Observation,
    RunStatus,
    TerminationReason,
    ToolCall,
    ToolOutcome,
    VerificationReason,
    VerificationStatus,
)
from context.history import ConversationSnapshotError
from context.token_budget import estimate_tokens
from core.base import (
    ToolErrorType,
    ToolEffect,
    ToolMetadata,
    ToolRegistry,
    ToolResult,
    ToolRetryDirective,
    ToolRole,
)
from core.streaming_executor import StreamingToolExecutor, partition_tool_calls
from llm.base import CacheStats, LLMMessage, LLMResponse, LLMToolSchema
from llm.tool_call_validator import validate_tool_calls

if TYPE_CHECKING:
    from agent.completion_guard import CompletionCheckResult
    from agent.loop.types import CompletionBlockTracker
    from agent.session.run_context import RunContext

logger = logging.getLogger(__name__)


class PreStepOutcome(str, Enum):
    CONTINUE = "continue"
    TERMINATE = "terminate"


@dataclass(frozen=True)
class PreStepEvaluation:
    """Typed result shared by all pre-provider lifecycle gates."""

    outcome: PreStepOutcome
    decision: StepDecision | None = None
    status: RunStatus | None = None
    summary: str = ""
    detail: str = ""
    error: str = ""
    steps_taken: int = 0
    termination_reason: TerminationReason | None = None
    cancelled: bool = False
    log_failure: bool = False


def evaluate_early_step_gate(
    *,
    step: int,
    cancellation_requested: bool,
    cancellation_detail: str,
    permission_circuit_tripped: bool,
) -> PreStepEvaluation:
    """Check gates that must run before step bookkeeping or message loading."""
    if cancellation_requested:
        return PreStepEvaluation(
            outcome=PreStepOutcome.TERMINATE,
            status=RunStatus.CANCELLED,
            summary=f"Task cancelled: {cancellation_detail}",
            detail=cancellation_detail,
            error=cancellation_detail,
            steps_taken=step - 1,
            cancelled=True,
            log_failure=True,
        )
    if permission_circuit_tripped:
        return PreStepEvaluation(
            outcome=PreStepOutcome.TERMINATE,
            status=RunStatus.GAVE_UP,
            summary=(
                "Session terminated: permission circuit breaker tripped."
            ),
            steps_taken=step,
        )
    return PreStepEvaluation(outcome=PreStepOutcome.CONTINUE)


def evaluate_runtime_step_gate(
    *,
    step: int,
    controller_check: Callable[[], StepDecision],
    guard_check: Callable[[], Any],
) -> PreStepEvaluation:
    """Run RuntimeController first, then the TSM guard safety net."""
    decision = controller_check()
    if decision.action is StepAction.TERMINATE:
        reason = decision.terminate_reason
        detail = decision.terminate_detail
        return PreStepEvaluation(
            outcome=PreStepOutcome.TERMINATE,
            decision=decision,
            status=decision.terminate_status or RunStatus.GAVE_UP,
            summary=(
                decision.terminate_summary
                or detail
                or reason.value
            ),
            detail=detail,
            steps_taken=step,
            termination_reason=reason,
            log_failure=True,
        )

    guard_result = guard_check()
    if not guard_result.passed and guard_result.terminate:
        return PreStepEvaluation(
            outcome=PreStepOutcome.TERMINATE,
            decision=decision,
            status=RunStatus.GAVE_UP,
            summary=guard_result.reason,
            detail=guard_result.reason,
            steps_taken=step,
            termination_reason=TerminationReason.GUARD_REJECTED,
            log_failure=True,
        )
    return PreStepEvaluation(
        outcome=PreStepOutcome.CONTINUE,
        decision=decision,
    )


@dataclass(frozen=True)
class PreparedTurn:
    """Immutable provider and execution inputs for one turn."""

    messages: tuple[LLMMessage, ...]
    tools: tuple[LLMToolSchema, ...]
    execution_registry: ToolRegistry


@dataclass(frozen=True)
class ProviderRequest:
    """Prepared provider input plus updated immutable loop state."""

    turn: PreparedTurn
    state: Any
    spawn_context: Any = None


def prepare_provider_request(
    *,
    messages: Sequence[LLMMessage],
    history_messages: Sequence[LLMMessage],
    registry: ToolRegistry,
    execution_context: "RunContext",
    state: Any,
    step: int,
    total_tokens: int,
    strip_tools: bool,
    child_phase_active: bool,
    parent_session_id: str,
    parent_agent_name: str,
    repo_path: str,
    model_name: str,
    spawn_context_factory: Callable[..., Any] | None = None,
) -> ProviderRequest:
    """Freeze visible tools, delegation snapshot, authority, and turn state."""
    tools = [] if strip_tools else list(registry.get_schemas())
    if child_phase_active:
        tools = [tool for tool in tools if tool.name != "Agent"]

    next_state = state.with_updates(
        turn_count=step,
        messages=tuple(history_messages),
        tool_schemas=tuple(tools),
        total_tokens=total_tokens,
    )
    has_delegate = any(
        (
            metadata := registry.metadata_for(schema.name)
        ) is not None
        and ToolRole.DELEGATE in metadata.roles
        for schema in tools
    )

    spawn_context = None
    if has_delegate:
        if spawn_context_factory is None:
            from agent.session.run_context import AgentSpawnContext

            spawn_context_factory = AgentSpawnContext.capture
        try:
            spawn_context = spawn_context_factory(
                messages=list(messages),
                parent_session_id=parent_session_id,
                parent_agent_name=parent_agent_name,
                repo_path=repo_path,
                model_name=model_name,
                tool_schemas=tools,
            )
        except ConversationSnapshotError as exc:
            logger.debug(
                "Live conversation snapshot unavailable for delegation: %s",
                exc,
            )
        execution_context = replace(
            execution_context,
            spawn_context=spawn_context,
        )

    return ProviderRequest(
        turn=prepare_turn(
            messages,
            tools,
            registry,
            execution_context,
        ),
        state=next_state,
        spawn_context=spawn_context,
    )


def prepare_turn(
    messages: Sequence[LLMMessage],
    tools: Sequence[LLMToolSchema],
    registry: ToolRegistry,
    execution_context: "RunContext",
) -> PreparedTurn:
    """Freeze provider input and bind the registry to this turn's authority."""
    return PreparedTurn(
        messages=tuple(messages),
        tools=tuple(tools),
        execution_registry=registry.with_run_context(execution_context),
    )


@dataclass(frozen=True)
class ProviderTurn:
    """Normalized output from either provider invocation mode."""

    action: Action
    response: LLMResponse | None
    billable_tokens: int
    output_tokens_estimate: int = 0
    streaming_executor: StreamingToolExecutor | None = None
    cache_stats: CacheStats | None = None


class ProviderErrorOutcome(str, Enum):
    RETRY = "retry"
    TERMINATE = "terminate"


@dataclass(frozen=True)
class ProviderErrorEvaluation:
    outcome: ProviderErrorOutcome
    state: Any
    termination_reason: TerminationReason = TerminationReason.MODEL_ERROR
    call_kind: str = "call"
    error: str = ""


def evaluate_provider_error(
    error: Exception,
    *,
    state: Any,
    streaming: bool,
    recover: Callable[[Exception, Any], tuple[Any, bool] | None],
) -> ProviderErrorEvaluation:
    """Try Runtime recovery, then classify the terminal provider failure."""
    recovery = recover(error, state)
    if recovery is not None:
        next_state, recovered = recovery
        if recovered:
            return ProviderErrorEvaluation(
                outcome=ProviderErrorOutcome.RETRY,
                state=next_state,
                call_kind="stream" if streaming else "call",
                error=str(error),
            )
        state = next_state

    lowered = str(error).lower()
    prompt_too_long = (
        not streaming
        and any(
            keyword in lowered
            for keyword in (
                "prompt too long",
                "context length",
                "413",
                "reduce the length",
            )
        )
    )
    return ProviderErrorEvaluation(
        outcome=ProviderErrorOutcome.TERMINATE,
        state=state,
        termination_reason=(
            TerminationReason.PROMPT_TOO_LONG
            if prompt_too_long
            else TerminationReason.MODEL_ERROR
        ),
        call_kind="stream" if streaming else "call",
        error=str(error),
    )


class OutputRecoveryOutcome(str, Enum):
    PROCEED = "proceed"
    RETRY = "retry"


@dataclass(frozen=True)
class OutputRecoveryEvaluation:
    outcome: OutputRecoveryOutcome
    state: Any
    max_tokens: int
    inject_message: str = ""
    exhausted: bool = False


def evaluate_output_recovery(
    provider_turn: ProviderTurn,
    *,
    state: Any,
    current_max_tokens: int,
    escalated_max_tokens: int,
    truncation_buffer_tokens: int,
) -> OutputRecoveryEvaluation:
    """Classify non-tool output truncation and build the next recovery state."""
    action = provider_turn.action
    response = provider_turn.response
    if response is not None:
        truncated = (
            response.finish_reason == "length"
            or response.output_tokens
            >= current_max_tokens - truncation_buffer_tokens
        )
    else:
        truncated = (
            action.action_type is ActionType.FINISH
            and not action.message
            and provider_turn.output_tokens_estimate >= current_max_tokens
        )
    if not truncated or action.action_type is ActionType.TOOL_CALL:
        return OutputRecoveryEvaluation(
            OutputRecoveryOutcome.PROCEED,
            state,
            current_max_tokens,
        )
    if state.recovery.can_escalate(current_max_tokens):
        next_state = state.with_recovery_update(escalation_applied=True)
        next_state = next_state.with_updates(
            transition=Transition.escalation(escalated_max_tokens),
        )
        return OutputRecoveryEvaluation(
            OutputRecoveryOutcome.RETRY,
            next_state,
            escalated_max_tokens,
        )
    if state.recovery.can_recover_output():
        next_count = state.recovery.output_recovery_count + 1
        next_state = state.with_recovery_update(
            output_recovery_count=next_count,
        )
        next_state = next_state.with_updates(
            transition=Transition.recovery(next_count),
        )
        return OutputRecoveryEvaluation(
            OutputRecoveryOutcome.RETRY,
            next_state,
            current_max_tokens,
            inject_message=(
                "[SYSTEM] Output truncated. Resume directly — "
                "no apology, no recap."
            ),
        )
    return OutputRecoveryEvaluation(
        OutputRecoveryOutcome.PROCEED,
        state,
        current_max_tokens,
        exhausted=True,
    )


def invoke_provider_turn(
    prepared: PreparedTurn,
    *,
    streaming: bool,
    stream_call: Callable[
        [list[LLMMessage], list[LLMToolSchema], StreamingToolExecutor],
        Action,
    ],
    complete_call: Callable[
        [list[LLMMessage], list[LLMToolSchema]],
        LLMResponse,
    ],
) -> ProviderTurn:
    """Invoke one provider path and normalize accounting for the loop."""
    messages = list(prepared.messages)
    tools = list(prepared.tools)
    if streaming:
        executor = StreamingToolExecutor(prepared.execution_registry)
        action = stream_call(messages, tools, executor)
        input_estimate = sum(
            estimate_tokens(str(message.content)) for message in messages
        )
        output_estimate = estimate_tokens(
            action.message or action.thought or "",
        )
        return ProviderTurn(
            action=action,
            response=None,
            billable_tokens=input_estimate + output_estimate,
            output_tokens_estimate=output_estimate,
            streaming_executor=executor,
        )

    response = complete_call(messages, tools)
    cache_stats = response.cache_stats
    billable_tokens = response.total_tokens
    if cache_stats and cache_stats.has_cache_activity:
        billable_tokens = max(
            0,
            billable_tokens - cache_stats.cache_read_tokens,
        )
    return ProviderTurn(
        action=response.action,
        response=response,
        billable_tokens=billable_tokens,
        cache_stats=cache_stats,
    )


class ActionContractStatus(str, Enum):
    ACCEPTED = "accepted"
    TOOLS_DISABLED = "tools_disabled"
    INVALID = "invalid"


@dataclass(frozen=True)
class ActionContractResult:
    """Control-plane disposition for a provider-generated action."""

    status: ActionContractStatus
    observation: Observation | None = None
    error_type: str = ""
    error_message: str = ""


def validate_action_contract(
    action: Action,
    tools: Sequence[LLMToolSchema],
    *,
    task_id: str,
    step: int,
) -> ActionContractResult:
    """Normalize call ids and enforce visible tool schemas."""
    if action.action_type is not ActionType.TOOL_CALL or not action.tool_calls:
        return ActionContractResult(ActionContractStatus.ACCEPTED)

    for index, tool_call in enumerate(action.tool_calls):
        if tool_call.id:
            continue
        identity = f"{task_id}:{step}:{index}:{tool_call.name}".encode("utf-8")
        tool_call.id = (
            "runtime_call_" + hashlib.sha256(identity).hexdigest()[:24]
        )

    if not tools:
        return ActionContractResult(ActionContractStatus.TOOLS_DISABLED)

    validation = validate_tool_calls(action.tool_calls, list(tools))
    if validation.valid:
        return ActionContractResult(ActionContractStatus.ACCEPTED)

    fake_result = ToolResult.from_error(
        error_type=ToolErrorType.INVALID_PARAMS,
        retry=ToolRetryDirective.RETRY,
        detail=validation.error_message,
    )
    observation = fake_result.to_observation(
        validation.offending_tool or action.tool_calls[0].name,
    )
    return ActionContractResult(
        ActionContractStatus.INVALID,
        observation=observation,
        error_type=validation.error_type,
        error_message=validation.error_message,
    )


def build_action_history(
    action: Action,
    observations: Sequence[Observation],
    *,
    supports_function_calling: bool,
    tool_calls: Sequence[ToolCall] | None = None,
    render_action: Callable[[Action], str],
    render_observations: Callable[[Sequence[Observation]], str],
    render_tool_result: Callable[[Observation], str],
) -> tuple[LLMMessage, ...]:
    """Render one action/result pair at the conversation boundary."""
    effective_calls = list(tool_calls or action.tool_calls)
    if supports_function_calling:
        thought = (
            ""
            if action.thought == NO_THOUGHT_SENTINEL
            else (action.thought or "")
        )
        messages = [
            LLMMessage(
                role="assistant",
                content=thought,
                tool_calls=effective_calls,
            ),
        ]
        messages.extend(
            LLMMessage(
                role="tool",
                content=render_tool_result(observation),
                tool_call_id=(
                    effective_calls[index].id
                    if index < len(effective_calls)
                    else None
                ),
            )
            for index, observation in enumerate(observations)
        )
        return tuple(messages)

    return (
        LLMMessage(role="assistant", content=render_action(action)),
        LLMMessage(role="user", content=render_observations(observations)),
    )


@dataclass(frozen=True)
class ExecutedAction:
    """Ordered calls and results produced by the execution seam."""

    tool_calls: tuple[ToolCall, ...]
    results: tuple[ToolResult, ...]


@dataclass(frozen=True)
class EnvironmentBlock:
    error_type: ToolErrorType
    detail: str
    alternative: str


@dataclass(frozen=True)
class ToolResultAnalysis:
    """Typed control facts derived from one raw tool result."""

    observation: Observation
    environment_block: EnvironmentBlock | None = None
    persisted_memory: bool = False
    delegated_tokens: int = 0
    structured_findings: tuple = ()
    plan_contract: dict | None = None
    tool_path: str = ""
    read_path: str = ""
    write_path: str = ""
    writes_workspace: bool = False
    test_was_run: bool = False
    verification_ok: bool = False
    test_failed: bool = False
    missing_test_target: bool = False


@dataclass(frozen=True)
class ObservationBatchEvaluation:
    """Failure accounting result for one ordered observation batch."""

    recorded_error: bool
    consecutive_failures: int
    give_up_reason: str = ""


def evaluate_observation_batch(
    observations: Sequence[Observation],
    *,
    record_error: Callable[[], None],
    record_success: Callable[[], None],
    get_consecutive_failures: Callable[[], int],
    max_consecutive_failures: int,
    description_limit: int,
) -> ObservationBatchEvaluation:
    """Update the breaker once per batch and classify forced termination."""
    # Per-observation failure tracking: any individual tool failure
    # increments the counter.  Only an all-success batch resets it.
    any_failed = any(
        not observation.is_success() for observation in observations
    )
    any_not_expected = any(
        not observation.is_expected_block() for observation in observations
    )
    recorded_error = any_failed and any_not_expected
    if recorded_error:
        record_error()
    else:
        record_success()

    consecutive_failures = get_consecutive_failures()
    reason = ""
    if consecutive_failures >= max_consecutive_failures:
        last = observations[-1]
        detail = last.error or last.output[:description_limit]
        reason = (
            f"Aborting: {consecutive_failures} consecutive tool failures. "
            f"Last error: {detail}"
        )
    return ObservationBatchEvaluation(
        recorded_error=recorded_error,
        consecutive_failures=consecutive_failures,
        give_up_reason=reason,
    )


class PostObservationOutcome(str, Enum):
    NONE = "none"
    REFLECT = "reflect"
    COMPLETE = "complete"
    GIVE_UP = "give_up"


@dataclass(frozen=True)
class PostObservationEvaluation:
    """Transition requested after history receives an observation batch."""

    outcome: PostObservationOutcome
    missing_followups: int | None = None
    reflection_count: int = 0
    reflection_reason: str = ""
    reflection_prompt: str = ""
    summary: str = ""


def evaluate_post_observation(
    *,
    step: int,
    any_test_failed: bool,
    missing_target_message: str | None,
    missing_followups: int | None,
    missing_detected_step: int | None,
    confirmation_search: bool,
    test_failure_count: int,
    test_failure_limit: int,
    task_anchor: str,
    missing_reflection: Callable[[str], str],
    test_failure_reflection: Callable[[], str],
) -> PostObservationEvaluation:
    """Classify missing-target guardrails and test-failure reflection."""
    next_followups = missing_followups
    if (
        missing_target_message is not None
        and next_followups is not None
        and missing_detected_step != step
    ):
        next_followups = (
            next_followups - 1
            if confirmation_search
            else 0
        )
        if next_followups <= 0:
            return PostObservationEvaluation(
                outcome=PostObservationOutcome.COMPLETE,
                missing_followups=next_followups,
                summary=missing_target_message,
            )

    if not any_test_failed:
        return PostObservationEvaluation(
            outcome=PostObservationOutcome.NONE,
            missing_followups=next_followups,
            reflection_count=test_failure_count,
        )
    if missing_target_message is not None:
        return PostObservationEvaluation(
            outcome=PostObservationOutcome.REFLECT,
            missing_followups=next_followups,
            reflection_count=test_failure_count,
            reflection_reason="missing_test_target",
            reflection_prompt=(
                missing_reflection(missing_target_message) + task_anchor
            ),
        )

    next_count = test_failure_count + 1
    if next_count >= test_failure_limit:
        return PostObservationEvaluation(
            outcome=PostObservationOutcome.GIVE_UP,
            missing_followups=next_followups,
            reflection_count=next_count,
            summary=(
                "Aborting: test failures repeated "
                f"{test_failure_limit} times without resolution."
            ),
        )
    return PostObservationEvaluation(
        outcome=PostObservationOutcome.REFLECT,
        missing_followups=next_followups,
        reflection_count=next_count,
        reflection_reason="test_failed",
        reflection_prompt=test_failure_reflection() + task_anchor,
    )


def analyze_tool_result(
    *,
    tool_name: str,
    params: dict,
    metadata: ToolMetadata,
    result: ToolResult,
    delegation_block_prefix: str,
) -> ToolResultAnalysis:
    """Convert a raw tool result into facts consumed by loop adapters."""
    observation = result.to_observation(tool_name)
    if (
        observation.error
        and observation.error.startswith(delegation_block_prefix)
    ):
        observation = replace(
            observation,
            metadata={
                **(observation.metadata or {}),
                "expected_block": True,
                "block_kind": "v2_delegation_policy",
            },
        )

    environment_block = None
    if not observation.is_success() and result.tool_error is not None:
        if (
            result.tool_error.error_type
            is ToolErrorType.ENVIRONMENT_UNAVAILABLE
        ):
            environment_block = EnvironmentBlock(
                error_type=result.tool_error.error_type,
                detail=result.tool_error.detail,
                alternative=result.tool_error.alternative,
            )

    path = (
        str(params.get(metadata.path_parameter) or "")
        if metadata.path_parameter
        else ""
    )
    success = observation.is_success()
    plan_contract = result.metadata.get("plan_contract")
    if not isinstance(plan_contract, dict):
        plan_contract = None
    is_test = ToolEffect.TEST in metadata.effects
    return ToolResultAnalysis(
        observation=observation,
        environment_block=environment_block,
        persisted_memory=(
            ToolRole.PERSIST_MEMORY in metadata.roles and success
        ),
        delegated_tokens=(
            result.subagent_tokens_used
            if ToolRole.DELEGATE in metadata.roles
            else 0
        ),
        structured_findings=tuple(result.structured_findings or ()),
        plan_contract=plan_contract,
        tool_path=path,
        read_path=(
            path
            if ToolEffect.READ_WORKSPACE in metadata.effects and success
            else ""
        ),
        write_path=(
            path
            if ToolEffect.WRITE_WORKSPACE in metadata.effects and success
            else ""
        ),
        writes_workspace=ToolEffect.WRITE_WORKSPACE in metadata.effects,
        test_was_run=is_test,
        verification_ok=is_test and success,
        test_failed=is_test and not success,
        missing_test_target=(
            is_test
            and not success
            and observation.outcome is ToolOutcome.TEST_TARGET_MISSING
        ),
    )


def execute_action(
    tool_calls: Sequence[ToolCall],
    registry: ToolRegistry,
    execution_context: "RunContext",
    *,
    streaming_executor: StreamingToolExecutor | None = None,
) -> ExecutedAction:
    """Deduplicate, schedule, execute, and order one tool-call action."""
    seen: set[str] = set()
    effective_calls: list[ToolCall] = []
    for call in tool_calls:
        digest = hashlib.sha256(
            json.dumps(
                call.params or {}, sort_keys=True, ensure_ascii=False,
            ).encode(),
        ).hexdigest()[:16]
        key = f"{call.name}:{digest}"
        if key in seen:
            logger.info("Batch dedup: skipping duplicate %s", call.name)
            continue
        seen.add(key)
        effective_calls.append(call)

    batches = partition_tool_calls(effective_calls, registry)
    max_batch = max((len(batch) for batch in batches), default=1)
    if max_batch > 1:
        execution_context = replace(
            execution_context,
            delegation_width=max_batch,
        )
    execution_registry = registry.with_run_context(execution_context)

    if streaming_executor is not None:
        executor = streaming_executor
        for call in effective_calls:
            executor.enqueue(call)
    else:
        executor = StreamingToolExecutor(execution_registry)
        for batch in batches:
            for call in batch:
                executor.enqueue(call)
    executor.dispatch()
    return ExecutedAction(
        tool_calls=tuple(effective_calls),
        results=tuple(executor.collect()),
    )


@dataclass(frozen=True)
class CompletionFacts:
    """Observed facts used to classify a successful completion."""

    has_changes: bool
    verification_ok: bool
    test_was_run: bool
    pytest_available: bool
    had_any_write: bool
    is_git_repo: bool


@dataclass(frozen=True)
class CompletionDecision:
    status: VerificationStatus
    reason: VerificationReason = VerificationReason.NONE
    detail: str = ""


class CompletionOutcome(str, Enum):
    COMPLETE = "complete"
    RETRY = "retry"
    GIVE_UP = "give_up"


class CompletionRetrySource(str, Enum):
    NONE = "none"
    STOP_HOOK = "stop_hook"
    CHECK = "check"


@dataclass(frozen=True)
class CompletionEvaluation:
    """Typed transition requested by the complete-run policy."""

    outcome: CompletionOutcome
    verification: CompletionDecision | None = None
    retry_source: CompletionRetrySource = CompletionRetrySource.NONE
    reason: str = ""
    inject_message: str = ""
    stop_hook_count: int = 0
    completion_blocked_increment: int = 0
    termination_reason: TerminationReason | None = None
    check_aborted: bool = False


def evaluate_completion(
    *,
    stop_message: str | None,
    stop_hook_count: int,
    max_stop_hook_retries: int,
    checks: Sequence[
        Callable[[], "CompletionCheckResult"] | None
    ],
    refresh_workspace: Callable[[], None],
    guard_check: Callable[[], "CompletionCheckResult"],
    block_tracker: "CompletionBlockTracker",
    block_threshold: int,
    facts_factory: Callable[[], CompletionFacts],
) -> CompletionEvaluation:
    """Evaluate completion policy without mutating loop or lifecycle state."""
    if stop_message is not None:
        next_count = stop_hook_count + 1
        if next_count > max_stop_hook_retries:
            reason = (
                f"Stop hook retry limit reached: {max_stop_hook_retries}"
            )
            return CompletionEvaluation(
                outcome=CompletionOutcome.GIVE_UP,
                reason=reason,
                stop_hook_count=next_count,
                termination_reason=TerminationReason.HOOK_STOPPED,
            )
        return CompletionEvaluation(
            outcome=CompletionOutcome.RETRY,
            retry_source=CompletionRetrySource.STOP_HOOK,
            inject_message=stop_message,
            stop_hook_count=next_count,
        )

    refresh_workspace()
    for check in checks:
        if check is None:
            continue
        result = check()
        if result.can_complete:
            continue
        if result.verdict == "abort":
            return CompletionEvaluation(
                outcome=CompletionOutcome.GIVE_UP,
                reason=result.blocked_reason,
                inject_message=result.inject_message,
                termination_reason=TerminationReason.AGENT_GAVE_UP,
                check_aborted=True,
            )
        return CompletionEvaluation(
            outcome=CompletionOutcome.RETRY,
            retry_source=CompletionRetrySource.CHECK,
            reason=result.blocked_reason,
            inject_message=result.inject_message,
        )

    guard_result = guard_check()
    if not guard_result.can_complete:
        if block_tracker.should_block(guard_result.blocked_reason):
            reason = (
                "Agent gave up: completion blocked "
                f"{block_threshold} times for: "
                f"{guard_result.blocked_reason}"
            )
            return CompletionEvaluation(
                outcome=CompletionOutcome.GIVE_UP,
                reason=reason,
                completion_blocked_increment=1,
                termination_reason=TerminationReason.AGENT_GAVE_UP,
            )
        return CompletionEvaluation(
            outcome=CompletionOutcome.RETRY,
            retry_source=CompletionRetrySource.CHECK,
            reason=guard_result.blocked_reason,
            inject_message=guard_result.inject_message,
            completion_blocked_increment=1,
        )

    return CompletionEvaluation(
        outcome=CompletionOutcome.COMPLETE,
        verification=complete_run(facts_factory()),
    )


def complete_run(facts: CompletionFacts) -> CompletionDecision:
    """Classify completion from facts without mutating lifecycle state."""
    if facts.has_changes and facts.verification_ok:
        return CompletionDecision(
            VerificationStatus.VERIFIED,
            detail="guards passed + workspace delta + verification confirmed",
        )
    if facts.has_changes and facts.test_was_run:
        return CompletionDecision(
            VerificationStatus.FAILED,
            VerificationReason.TEST_FAILED,
            "tests ran but failed",
        )
    if facts.has_changes and not facts.pytest_available:
        return CompletionDecision(
            VerificationStatus.UNAVAILABLE,
            VerificationReason.NO_TEST_ENVIRONMENT,
            "no test environment available",
        )
    if facts.has_changes:
        return CompletionDecision(
            VerificationStatus.UNVERIFIED,
            VerificationReason.NOT_RUN,
            "guards passed — unverified",
        )
    if facts.had_any_write and not facts.is_git_repo:
        return CompletionDecision(
            VerificationStatus.UNAVAILABLE,
            VerificationReason.NO_VERSION_CONTROL,
            "no Git fact source available",
        )
    if facts.had_any_write:
        return CompletionDecision(
            VerificationStatus.UNVERIFIED,
            VerificationReason.NO_NET_CHANGE,
            "guards passed — no net workspace changes detected",
        )
    return CompletionDecision(
        VerificationStatus.NOT_APPLICABLE,
        detail="guards passed — analysis/read-only task",
    )
