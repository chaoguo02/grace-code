"""
HookBridge —旧 HookDispatcher 接口兼容层，内部委托给新 hook_core。

所有 8 个调用点无需改动。Internal hooks 走新 typed pipeline，
external (command) hooks 包装为 callable 后也走新 pipeline。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _create_bridge(registry, cwd=None, runtime=None, *, total_timeout=30.0):
    """Create a HookBridge that is API-compatible with old HookDispatcher.

    Usage (drop-in replacement in hook_bootstrap.py):
        OLD: dispatcher = HookDispatcher(registry, cwd=repo_path)
        NEW: dispatcher = _create_bridge(registry, cwd=repo_path)
    """
    from hooks.dispatcher import HookDispatcher as OldHookDispatcher

    bridge = HookBridge(
        old_registry=registry,
        old_dispatcher=OldHookDispatcher(registry, cwd=cwd, runtime=runtime,
                                          total_timeout=total_timeout),
        cwd=cwd,
    )
    return bridge


class HookBridge:
    """Compatibility bridge: old HookDispatcher interface → new hook_core.

    Internal hooks go through hook_core.HookDispatcher (typed path).
    External command hooks go through old HookDispatcher (subprocess path).
    Results are merged into old DispatchResult format.
    """

    def __init__(self, old_registry, old_dispatcher, cwd=None):
        from hook_core.registry import HookRegistry as NewRegistry
        from hook_core.dispatcher import HookDispatcher as NewDispatcher

        self._old_registry = old_registry
        self._old_dispatcher = old_dispatcher
        self._cwd = str(Path(cwd or Path.cwd()).resolve())
        self._new_registry = NewRegistry()
        self._new_dispatcher = NewDispatcher(self._new_registry)
        self._total_timeout = getattr(old_dispatcher, '_total_timeout', 30.0)

    # ── Public API (old HookDispatcher compatible) ──────────────────────

    def dispatch(self, event, context):
        """Dispatch hook event to matching hooks. Returns old DispatchResult."""
        from hooks.events import HookEvent as OldEvent
        from hooks.protocol import DispatchResult as OldResult, HookControl, HookAttachment, HookAttachmentKind

        event_str = event.value if hasattr(event, 'value') else str(event)

        # 1. Find hooks from old registry
        matcher_subject = getattr(context, 'matcher_subject', '')
        tool_input = getattr(context, 'tool_input', {}) or {}
        agent_id = getattr(context, 'agent_id', '') or getattr(context, 'session_id', '')

        internal_hooks = self._old_registry.find_internal(
            event, matcher_subject, tool_input,
        )
        external_hooks = self._old_registry.find_external(
            event, matcher_subject, tool_input, agent_id=agent_id,
        )

        # 2. Map context → new typed input
        hook_input = _context_to_input(event, context)

        # 3. If no hooks match, return empty CONTINUE
        if not internal_hooks and not external_hooks:
            return OldResult()

        # 4. Execute internal hooks through new dispatcher
        new_result = None
        if internal_hooks:
            # Temporarily register internal hooks on the new registry.
            # The old HookContext is captured in each handler's closure.
            self._sync_internal_hooks(event_str, internal_hooks, event, context)
            try:
                new_result = self._new_dispatcher.dispatch(
                    event_str, hook_input,
                    tool_name=matcher_subject,
                )
            finally:
                self._clear_synced_hooks()

        # 5. Execute external hooks through old dispatcher
        old_result = None
        if external_hooks:
            old_result = self._old_dispatcher.dispatch(event, context)

        # 6. Merge results → old DispatchResult
        return _merge_to_old_result(new_result, old_result, event_str)

    def dispatch_stop(self, context):
        from hooks.events import HookEvent
        return self.dispatch(HookEvent.STOP, context)

    def clone_registry(self):
        return self._old_registry.clone()

    def register_internal(self, event, hook):
        self._old_registry.register_internal(event, hook)

    def derive(self, registry):
        """Create a derived bridge with a session-scoped registry."""
        from hooks.dispatcher import HookDispatcher as OldDispatcher
        derived_old = OldDispatcher(
            registry=registry, cwd=self._cwd,
            runtime=getattr(self._old_dispatcher, '_runtime', None),
            total_timeout=self._total_timeout,
        )
        return HookBridge(
            old_registry=registry,
            old_dispatcher=derived_old,
            cwd=self._cwd,
        )

    @property
    def hook_dispatcher(self):
        """For callers that access .hook_dispatcher attribute."""
        return self

    # ── Internal helpers ────────────────────────────────────────────────

    def _sync_internal_hooks(self, event_str, internal_hooks, old_event, old_context):
        """Register old internal hooks on the new registry.

        Each handler captures the old HookContext in its closure, so the
        old callback receives exactly what it expects.
        """
        from hook_core.matcher import HookSelector

        for i, hook in enumerate(internal_hooks):
            name = hook.hook_id or f"internal-{i}"

            # Capture hook + context in closure
            def _make_handler(h, ctx):
                def handler(_new_input):
                    result = h.callback(ctx)
                    return _old_output_to_decision(event_str, result)
                return handler

            # Build selector from old matcher
            sel = _old_matcher_to_selector(hook.matcher)

            self._new_registry.register(
                name, event_str, _make_handler(hook, old_context),
                selector=sel, priority=hook.priority,
            )

    def _clear_synced_hooks(self):
        """Remove all synced hooks from the new registry."""
        self._new_registry = type(self._new_registry)()
        self._new_dispatcher = type(self._new_dispatcher)(self._new_registry)


# ── Context ↔ Input mapping ──────────────────────────────────────────────

def _context_to_input(event, context) -> object:
    """Map old HookContext to new typed input class."""
    from hooks.events import HookEvent
    from hook_core.inputs import (
        PreToolUseInput, PostToolUseInput, PostToolUseFailureInput,
        PermissionRequestInput, PermissionDeniedInput,
        UserPromptSubmitInput, StopInput, StopFailureInput,
        SessionStartInput, SessionEndInput,
        SubagentStartInput, SubagentStopInput,
        PreCompactInput, PostCompactInput, PostToolBatchInput,
        NotificationInput,
    )
    sid = getattr(context, 'session_id', '') or ''
    tool_name = getattr(context, 'tool_name', '') or ''
    tool_input = getattr(context, 'tool_input', None) or {}

    event_val = event.value if hasattr(event, 'value') else str(event)

    _map = {
        "PreToolUse": lambda: PreToolUseInput(
            tool_name=tool_name, tool_input=tool_input,
            session_id=sid,
        ),
        "PostToolUse": lambda: PostToolUseInput(
            tool_name=tool_name, tool_input=tool_input,
            tool_output=json.dumps(getattr(context, 'tool_output', None) or {}),
            session_id=sid,
            success=True,
        ),
        "PostToolUseFailure": lambda: PostToolUseFailureInput(
            tool_name=tool_name, tool_input=tool_input,
            session_id=sid,
        ),
        "PermissionRequest": lambda: PermissionRequestInput(
            tool_name=tool_name, tool_input=tool_input,
            required_permissions=getattr(context, 'required_permissions', frozenset()),
            session_id=sid,
            agent_id=getattr(context, 'agent_id', '') or '',
        ),
        "PermissionDenied": lambda: PermissionDeniedInput(
            tool_name=tool_name, tool_input=tool_input,
            session_id=sid,
        ),
        "UserPromptSubmit": lambda: UserPromptSubmitInput(
            prompt=getattr(context, 'user_input', '') or '',
            session_id=sid,
        ),
        "Stop": lambda: StopInput(
            session_id=sid,
            stop_hook_active=getattr(context, 'stop_hook_active', False),
            last_assistant_message=getattr(context, 'last_assistant_message', '') or '',
            agent_id=getattr(context, 'agent_id', '') or '',
            agent_type=getattr(context, 'agent_type', '') or '',
            messages=getattr(context, 'messages', None),
        ),
        "StopFailure": lambda: StopFailureInput(session_id=sid),
        "SessionStart": lambda: SessionStartInput(
            session_id=sid,
            agent_type=getattr(context, 'agent_type', '') or '',
            source=str(getattr(context, 'session_start_source', 'startup') or 'startup'),
        ),
        "SessionEnd": lambda: SessionEndInput(session_id=sid),
        "SubagentStart": lambda: SubagentStartInput(
            session_id=sid,
            agent_id=getattr(context, 'agent_id', '') or '',
            agent_type=getattr(context, 'agent_type', '') or '',
        ),
        "SubagentStop": lambda: SubagentStopInput(
            session_id=sid,
            agent_id=getattr(context, 'agent_id', '') or '',
            agent_name=getattr(context, 'agent_type', '') or '',
        ),
        "PreCompact": lambda: PreCompactInput(
            session_id=sid,
        ),
        "PostCompact": lambda: PostCompactInput(session_id=sid),
        "PostToolBatch": lambda: PostToolBatchInput(session_id=sid),
        "Notification": lambda: NotificationInput(
            session_id=sid,
            message=getattr(context, 'last_assistant_message', '') or '',
        ),
    }

    factory = _map.get(event_val)
    if factory is not None:
        return factory()

    # Fallback: generic notification
    return NotificationInput(session_id=sid, message=event_val)


# ── Decision mapping ─────────────────────────────────────────────────────

def _old_output_to_decision(event_str: str, result) -> object:
    """Map old hook return value to new typed decision."""
    from hooks.protocol import HookOutput, HookResult, HookDecision as OldDecision
    from hook_core.decisions import (
        PermissionDecision, PreToolUseDecision, PostToolUseDecision,
        StopDecision, StopVerdict, SessionStartDecision,
        UserPromptSubmitDecision,
    )

    # Unwrap HookResult → HookOutput
    output = None
    if isinstance(result, dict):
        output = HookOutput.from_dict(result)
    elif isinstance(result, HookOutput):
        output = result
    elif isinstance(result, object) and hasattr(result, 'parsed') and result.parsed is not None:
        output = result.parsed

    if output is None:
        return None

    # Map to typed decision based on event type
    if event_str == "PreToolUse":
        perm = PermissionDecision.ALLOW
        if output.decision == OldDecision.BLOCK:
            perm = PermissionDecision.DENY
        return PreToolUseDecision(
            permission=perm,
            updated_input=output.updated_input,
            reason=output.reason or '',
        )

    if event_str in ("PostToolUse", "PostToolUseFailure"):
        return PostToolUseDecision(
            additional_context=output.additional_context or '',
            replace_output=output.updated_output if isinstance(output.updated_output, str) else None,
        )

    if event_str in ("Stop", "SubagentStop"):
        verdict = StopVerdict.BLOCK if output.decision == OldDecision.BLOCK else StopVerdict.CONTINUE
        return StopDecision(decision=verdict, reason=output.reason or '')

    if event_str == "SessionStart":
        return SessionStartDecision(
            additional_context=output.additional_context or '',
        )

    if event_str == "UserPromptSubmit":
        return UserPromptSubmitDecision(
            block=(output.decision == OldDecision.BLOCK),
            reason=output.reason or '',
        )

    return None


def _old_matcher_to_selector(old_matcher) -> object:
    """Convert old HookMatcher to new HookSelector."""
    from hook_core.matcher import HookMatcher, HookSelector
    pattern = getattr(old_matcher, 'pattern', '*') or '*'
    # Old matcher uses pipe-separated patterns — already CC-compatible!
    if pattern == '*' or not pattern:
        return HookSelector.all_tools()
    return HookSelector.matching(*[p.strip() for p in pattern.split('|') if p.strip()])


# ── Result merging ───────────────────────────────────────────────────────

def _merge_to_old_result(new_result, old_result, event_str):
    """Merge new and old dispatch results into old DispatchResult format."""
    from hooks.protocol import (
        DispatchResult as OldResult, HookControl, HookAttachment, HookAttachmentKind,
    )

    if new_result is None and old_result is None:
        return OldResult()

    if new_result is None:
        return old_result

    if old_result is None:
        return _new_result_to_old(new_result, event_str)

    # Merge: deny from either wins
    if new_result.blocked or getattr(old_result, 'control', None) == HookControl.BLOCK:
        reason = new_result.block_reason or getattr(old_result, 'reason', '')
        return OldResult(control=HookControl.BLOCK, reason=reason)

    # Merge transforms
    updated_input = dict(new_result.updated_input or {})
    if getattr(old_result, 'updated_input', None):
        updated_input.update(old_result.updated_input)

    updated_output = new_result.replace_output or getattr(old_result, 'updated_output', None)

    additional_context = new_result.additional_context
    old_ctx = getattr(old_result, 'additional_context', '') or ''
    if old_ctx:
        additional_context = (additional_context + '\n' + old_ctx).strip()

    warnings = list(new_result.warnings or [])
    old_warnings = getattr(old_result, 'warnings', None) or []
    warnings.extend(old_warnings)

    return OldResult(
        control=HookControl.CONTINUE,
        additional_context=additional_context,
        updated_input=updated_input or None,
        updated_output=updated_output,
        warnings=warnings,
    )


def _new_result_to_old(new_result, event_str: str):
    """Convert new DispatchResult to old DispatchResult."""
    from hooks.protocol import (
        DispatchResult as OldResult, HookControl,
    )
    from hook_core.decisions import PermissionDecision

    # Determine control
    control = HookControl.CONTINUE
    reason = new_result.block_reason

    if new_result.blocked:
        control = HookControl.BLOCK
    elif new_result.permission == PermissionDecision.ALLOW:
        control = HookControl.APPROVE
    elif new_result.permission == PermissionDecision.ASK:
        control = HookControl.CONTINUE
    elif new_result.permission == PermissionDecision.DEFER:
        control = HookControl.CONTINUE

    updated_output = new_result.replace_output

    return OldResult(
        control=control,
        reason=reason,
        additional_context=new_result.additional_context,
        updated_input=new_result.updated_input,
        updated_output=updated_output,
        warnings=list(new_result.warnings or []),
    )
