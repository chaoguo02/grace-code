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

        # CC-aligned: ensure every backend can participate in the async loop.
        # Sync-only backends (test fakes, legacy adapters) are wrapped via
        # to_thread — the same pattern CC uses for sync I/O in async context.
        # This is boot-time harness wiring, not a runtime fallback.
        if not hasattr(self._backend, 'ainvoke') and hasattr(self._backend, 'invoke'):
            import asyncio as _asyncio
            _sync_invoke = self._backend.invoke
            async def _ainvoke(conversation, *, tool_choice=None, cancellation=None):
                return await _asyncio.to_thread(
                    _sync_invoke, conversation,
                    tool_choice=tool_choice, cancellation=cancellation,
                )
            self._backend.ainvoke = _ainvoke  # type: ignore[attr-defined]

        # CC-aligned: wrap sync stream_iter → async astream_iter.
        # Sync-only streaming backends (test fakes) get async generator wrapper
        # so they can participate in aiterate without blocking the event loop.
        if not hasattr(self._backend, 'astream_iter') and hasattr(self._backend, 'stream_iter'):
            import asyncio as _asyncio
            _sync_stream = self._backend.stream_iter
            async def _astream_iter(conversation, *, tool_choice=None, model=""):
                # Yield events from sync iterator via to_thread batches
                def _collect():
                    return list(_sync_stream(conversation, tool_choice=tool_choice, model=model))
                events = await _asyncio.to_thread(_collect)
                for event in events:
                    yield event
            self._backend.astream_iter = _astream_iter  # type: ignore[attr-defined]

        # Store is optional — when absent, persistence is skipped (test mode)
        self._store = store
        self._scheduler = scheduler or ToolScheduler()
        self._state: ConversationState | None = None
        self._budget = context_budget or ContextBudgetManager()

    # ── Main loop (CC query) ─────────────────────────────────────────────────

    async def aiterate(
        self, context: RuntimeExecution, *,
        text_callback: "Callable[[str], None] | None" = None,
    ):
        """CC query() 等价 — async generator, yield 事件给消费方.

        Phase E: async 是主循环。yield 事件:
          - {"type": "text_delta", "text": ...}
          - {"type": "tool_result", "tool_call": ..., "outcome": ...}
          - {"type": "completed", "outcome": ...}
          - {"type": "failed", "outcome": ...}
        await model (astream_iter/ainvoke) + await tool (_atool_calls).
        """
        import asyncio

        # 初始化 state
        if self._store is not None:
            self._state = ConversationState.rebuild_from(
                self._store.rebuild_conversation(),
            )
        else:
            self._state = ConversationState()

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

        for turn in range(context.max_steps):
            if context.cancellation.cancelled:
                self._state.drain_pending_as_errors()
                self._flush_store()
                outcome = self._cancelled_outcome(
                    context.run_id, steps_taken,
                    total_tokens_in + total_tokens_out,
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks,
                )
                yield {"type": "cancelled", "outcome": outcome}
                return

            self._state.record_transition('step')
            steps_taken += 1

            # Context budget
            conv = self._state.to_conversation()
            pruned_conv, _ = self._budget.ensure_budget(conv)

            # CC callModel — await (async generator if stream, else ainvoke)
            try:
                tool_uses: list[ToolCall] = []
                model_action = None
                if hasattr(self._backend, 'astream_iter'):
                    # async streaming — astream_iter may be a coroutine returning
                    # an async generator, or an async generator function.
                    _stream = self._backend.astream_iter(
                        pruned_conv, tool_choice={"type": "auto"},
                    )
                    if asyncio.iscoroutine(_stream):
                        _stream = await _stream
                    text_parts: list[str] = []
                    async for event in _stream:
                        from llm.base import StreamEventKind
                        if event.kind == StreamEventKind.TEXT_DELTA:
                            text_parts.append(event.text)
                            yield {"type": "text_delta", "text": event.text}
                            if text_callback:
                                text_callback(event.text)
                        elif event.kind == StreamEventKind.TOOL_USE:
                            tool_uses.append(event.tool_call)
                        elif event.kind == StreamEventKind.FINISH:
                            break
                    if tool_uses:
                        if len(tool_uses) == 1:
                            model_action = tool_uses[0]
                        else:
                            model_action = ToolCallBatch(calls=tuple(tool_uses))
                    else:
                        model_action = AssistantText(
                            text="".join(text_parts), stop_reason="end_turn",
                        )
                elif hasattr(self._backend, 'ainvoke'):
                    model_action = await self._backend.ainvoke(
                        pruned_conv, tool_choice={"type": "auto"},
                        cancellation=context.cancellation,
                    )
                else:
                    # CC architecture: async loop demands async backend.
                    # No silent fallback to sync invoke() — that blocks the
                    # event loop and masks missing async support in backends.
                    raise RuntimeError(
                        f"Backend {type(self._backend).__name__!r} has neither "
                        f"astream_iter nor ainvoke. All backends in the native "
                        f"async path must implement async model calls."
                    )
            except Exception as exc:
                outcome = RuntimeOutcome.failed(
                    context.run_id,
                    error=f"LLM invoke failed: {exc}",
                    steps=steps_taken,
                    tokens=total_tokens_in + total_tokens_out,
                )
                yield {"type": "failed", "outcome": outcome}
                return

            # Token usage
            if model_action is not None and getattr(model_action, 'usage', None) is not None:
                total_tokens_in += model_action.usage.input_tokens
                total_tokens_out += model_action.usage.output_tokens

            self._state.add_assistant_message(model_action)
            if self._store is not None:
                self._store.append_message(self._state.last_message, turn_index=turn)

            # CC: needsFollowUp? tool_use → run tools
            if isinstance(model_action, (ToolCall, ToolCallBatch)):
                calls = (model_action,) if isinstance(model_action, ToolCall) else model_action.calls
                # await 工具 (async, 不阻塞)
                tool_results = await self._atool_calls(calls, context)

                for tr in tool_results:
                    tool_evidences.append(tr.evidence)
                    self._ports.live_events.publish(
                        event_type="tool.executed.v1",
                        payload=freeze_json({
                            "tool": tr.tool_call.name,
                            "success": isinstance(tr.outcome, ToolSuccess),
                        }),
                        scope=(
                            ScopeToken.session_scope(_uuid.uuid4(), context.session_id)
                            if context.session_id is not None else None
                        ),
                    )
                    self._state.add_tool_result(
                        tr.outcome or ToolFailure(
                            tool_name=tr.tool_call.name,
                            error=tr.hook_deny_reason or "unknown",
                            error_type=ToolErrorType.EXECUTION_ERROR,
                        ),
                        tr.tool_call,
                    )
                    if self._store is not None:
                        self._store.append_message(
                            self._state.last_message, turn_index=turn,
                        )
                    yield {"type": "tool_result", "tool_call": tr.tool_call, "outcome": tr.outcome}

                self._state.record_transition('tool_use')
                self._flush_store()
                continue

            # CC: 无 tool_use → 文本完成
            if isinstance(model_action, AssistantText):
                self._flush_store()
                outcome = self._finalize_outcome(
                    RuntimeOutcome.completed(
                        context.run_id, steps=steps_taken,
                        tokens=total_tokens_in + total_tokens_out,
                        summary=model_action.text[:500],
                    ),
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks,
                )
                yield {"type": "completed", "outcome": outcome}
                return

            if isinstance(model_action, ModelFailure):
                if getattr(model_action, 'retryable', False):
                    self._state.record_transition('error_retry')
                    continue
                outcome = self._finalize_outcome(
                    RuntimeOutcome.failed(
                        context.run_id, error=model_action.error,
                        steps=steps_taken,
                        tokens=total_tokens_in + total_tokens_out,
                    ),
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks,
                )
                yield {"type": "failed", "outcome": outcome}
                return

            # CC: ModelRefusal → blocked (sync execute() parity)
            if isinstance(model_action, ModelRefusal):
                self._flush_store()
                outcome = self._finalize_outcome(
                    RuntimeOutcome.blocked(
                        context.run_id, steps=steps_taken,
                        tokens=total_tokens_in + total_tokens_out,
                        blocked_by="model_refusal",
                        detail=model_action.reason,
                    ),
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks,
                )
                yield {"type": "blocked", "outcome": outcome}
                return

            # CC: ModelStop → completed (sync execute() parity)
            if isinstance(model_action, ModelStop):
                self._flush_store()
                outcome = self._finalize_outcome(
                    RuntimeOutcome.completed(
                        context.run_id, steps=steps_taken,
                        tokens=total_tokens_in + total_tokens_out,
                        summary=model_action.text or model_action.stop_reason,
                    ),
                    total_tokens_in, total_tokens_out,
                    tool_evidences, files_touched, hook_blocks,
                )
                yield {"type": "completed", "outcome": outcome}
                return

        # max_steps 达到
        outcome = self._finalize_outcome(
            RuntimeOutcome.blocked(
                context.run_id, steps=steps_taken,
                tokens=total_tokens_in + total_tokens_out,
                blocked_by="max_steps",
                detail=f"max_steps={context.max_steps}",
            ),
            total_tokens_in, total_tokens_out,
            tool_evidences, files_touched, hook_blocks,
        )
        yield {"type": "blocked", "outcome": outcome}

    # ── Async tool execution (CC runTools) ────────────────────────────────

    async def _atool_calls(
        self, calls: tuple[ToolCall, ...],
        context: RuntimeExecution,
    ) -> list[ToolResult]:
        """CC runTools() 等价 — 读工具并行, 写工具串行.

        Phase D: async 是工具执行主路径。分区（partitionToolCalls 等价）：
          - read_only + concurrency_safe → 并行 (TaskGroup, cap 10)
          - 其他 (写/破坏性) → 串行
        """
        import asyncio

        # CC partitionToolCalls
        safe: list[ToolCall] = []
        serial: list[ToolCall] = []
        for tc in calls:
            meta = self._scheduler._registry.get(tc.name)
            if meta and meta.read_only and meta.concurrency_safe:
                safe.append(tc)
            else:
                serial.append(tc)

        results: list[ToolResult] = []
        # 并行读 (CC runToolsConcurrently, cap 10)
        if safe:
            for i in range(0, len(safe), 10):
                chunk = safe[i:i + 10]
                chunk_results = await asyncio.gather(
                    *[self._atool_one(tc, context) for tc in chunk],
                )
                results.extend(chunk_results)
        # 串行写 (CC runToolsSerially)
        for tc in serial:
            results.append(await self._atool_one(tc, context))
        return results

    async def _atool_one(
        self, tc: ToolCall, context: RuntimeExecution,
    ) -> ToolResult:
        """CC runToolUse() 等价 — hook → permission → await tool → post.

        Phase D: 工具执行 await tool.aexecute (async, 不阻塞事件循环)。
        """
        import asyncio
        from hook_core.inputs import PreToolUseInput, PostToolUseInput

        if context.cancellation.cancelled:
            return ToolResult(
                tool_call=tc, hook_allowed=False, hook_deny_reason="cancelled",
            )

        hook_input = PreToolUseInput(
            tool_name=tc.name, tool_input=tc.params, tool_use_id=tc.id,
            session_id=(
                str(context.session_id)
                if context.session_id is not None else ""
            ),
            cwd=(context.workspace if context.workspace else ""),
        )
        try:
            gate_result = self._ports.hooks.check(
                "PreToolUse", hook_input, tool_name=tc.name,
            )
        except Exception:
            gate_result = HookGateResult(allowed=False, reason="hook error")

        if not gate_result.allowed:
            return ToolResult(
                tool_call=tc, hook_allowed=False,
                hook_deny_reason=gate_result.reason or "denied",
            )

        final_params = gate_result.updated_input or tc.params

        if context.cancellation.cancelled:
            return ToolResult(
                tool_call=tc, hook_allowed=False, hook_deny_reason="cancelled",
            )

        try:
            # CC tool.call() — await async 工具执行
            tool_outcome = await self._ports.tools.aexecute(
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

        return ToolResult(
            tool_call=tc, outcome=tool_outcome,
            hook_allowed=True, post_hook_context=post_context,
        )

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
