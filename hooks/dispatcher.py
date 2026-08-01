"""Synchronous policy-hook dispatcher with explicit authority contracts."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from hooks.events import BLOCKABLE_EVENTS, HookContext, HookEvent
from hooks.executor import execute_hook
from hooks.protocol import (
    DispatchResult,
    HookAttachment,
    HookAttachmentKind,
    HookControl,
    HookOutput,
    HookResult,
)
from hooks.registry import (
    ExternalHookConfig,
    HookDataAuthority,
    HookDecisionAuthority,
    HookFailurePolicy,
    HookRegistry,
    HookScheduling,
    InternalHook,
)

logger = logging.getLogger(__name__)


class HookDispatcher:
    """Dispatch hooks deterministically and aggregate every awaited result."""

    def __init__(
        self,
        registry: HookRegistry,
        cwd: str | None = None,
        runtime: Any = None,
        *,
        total_timeout: float = 30.0,
    ) -> None:
        self._registry = registry
        self._cwd = str(Path(cwd or Path.cwd()).resolve())
        if runtime is None:
            from core.process import LocalRuntime

            runtime = LocalRuntime(workspace_root=self._cwd)
        self._runtime = runtime
        self._total_timeout = max(0.1, float(total_timeout))

    def dispatch(self, event: HookEvent, context: HookContext) -> DispatchResult:
        return self._dispatch(event, context)

    def dispatch_stop(self, context: HookContext) -> DispatchResult:
        return self.dispatch(HookEvent.STOP, context)

    def clone_registry(self) -> HookRegistry:
        return self._registry.clone()

    def register_internal(self, event: HookEvent, hook: InternalHook) -> None:
        """Register a lifecycle hook through the dispatcher boundary."""
        self._registry.register_internal(event, hook)

    def derive(self, registry: HookRegistry) -> "HookDispatcher":
        return HookDispatcher(
            registry=registry,
            cwd=self._cwd,
            runtime=self._runtime,
            total_timeout=self._total_timeout,
        )

    def _dispatch(
        self,
        event: HookEvent,
        context: HookContext,
    ) -> DispatchResult:
        if context.event is not event:
            raise ValueError("Hook context event does not match dispatch event")

        is_blockable = event in BLOCKABLE_EVENTS
        agent_id = context.agent_id or context.session_id
        internal = self._registry.find_internal(
            event, context.matcher_subject, context.tool_input,
        )
        external = self._registry.find_external(
            event,
            context.matcher_subject,
            context.tool_input,
            agent_id=agent_id,
        )
        hooks: list[tuple[int, int, str, InternalHook | ExternalHookConfig]] = []
        sequence = 0
        for hook in internal:
            hooks.append((hook.priority, sequence, "internal", hook))
            sequence += 1
        for hook in external:
            hooks.append((hook.priority, sequence, "external", hook))
            sequence += 1
        hooks.sort(key=lambda item: (item[0], item[1]))

        warnings: list[str] = []
        contexts: list[str] = []
        attachments: list[HookAttachment] = []
        updated_input: dict[str, Any] | None = None
        updated_output: dict[str, Any] | str | None = None
        approved = False
        deadline = time.monotonic() + self._total_timeout

        for _, _, kind, hook in hooks:
            source = self._source(kind, hook)
            if hook.scheduling is HookScheduling.DETACHED:
                self._launch_detached(kind, hook, context, source)
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = (
                    f"Hook dispatch deadline exceeded before {source} "
                    f"for {event.value}"
                )
                blocked = self._handle_failure(
                    hook, failure, is_blockable, warnings,
                )
                if blocked is not None:
                    return blocked
                continue

            try:
                result = self._execute_awaited(
                    kind, hook, context, remaining,
                )
            except Exception as exc:
                failure = f"Hook {source} failed for {event.value}: {exc}"
                blocked = self._handle_failure(
                    hook, failure, is_blockable, warnings,
                )
                if blocked is not None:
                    return blocked
                continue

            if result.control is HookControl.NON_BLOCKING_ERROR:
                failure = (
                    f"Hook {source} failed for {event.value}: "
                    f"{result.stderr or 'exit ' + str(result.exit_code)}"
                )
                blocked = self._handle_failure(
                    hook, failure, is_blockable, warnings,
                )
                if blocked is not None:
                    return blocked
                continue

            if (
                result.control is HookControl.BLOCK
                and hook.decision_authority is HookDecisionAuthority.POLICY
                and is_blockable
            ):
                reason = (
                    result.parsed.reason
                    if result.parsed and result.parsed.reason
                    else result.stderr or result.stdout or f"Blocked by {source}"
                )
                return DispatchResult(
                    control=HookControl.BLOCK,
                    reason=reason,
                    warnings=warnings or None,
                )
            if (
                result.control is HookControl.APPROVE
                and hook.decision_authority is HookDecisionAuthority.POLICY
            ):
                approved = True

            if hook.data_authority is HookDataAuthority.TRANSFORM:
                parsed = result.parsed
                if parsed and parsed.updated_input is not None:
                    updated_input = {
                        **(updated_input or {}),
                        **parsed.updated_input,
                    }
                if parsed and parsed.updated_output is not None:
                    updated_output = parsed.updated_output
                if result.context:
                    contexts.append(result.context)
                    attachments.append(HookAttachment(
                        kind=HookAttachmentKind.CONTEXT,
                        text=result.context,
                        source=source,
                    ))

        return DispatchResult(
            control=HookControl.APPROVE if approved else HookControl.CONTINUE,
            additional_context="\n".join(contexts),
            attachments=tuple(attachments),
            updated_input=updated_input,
            updated_output=updated_output,
            warnings=warnings or None,
        )

    def _execute_awaited(
        self,
        kind: str,
        hook: InternalHook | ExternalHookConfig,
        context: HookContext,
        remaining: float,
    ) -> HookResult:
        if kind == "internal":
            assert isinstance(hook, InternalHook)
            return self._normalize_internal(hook.callback(context))
        assert isinstance(hook, ExternalHookConfig)
        return execute_hook(
            command=hook.command,
            context=context,
            timeout=max(0.1, min(float(hook.timeout), remaining)),
            cwd=self._cwd,
            runtime=self._runtime,
        )

    @staticmethod
    def _normalize_internal(value: Any) -> HookResult:
        if value is None:
            return HookResult(exit_code=0)
        if isinstance(value, HookResult):
            return value
        if isinstance(value, HookOutput):
            return HookResult(exit_code=0, parsed=value)
        if isinstance(value, dict):
            return HookResult(exit_code=0, parsed=HookOutput.from_dict(value))
        raise TypeError(
            "Internal hook must return None, dict, HookOutput, or HookResult"
        )

    def _launch_detached(
        self,
        kind: str,
        hook: InternalHook | ExternalHookConfig,
        context: HookContext,
        source: str,
    ) -> None:
        def run() -> None:
            try:
                self._execute_awaited(
                    kind, hook, context, self._total_timeout,
                )
            except Exception:
                logger.warning("Detached hook %s failed", source, exc_info=True)

        threading.Thread(
            target=run,
            name=f"grace-hook-{source}"[:80],
            daemon=True,
        ).start()

    @staticmethod
    def _source(
        kind: str,
        hook: InternalHook | ExternalHookConfig,
    ) -> str:
        if hook.hook_id:
            return hook.hook_id
        if kind == "external":
            assert isinstance(hook, ExternalHookConfig)
            return hook.command
        assert isinstance(hook, InternalHook)
        return getattr(hook.callback, "__name__", "internal-hook")

    @staticmethod
    def _handle_failure(
        hook: InternalHook | ExternalHookConfig,
        message: str,
        is_blockable: bool,
        warnings: list[str],
    ) -> DispatchResult | None:
        fail_closed = (
            hook.failure_policy is HookFailurePolicy.FAIL_CLOSED
            or (
                hook.failure_policy is HookFailurePolicy.EVENT_DEFAULT
                and is_blockable
            )
        )
        logger.warning(message)
        if fail_closed:
            return DispatchResult(
                control=HookControl.BLOCK,
                reason=message,
                warnings=[*warnings, message],
            )
        if hook.failure_policy is not HookFailurePolicy.FAIL_OPEN:
            warnings.append(message)
        return None
