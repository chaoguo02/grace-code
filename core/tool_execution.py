"""Atomic tool execution boundary.

The registry resolves names and descriptors.  This module owns the mandatory
per-call sequence: schema validation, capability interception, permission
evaluation, final-parameter validation, execution, and post-tool hooks.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
import uuid
from typing import TYPE_CHECKING, Any

from core.errors import ToolErrorType
from hooks.events import HookContext, HookEvent

if TYPE_CHECKING:
    from core.base import BaseTool, ToolResult


@dataclass(frozen=True)
class AuthorizedToolCall:
    """Final immutable call passed to a concrete tool implementation."""

    name: str
    params: dict[str, Any]
    thought: str = ""


class ToolExecutionPipeline:
    """Execute one tool call through all mandatory boundary checks."""

    def __init__(
        self,
        *,
        permission_pipeline: Any = None,
        hook_dispatcher: Any = None,
        tool_availability_guard: Any = None,
        session_id: str = "",
        budget: Any = None,
        resource_governor: Any = None,
        root_session_resolver: Any = None,
        evidence_recorder: Any = None,
    ) -> None:
        self._permission_pipeline = permission_pipeline
        self._hook_dispatcher = hook_dispatcher
        self._tool_availability_guard = tool_availability_guard
        self._session_id = session_id
        self._budget = budget
        self._resource_governor = resource_governor
        self._root_session_resolver = root_session_resolver
        self._evidence_recorder = evidence_recorder

    _NEVER_CANCELLED: "CancellationToken | None" = None  # lazy-init sentinel

    def execute(
        self,
        tool: "BaseTool",
        params: dict[str, Any],
        *,
        thought: str = "",
        invocation_id: str = "",
        cancellation_token: "CancellationToken | None" = None,
    ) -> "ToolResult":
        """Validate and execute one logical call, including safe retries.

        Args:
            cancellation_token: If the tool declares
                ``supports_cancellation=True``, this token is passed to the
                tool for cooperative cancellation.  If ``None`` and the
                tool supports cancellation, a never-cancelled sentinel is
                auto-created.  If the tool does NOT support cancellation,
                this parameter is ignored (the tool cannot be interrupted).
        """
        from core.base import ToolResult
        from core.types import RetryMode

        # Resolve cancellation token: auto-create sentinel if tool supports it
        if cancellation_token is None and tool.supports_cancellation:
            if self._NEVER_CANCELLED is None:
                from agent.session.run_context import CancellationToken
                self._NEVER_CANCELLED = CancellationToken()
            cancellation_token = self._NEVER_CANCELLED

        # Phase 2 #6: Store cancellation token for _run_with_cancellation
        self._cancellation = cancellation_token

        validation_error = self._validate_params(tool, params)
        if validation_error is not None:
            return validation_error

        availability_error = self._check_tool_availability(tool)
        if availability_error is not None:
            return availability_error

        # ── Per-tool budget gate (EXHAUSTED only — WARNING/CRITICAL
        #     are handled per-step to avoid double-penalty message injection) ──
        if self._budget is not None:
            budget_status = self._budget.check()
            if getattr(budget_status, "is_exhausted", False):
                return ToolResult.from_error(
                    error_type=ToolErrorType.UNAVAILABLE,
                    detail=getattr(budget_status, "inject_message", "") or "Budget exhausted",
                )

        logical_id = invocation_id or f"tool_{uuid.uuid4().hex}"
        policy = tool.retry_policy(params)

        # ── Evidence: record tool call started ──
        if self._evidence_recorder is not None:
            self._evidence_recorder.record_started(
                tool_name=tool.name,
                params=params,
                invocation_id=logical_id,
                tool=tool,
                session_id=self._session_id,
            )

        attempts: list[dict[str, Any]] = []
        delay_ms = policy.base_delay_ms
        result: ToolResult | None = None

        for attempt in range(1, policy.max_attempts + 1):
            permission_result = self._authorize(
                tool,
                params,
                thought,
                force_retry_approval=(
                    attempt > 1 and policy.mode is RetryMode.APPROVAL
                ),
                attempt=attempt,
                invocation_id=logical_id,
            )
            if isinstance(permission_result, ToolResult):
                result = permission_result
                attempts.append(self._attempt_fact(attempt, result))
                break

            actual_params = dict(params)
            if (
                permission_result is not None
                and permission_result.updated_params
            ):
                actual_params.update(permission_result.updated_params)

            validation_error = self._validate_params(tool, actual_params)
            if validation_error is not None:
                result = validation_error
                attempts.append(self._attempt_fact(attempt, result))
                break

            call = AuthorizedToolCall(
                name=tool.name,
                params=actual_params,
                thought=thought,
            )
            try:
                result = self._execute_once(
                    tool,
                    call.params,
                    invocation_id=logical_id,
                    attempt=attempt,
                )
            except Exception as exc:
                result = ToolResult.from_error(
                    error_type=ToolErrorType.INTERNAL,
                    detail=(
                        f"Tool '{tool.name}' raised an unexpected error: {exc}"
                    ),
                )

            self._fire_post_tool_hook(call, result)
            attempts.append(self._attempt_fact(attempt, result))
            if result.success or not self._should_retry(result, policy, attempt):
                break
            if delay_ms:
                time.sleep(delay_ms / 1000)
            delay_ms = min(policy.max_delay_ms, max(delay_ms * 2, 1))

        assert result is not None
        result.invocation_id = logical_id
        result.attempt_count = len(attempts)
        result.eventual_success = result.success
        result.metadata = {
            **(result.metadata or {}),
            "invocation_id": logical_id,
            "attempt_count": len(attempts),
            "eventual_success": result.success,
            "retry_mode": policy.mode.value,
            "attempts": attempts,
        }
        # ── Evidence: record tool call completed ──
        if self._evidence_recorder is not None:
            self._evidence_recorder.record_completed(
                tool_name=tool.name,
                params=params,
                result=result,
                invocation_id=logical_id,
                tool=tool,
                session_id=self._session_id,
            )
        return result

    def _execute_once(
        self,
        tool: "BaseTool",
        params: dict[str, Any],
        *,
        invocation_id: str,
        attempt: int,
    ) -> "ToolResult":
        """Execute one attempt under the shared external-work capacity."""
        from core.types import ToolEffect

        governed_effects = {
            ToolEffect.EXECUTE,
            ToolEffect.TEST,
            ToolEffect.NETWORK,
        }
        needs_slot = bool(
            getattr(tool, "is_mcp", False)
            or (tool.metadata.effects & governed_effects)
        )
        if not needs_slot or self._resource_governor is None:
            return self._run_with_cancellation(tool, params)

        from core.base import ToolResult
        from core.resource_governor import (
            AdmissionOutcome,
            ResourceKind,
            ResourceRequest,
        )

        root_session_id = self._session_id
        if callable(self._root_session_resolver):
            try:
                root_session_id = (
                    self._root_session_resolver(self._session_id)
                    or root_session_id
                )
            except Exception:
                # Session cleanup may race with a final tool call. Falling
                # back to session ownership stays bounded and fail-closed.
                pass
        queue_cfg = getattr(
            getattr(self._resource_governor, "_config", None), "queue", None
        )
        timeout_s = float(getattr(queue_cfg, "timeout_seconds", 120.0))
        admission = self._resource_governor.admit_wait(ResourceRequest(
            request_id=f"{invocation_id}:attempt-{attempt}",
            root_session_id=root_session_id or "unscoped",
            session_id=self._session_id,
            run_id=invocation_id,
            resources={ResourceKind.TOOL_SLOT: 1},
            timeout_s=timeout_s,
        ))
        if (
            admission.outcome is not AdmissionOutcome.GRANTED
            or admission.lease is None
        ):
            from observability.failure_policy import classify_resource_failure

            error = ToolResult.from_error(
                error_type=ToolErrorType.UNAVAILABLE,
                detail=(
                    f"Tool capacity {admission.outcome.value}: "
                    f"{admission.reason or 'tool slot unavailable'}"
                ),
            )
            error.metadata = {
                **(error.metadata or {}),
                "resource_failure": admission.outcome.value,
                "resource_failure_category": classify_resource_failure(
                    admission.outcome,
                    ResourceKind.TOOL_SLOT,
                    admission.reason,
                ).value,
                "resource_kind": ResourceKind.TOOL_SLOT.value,
                "wait_time_s": admission.wait_time_s,
            }
            return error
        try:
            return self._run_with_cancellation(tool, params)
        finally:
            admission.lease.release()

    def _authorize(
        self,
        tool: "BaseTool",
        params: dict[str, Any],
        thought: str,
        *,
        force_retry_approval: bool,
        attempt: int,
        invocation_id: str,
    ) -> Any:
        from core.base import ToolResult

        permission_result = None
        if self._permission_pipeline is not None:
            permission_result = self._permission_pipeline.check(
                tool,
                params,
                thought=thought,
                force_interactive_override=force_retry_approval,
                decision_reason_override=(
                    f"Retry attempt {attempt} for {invocation_id}; this tool "
                    "may repeat external side effects."
                    if force_retry_approval else ""
                ),
            )
            from hitl.pipeline import PermissionDecision
            if permission_result.decision is PermissionDecision.DENY:
                feedback = getattr(permission_result, "feedback", "")
                detail = f"Tool '{tool.name}' denied: {permission_result.reason}"
                if feedback:
                    detail += f" Feedback: {feedback}"
                return ToolResult.from_error(
                    error_type=ToolErrorType.PERMISSION_DENIED,
                    detail=detail,
                )
        elif force_retry_approval:
            return ToolResult.from_error(
                error_type=ToolErrorType.PERMISSION_DENIED,
                detail="Interactive retry approval is unavailable.",
            )
        return permission_result

    @staticmethod
    def _should_retry(result: "ToolResult", policy: Any, attempt: int) -> bool:
        from core.errors import ToolRetryDirective
        from core.types import RetryMode

        if attempt >= policy.max_attempts or policy.mode is RetryMode.NEVER:
            return False
        error = result.tool_error
        return bool(
            error is not None
            and error.retry is ToolRetryDirective.RETRY
            and error.error_type.value in policy.retryable_error_types
        )

    @staticmethod
    def _attempt_fact(attempt: int, result: "ToolResult") -> dict[str, Any]:
        return {
            "attempt": attempt,
            "success": result.success,
            "error_type": (
                result.tool_error.error_type.value
                if result.tool_error is not None else ""
            ),
            "duration_ms": result.duration_ms,
        }

    @staticmethod
    def _validate_params(
        tool: "BaseTool",
        params: dict[str, Any],
    ) -> "ToolResult | None":
        from agent.task import ToolCall
        from core.base import ToolResult
        from llm.tool_call_validator import validate_tool_calls

        validation = validate_tool_calls(
            [ToolCall(name=tool.name, params=params)],
            [tool.to_llm_schema()],
        )
        if validation.valid:
            return None
        return ToolResult.from_error(
            error_type=ToolErrorType.INVALID_PARAMS,
            detail=validation.error_message,
        )

    def _check_tool_availability(self, tool: "BaseTool") -> "ToolResult | None":
        if self._tool_availability_guard is None:
            return None

        import json

        from agent.tool_availability_guard import InterceptDecision
        from core.base import ToolResult

        intercept = self._tool_availability_guard.intercept(
            tool.name,
            session_id=self._session_id,
        )
        if intercept.decision is not InterceptDecision.BLOCK:
            return None
        feedback = json.dumps(intercept.feedback, ensure_ascii=False)
        return ToolResult.from_error(
            error_type=ToolErrorType.UNAVAILABLE,
            detail=f"Tool '{tool.name}' blocked: {feedback}",
        )

    def _run_with_cancellation(
        self,
        tool: "BaseTool",
        params: dict[str, Any],
    ) -> "ToolResult":
        """Execute tool, injecting CancellationToken if tool supports it.

        Phase 2 #6: Tools that declare ``supports_cancellation=True``
        receive a ``_cancellation_token`` attribute before ``execute()``
        and have it cleared after.  The tool is responsible for checking
        the token during execution (e.g., Bash sends SIGTERM when the
        token fires).
        """
        token = getattr(self, "_cancellation", None)
        if getattr(tool, "supports_cancellation", False) and token is not None:
            tool._cancellation_token = token  # type: ignore[attr-defined]
            try:
                return tool.execute(params)
            finally:
                tool._cancellation_token = None  # type: ignore[attr-defined]
        return tool.execute(params)

    def _fire_post_tool_hook(
        self,
        call: AuthorizedToolCall,
        result: "ToolResult",
    ) -> None:
        if self._hook_dispatcher is None:
            return

        event = (
            HookEvent.POST_TOOL_USE
            if result.success
            else HookEvent.POST_TOOL_USE_FAILURE
        )
        context = HookContext(
            event=event,
            tool_name=call.name,
            tool_input=call.params,
            tool_output={
                "success": result.success,
                "output": result.output[:2000],
                "error": result.error or "",
            },
        )
        try:
            dispatch_result = self._hook_dispatcher.dispatch(event, context)
        except Exception:
            return
        if dispatch_result.attachments:
            result.attachments = (
                *result.attachments,
                *dispatch_result.attachments,
            )
