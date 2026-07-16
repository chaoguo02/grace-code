"""Typed control surface for existing direct-child agent sessions."""

from __future__ import annotations

import copy
from enum import Enum
from typing import TYPE_CHECKING, Any

from agent.v2.models import (
    AgentCancelOutcome,
    AgentMessageOutcome,
    AgentWaitOutcome,
)
from tools.base import (
    BaseTool,
    ToolEffect,
    ToolMetadata,
    ToolResult,
    ToolRole,
)

if TYPE_CHECKING:
    from agent.v2.runtime import SessionRuntime


class AgentControlAction(str, Enum):
    MESSAGE = "message"
    CANCEL = "cancel"
    WAIT = "wait"


class AgentControlTool(BaseTool):
    """Resume, cancel, or objectively wait for one direct child session."""

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
        self._run_context = None
        self.metadata = ToolMetadata(
            effects=frozenset({delegation_effect}),
            roles=frozenset({ToolRole.DELEGATE}),
        )

    def with_run_context(self, context: Any) -> "AgentControlTool":
        from agent.v2.run_context import RunContext
        if not isinstance(context, RunContext):
            raise TypeError("AgentControlTool requires a RunContext")
        bound = copy.copy(self)
        bound._run_context = context
        return bound

    @property
    def name(self) -> str:
        return "agent_control"

    @property
    def description(self) -> str:
        return (
            "Control an existing direct child by session ID. message resumes a "
            "stopped child in the background with its complete transcript; a "
            "running child cannot be live-steered without an agent-team channel. "
            "cancel requests cooperative stop; wait checks an in-process child."
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
                    "description": "Direct child session ID returned by task.",
                },
                "message": {
                    "type": "string",
                    "description": "Continuation instruction for action=message.",
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
        session_id = params.get("session_id")
        try:
            action = AgentControlAction(raw_action)
        except (TypeError, ValueError):
            return ToolResult(
                success=False, output="",
                error="action must be message, cancel, or wait",
            )
        if not isinstance(session_id, str) or not session_id.strip():
            return ToolResult(
                success=False, output="", error="session_id is required",
            )
        session_id = session_id.strip()

        try:
            if action is AgentControlAction.MESSAGE:
                return self._send_message(session_id, params.get("message"))
            if action is AgentControlAction.CANCEL:
                detail = params.get("message")
                if detail is not None and not isinstance(detail, str):
                    return ToolResult(
                        success=False, output="",
                        error="message must be text when provided",
                    )
                result = self._runtime.cancel_agent(
                    parent_session_id=self._parent_session_id,
                    child_session_id=session_id,
                    detail=(detail or "").strip(),
                )
                return ToolResult(
                    success=result.outcome is not AgentCancelOutcome.UNAVAILABLE,
                    output=self._format_state(
                        action, session_id, result.generation,
                        result.outcome.value, result.session_status.value,
                    ),
                    error=(
                        "Child is not active in this Runtime process"
                        if result.outcome is AgentCancelOutcome.UNAVAILABLE else ""
                    ),
                )

            timeout = params.get("timeout_seconds", 0)
            if not isinstance(timeout, (int, float)) or not 0 <= timeout <= 60:
                return ToolResult(
                    success=False, output="",
                    error="timeout_seconds must be between 0 and 60",
                )
            result = self._runtime.wait_for_agent(
                parent_session_id=self._parent_session_id,
                child_session_id=session_id,
                timeout_seconds=float(timeout),
            )
            return ToolResult(
                success=result.outcome is not AgentWaitOutcome.UNAVAILABLE,
                output=self._format_state(
                    action, session_id, result.generation,
                    result.outcome.value, result.session_status.value,
                ),
                error=(
                    "Child liveness is not owned by this Runtime process"
                    if result.outcome is AgentWaitOutcome.UNAVAILABLE else ""
                ),
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc))

    def _send_message(self, session_id: str, raw_message: Any) -> ToolResult:
        if not isinstance(raw_message, str) or not raw_message.strip():
            return ToolResult(
                success=False, output="",
                error="message is required for action=message",
            )
        context = self._run_context
        if (
            context is None
            or context.phase_policy is None
            or context.delegation_effects is None
            or context.delegation_step_limit is None
        ):
            return ToolResult(
                success=False, output="",
                error="Agent messaging requires a Runtime-bound run context",
            )
        if context.cancellation.is_cancelled:
            return ToolResult(
                success=False, output="", error=context.cancellation.detail,
            )
        receipt = self._runtime.send_agent_message(
            parent_session_id=self._parent_session_id,
            child_session_id=session_id,
            message=raw_message,
            budget_tokens=context.delegation_token_limit,
            parent_max_steps=context.delegation_step_limit,
            cancellation_token=context.cancellation,
            parent_policy=context.phase_policy.with_allowed_effects(
                context.delegation_effects
            ),
        )
        available = receipt.outcome is not AgentMessageOutcome.RUNNING_UNAVAILABLE
        return ToolResult(
            success=available,
            output=self._format_state(
                AgentControlAction.MESSAGE,
                session_id,
                receipt.generation,
                receipt.outcome.value,
                "running",
            ),
            error=(
                "Live steering requires an agent-team communication channel"
                if not available else ""
            ),
        )

    @staticmethod
    def _format_state(
        action: AgentControlAction,
        session_id: str,
        generation: int,
        outcome: str,
        status: str,
    ) -> str:
        return "\n".join([
            "<agent-control>",
            f"  <action>{action.value}</action>",
            f"  <session-id>{session_id}</session-id>",
            f"  <generation>{generation}</generation>",
            f"  <outcome>{outcome}</outcome>",
            f"  <status>{status}</status>",
            "</agent-control>",
        ])
