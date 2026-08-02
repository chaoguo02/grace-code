"""
Hook executor — runs a single hook, enforces policy timeout and failure.

Supports two handler types:
  - callable:  handler(input) → decision
  - command:   subprocess with stdin JSON, exit-code protocol
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass

from hook_core.policies import HookPolicy


class HookTimeoutError(RuntimeError):
    """Hook exceeded its policy timeout."""


class HookExecutionError(RuntimeError):
    """Hook raised an unexpected exception."""


@dataclass(frozen=True, slots=True)
class HookExecution:
    hook_name: str
    decision: object | None   # typed decision, or None on failure
    duration_ms: float
    error: str = ""
    timed_out: bool = False


def execute_hook(
    hook_name: str,
    handler: object,
    hook_input: object,
    policy: HookPolicy,
) -> HookExecution:
    """Execute one hook.

    If *handler* is a callable, calls it synchronously.
    If *handler* is a string, treats it as a shell command and runs via subprocess.
    """
    if callable(handler):
        return _execute_callable(hook_name, handler, hook_input, policy)
    if isinstance(handler, str):
        return _execute_command(hook_name, handler, hook_input, policy)
    return HookExecution(
        hook_name=hook_name, decision=None, duration_ms=0.0,
        error=f"Unsupported handler type: {type(handler).__name__}",
    )


def _execute_callable(
    hook_name: str,
    handler,
    hook_input: object,
    policy: HookPolicy,
) -> HookExecution:
    started = time.monotonic()

    try:
        result = handler(hook_input)
    except Exception as exc:
        duration_ms = (time.monotonic() - started) * 1000
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=duration_ms, error=str(exc),
        )

    duration_ms = (time.monotonic() - started) * 1000
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


def _execute_command(
    hook_name: str,
    command: str,
    hook_input: object,
    policy: HookPolicy,
) -> HookExecution:
    """Execute a shell command hook with stdin JSON.

    Exit code semantics (CC-aligned):
      0 = success → stdout parsed as JSON decision
      2 = blocking error → stderr is the block reason
      other = non-blocking error
    """
    import dataclasses

    started = time.monotonic()

    # Serialize input to JSON for stdin
    if hasattr(hook_input, '__dataclass_fields__'):
        input_json = json.dumps(dataclasses.asdict(hook_input))
    else:
        input_json = json.dumps(hook_input)

    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=policy.timeout_s,
            input=input_json,
        )
    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic() - started) * 1000
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=duration_ms, timed_out=True,
            error=f"Command '{command}' timed out after {policy.timeout_s}s",
        )
    except Exception as exc:
        duration_ms = (time.monotonic() - started) * 1000
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=duration_ms, error=str(exc),
        )

    duration_ms = (time.monotonic() - started) * 1000

    # Exit code 2 = blocking error
    if proc.returncode == 2:
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=duration_ms,
            error=f"[exit 2] {proc.stderr.strip() or 'blocked by hook'}",
        )

    # Exit code 0 = success, try to parse stdout as JSON
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # Treat non-JSON stdout as raw context (for SessionStart etc.)
            return HookExecution(
                hook_name=hook_name,
                decision={"raw_stdout": proc.stdout.strip()},
                duration_ms=duration_ms,
            )
        # Try to convert parsed dict to a typed decision
        decision = _parse_decision(parsed)
        if decision is not None:
            return HookExecution(
                hook_name=hook_name, decision=decision,
                duration_ms=duration_ms,
            )
        # Return raw parsed dict
        return HookExecution(
            hook_name=hook_name, decision=parsed,
            duration_ms=duration_ms,
        )

    # Non-zero exit code (not 2) = non-blocking error
    return HookExecution(
        hook_name=hook_name, decision=None,
        duration_ms=duration_ms,
        error=f"[exit {proc.returncode}] {proc.stderr.strip() or proc.stdout.strip()}",
    )


def _parse_decision(parsed: dict) -> object | None:
    """Try to convert a parsed JSON dict to a typed decision object."""
    from hook_core.decisions import (
        PermissionDecision, PreToolUseDecision, PostToolUseDecision,
        StopDecision, StopVerdict, UserPromptSubmitDecision,
    )

    # CC-style: hookSpecificOutput.permissionDecision
    hso = parsed.get("hookSpecificOutput", parsed)

    if "permissionDecision" in hso:
        perm_str = hso["permissionDecision"]
        try:
            perm = PermissionDecision(perm_str)
        except ValueError:
            perm = PermissionDecision.ALLOW
        return PreToolUseDecision(
            permission=perm,
            updated_input=hso.get("updatedInput"),
            reason=hso.get("permissionDecisionReason", ""),
        )

    # CC-style: decision: "block" (PostToolUse, Stop)
    if parsed.get("decision") == "block":
        reason = parsed.get("reason", "")
        if "stop_hook" in parsed.get("hook_event_name", "").lower():
            return StopDecision(decision=StopVerdict.BLOCK, reason=reason)
        return PostToolUseDecision(decision="block", reason=reason)

    # additionalContext (SessionStart)
    if "additionalContext" in parsed:
        from hook_core.decisions import SessionStartDecision
        return SessionStartDecision(
            additional_context=parsed["additionalContext"],
        )

    return None

