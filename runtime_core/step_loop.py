"""
G17: Hook/Tool loop — PreToolUse gate, permission, execute, PostToolUse.

ToolCall processing pipeline:
  PreToolUse hook → permission decision (allow/deny/ask/defer/transform)
  → ToolPort.execute() for allowed tools
  → PostToolUse hook
  → conversation block + live event candidate

PostToolUse never rolls back completed tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.eventing.identifiers import RunId
from core.json_values import freeze_json, FrozenJsonObject
from runtime_core.execution import RuntimeExecution
from runtime_core.model_actions import (
    ModelAction,
    AssistantText,
    ToolCall,
    ToolCallBatch,
    ModelStop,
    ModelRefusal,
    ModelFailure,
)
from runtime_core.execution import CancellationHandle
from runtime_core.outcome import (
    RuntimeOutcome, RunStatus, CancellationReason,
    ToolEvidence, RunEvidence,
)
from runtime_core.ports import RuntimePorts, HookGateResult, ToolOutcome, ToolSuccess, ToolFailure, ToolDenied
from runtime_core.tool_scheduler import ToolScheduler, ToolMetadata
from hook_core.inputs import PreToolUseInput, PostToolUseInput


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Result of processing one tool call through hook+execute."""
    tool_call: ToolCall
    outcome: ToolOutcome | None = None
    hook_allowed: bool = True
    hook_deny_reason: str = ""
    post_hook_context: str = ""

    # H4: Evidence derived from this tool execution
    @property
    def evidence(self) -> ToolEvidence:
        success = self.hook_allowed and (
            isinstance(self.outcome, ToolSuccess) if self.outcome else False
        )
        duration = getattr(self.outcome, 'duration_ms', 0.0) if self.outcome else 0.0
        return ToolEvidence(
            tool_name=self.tool_call.name,
            success=success,
            duration_ms=duration,
        )


@dataclass(frozen=True, slots=True)
class StepResult:
    """Immutable result of one step in the model loop."""
    turn_index: int
    model_action: ModelAction | None = None
    tool_results: tuple[ToolResult, ...] = ()
    hook_result: HookGateResult | None = None
    should_continue: bool = True
    tokens_input: int = 0
    tokens_output: int = 0


class StepLoop:
    """Model → Hook → Tool → Outcome loop.  Pure logic."""

    MAX_STEPS = 25

    def __init__(self, ports: RuntimePorts,
                 scheduler: ToolScheduler | None = None) -> None:
        self._ports = ports
        self._scheduler = scheduler or ToolScheduler()

    def execute(self, context: RuntimeExecution) -> RuntimeOutcome:
        steps: list[StepResult] = []
        total_tokens_in = 0
        total_tokens_out = 0
        # H4: Collect tool evidence throughout the run
        tool_evidences: list[ToolEvidence] = []
        files_touched: set[str] = set()
        hook_blocks: list[str] = []

        for turn in range(context.max_steps):
            # G18: Cancellation check at top of every iteration
            if context.cancellation.cancelled:
                return self._cancelled_outcome(context.run_id, steps,
                                               total_tokens_in + total_tokens_out,
                                               total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks)

            # ── 1. Model call ───────────────────────────────────────
            conv_json = freeze_json({
                "messages": [
                    {"role": m.get("role", ""), "content": m.get("content", "")}
                    for m in context.conversation.messages
                ],
            })

            try:
                model_action = self._ports.llm.invoke(conv_json)
            except Exception as exc:
                return self._finalize_outcome(RuntimeOutcome.failed(
                    context.run_id, error=f"LLM invoke failed: {exc}",
                    steps=len(steps),
                    tokens=total_tokens_in + total_tokens_out,
                ), total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks)

            # H3: Extract real token usage from model action (no more hardcoded 100/50)
            if model_action is not None and hasattr(model_action, 'usage') and model_action.usage is not None:
                step_tokens_in = model_action.usage.input_tokens
                step_tokens_out = model_action.usage.output_tokens
            else:
                step_tokens_in = 0
                step_tokens_out = 0
            total_tokens_in += step_tokens_in
            total_tokens_out += step_tokens_out

            # ── 2. Process model action ─────────────────────────────
            if isinstance(model_action, AssistantText):
                steps.append(StepResult(turn_index=turn, model_action=model_action,
                                        should_continue=False,
                                        tokens_input=step_tokens_in, tokens_output=step_tokens_out))
                return self._finalize_outcome(RuntimeOutcome.completed(
                    context.run_id, steps=len(steps),
                    tokens=total_tokens_in + total_tokens_out,
                    summary=model_action.text[:500]), total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks)

            if isinstance(model_action, ModelStop):
                steps.append(StepResult(turn_index=turn, model_action=model_action,
                                        should_continue=False,
                                        tokens_input=step_tokens_in, tokens_output=step_tokens_out))
                return self._finalize_outcome(RuntimeOutcome.completed(
                    context.run_id, steps=len(steps),
                    tokens=total_tokens_in + total_tokens_out,
                    summary=model_action.text or model_action.stop_reason),
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks)

            if isinstance(model_action, (ToolCall, ToolCallBatch)):
                calls = (model_action,) if isinstance(model_action, ToolCall) else model_action.calls
                # H5: Default to parallel execution for multi-tool batches
                if isinstance(model_action, ToolCallBatch) and len(calls) > 1:
                    import asyncio
                    tool_results = asyncio.run(
                        self._process_tool_calls_parallel(calls, context=context)
                    )
                else:
                    tool_results = self._process_tool_calls(calls, context=context)
                # H4: Collect tool evidence from results
                for tr in tool_results:
                    tool_evidences.append(tr.evidence)

                # Publish live events for each tool result
                for tr in tool_results:
                    if tr.outcome is not None:
                        self._ports.live_events.publish(
                            event_type="tool.executed.v1",
                            payload=freeze_json({"tool": tr.tool_call.name, "success": isinstance(tr.outcome, ToolSuccess)}),
                        )

                steps.append(StepResult(turn_index=turn, model_action=model_action,
                                        tool_results=tuple(tool_results),
                                        should_continue=True,
                                        tokens_input=step_tokens_in, tokens_output=step_tokens_out))
                continue

            if isinstance(model_action, ModelRefusal):
                steps.append(StepResult(turn_index=turn, model_action=model_action,
                                        should_continue=False,
                                        tokens_input=step_tokens_in, tokens_output=step_tokens_out))
                return self._finalize_outcome(RuntimeOutcome.blocked(
                    context.run_id, steps=len(steps),
                    tokens=total_tokens_in + total_tokens_out,
                    blocked_by="model_refusal", detail=model_action.reason),
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks)

            if isinstance(model_action, ModelFailure):
                if model_action.retryable:
                    steps.append(StepResult(turn_index=turn, model_action=model_action,
                                            tokens_input=step_tokens_in, tokens_output=step_tokens_out))
                    continue
                return self._finalize_outcome(RuntimeOutcome.failed(
                    context.run_id, error=model_action.error,
                    steps=len(steps),
                    tokens=total_tokens_in + total_tokens_out),
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks)

        return self._finalize_outcome(RuntimeOutcome.blocked(
            context.run_id, steps=len(steps),
            tokens=total_tokens_in + total_tokens_out,
            blocked_by="max_steps",
            detail=f"max_steps={context.max_steps}"),
            total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks)

    # ── G17: Tool call pipeline ────────────────────────────────────────

    @staticmethod
    def _finalize_outcome(outcome: RuntimeOutcome, input_tokens: int,
                          output_tokens: int,
                          tool_evidences: list[ToolEvidence],
                          files_touched: set[str],
                          hook_blocks: list[str]) -> RuntimeOutcome:
        """H3+H4: Inject tokens + evidence into a frozen outcome."""
        object.__setattr__(outcome, 'input_tokens', input_tokens)
        object.__setattr__(outcome, 'output_tokens', output_tokens)
        if tool_evidences or files_touched or hook_blocks:
            evidence = RunEvidence(
                tool_calls=tuple(tool_evidences),
                files_touched=tuple(sorted(files_touched)),
                hook_blocks=tuple(hook_blocks),
            )
            object.__setattr__(outcome, 'evidence', evidence)
        return outcome

    def _cancelled_outcome(self, run_id: RunId, steps: list, tokens: int,
                           tokens_in: int = 0, tokens_out: int = 0,
                           tool_evidences=None, files_touched=None,
                           hook_blocks=None) -> RuntimeOutcome:
        """H6: Build cancelled outcome with evidence."""
        return self._finalize_outcome(RuntimeOutcome.cancelled(
            run_id,
            reason=CancellationReason.USER_REQUESTED,
            steps=len(steps),
            tokens=tokens,
        ), tokens_in, tokens_out,
           tool_evidences or [], files_touched or set(), hook_blocks or [])

    def _process_tool_calls(self, calls: tuple[ToolCall, ...],
                            context: RuntimeExecution | None = None) -> list[ToolResult]:
        """PreToolUse hook → permission → execute → PostToolUse hook.

        G18: Checks cancellation before each tool call.
        """
        results: list[ToolResult] = []

        for tc in calls:
            # G18: Cancellation check before each tool
            if context is not None and context.cancellation.cancelled:
                break

            # ── PreToolUse hook gate ────────────────────────────────
            # tc.params is already FrozenJsonObject — pass directly
            hook_input = PreToolUseInput(
                tool_name=tc.name,
                tool_input=tc.params,
                tool_use_id=tc.id,
            )

            try:
                gate_result = self._ports.hooks.check(
                    "PreToolUse", hook_input, tool_name=tc.name,
                )
            except Exception:
                gate_result = HookGateResult(
                    allowed=False, reason="hook execution failed",
                )

            if not gate_result.allowed:
                # G17: Denied — return typed denial, do NOT call ToolPort
                results.append(ToolResult(
                    tool_call=tc,
                    hook_allowed=False,
                    hook_deny_reason=gate_result.reason or "denied by hook",
                ))
                continue

            # ── Check for ASK permission ─────────────────────────────
            if gate_result.decision is not None and hasattr(gate_result.decision, 'permission'):
                perm = gate_result.decision.permission
                if hasattr(perm, 'value') and str(perm) == 'ask':
                    results.append(ToolResult(
                        tool_call=tc,
                        hook_allowed=False,
                        hook_deny_reason="approval required (ask)",
                    ))
                    continue
                if hasattr(perm, 'value') and str(perm) == 'defer':
                    results.append(ToolResult(
                        tool_call=tc,
                        hook_allowed=False,
                        hook_deny_reason="deferred by hook",
                    ))
                    continue

            # ── Transform: replace tool input ────────────────────────
            final_params = tc.params
            if gate_result.updated_input is not None:
                final_params = gate_result.updated_input

            # G18: Cancellation check before tool execution
            if context is not None and context.cancellation.cancelled:
                break

            # ── Execute tool ─────────────────────────────────────────
            try:
                tool_outcome = self._ports.tools.execute(
                    tc.name, final_params, tc.id,
                )
            except Exception as exc:
                tool_outcome = ToolFailure(
                    tool_name=tc.name,
                    error=f"{type(exc).__name__}: {exc}",
                )

            # ── PostToolUse hook (observe only — cannot rollback) ────
            post_context = ""
            try:
                post_input = PostToolUseInput(
                    tool_name=tc.name,
                    tool_input=final_params,
                    tool_output=getattr(tool_outcome, 'output', ''),
                    tool_use_id=tc.id,
                    success=isinstance(tool_outcome, ToolSuccess),
                )
                post_result = self._ports.hooks.check(
                    "PostToolUse", post_input, tool_name=tc.name,
                )
                if post_result.additional_context:
                    post_context = post_result.additional_context
            except Exception:
                pass  # PostToolUse failure is non-blocking

            results.append(ToolResult(
                tool_call=tc,
                outcome=tool_outcome,
                hook_allowed=True,
                post_hook_context=post_context,
            ))

        return results

    # ── G19: Parallel tool execution ────────────────────────────────────

    async def _process_tool_calls_parallel(
        self, calls: tuple[ToolCall, ...],
        context: RuntimeExecution,
    ) -> list[ToolResult]:
        """Execute tools using ToolScheduler for parallel-safe grouping.

        G19: read-only + concurrency-safe tools run in parallel.
        Write/destructive tools run serially.
        Results in original call order.
        """
        import asyncio

        batches = self._scheduler.schedule(calls)
        all_results: list[ToolResult] = []

        for batch in batches:
            if context.cancellation.cancelled:
                break

            if len(batch) == 1:
                # Single tool — use serial path
                results = self._process_tool_calls(
                    tuple(batch), context=context,
                )
                all_results.extend(results)
            else:
                # Parallel batch — run via TaskGroup
                results = await self._execute_parallel_batch(
                    batch, context,
                )
                all_results.extend(results)

        return all_results

    async def _execute_parallel_batch(
        self, batch: list[ToolCall], context: RuntimeExecution,
    ) -> list[ToolResult]:
        """Execute a single parallel batch of tools."""
        import asyncio

        results: list[ToolResult] = [None] * len(batch)  # type: ignore
        cancel_evt = asyncio.Event()

        async def run_one(idx: int, tc: ToolCall) -> None:
            if context.cancellation.cancelled:
                cancel_evt.set()
                results[idx] = ToolResult(
                    tool_call=tc, hook_allowed=False,
                    hook_deny_reason="cancelled",
                )
                return

            # PreToolUse hook
            hook_input = PreToolUseInput(
                tool_name=tc.name, tool_input=tc.params, tool_use_id=tc.id,
            )
            try:
                gate = self._ports.hooks.check(
                    "PreToolUse", hook_input, tool_name=tc.name,
                )
            except Exception:
                gate = HookGateResult(allowed=False, reason="hook error")

            if not gate.allowed:
                results[idx] = ToolResult(
                    tool_call=tc, hook_allowed=False,
                    hook_deny_reason=gate.reason or "denied",
                )
                return

            # Execute
            params = gate.updated_input or tc.params
            try:
                outcome = self._ports.tools.execute(tc.name, params, tc.id)
            except Exception as exc:
                outcome = ToolFailure(tool_name=tc.name, error=str(exc))

            results[idx] = ToolResult(
                tool_call=tc, outcome=outcome, hook_allowed=True,
            )

        try:
            async with asyncio.TaskGroup() as tg:
                for i, tc in enumerate(batch):
                    tg.create_task(run_one(i, tc))
        except* Exception:
            pass

        # Filter out None (cancelled before start)
        return [r for r in results if r is not None]
