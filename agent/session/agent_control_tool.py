"""Typed child-session control tools plus a compatibility wrapper."""

from __future__ import annotations

import copy
from enum import Enum
from typing import TYPE_CHECKING, Any

from agent.session.models import (
    AgentCancelOutcome,
    AgentMessageOutcome,
    AgentWaitOutcome,
)
from core.base import (
    BaseTool,
    ToolEffect,
    ToolMetadata,
    ToolResult,
    ToolRole,
)

if TYPE_CHECKING:
    from agent.session.runtime import SessionRuntime
    from agent.session.run_context import RunContext


class AgentControlAction(str, Enum):
    MESSAGE = "message"
    CANCEL = "cancel"
    WAIT = "wait"


class _BaseAgentControlTool(BaseTool):
    """Shared runtime binding + delegation metadata for child control tools."""

    def __init__(
        self,
        runtime: "SessionRuntime",
        parent_session_id: str,
        *,
        delegation_effect: ToolEffect,
    ) -> None:
        if delegation_effect not in {
            ToolEffect.DELEGATE_READ_ONLY,
            ToolEffect.DELEGATE_WRITE,
        }:
            raise ValueError("Agent control requires a delegation effect")
        self._runtime = runtime
        self._parent_session_id = parent_session_id
        self._run_context: "RunContext | None" = None
        self.metadata = ToolMetadata(
            effects=frozenset({delegation_effect}),
            roles=frozenset({ToolRole.DELEGATE}),
        )

    def with_run_context(self, context: Any) -> "_BaseAgentControlTool":
        from agent.session.run_context import RunContext
        if not isinstance(context, RunContext):
            raise TypeError(f"{type(self).__name__} requires a RunContext")
        bound = copy.copy(self)
        bound._run_context = context
        return bound

    def _validated_session_id(self, params: dict[str, Any]) -> str | None:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        return session_id.strip()

    def _require_run_context(self) -> "RunContext | None":
        context = self._run_context
        if (
            context is None
            or context.phase_policy is None
            or context.delegation_effects is None
            or context.delegation_step_limit is None
        ):
            return None
        return context


class SendMessageTool(_BaseAgentControlTool):
    """Steer a live direct child or resume a terminal child."""

    @property
    def name(self) -> str:
        return "SendMessage"

    @property
    def description(self) -> str:
        return (
            "Send a follow-up message to an existing direct child session. "
            "For a running child, the message is queued and becomes visible at "
            "its next safe model turn; it does not interrupt an active tool. "
            "For a stopped child, this starts a new background generation with "
            "the persisted transcript."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Direct child session ID returned by Agent.",
                },
                "message": {
                    "type": "string",
                    "description": "Continuation instruction for the child session.",
                },
            },
            "required": ["session_id", "message"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        session_id = self._validated_session_id(params)
        if session_id is None:
            return ToolResult(success=False, output="", error="session_id is required")
        raw_message = params.get("message")
        if not isinstance(raw_message, str) or not raw_message.strip():
            return ToolResult(
                success=False, output="", error="message is required",
            )
        context = self._require_run_context()
        if context is None:
            return ToolResult(
                success=False, output="",
                error="Agent messaging requires a Runtime-bound run context",
            )
        if context.cancellation.is_cancelled:
            return ToolResult(
                success=False, output="", error=context.cancellation.detail,
            )
        try:
            receipt = self._runtime.send_agent_message(
                parent_session_id=self._parent_session_id,
                child_session_id=session_id,
                message=raw_message.strip(),
                budget_tokens=context.delegation_token_limit,
                parent_max_steps=context.delegation_step_limit,
                cancellation_token=context.cancellation,
                parent_policy=context.phase_policy.with_allowed_effects(
                    context.delegation_effects
                ),
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc))
        available = receipt.outcome is not AgentMessageOutcome.RUNNING_UNAVAILABLE
        return ToolResult(
            success=available,
            output=_format_state(
                "message", session_id, receipt.generation,
                receipt.outcome.value, "running",
            ),
            error=(
                "Running child sessions cannot receive live follow-up messages; "
                "use WaitForAgent or CancelAgent, then resume with SendMessage "
                "after the child becomes terminal"
                if not available else ""
            ),
        )


class WaitForAgentTool(_BaseAgentControlTool):
    """Wait briefly for one running direct child session."""

    @property
    def name(self) -> str:
        return "WaitForAgent"

    @property
    def description(self) -> str:
        return (
            "Wait for an existing direct child session to finish. This checks "
            "only Runtime-owned in-process liveness and returns quickly."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Direct child session ID returned by Agent.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 60,
                    "description": "Bounded wait duration.",
                },
            },
            "required": ["session_id"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        session_id = self._validated_session_id(params)
        if session_id is None:
            return ToolResult(success=False, output="", error="session_id is required")
        timeout = params.get("timeout_seconds", 0)
        if not isinstance(timeout, (int, float)) or not 0 <= timeout <= 60:
            return ToolResult(
                success=False, output="",
                error="timeout_seconds must be between 0 and 60",
            )
        try:
            result = self._runtime.wait_for_agent(
                parent_session_id=self._parent_session_id,
                child_session_id=session_id,
                timeout_seconds=float(timeout),
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc))
        return ToolResult(
            success=result.outcome is not AgentWaitOutcome.UNAVAILABLE,
            output=_format_state(
                "wait", session_id, result.generation,
                result.outcome.value, result.session_status.value,
            ),
            error=(
                "Child liveness is not owned by this Runtime process"
                if result.outcome is AgentWaitOutcome.UNAVAILABLE else ""
            ),
        )


class CancelAgentTool(_BaseAgentControlTool):
    """Request cooperative cancellation of one direct child session."""

    @property
    def name(self) -> str:
        return "CancelAgent"

    @property
    def description(self) -> str:
        return (
            "Request cooperative cancellation of an existing direct child "
            "session by session ID."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Direct child session ID returned by Agent.",
                },
                "message": {
                    "type": "string",
                    "description": "Optional cancellation detail shown to the child.",
                },
            },
            "required": ["session_id"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        session_id = self._validated_session_id(params)
        if session_id is None:
            return ToolResult(success=False, output="", error="session_id is required")
        detail = params.get("message")
        if detail is not None and not isinstance(detail, str):
            return ToolResult(
                success=False, output="", error="message must be text when provided",
            )
        try:
            result = self._runtime.cancel_agent(
                parent_session_id=self._parent_session_id,
                child_session_id=session_id,
                detail=(detail or "").strip(),
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc))
        return ToolResult(
            success=result.outcome is not AgentCancelOutcome.UNAVAILABLE,
            output=_format_state(
                "cancel", session_id, result.generation,
                result.outcome.value, result.session_status.value,
            ),
            error=(
                "Child is not active in this Runtime process"
                if result.outcome is AgentCancelOutcome.UNAVAILABLE else ""
            ),
        )


class AgentControlTool(_BaseAgentControlTool):
    """Compatibility wrapper over the split child-control tool surface."""

    def __init__(
        self,
        runtime: "SessionRuntime",
        parent_session_id: str,
        *,
        delegation_effect: ToolEffect,
    ) -> None:
        super().__init__(
            runtime, parent_session_id, delegation_effect=delegation_effect,
        )
        self._send = SendMessageTool(
            runtime, parent_session_id, delegation_effect=delegation_effect,
        )
        self._wait = WaitForAgentTool(
            runtime, parent_session_id, delegation_effect=delegation_effect,
        )
        self._cancel = CancelAgentTool(
            runtime, parent_session_id, delegation_effect=delegation_effect,
        )

    def with_run_context(self, context: Any) -> "AgentControlTool":
        bound = copy.copy(self)
        bound._run_context = context
        bound._send = self._send.with_run_context(context)
        bound._wait = self._wait.with_run_context(context)
        bound._cancel = self._cancel.with_run_context(context)
        return bound

    @property
    def name(self) -> str:
        return "agent_control"

    @property
    def description(self) -> str:
        return (
            "Compatibility wrapper for child control. Prefer SendMessage, "
            "WaitForAgent, and CancelAgent when available. Live messages are "
            "queued for the child's next safe model turn."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [action.value for action in AgentControlAction],
                },
                "session_id": {
                    "type": "string",
                    "description": "Direct child session ID returned by Agent.",
                },
                "message": {
                    "type": "string",
                    "description": "Continuation instruction or cancel detail.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 60,
                    "description": "Bounded wait for action=wait.",
                },
            },
            "required": ["action", "session_id"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        raw_action = params.get("action")
        try:
            action = AgentControlAction(raw_action)
        except (TypeError, ValueError):
            return ToolResult(
                success=False, output="",
                error="action must be message, cancel, or wait",
            )
        if action is AgentControlAction.MESSAGE:
            return self._send.execute(params)
        if action is AgentControlAction.CANCEL:
            return self._cancel.execute(params)
        return self._wait.execute(params)


def _format_state(
    action: str,
    session_id: str,
    generation: int,
    outcome: str,
    status: str,
) -> str:
    return "\n".join([
        "<agent-control>",
        f"  <action>{action}</action>",
        f"  <session-id>{session_id}</session-id>",
        f"  <generation>{generation}</generation>",
        f"  <outcome>{outcome}</outcome>",
        f"  <status>{status}</status>",
        "</agent-control>",
    ])
