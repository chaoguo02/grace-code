"""
G13: Hook executor — callable + process, proper timeout, no shell=True.

- Callable handlers measured with monotonic clock.
- Command handlers use ProcessRunner (argv-based, shell=False, byte caps).
- Exit code 0 → parse JSON.  2 → blocking error.  Other → non-blocking.
- No raw dict returned as decision.
- Timeout handled by ProcessRunner (terminate → grace → kill).
"""

from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass

from core.json_values import FrozenJsonObject
from hook_core.policies import HookPolicy
from hook_core.process_runner import (
    HookCommand,
    ProcessRunner,
    ProcessRegistry,
    ProcessResult,
)


class HookTimeoutError(RuntimeError):
    """Hook exceeded its policy timeout."""


class HookExecutionError(RuntimeError):
    """Hook raised an unexpected exception."""


@dataclass(frozen=True, slots=True)
class HookExecution:
    hook_name: str
    decision: object | None
    duration_ms: float
    error: str = ""
    timed_out: bool = False


# ── Public API ──────────────────────────────────────────────────────────────

def execute_hook(
    hook_name: str,
    handler: object,
    hook_input: object,
    policy: HookPolicy,
    *,
    process_runner: ProcessRunner | None = None,
) -> HookExecution:
    """Execute one hook.

    - callable: calls handler(hook_input) synchronously.
    - str: treated as a shell command (deprecated — use HookCommand).
    - HookCommand: run via ProcessRunner (G13).
    """
    if isinstance(handler, HookCommand):
        return _execute_process(
            hook_name, handler, hook_input, policy,
            process_runner=process_runner,
        )
    if callable(handler):
        return _execute_callable(hook_name, handler, hook_input, policy)
    if isinstance(handler, str):
        # Legacy: string command → convert to HookCommand with warning
        return _execute_legacy_command(
            hook_name, handler, hook_input, policy,
            process_runner=process_runner,
        )
    return HookExecution(
        hook_name=hook_name, decision=None, duration_ms=0.0,
        error=f"Unsupported handler type: {type(handler).__name__}",
    )


# ── Callable handler ────────────────────────────────────────────────────────

def _execute_callable(
    hook_name: str,
    handler,
    hook_input: object,
    policy: HookPolicy,
) -> HookExecution:
    started = _time.monotonic()

    try:
        result = handler(hook_input)
    except Exception as exc:
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=(_time.monotonic() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )

    duration_ms = (_time.monotonic() - started) * 1000

    if duration_ms / 1000 > policy.timeout_s:
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=duration_ms, timed_out=True,
            error=f"Hook exceeded {policy.timeout_s}s timeout "
                  f"(took {duration_ms/1000:.1f}s)",
        )

    return HookExecution(
        hook_name=hook_name, decision=result,
        duration_ms=duration_ms,
    )


# ── Process handler (G13) ───────────────────────────────────────────────────

def _execute_process(
    hook_name: str,
    command: HookCommand,
    hook_input: object,
    policy: HookPolicy,
    *,
    process_runner: ProcessRunner | None = None,
) -> HookExecution:
    runner = process_runner or ProcessRunner()
    result = runner.run(hook_name, command, hook_input, timeout_s=policy.timeout_s)

    if result.timed_out:
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=result.duration_ms, timed_out=True,
            error=f"Command timed out after {policy.timeout_s}s",
        )

    # Exit code 2 = blocking error
    if result.returncode == 2:
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=result.duration_ms,
            error=f"[exit 2] {result.stderr.strip() or 'blocked by hook'}",
        )

    # Exit code 0 = parse stdout as JSON decision
    if result.returncode == 0 and result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            return HookExecution(
                hook_name=hook_name, decision=None,
                duration_ms=result.duration_ms,
                error=f"Invalid JSON from hook stdout: {result.stdout[:200]}",
            )
        decision = _parse_decision(parsed)
        if decision is not None:
            return HookExecution(
                hook_name=hook_name, decision=decision,
                duration_ms=result.duration_ms,
            )
        # G13: raw dict is NOT a valid decision
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=result.duration_ms,
            error=f"Hook returned unrecognized JSON: "
                  f"{json.dumps(parsed)[:200]}",
        )

    # Non-zero exit code → non-blocking or blocking per policy
    return HookExecution(
        hook_name=hook_name, decision=None,
        duration_ms=result.duration_ms,
        error=f"[exit {result.returncode}] "
              f"{result.stderr.strip() or result.stdout.strip()}",
    )


def _execute_legacy_command(
    hook_name: str,
    command_str: str,
    hook_input: object,
    policy: HookPolicy,
    *,
    process_runner: ProcessRunner | None = None,
) -> HookExecution:
    """Legacy: string command converted to HookCommand with shell=False."""
    import shlex
    try:
        argv = tuple(shlex.split(command_str))
    except ValueError:
        argv = (command_str,)
    cmd = HookCommand(argv=argv)
    return _execute_process(
        hook_name, cmd, hook_input, policy, process_runner=process_runner,
    )


# ── Decision parser ─────────────────────────────────────────────────────────

def _parse_decision(parsed: dict) -> object | None:
    """Convert parsed JSON dict to a typed decision."""
    from hook_core.decisions import (
        PermissionDecision, PreToolUseDecision, PostToolUseDecision,
        StopDecision, StopVerdict, UserPromptSubmitDecision,
    )

    hso = parsed.get("hookSpecificOutput", parsed)

    if "permissionDecision" in hso:
        perm_str = hso["permissionDecision"]
        try:
            perm = PermissionDecision(perm_str)
        except ValueError:
            perm = PermissionDecision.ALLOW
        return PreToolUseDecision(
            permission=perm,
            updated_input=None,
            reason=hso.get("permissionDecisionReason", ""),
        )

    if parsed.get("decision") == "block":
        reason = parsed.get("reason", "")
        if "stop_hook" in parsed.get("hook_event_name", "").lower():
            return StopDecision(decision=StopVerdict.BLOCK, reason=reason)
        return PostToolUseDecision(decision="block", reason=reason)

    if "additionalContext" in parsed:
        from hook_core.decisions import SessionStartDecision
        return SessionStartDecision(
            additional_context=parsed["additionalContext"],
        )

    return None


# ── G14: Async hook execution ──────────────────────────────────────────────

async def execute_hook_async(
    hook_name: str,
    handler: object,
    hook_input: object,
    policy: HookPolicy,
) -> HookExecution:
    """Execute one hook asynchronously.

    - async callable: await handler(hook_input)
    - sync callable: run in thread (no blocking the event loop)
    - HookCommand: run via ProcessRunner (already subprocess-based)
    """
    import asyncio

    started = _time.monotonic()

    try:
        if asyncio.iscoroutinefunction(handler):
            result = await handler(hook_input)
        elif callable(handler):
            # Sync callable — run in thread to avoid blocking event loop
            result = await asyncio.to_thread(handler, hook_input)
        else:
            # HookCommand or legacy string → use sync execute_hook in thread
            return await asyncio.to_thread(
                execute_hook, hook_name, handler, hook_input, policy,
            )
    except Exception as exc:
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=(_time.monotonic() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )

    duration_ms = (_time.monotonic() - started) * 1000

    if duration_ms / 1000 > policy.timeout_s:
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=duration_ms, timed_out=True,
            error=f"Hook exceeded {policy.timeout_s}s timeout",
        )

    return HookExecution(
        hook_name=hook_name, decision=result,
        duration_ms=duration_ms,
    )
