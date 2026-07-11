"""ToolExecutor — single tool call execution + observation recording.

Extracted from ReActAgent._run_body() tool execution loop. Encapsulates:
  - Gated tool decisions (read plan, verification read)
  - Tool execution via registry
  - Observation construction
  - Completion context tracking
  - Structured findings accumulation
  - Macro loop + circuit breaker signaling
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from agent.task import Observation, ObservationStatus, ToolCall

if TYPE_CHECKING:
    from agent.core import AgentConfig
    from agent.completion_guard import CompletionContext
    from agent.policy import TaskPolicy
    from tools.base import ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

_V2_DELEGATION_BLOCK_PREFIX = "BLOCKED_BY_DELEGATION_POLICY:"


@dataclass
class ToolExecResult:
    """Result of executing a single tool call."""
    observation: Observation
    gated: bool = False
    is_edit: bool = False
    is_test_failure: bool = False
    has_structured_findings: bool = False
    structured_findings: tuple = ()
    subagent_tokens: int = 0
    subagent_terminated_by_loop: bool = False
    file_path: str = ""


class ToolExecutor:
    """Execute a single tool call and produce an Observation. Stateless."""

    def __init__(
        self,
        registry: Any,              # ToolRegistry | PolicyAwareToolRegistry
        config: Any,                # AgentConfig
        policy: Any = None,         # TaskPolicy | None
        analysis_phase_state: Any = None,
        analysis_read_plan: Any = None,
        submission_plan_ref: Any = None,
    ) -> None:
        self._registry = registry
        self._cfg = config
        self._policy = policy
        self._analysis_phase_state = analysis_phase_state
        self._analysis_read_plan = analysis_read_plan
        self._submit_plan_ref = submission_plan_ref

    def execute(
        self,
        tool_call: ToolCall,
        *,
        completion_ctx: Any = None,    # CompletionContext | None
        thought: str = "",
        repo_path: str = ".",
    ) -> ToolExecResult:
        """Execute one tool call. Returns a ToolExecResult."""
        gated_decision = self._read_plan_gate(tool_call, repo_path)
        if gated_decision is None:
            gated_decision = self._verification_read_gate(tool_call, repo_path)

        if gated_decision is not None and not gated_decision.allowed:
            observation = self._build_gated_observation(tool_call, gated_decision)
            return ToolExecResult(observation=observation, gated=True)

        # Actually execute the tool
        result = self._registry.execute_tool(tool_call.name, tool_call.params, thought=thought)
        observation = result.to_observation(tool_call.name)

        # Check delegation policy blocks
        if observation.error and observation.error.startswith(_V2_DELEGATION_BLOCK_PREFIX):
            observation.metadata["expected_block"] = True
            observation.metadata["block_kind"] = "v2_delegation_policy"

        # Build result
        exec_result = ToolExecResult(observation=observation)
        exec_result.subagent_tokens = getattr(result, "subagent_tokens_used", 0)
        exec_result.subagent_terminated_by_loop = getattr(result, "subagent_terminated_by_loop", False)

        # Structured findings accumulation
        _sf = getattr(result, "structured_findings", None)
        if _sf:
            exec_result.has_structured_findings = True
            exec_result.structured_findings = _sf

        # Track file operations
        if observation.is_success() and gated_decision is None:
            exec_result.file_path = self._extract_file_path(tool_call)
            if tool_call.name in ("file_write", "file_edit", "edit"):
                exec_result.is_edit = True
            if tool_call.name in self._cfg.test_tool_names and not observation.is_success():
                exec_result.is_test_failure = True
            # submit_read_plan transition
            if tool_call.name == "submit_read_plan":
                self._handle_submit_plan()

        # Completion context tracking
        if completion_ctx is not None:
            completion_ctx.record_tool_result(
                tool_name=tool_call.name,
                path=exec_result.file_path,
                success=observation.is_success() and gated_decision is None,
            )

        return exec_result

    # ── Internal gate helpers ──────────────────────────────────────────

    def _read_plan_gate(self, tool_call: ToolCall, repo_path: str):
        """Check if this read tool is gated by the read plan."""
        state = self._analysis_phase_state
        if state is None or not getattr(state, "enabled", False):
            return None
        if tool_call.name not in ("file_read", "file_view"):
            return None
        # Delegate to the ReActAgent method if available
        return None  # Simplified: V2 disables analysis phase

    def _verification_read_gate(self, tool_call: ToolCall, repo_path: str):
        """Check if this read tool is gated by verification phase."""
        return None  # Simplified: V2 disables analysis phase

    def _build_gated_observation(self, tool_call: ToolCall, decision: Any) -> Observation:
        from agent.runtime_controller import ToolDecision
        if isinstance(decision, ToolDecision):
            return Observation(
                status=ObservationStatus.SUCCESS,
                tool_name=tool_call.name,
                output=decision.synthetic_observation or decision.reason,
            )
        return Observation(
            status=ObservationStatus.SUCCESS,
            tool_name=tool_call.name,
            output="Deferred by analysis controller.",
        )

    @staticmethod
    def _extract_file_path(tool_call: ToolCall) -> str:
        return str(tool_call.params.get("path") or tool_call.params.get("file_path") or "")

    def _handle_submit_plan(self) -> None:
        ref = self._submit_plan_ref
        if ref is not None:
            ref.pending_plan = None
