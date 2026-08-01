"""
server/services/chat_pipeline.py

ChatPipeline — 6-stage orchestrator for a single chat execution.

Extracted from ``_run_and_notify()`` nested function in AgentService (P1-10).
Stages transform an immutable request into an immutable prepared run, making
the pipeline independently testable and preventing partially-mutated state.

Usage::

    pipeline = ChatPipeline(ports)
    request = ChatRequest(session_id="abc", prompt="fix the bug", ...)
    pipeline.run_in_background(request)
"""

from __future__ import annotations

import logging
import os
import re as _re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from agent.task import RunResult, TaskIntent
from llm.base import LLMMessage

if TYPE_CHECKING:
    from agent.session.runtime import SessionRuntime
    from hooks.protocol import HookAttachment
    from llm.base import LLMBackend
    from server.services.event_bus import EventBus

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Sensitive paths that must NOT be resolved via @mention expansion
_DENY_PREFIXES: tuple[str, ...] = (
    ".git/", ".git", ".forge-agent/", ".grace/",
    ".claude/", ".env", "settings.json", "secrets",
)

_AT_RE = _re.compile(r"(?:^|\s)@(\S+)")
_MENTION_MAX_CHARS: int = 5000


@dataclass(frozen=True)
class ChatPipelinePorts:
    """Explicit dependencies required by ChatPipeline."""

    runtime: Any
    session_service: Any
    backend: Any
    config: Any
    effective_llm_config: Mapping[str, Any]
    repo_path: str
    build_confirm_callback: Callable[[str], Callable]
    reload_rules: Callable[[], None]
    loaded_rules: Callable[[], list]
    accumulate_session_stats: Callable[[str, RunResult], None]
    compact_session_async: Callable[[str], None]
    event_bus: Any = None
    plan_revisions: Any = None


def _maybe_auto_compact(
    ports: ChatPipelinePorts, session_id: str, result: RunResult,
) -> None:
    """Trigger auto-compaction after a round if thresholds are met.

    Mirrors CLI ChatSession._maybe_auto_compact_after_round (chat.py:393-408).
    Checks four gates: config enabled → result status → round count → token threshold.
    """
    try:
        from agent.task import RunStatus

        _config = ports.config
        if _config is None:
            return

        # Gate 1: auto-compaction enabled in config
        if not getattr(_config.context, 'auto_compact_after_round', True):
            return

        # Gate 2: result status must be terminal
        if result.status not in (RunStatus.SUCCESS, RunStatus.GAVE_UP, RunStatus.MAX_STEPS):
            return

        # Gate 3: round count
        _rec = ports.session_service.get_session(session_id)
        if _rec is None:
            return
        _round_count = _rec.metadata.get("round_count", 0) if _rec.metadata else 0
        _compact_every = getattr(_config.context, 'compact_every_rounds', 3)
        if _compact_every <= 0 or _round_count % _compact_every != 0:
            return

        # Gate 4: token threshold
        try:
            _msgs = ports.session_service.get_messages(session_id)
            _token_est = sum(max(1, len(str(m.get("content", ""))) // 3) for m in _msgs)
        except Exception:
            _token_est = 0
        _threshold = getattr(_config.context, 'session_compact_tokens', 30_000)
        if _token_est < _threshold:
            return

        logger.info("Auto-compaction triggered — session=%s round=%d tokens=%d",
                     session_id[:8], _round_count, _token_est)
        ports.compact_session_async(session_id)
    except Exception:
        logger.debug("Auto-compaction check skipped", exc_info=True)


# ── Immutable request/preparation values ─────────────────────────────────────


@dataclass(frozen=True)
class ChatRequest:
    """Caller-owned, immutable input for one chat run."""

    session_id: str
    prompt: str
    display_prompt: str = ""
    """User-visible prompt persisted to history when execution prompt differs."""
    agent_name: str = "build"
    skill_name: str = ""
    """User-invocable Skill name from the request body."""
    skill_arguments: str = ""
    """Raw structured Skill arguments from the request body."""
    product_mode: str = ""
    """Explicit product mode: 'plan', 'build', or 'multi-agent'.
    When empty, derived from agent_name as fallback for legacy callers."""
    intent: TaskIntent | None = None
    permission_mode: str = "acceptEdits"
    repo_path: str = "."
    allowed_prompts: tuple[dict[str, str], ...] = ()
    """CC-aligned: ExitPlanMode pre-approved tool calls for the build session."""
    run_context: Any = None
    """RunContext from POST /chat transaction — carries run_id/turn_id/turn_index."""


@dataclass(frozen=True)
class PreparedChatRun:
    """Pipeline-owned values prepared before agent execution."""

    request: ChatRequest
    resolved_prompt: str
    session_context_text: str | None = None
    confirm_callback: Callable | None = None
    stream_callback: Callable | None = None
    prompt_attachments: tuple["HookAttachment", ...] = ()


@dataclass(frozen=True)
class SubmittedPrompt:
    """User input after the blockable UserPromptSubmit hook boundary."""

    text: str
    attachments: tuple["HookAttachment", ...] = ()


# ── ChatPipeline ─────────────────────────────────────────────────────────────


class ChatPipeline:
    """6-stage orchestrator for a single chat execution.

    Replaces the 280-line ``_run_and_notify()`` nested function in
    ``AgentService.run_chat_async()`` (P1-10).
    """

    def __init__(self, ports: ChatPipelinePorts) -> None:
        self._ports = ports
        self._metrics_callbacks: list[Callable] = []
        """Hook-based observability: callbacks invoked after LLM invocation.
        Each callback receives a ``RetryMetrics`` dataclass.  Zero-overhead
        when empty (P2-18)."""

    # ── helpers ──────────────────────────────────────────────────────────

    @property
    def _runtime(self) -> "SessionRuntime":
        return self._ports.runtime

    @property
    def _event_bus(self) -> "EventBus | None":
        return self._ports.event_bus

    @property
    def _backend(self) -> "LLMBackend":
        return self._ports.backend

    @property
    def _config(self) -> Any:
        return self._ports.config

    # ── Stage 1: @mention resolution ─────────────────────────────────────

    def submit_user_prompt(self, request: ChatRequest) -> SubmittedPrompt:
        """Dispatch the blockable user-input lifecycle event."""
        dispatcher = self._runtime.hook_dispatcher
        if dispatcher is None:
            return SubmittedPrompt(request.prompt)

        from hooks.events import HookContext, HookEvent
        from hooks.protocol import HookControl

        result = dispatcher.dispatch(
            HookEvent.USER_PROMPT_SUBMIT,
            HookContext(
                event=HookEvent.USER_PROMPT_SUBMIT,
                session_id=request.session_id,
                user_input=request.prompt,
            ),
        )
        if result.control is HookControl.BLOCK:
            raise PermissionError(
                result.reason or "User prompt blocked by hook",
            )
        text = request.prompt
        if result.updated_input and "user_input" in result.updated_input:
            updated = result.updated_input["user_input"]
            if not isinstance(updated, str):
                raise TypeError("UserPromptSubmit updated_input.user_input must be a string")
            text = updated
        return SubmittedPrompt(text=text, attachments=result.attachments)

    def resolve_mentions(
        self, request: ChatRequest, prompt: str | None = None,
    ) -> str:
        """Expand safe ``@<path>`` references and return a new prompt.

        Blocked paths (``_DENY_PREFIXES``, project-external, directories)
        are kept as-is — no expansion, no error.
        """
        repo_root = Path(request.repo_path).resolve()

        def _resolve_one(match: _re.Match) -> str:
            ref = match.group(1).rstrip(".,;:!?")
            for prefix in _DENY_PREFIXES:
                if ref.startswith(prefix) or prefix in ref:
                    return match.group(0)
            full = (repo_root / ref).resolve()
            try:
                full.relative_to(repo_root)
            except ValueError:
                return match.group(0)
            if full.is_file():
                try:
                    content = full.read_text(encoding="utf-8")[: _MENTION_MAX_CHARS]
                    lines = content.count("\n") + 1
                    return (
                        f"\n[FILE: {ref} ({lines} lines)]\n"
                        f"{content}\n"
                        f"[/FILE]\n"
                    )
                except Exception:
                    return match.group(0)
            return match.group(0)

        return _AT_RE.sub(_resolve_one, request.prompt if prompt is None else prompt)

    # ── Stage 2: model switch ────────────────────────────────────────────

    def apply_model_switch(
        self, request: ChatRequest,
    ) -> "LLMBackend | None":
        """Pop pending model switch → create per-session backend.

        Returns the new backend if a switch was applied, or ``None``
        if no pending switch exists.
        """
        pending = self._runtime.pop_pending_model(request.session_id)
        if not pending:
            return None

        model, provider = pending
        logger.info(
            "Model switch — session=%s model=%s provider=%s",
            request.session_id[:8], model, provider,
        )
        from llm.router import create_backend_from_config

        ec = self._ports.effective_llm_config
        session_backend = create_backend_from_config({
            "provider": provider or ec["provider"],
            "model": model,
            "api_key": ec["api_key"],
            "base_url": ec["base_url"],
            "max_tokens": ec["max_tokens"],
            "timeout_seconds": ec["timeout_seconds"],
        })
        self._runtime.set_backend_for_session(request.session_id, session_backend)
        return session_backend

    # ── Stage 3: session context injection ───────────────────────────────

    def inject_session_context(self, request: ChatRequest) -> str | None:
        """Ask SessionService for changed runtime context."""
        return self._ports.session_service.claim_session_context(
            request.session_id, request.repo_path,
        )

    # ── Stage 4: build callbacks ─────────────────────────────────────────

    def build_callbacks(
        self, request: ChatRequest,
    ) -> tuple[Callable | None, Callable | None]:
        """Create and register callbacks, returning prepared values."""
        confirm_callback = self._ports.build_confirm_callback(
            request.session_id,
        )
        self._runtime.set_web_confirm_callback(
            request.session_id, confirm_callback,
        )

        stream_callback = None
        if self._event_bus is not None:
            eb = self._event_bus
            sid = request.session_id
            run_ctx = request.run_context  # captured in closures, never stored on EventBus

            from server.events import (
                WsThoughtDelta,
                WsAssistantTextStart, WsAssistantTextDelta,
                WsAssistantTextEnd, WsAssistantTextAborted,
            )

            _thought_buffer = ""
            _text_buffers: dict[str, str] = {}

            def _chunk_ready(text: str) -> bool:
                stripped = text.rstrip()
                return (
                    len(text) >= 160
                    or "\n\n" in text
                    or stripped.endswith(
                        ("。", "！", "？", ".", "!", "?", "；", ";", ":")
                    )
                )

            def _stream_cb(text: str) -> None:
                nonlocal _thought_buffer
                try:
                    _thought_buffer += text
                    if _chunk_ready(_thought_buffer):
                        eb.publish_typed(
                            sid,
                            WsThoughtDelta(text=_thought_buffer),
                            run_context=run_ctx,
                        )
                        _thought_buffer = ""
                except Exception:
                    pass

            stream_callback = _stream_cb
            self._runtime.set_stream_callback(request.session_id, _stream_cb)

            # ── Text stream lifecycle callback → assistant_text_start/end/aborted ──
            def _flush_text(block_id: str) -> None:
                buffered = _text_buffers.pop(block_id, "")
                if buffered:
                    eb.publish_typed(
                        sid,
                        WsAssistantTextDelta(
                            block_id=block_id,
                            text=buffered,
                        ),
                        run_context=run_ctx,
                    )

            def _text_lifecycle_cb(evt_type: str, block_id: str, reason: str = "") -> None:
                try:
                    if evt_type == "start":
                        _text_buffers[block_id] = ""
                        eb.publish_typed(sid, WsAssistantTextStart(block_id=block_id), run_context=run_ctx)
                    elif evt_type == "end":
                        _flush_text(block_id)
                        eb.publish_typed(sid, WsAssistantTextEnd(block_id=block_id), run_context=run_ctx)
                    elif evt_type == "aborted":
                        _flush_text(block_id)
                        eb.publish_typed(sid, WsAssistantTextAborted(block_id=block_id, reason=reason), run_context=run_ctx)
                except Exception:
                    pass

            def _text_delta_cb(block_id: str, text: str) -> None:
                try:
                    _text_buffers[block_id] = _text_buffers.get(block_id, "") + text
                    if _chunk_ready(_text_buffers[block_id]):
                        _flush_text(block_id)
                except Exception:
                    pass

            self._runtime.set_text_stream_callbacks(
                request.session_id,
                _text_lifecycle_cb,
                _text_delta_cb,
            )
        return confirm_callback, stream_callback

    # ── Stage 5: execute ─────────────────────────────────────────────────

    def execute(
        self, prepared: PreparedChatRun,
    ) -> RunResult:
        """Run the agent via SessionRuntime.run_session().

        Returns the ``RunResult`` — *does not* push any WS events.
        Call ``finish()`` afterwards to handle plan_ready / completed / failed.
        """
        request = prepared.request
        self._ports.reload_rules()

        # Apply pending Skill/model runtime modifiers exactly once.
        effort_override = (
            self._runtime.pop_pending_effort(request.session_id) or ""
        )
        pop_skill_modifier = getattr(
            self._runtime,
            "pop_pending_skill_modifier",
            None,
        )
        skill_modifier = (
            pop_skill_modifier(request.session_id)
            if callable(pop_skill_modifier)
            else None
        )
        self._runtime.pop_pending_thinking(request.session_id)

        # Permission mode — consumed by run_chat_async() and passed via ctx.
        # NOT re-popped here; the pop happens once in the caller.
        inject_rules = None
        loaded_rules = self._ports.loaded_rules()
        if loaded_rules:
            inject_rules = list(loaded_rules)

        # Register agent name for stats tracking
        if self._event_bus is not None and self._event_bus.recorder is not None:
            self._event_bus.recorder.set_session_agent(
                request.session_id, request.agent_name,
            )

        result = self._runtime.run_session(
            session_id=request.session_id,
            agent_name=request.agent_name,
            task_description=self._render_prepared_prompt(prepared),
            intent=request.intent,
            product_mode=request.product_mode,
            request_skill=request.skill_name,
            skill_arguments=request.skill_arguments,
            messages=(
                [LLMMessage(role="user", content=request.display_prompt)]
                if request.display_prompt
                and request.display_prompt != request.prompt
                else None
            ),
            inject_permission_mode=request.permission_mode,
            inject_rules=inject_rules,
            session_context_text=prepared.session_context_text or "",
            allowed_prompts=list(request.allowed_prompts),
            effort_override=effort_override,
            skill_modifier=skill_modifier,
            run_context=request.run_context,
        )

        # Accumulate cross-round stats in session metadata
        self._ports.accumulate_session_stats(request.session_id, result)
        return result

    @staticmethod
    def _render_prepared_prompt(prepared: PreparedChatRun) -> str:
        text = prepared.resolved_prompt or prepared.request.prompt
        if not prepared.prompt_attachments:
            return text
        from agent.observation_rendering import render_attachments

        attachment_text = render_attachments(prepared.prompt_attachments)
        return f"{text}\n\n{attachment_text}" if attachment_text else text

    # ── Stage 6: finish ──────────────────────────────────────────────────

    def finish(self, request: ChatRequest, result: RunResult) -> None:
        """Post-execution hook — logging and observability only.

        Event emission (plan_ready / status:completed) is handled by
        the EventLog → _translate_event pipeline in event_bus.py.
        This method MUST NOT push WS events to avoid double emission.
        """
        _is_plan = request.agent_name == "plan"
        _has_plan = _is_plan or bool(result.contract)
        _verdict = "plan_ready" if _has_plan else "completed"
        logger.info(
            "ChatPipeline finished — session=%s verdict=%s steps=%d tokens=%d",
            request.session_id[:8], _verdict, result.steps_taken, result.total_tokens,
        )
        # Persist each distinct generated plan revision after generation
        # succeeds. Reject marks the prior revision rejected; it must not
        # pre-create a duplicate "next" revision before the model responds.
        if _has_plan and result.summary and self._ports.plan_revisions is not None:
            try:
                _existing = self._ports.plan_revisions.list_revisions(request.session_id)
                _latest = _existing[-1] if _existing else None
                _latest_content = (
                    _latest.get("content", "")
                    if isinstance(_latest, dict)
                    else getattr(_latest, "content", "")
                )
                _latest_status = (
                    _latest.get("status", "")
                    if isinstance(_latest, dict)
                    else getattr(_latest, "status", "")
                )
                if (
                    _latest_content != result.summary
                    or _latest_status in {"rejected", "aborted"}
                ):
                    _revision = self._ports.plan_revisions.append_revision(
                        request.session_id, result.summary,
                    )
                    _revision_number = int(
                        getattr(_revision, "revision", 0)
                        or (
                            _revision.get("revision", 0)
                            if isinstance(_revision, dict)
                            else 0
                        )
                    )
                else:
                    _revision_number = int(
                        _latest.get("revision", 0)
                        if isinstance(_latest, dict)
                        else getattr(_latest, "revision", 0)
                    )
                if _revision_number:
                    self._ports.session_service.merge_metadata(
                        request.session_id,
                        {"plan_revision": _revision_number},
                    )
            except Exception:
                logger.debug("Plan revision persistence failed", exc_info=True)
        # Plan file is written by ExitPlanModeTool during agent execution.
        # This is a fallback only — catches cases where the agent produced
        # a contract without calling ExitPlanMode (e.g. max_turns reached).
        if _has_plan and result.summary:
            try:
                _plan_dir = Path(self._ports.repo_path) / ".grace" / "plans"
                _plan_dir.mkdir(parents=True, exist_ok=True)
                _plan_file = _plan_dir / f"{request.session_id}.md"
                # Only write if ExitPlanMode hasn't already written it
                if not _plan_file.is_file():
                    _contract = result.contract
                    _plan_content = result.summary
                    if _contract:
                        _plan_content = (
                            f"---\n"
                            f"goal: {_contract.get('goal', '')}\n"
                            f"steps:\n"
                            + "".join(f"  - {s}\n" for s in _contract.get('steps', []))
                            + f"target_files:\n"
                            + "".join(f"  - {f}\n" for f in _contract.get('target_files', []))
                            + f"verification: {_contract.get('verification', '')}\n"
                            + f"---\n\n"
                            + result.summary
                        )
                    _plan_file.write_text(_plan_content, encoding="utf-8")
                    logger.info("Plan file written (fallback): %s", _plan_file)
                else:
                    logger.debug("Plan file already exists (ExitPlanMode wrote it): %s", _plan_file)
            except Exception:
                logger.debug("Plan file write skipped", exc_info=True)
        # Update DB agent_name if LLM produced plan in non-plan session
        if not _is_plan and result.contract:
            try:
                self._ports.session_service.update_agent_name(request.session_id, "plan")
            except Exception:
                pass

        # ── Auto-compaction (CLI ChatSession._maybe_auto_compact_after_round) ──
        _maybe_auto_compact(self._ports, request.session_id, result)

    # ── Convenience: run everything in a background thread ───────────────

    def run_in_background(self, request: ChatRequest) -> None:
        """Run all 6 stages in a daemon thread."""
        def _pipeline() -> None:
            try:
                submitted = self.submit_user_prompt(request)
                resolved_prompt = self.resolve_mentions(
                    request, submitted.text,
                )
                self.apply_model_switch(request)
                session_context_text = self.inject_session_context(request)
                confirm_callback, stream_callback = self.build_callbacks(request)
                prepared = PreparedChatRun(
                    request=request,
                    resolved_prompt=resolved_prompt,
                    session_context_text=session_context_text,
                    confirm_callback=confirm_callback,
                    stream_callback=stream_callback,
                    prompt_attachments=submitted.attachments,
                )
                result = self.execute(prepared)
                self.finish(request, result)
            except Exception as exc:
                logger.exception("ChatPipeline failed for session %s", request.session_id)
                if self._event_bus is not None:
                    # Send run_terminal (not status:failed) — consistent with _finalize_run.
                    # If run_context is available, use its run_id/turn_id so the frontend
                    # can deduplicate by run_id and properly archive the optimistic turn.
                    _rc = request.run_context
                    from server.events import WsRunTerminal
                    self._event_bus.publish_typed(request.session_id, WsRunTerminal(
                        run_id=getattr(_rc, "run_id", "") if _rc else "",
                        turn_id=getattr(_rc, "turn_id", "") if _rc else "",
                        turn_index=getattr(_rc, "turn_index", 0) if _rc else 0,
                        status="failed", error=str(exc),
                        session_id=request.session_id,
                    ))
                    # Also CAS-update the Run record so DB reflects the failure
                    _run_id = getattr(_rc, "run_id", "") if _rc else ""
                    if _run_id:
                        try:
                            self._runtime.update_run(
                                _run_id,
                                status="failed",
                                error=str(exc),
                                expect_status="running",
                            )
                        except Exception:
                            pass
            finally:
                self._runtime.release_session(request.session_id)
                self._runtime.release_backend_for_session(request.session_id)

        thread = threading.Thread(target=_pipeline, daemon=True)
        thread.start()
