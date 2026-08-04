"""runtime_core/native_step_loop.py

Native 路径 StepLoop — 零 LLMMessage、零 message_mapper、零补丁。

替代旧 StepLoop（runtime_core/step_loop.py）在 Native 路径的使用。
旧 StepLoop 继续服务 Legacy 路径（OpenAI/DeepSeek/文本模式）。

五条架构原则全部在此体现：
1. NativeMessage 原语（非 LLMMessage str|list）
2. ConversationState 保证协议完整性（StepLoop 不碰 tool_use_id）
3. NativeBackend 持有工具（invoke 不传 tools）
4. ConversationStore 事件溯源（即时落库，非事后追加）
5. 强类型封闭 — 核心循环零 isinstance(content, str)
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass, field

from core.eventing.identifiers import RunId
from core.eventing.scope import ScopeToken
from core.json_values import freeze_json
from runtime_core.context_budget import ContextBudgetManager
from runtime_core.conversation_state import ConversationState
from runtime_core.conversation_store import ConversationStore
from runtime_core.execution import CancellationHandle, RuntimeExecution
from runtime_core.model_actions import (
    AssistantText,
    ModelAction,
    ModelFailure,
    ModelRefusal,
    ModelStop,
    ToolCall,
    ToolCallBatch,
)
from runtime_core.native_backend import NativeBackend
from runtime_core.outcome import (
    CancellationReason,
    RunEvidence,
    RuntimeOutcome,
    RunStatus,
    ToolEvidence,
)
from runtime_core.ports import (
    HookGateResult,
    RuntimePorts,
    ToolDenied,
    ToolFailure,
    ToolOutcome,
    ToolSuccess,
    ToolErrorType,
)
from runtime_core.tool_scheduler import ToolScheduler


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Result of processing one tool call through hook+execute."""
    tool_call: ToolCall
    outcome: ToolOutcome | None = None
    hook_allowed: bool = True
    hook_deny_reason: str = ""
    post_hook_context: str = ""

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
            tool_use_id=self.tool_call.id,
        )


# ── NativeStepLoop ──────────────────────────────────────────────────────────


class NativeStepLoop:
    """Native 路径 StepLoop — 纯净的消息管道。

    与旧 StepLoop 的本质区别：
    - 零 dict 消息构造（不再 build_tool_messages）
    - 零工具 schema 透传（NativeBackend 已绑定）
    - 零内存消息收集（ConversationStore 即时落库）
    - 零 isinstance(content, str) — 全程 NativeMessage
    """

    MAX_STEPS = 25

    def __init__(
        self,
        ports: RuntimePorts,
        backend: "NativeBackend | None" = None,
        store: "ConversationStore | None" = None,
        scheduler: ToolScheduler | None = None,
        *,
        context_budget: ContextBudgetManager | None = None,
    ) -> None:
        self._ports = ports
        # Derive backend from ports.llm if not explicitly provided.
        # Prefer ports.llm directly (allows test overrides on .invoke).
        # Fall back to ports.llm._backend for NativeBackendAdapter wrapping.
        if backend is not None:
            self._backend = backend
        elif hasattr(ports.llm, '_backend') and hasattr(ports.llm._backend, 'invoke'):
            self._backend = ports.llm._backend
        else:
            self._backend = ports.llm
        # Store is optional — when absent, persistence is skipped (test mode)
        self._store = store
        self._scheduler = scheduler or ToolScheduler()
        self._state: ConversationState | None = None
        self._budget = context_budget or ContextBudgetManager()

    # ── Main loop ───────────────────────────────────────────────────────

    def execute(self, context: RuntimeExecution, *,
                text_callback: "Callable[[str], None] | None" = None,
                ) -> RuntimeOutcome:
        """Execute one Run -- pure Model -> Hook -> Tool -> Outcome loop.

        text_callback: if set, model calls use stream_iter() and push each
        text delta to this callback (CC-aligned streaming text output).
        """
        # Rebuild state from DB if store available (crash recovery)
        if self._store is not None:
            self._state = ConversationState.rebuild_from(
                self._store.rebuild_conversation()
            )
        else:
            self._state = ConversationState()

        # 注入本轮用户输入（如果尚未持久化）
        for msg_dict in context.conversation.messages:
            role = msg_dict.get("role", "user") if isinstance(msg_dict, dict) else "user"
            content = msg_dict.get("content", "") if isinstance(msg_dict, dict) else str(msg_dict)
            if role == "user" and content:
                self._state.add_user_message(content)
            elif role == "system" and content:
                self._state.add_system_message(content)

        total_tokens_in = 0
        total_tokens_out = 0
        tool_evidences: list[ToolEvidence] = []
        files_touched: set[str] = set()
        hook_blocks: list[str] = []
        steps_taken = 0

        _exec_scope = ScopeToken.session_scope(
            _uuid.uuid4(), context.session_id,
        ) if context.session_id is not None else None

        for turn in range(context.max_steps):
            # G18+P2: Cancellation check — 协作式取消
            if context.cancellation.cancelled:
                # P2: 优雅收敛 — drain pending tool_uses as errors
                self._state.drain_pending_as_errors()
                self._flush_store()
                return self._cancelled_outcome(
                    context.run_id, steps_taken,
                    total_tokens_in + total_tokens_out,
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks,
                )

            steps_taken += 1

            # ── P1: Context budget check ──────────────────────────────
            conv = self._state.to_conversation()
            pruned_conv, budget_report = self._budget.ensure_budget(conv)
            if budget_report.messages_trimmed > 0:
                logger = __import__("logging").getLogger(__name__)
                logger.info(
                    "Context budget: %d→%d tokens (%d messages trimmed, %d tool_results)",
                    budget_report.original_tokens,
                    budget_report.original_tokens - budget_report.trimmed_tokens,
                    budget_report.messages_trimmed,
                    budget_report.tool_results_trimmed,
                )

            # ── 1. Model call — 无需传 tools！ ────────────────────────
            try:
                if text_callback is not None and hasattr(self._backend, 'stream_iter'):
                    model_action = self._stream_model_call(
                        pruned_conv, text_callback,
                        cancellation=context.cancellation,
                    )
                else:
                    model_action = self._backend.invoke(
                        pruned_conv,
                        tool_choice={"type": "auto"},
                        cancellation=context.cancellation,
                   )
            except Exception as exc:
                return RuntimeOutcome.failed(
                    context.run_id,
                    error=f"LLM invoke failed: {exc}",
                    steps=steps_taken,
                    tokens=total_tokens_in + total_tokens_out,
                )

            # H3: Extract real token usage
            if model_action is not None and hasattr(model_action, 'usage') and model_action.usage is not None:
                step_tokens_in = model_action.usage.input_tokens
                step_tokens_out = model_action.usage.output_tokens
            else:
                step_tokens_in = 0
                step_tokens_out = 0
            total_tokens_in += step_tokens_in
            total_tokens_out += step_tokens_out

            # ── 2. State 自动记录 assistant 消息 ──────────────────────
            self._state.add_assistant_message(model_action)

            # ── 3. 即时持久化 assistant 消息 ─────────────────────────
            if self._store is not None:
                self._store.append_message(
                    self._state.last_message, turn_index=turn,
                )

            # ── 4. Process model action ───────────────────────────────
            if isinstance(model_action, AssistantText):
                # P1: Flush before completion
                self._flush_store()
                return self._finalize_outcome(
                    RuntimeOutcome.completed(
                        context.run_id, steps=steps_taken,
                        tokens=total_tokens_in + total_tokens_out,
                        summary=model_action.text[:500],
                    ),
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks,
                )

            if isinstance(model_action, ModelStop):
                self._flush_store()
                return self._finalize_outcome(
                    RuntimeOutcome.completed(
                        context.run_id, steps=steps_taken,
                        tokens=total_tokens_in + total_tokens_out,
                        summary=model_action.text or model_action.stop_reason,
                    ),
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks,
                )

            if isinstance(model_action, (ToolCall, ToolCallBatch)):
                calls = (model_action,) if isinstance(model_action, ToolCall) else model_action.calls

                # 执行工具（串行或并行）
                if isinstance(model_action, ToolCallBatch) and len(calls) > 1:
                    import asyncio
                    tool_results = asyncio.run(
                        self._process_tool_calls_parallel(calls, context=context)
                    )
                else:
                    tool_results = self._process_tool_calls(calls, context=context)

                # H4: Collect tool evidence
                for tr in tool_results:
                    tool_evidences.append(tr.evidence)

                # Publish live events
                for tr in tool_results:
                    if tr.outcome is not None:
                        self._ports.live_events.publish(
                            event_type="tool.executed.v1",
                            payload=freeze_json({
                                "tool": tr.tool_call.name,
                                "success": isinstance(tr.outcome, ToolSuccess),
                            }),
                            scope=_exec_scope,
                        )

                # ── 5. State 自动构造 tool_result（StepLoop 不碰 tool_use_id） ──
                for tr in tool_results:
                    self._state.add_tool_result(
                        tr.outcome or ToolFailure(
                            tool_name=tr.tool_call.name,
                            error=tr.hook_deny_reason or "unknown",
                            error_type=ToolErrorType.EXECUTION_ERROR,
                        ),
                        tr.tool_call,
                    )
                    # ── 6. 即时持久化 tool_result ────────────────────
                    if self._store is not None:
                        self._store.append_message(
                            self._state.last_message, turn_index=turn,
                        )

                # T8: PostToolBatch hook
                if len(tool_results) > 0:
                    try:
                        from hook_core.inputs import PostToolBatchInput
                        self._ports.hooks.check(
                            "PostToolBatch",
                            PostToolBatchInput(
                                session_id=str(context.session_id),
                                tool_count=len(tool_results),
                            ),
                            tool_name="",
                        )
                    except Exception:
                        pass

                # P1: Flush after tool_use→tool_result pairs complete
                self._flush_store()

                continue

            if isinstance(model_action, ModelRefusal):
                return self._finalize_outcome(
                    RuntimeOutcome.blocked(
                        context.run_id, steps=steps_taken,
                        tokens=total_tokens_in + total_tokens_out,
                        blocked_by="model_refusal",
                        detail=model_action.reason,
                    ),
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks,
                )

            if isinstance(model_action, ModelFailure):
                if model_action.retryable:
                    continue
                return self._finalize_outcome(
                    RuntimeOutcome.failed(
                        context.run_id,
                        error=model_action.error,
                        steps=steps_taken,
                        tokens=total_tokens_in + total_tokens_out,
                    ),
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks,
                )

        return self._finalize_outcome(
            RuntimeOutcome.blocked(
                context.run_id, steps=steps_taken,
                tokens=total_tokens_in + total_tokens_out,
                blocked_by="max_steps",
                detail=f"max_steps={context.max_steps}",
            ),
            total_tokens_in, total_tokens_out,
            tool_evidences, files_touched, hook_blocks,
        )

    # ── Streaming model call (Phase 10) ────────────────────────────────────

    def _stream_model_call(self, conversation, text_callback, *, cancellation):
        """stream_iter → text_callback 实时输出 → 返回完整 ModelAction。

        CC-aligned streaming pipeline:
          TEXT_DELTA → text_callback (rendering)
          TOOL_USE → collect
          FINISH / end of stream → assemble ModelAction
        Errors propagate as ModelFailure.
        """
        tool_uses: list[ToolCall] = []
        final_text_parts: list[str] = []

        try:
            for event in self._backend.stream_iter(
                conversation, tool_choice={"type": "auto"},
            ):
                if cancellation is not None and getattr(cancellation, 'cancelled', False):
                    break
                kind = getattr(event, 'kind', None)
                if kind is None:
                    continue
                from llm.base import StreamEventKind
                if kind == StreamEventKind.TEXT_DELTA:
                    text = getattr(event, 'text', '') or ''
                    if text:
                        text_callback(text)
                        final_text_parts.append(text)
                elif kind == StreamEventKind.TOOL_USE:
                    tc = getattr(event, 'tool_call', None)
                    if tc is not None:
                        tool_uses.append(tc)
                elif kind == StreamEventKind.ERROR:
                    return ModelFailure(
                        error=getattr(event, 'text', '') or 'stream error',
                        retryable=False,
                    )
                elif kind == StreamEventKind.FINISH:
                    break
        except Exception as exc:
            return ModelFailure(
                error=f"Stream failed: {exc}", retryable=True,
            )

        if tool_uses:
            if len(tool_uses) == 1:
                return tool_uses[0]
            return ToolCallBatch(calls=tuple(tool_uses))
        return AssistantText(
            text="".join(final_text_parts), stop_reason="end_turn",
        )

    # ── Tool processing (same as old StepLoop — unchanged contract) ─────

    def _process_tool_calls(
        self, calls: tuple[ToolCall, ...],
        context: RuntimeExecution | None = None,
    ) -> list[ToolResult]:
        """PreToolUse hook → permission → execute → PostToolUse hook."""
        from hook_core.inputs import PreToolUseInput, PostToolUseInput

        results: list[ToolResult] = []
        for tc in calls:
            if context is not None and context.cancellation.cancelled:
                break

            hook_input = PreToolUseInput(
                tool_name=tc.name, tool_input=tc.params, tool_use_id=tc.id,
            )
            try:
                gate_result = self._ports.hooks.check(
                    "PreToolUse", hook_input, tool_name=tc.name,
                )
            except Exception:
                gate_result = HookGateResult(allowed=False, reason="hook error")

            if not gate_result.allowed:
                results.append(ToolResult(
                    tool_call=tc, hook_allowed=False,
                    hook_deny_reason=gate_result.reason or "denied",
                ))
                continue

            final_params = gate_result.updated_input or tc.params

            if context is not None and context.cancellation.cancelled:
                break

            try:
                tool_outcome = self._ports.tools.execute(
                    tc.name, final_params, tc.id,
                )
            except Exception as exc:
                tool_outcome = ToolFailure(
                    tool_name=tc.name,
                    error=f"{type(exc).__name__}: {exc}",
                    error_type=ToolErrorType.EXECUTION_ERROR,
                )

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
                pass

            results.append(ToolResult(
                tool_call=tc, outcome=tool_outcome,
                hook_allowed=True, post_hook_context=post_context,
            ))

        return results

    async def _process_tool_calls_parallel(
        self, calls: tuple[ToolCall, ...],
        context: RuntimeExecution,
    ) -> list[ToolResult]:
        """G19: 并行工具执行。"""
        import asyncio

        batches = self._scheduler.schedule(calls)
        all_results: list[ToolResult] = []

        for batch in batches:
            if context.cancellation.cancelled:
                break
            if len(batch) == 1:
                results = self._process_tool_calls(tuple(batch), context=context)
                all_results.extend(results)
            else:
                results = await self._execute_parallel_batch(batch, context)
                all_results.extend(results)

        return all_results

    async def _execute_parallel_batch(
        self, batch: list[ToolCall], context: RuntimeExecution,
    ) -> list[ToolResult]:
        """Execute a single parallel batch."""
        import asyncio
        from hook_core.inputs import PreToolUseInput

        results: list[ToolResult] = [None] * len(batch)  # type: ignore

        # Phase 7: asyncio.to_thread so blocking tool.execute() calls
        # run in real OS threads — not blocking the event loop.
        async def _run_in_thread(idx: int, tc: ToolCall) -> None:
            await asyncio.to_thread(_run_sync, idx, tc)

        def _run_sync(idx: int, tc: ToolCall) -> None:
            if context.cancellation.cancelled:
                results[idx] = ToolResult(
                    tool_call=tc, hook_allowed=False, hook_deny_reason="cancelled",
                )
                return
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
            params = gate.updated_input or tc.params
            try:
                outcome = self._ports.tools.execute(tc.name, params, tc.id)
            except Exception as exc:
                outcome = ToolFailure(
                    tool_name=tc.name, error=str(exc),
                    error_type=ToolErrorType.EXECUTION_ERROR,
                )
            results[idx] = ToolResult(tool_call=tc, outcome=outcome, hook_allowed=True)

        try:
            async with asyncio.TaskGroup() as tg:
                for i, tc in enumerate(batch):
                    tg.create_task(_run_in_thread(i, tc))
        except* Exception:
            pass

        return [r for r in results if r is not None]

    # ── Persistence helper ──────────────────────────────────────────────

    def _flush_store(self) -> None:
        """P1: Explicit flush — write buffered messages to DB.

        Called at critical boundaries:
        - After tool_use->tool_result pair completion
        - Before text response (end of run)
        - After cancellation convergence
        """
        if self._store is not None and hasattr(self._store, 'flush'):
            self._store.flush()

    # ── Outcome helpers ──────────────────────────────────────────────────

    @staticmethod
    def _finalize_outcome(
        outcome: RuntimeOutcome,
        input_tokens: int,
        output_tokens: int,
        tool_evidences: list[ToolEvidence],
        files_touched: set[str],
        hook_blocks: list[str],
    ) -> RuntimeOutcome:
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

    def _cancelled_outcome(
        self, run_id: RunId, steps: int, tokens: int,
        tokens_in: int = 0, tokens_out: int = 0,
        tool_evidences=None, files_touched=None, hook_blocks=None,
    ) -> RuntimeOutcome:
        return self._finalize_outcome(
            RuntimeOutcome.cancelled(
                run_id,
                reason=CancellationReason.USER_REQUESTED,
                steps=steps, tokens=tokens,
            ),
            tokens_in, tokens_out,
            tool_evidences or [], files_touched or set(), hook_blocks or [],
        )
