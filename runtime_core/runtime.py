"""
G16: AgentRuntime -- thin wrapper, no DB writes, per-run local state.

Creates StepLoop per run.  No persistent mutable state across runs.

Phase 7A: Accepts optional NativeBackend + ConversationStore.
When both are provided (Anthropic provider), creates NativeStepLoop
instead of the Legacy StepLoop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from runtime_core.execution import RuntimeExecution
from runtime_core.outcome import RuntimeOutcome, RunStatus
from runtime_core.ports import RuntimePorts

if TYPE_CHECKING:
    from runtime_core.native_backend import NativeBackend
    from runtime_core.conversation_store import ConversationStore
    from runtime_core.context_budget import ContextBudgetManager


class AgentRuntime:
    """Thin runtime wrapper.  No DB, no WebSocket, no SQLite.

    G16: Creates a fresh StepLoop per run() call -- no mutable state
    retained between runs.

    Phase 7A: When native_backend + conversation_store are provided,
    creates NativeStepLoop (Anthropic Native pipeline).  Otherwise
    falls back to Legacy StepLoop (OpenAI/DeepSeek/test mode).
    """

    def __init__(
        self,
        ports: RuntimePorts,
        native_backend: "NativeBackend | None" = None,
        conversation_store: "ConversationStore | None" = None,
        context_budget: "ContextBudgetManager | None" = None,
    ) -> None:
        self._ports = ports
        self._native_backend = native_backend
        self._store = conversation_store
        self._budget = context_budget

    def run(self, context: RuntimeExecution) -> RuntimeOutcome:
        """Execute one run.  Pure logic -- no side effects.

        G18: Cancellation is checked via context.cancellation at every
        boundary (model, hook, tool execution).
        """
        if self._native_backend is not None and self._store is not None:
            from runtime_core.native_step_loop import NativeStepLoop
            loop = NativeStepLoop(
                self._ports,
                self._native_backend,
                self._store,
                context_budget=self._budget,
            )
        else:
            from runtime_core.step_loop import StepLoop
            loop = StepLoop(self._ports)

        outcome = loop.execute(context)

        # H7: Publish live event with FrozenJsonObject payload
        from core.json_values import freeze_json
        import uuid as _uuid
        from core.eventing.scope import ScopeToken
        _scope = ScopeToken.session_scope(_uuid.uuid4(), context.session_id) if context.session_id is not None else None
        self._ports.live_events.publish(
            event_type=f"run.{outcome.status.value}.v1",
            payload=freeze_json({
                "run_id": str(outcome.run_id),
                "status": outcome.status.value,
                "summary": outcome.summary,
                "steps_taken": outcome.steps_taken,
                "tokens_used": outcome.tokens_used,
            }),
            scope=_scope,
        )

        # H3: Record separated input/output token usage
        if outcome.tokens_used > 0:
            self._ports.token_usage.record(
                context.run_id,
                outcome.input_tokens or outcome.tokens_used,
                outcome.output_tokens,
            )

        return outcome

    @property
    def ports(self) -> RuntimePorts:
        return self._ports
