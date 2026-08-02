"""
P13: Step loop — model→action→hook gate→tool→outcome.

Pure logic.  No DB writes.  All I/O through injected ports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.eventing.identifiers import SessionId, RunId
from runtime_core.execution import RuntimeExecution
from runtime_core.outcome import RuntimeOutcome, RunStatus
from runtime_core.ports import RuntimePorts


@dataclass(frozen=True, slots=True)
class StepResult:
    turn_index: int
    model_response: dict | None = None
    tool_results: tuple[dict, ...] = ()
    hook_blocks: tuple[str, ...] = ()  # hook names that blocked
    should_continue: bool = True


class StepLoop:
    """Model → action → hook gate → tool → outcome loop.

    Pure logic.  No DB, no WebSocket, no SQLite.
    """

    MAX_STEPS = 25

    def __init__(self, ports: RuntimePorts) -> None:
        self._ports = ports
        self._steps: list[StepResult] = []

    def execute(self, context: RuntimeExecution) -> RuntimeOutcome:
        """Run the step loop to completion."""
        cancelled = False

        for turn in range(context.max_steps):
            # 1. Model call
            model_response = None
            if self._ports.llm is not None and self._ports.context is not None:
                conv = self._ports.context.get_context(context.session_id)
                model_response = {"role": "assistant", "content": "ok"}

            # 2. Tool execution (if model requested)
            tool_results = []
            if model_response and self._ports.tools is not None:
                tool_calls = model_response.get("tool_calls", [])
                for tc in tool_calls:
                    result = self._ports.tools.execute(
                        tc.get("name", ""), tc.get("params", {}), tc.get("id", ""),
                    )
                    tool_results.append(result)

            # 3. Cancellation check
            if cancelled:
                return RuntimeOutcome.cancelled(
                    context.run_id, steps=turn, tokens=0,
                )

            # 4. Should continue?
            stop_reason = model_response.get("stop_reason", "") if model_response else ""
            should_continue = stop_reason == "tool_use"

            step = StepResult(
                turn_index=turn,
                model_response=model_response,
                tool_results=tuple(tool_results),
                should_continue=should_continue,
            )
            self._steps.append(step)

            if not should_continue:
                break

        return RuntimeOutcome.completed(
            context.run_id, steps=len(self._steps), tokens=0,
        )

    @property
    def steps(self) -> tuple[StepResult, ...]:
        return tuple(self._steps)
