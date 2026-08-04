"""
G16: AgentRuntime -- thin wrapper, no DB writes, per-run local state.

Creates NativeStepLoop per run.  All providers (Anthropic, OpenAI compat)
use the Native pipeline.  No persistent mutable state across runs.
"""

from __future__ import annotations

from runtime_core.execution import RuntimeExecution
from runtime_core.native_step_loop import NativeStepLoop
from runtime_core.outcome import RuntimeOutcome, RunStatus
from runtime_core.ports import RuntimePorts


class AgentRuntime:
    """Thin runtime wrapper.  No DB, no WebSocket, no SQLite.

    G16: Creates a fresh NativeStepLoop per run() call -- no mutable state
    retained between runs.  All providers now use the Native pipeline.
    """

    def __init__(self, ports: RuntimePorts, scheduler=None) -> None:
        self._ports = ports
        self._scheduler = scheduler  # Phase 7: ToolScheduler for parallel tool execution

    def run(self, context: RuntimeExecution, *,
            text_callback: "Callable[[str], None] | None" = None,
            ) -> RuntimeOutcome:
        """Execute one run.  Pure logic -- no side effects.

        G18: Cancellation is checked via context.cancellation at every
        boundary (model, hook, tool execution).

        text_callback: CC-aligned streaming text output (Phase 10).
        """
        loop = NativeStepLoop(self._ports, scheduler=self._scheduler)
        outcome = loop.execute(context, text_callback=text_callback)

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

    async def arun(
        self, context: RuntimeExecution, *,
        event_handler: "Callable[[dict], None] | None" = None,
        text_callback: "Callable[[str], None] | None" = None,
    ) -> RuntimeOutcome:
        """CC query() 等价 — async 执行, 消费 aiterate async generator.

        Phase F: async 是主路径。event_handler 可选消费 aiterate 事件
        (text_delta / tool_result / completed), 用于 WebSocket 推流。
        """
        loop = NativeStepLoop(self._ports, scheduler=self._scheduler)
        final_outcome = None
        async for event in loop.aiterate(context, text_callback=text_callback):
            if event_handler:
                try:
                    event_handler(event)
                except Exception:
                    pass
            if event["type"] in ("completed", "failed", "cancelled", "blocked"):
                final_outcome = event["outcome"]
        if final_outcome is None:
            final_outcome = RuntimeOutcome.failed(
                context.run_id, error="aiterate produced no terminal outcome",
            )

        # 复用 run() 的事件发布 + token 记录
        from core.json_values import freeze_json
        import uuid as _uuid
        from core.eventing.scope import ScopeToken
        _scope = ScopeToken.session_scope(
            _uuid.uuid4(), context.session_id,
        ) if context.session_id is not None else None
        self._ports.live_events.publish(
            event_type=f"run.{final_outcome.status.value}.v1",
            payload=freeze_json({
                "run_id": str(final_outcome.run_id),
                "status": final_outcome.status.value,
                "summary": final_outcome.summary,
                "steps_taken": final_outcome.steps_taken,
                "tokens_used": final_outcome.tokens_used,
            }),
            scope=_scope,
        )
        if final_outcome.tokens_used > 0:
            self._ports.token_usage.record(
                context.run_id,
                final_outcome.input_tokens or final_outcome.tokens_used,
                final_outcome.output_tokens,
            )
        return final_outcome

    @property
    def ports(self) -> RuntimePorts:
        return self._ports
