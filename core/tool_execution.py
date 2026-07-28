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
        hitl_manager: Any = None,
        hook_dispatcher: Any = None,
        capability_registry: Any = None,
        session_id: str = "",
        budget: Any = None,  # ExecutionBudget — only EXHAUSTED gates tool calls
    ) -> None:
        self._permission_pipeline = permission_pipeline
        self._hitl_manager = hitl_manager
        self._hook_dispatcher = hook_dispatcher
        self._capability_registry = capability_registry
        self._session_id = session_id
        self._budget = budget

    def execute(
        self,
        tool: "BaseTool",
        params: dict[str, Any],
        *,
        thought: str = "",
        invocation_id: str = "",
    ) -> "ToolResult":
        """Validate and execute one logical call, including safe retries."""
        from core.base import ToolResult
        from core.types import RetryMode

        validation_error = self._validate_params(tool, params)
        if validation_error is not None:
            return validation_error

        capability_error = self._check_capability(tool)
        if capability_error is not None:
            return capability_error

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
                result = tool.execute(call.params)
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
        return result

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
        elif self._hitl_manager is not None:
            hitl_result = self._hitl_manager.check(
                tool, params, thought=thought,
            )
            if hitl_result.is_denied:
                detail = f"Tool '{tool.name}' denied by user."
                if hitl_result.feedback_note:
                    detail += f" Feedback: {hitl_result.feedback_note}"
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

    def _check_capability(self, tool: "BaseTool") -> "ToolResult | None":
        if self._capability_registry is None:
            return None

        import json

        from agent.capability_registry import InterceptDecision
        from core.base import ToolResult

        intercept = self._capability_registry.intercept(
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
