"""
CC-Native ContextWindowManager — single entry point for context assembly.

Design (P0_1 Batch 2):
  ONE build_context() for ALL paths (main session, sub-agent, fork).
  Progressive degradation:
    Micro → SessionMemory → API compaction
      API fails / circuit breaker open
        → DeterministicTrimmer (pair-aware, never throws)
          → HARD invariant: context_tokens + output_room <= provider_limit

  Decoupled from:
    - Session database format (receives list[dict])
    - MCP transport layer
    - Tool registry
    - HITL pipeline
    - Specific LLM backend (only through LocalTokenEstimator interface)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from context.counters import LocalTokenEstimator
    from context.planner import BudgetPlan, TokenPlanner
    from context.compaction_v2 import CompactionStrategy, CompactResult
    from context.trimmer import DeterministicTrimmer

logger = logging.getLogger(__name__)


# ── ContextAssembly ─────────────────────────────────────────────────────────

@dataclass
class ContextAssembly:
    """Result of a single context assembly — ready to send to the provider."""
    messages: list[dict]
    system_content: str | list[dict]
    compaction_applied: list[str] = field(default_factory=list)
    fallback_trim_applied: bool = False
    estimated_tokens: int = 0
    budget_total: int = 0


# ── TaskContext ─────────────────────────────────────────────────────────────

@dataclass
class TaskContext:
    """Clean, explicit task contract for sub-agents.

    CC paradigm: sub-agents start with a CLEAN context.
    They never inherit the parent's conversation history.

    task + agent_type:        required — what to do and what role
    workspace_scope:          filesystem boundary (worktree root)
    constraints:              explicit constraints (e.g. "read-only")
    artifact_refs:            references to large parent outputs
    expected_output:          deliverable definition
    parent_run_id:            tracking only — NOT context
    context_provenance:       "primary" | "fork" | "worktree"
    """

    task: str
    agent_type: str = "general"
    workspace_scope: str | None = None
    constraints: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    expected_output: str | None = None
    parent_run_id: str | None = None
    context_provenance: str = "fork"
    tool_allowlist: list[str] | None = None
    max_turns: int = 25

    def to_system_prompt(self) -> str:
        """Render task contract as sub-agent system prompt content."""
        parts = [f"## Task\n{self.task}"]
        if self.constraints:
            parts.append("## Constraints\n" + "\n".join(f"- {c}" for c in self.constraints))
        if self.expected_output:
            parts.append(f"## Expected Output\n{self.expected_output}")
        if self.artifact_refs:
            parts.append("## Available Artifacts\n" + "\n".join(f"- {r}" for r in self.artifact_refs))
        if self.workspace_scope:
            parts.append(f"## Workspace\n{self.workspace_scope}")
        return "\n\n".join(parts)


# ── ContextWindowManager ────────────────────────────────────────────────────

@dataclass
class ContextWindowManagerConfig:
    """Tunables for the context window manager.

    All values are constructor-injected — no env or config-file dependency.
    """
    auto_compact_threshold: float = 0.80
    max_consecutive_api_failures: int = 3
    output_room_default: int = 4096


class ContextWindowManager:
    """CC-Native context window manager — single entry point.

    Progressive degradation:
      1. TokenPlanner.plan() → budget allocation
      2. LocalTokenEstimator → check if over threshold
      3. CompactionStrategy chain (Micro → SessionMemory → API)
      4. API fails / circuit breaker open → DeterministicTrimmer
      5. Final invariant check: estimated_tokens + output_room <= window
    """

    def __init__(
        self,
        estimator: LocalTokenEstimator,
        planner: TokenPlanner,
        compaction_chain: list[CompactionStrategy],
        trimmer: DeterministicTrimmer,
        config: ContextWindowManagerConfig | None = None,
    ) -> None:
        self._estimator = estimator
        self._planner = planner
        self._compaction_chain = compaction_chain
        self._trimmer = trimmer
        self._cfg = config or ContextWindowManagerConfig()
        self._consecutive_api_failures = 0

    # ── single entry point ──────────────────────────────────────────────

    def build_context(
        self,
        *,
        system_content: str,
        history: list[dict],
        memory_context: str = "",
        task_anchor: str = "",
        repo_map_text: str = "",
        consumed_tokens: int = 0,
        output_room: int | None = None,
        task_context: TaskContext | None = None,
    ) -> ContextAssembly:
        """Assemble context for ONE request — all paths.

        Args:
            system_content:  Core system prompt text.
            history:         Conversation history as dicts.
            memory_context:  Long-term memory text.
            task_anchor:     Task anchor prompt.
            repo_map_text:   Repository structure summary.
            consumed_tokens: Tokens consumed so far this session.
            output_room:     Reserved output tokens (None → default).
            task_context:    Sub-agent task contract (None → main session).

        Returns:
            ContextAssembly ready to send.
        """
        room = output_room if output_room is not None else self._cfg.output_room_default
        window = self._estimator.model_context_window

        # 1. Budget allocation
        plan = self._planner.plan(
            model_window=window,
            consumed_tokens=consumed_tokens,
            output_room=room,
        )

        # 2. Build raw messages — sub-agent uses clean TaskContext
        if task_context is not None:
            messages = self._build_sub_agent_messages(
                task_context=task_context,
                system_content=system_content,
            )
        else:
            messages = self._build_main_messages(
                history=history,
                system_content=system_content,
                memory_context=memory_context,
                task_anchor=task_anchor,
                repo_map_text=repo_map_text,
            )

        # 3. Estimate and check threshold
        est = self._estimator.estimate_messages(messages)
        threshold = int(window * self._cfg.auto_compact_threshold)

        compaction_applied: list[str] = []
        fallback_trim = False

        # 4. Progressive compaction
        if est > threshold and self._compaction_chain:
            for strategy in self._compaction_chain:
                if self._estimator.estimate_messages(messages) <= threshold:
                    break

                # Circuit breaker for API compaction
                if strategy.name == "api" and self._consecutive_api_failures >= self._cfg.max_consecutive_api_failures:
                    logger.warning(
                        "API compaction circuit breaker open (%d consecutive failures). "
                        "Falling back to deterministic trim.",
                        self._consecutive_api_failures,
                    )
                    break

                try:
                    result = strategy.compact(
                        messages=messages,
                        current_tokens=self._estimator.estimate_messages(messages),
                        target_tokens=threshold,
                        task_context=task_context.task if task_context else "",
                    )
                    if result.tokens_saved > 0:
                        messages = result.compacted_messages
                        compaction_applied.append(strategy.name)
                        if strategy.name == "api":
                            self._consecutive_api_failures = 0
                except Exception:
                    if strategy.name == "api":
                        self._consecutive_api_failures += 1
                        logger.warning(
                            "API compaction failed (%d/%d consecutive).",
                            self._consecutive_api_failures,
                            self._cfg.max_consecutive_api_failures,
                        )

        # 5. Deterministic trim — always as safety net
        if self._estimator.estimate_messages(messages) > threshold:
            trim_result = self._trimmer.trim(
                messages=messages,
                max_tokens=threshold,
                estimator=self._estimator,
            )
            messages = trim_result.messages
            fallback_trim = True

        # 6. Final estimate
        final_est = self._estimator.estimate_messages(messages)

        return ContextAssembly(
            messages=messages,
            system_content=system_content,
            compaction_applied=compaction_applied,
            fallback_trim_applied=fallback_trim,
            estimated_tokens=final_est,
            budget_total=plan.total,
        )

    # ── internal: message builders ───────────────────────────────────────

    def _build_main_messages(
        self,
        history: list[dict],
        system_content: str,
        memory_context: str,
        task_anchor: str,
        repo_map_text: str,
    ) -> list[dict]:
        """Build message list for main session path."""
        messages: list[dict] = []

        # System message
        system_text = system_content
        if repo_map_text:
            system_text += f"\n\n{repo_map_text}"
        messages.append({"role": "system", "content": system_text})

        # Memory context
        if memory_context:
            messages.append({"role": "user", "content": memory_context})
            messages.append({"role": "assistant", "content": "I'll keep this context in mind."})

        # History
        messages.extend(history)

        # Task anchor (as user message at the end)
        if task_anchor:
            messages.append({"role": "user", "content": task_anchor})

        return messages

    def _build_sub_agent_messages(
        self,
        task_context: TaskContext,
        system_content: str,
    ) -> list[dict]:
        """Build message list for sub-agent path.

        CC paradigm: sub-agent gets a CLEAN context.
        NO parent history — only system prompt + task contract.
        """
        task_prompt = task_context.to_system_prompt()

        return [
            {
                "role": "system",
                "content": f"{system_content}\n\n{task_prompt}",
            },
        ]
