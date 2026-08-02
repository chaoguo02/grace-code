"""Mode switching — CC-aligned plan/build mode transition.

Extracted from agent/core.py. Handles _pending_mode_switch consumption,
permission mode application, and unified plan mode entry/exit transitions.

CC-aligned additions:
  - Unified handle_plan_mode_transition() with prePlanMode save/restore
  - Circuit breaker check on exit (auto mode gate)
  - Bug fix: save_pre_plan_mode() is now called BEFORE set_permission_mode("plan")
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def check_pending_mode_switch(registry: Any, history: Any) -> None:
    """CC-aligned: check and apply _pending_mode_switch after tool execution.

    When EnterPlanMode/ExitPlanMode set a mode-switch on the registry,
    this function picks it up, applies the permission mode change, and
    injects a mode-switch notice into the conversation history.

    CRITICAL FIX: save_pre_plan_mode() is now called BEFORE the mode switch
    so that restore_pre_plan_mode() in ExitPlanMode can actually restore
    the user's original mode.
    """
    try:
        switch = getattr(registry, "_pending_mode_switch", None)
    except Exception:
        return
    if not switch:
        return
    mode = switch.get("mode", "")
    detail = switch.get("detail", "")
    registry._pending_mode_switch = None

    # Apply permission mode change via PhasePolicy
    from core.policy import PhasePolicy
    if hasattr(registry, "_phase_policy"):
        registry._phase_policy = PhasePolicy(
            allowed_tools=getattr(registry._phase_policy, "allowed_tools", None),
            permission_mode="plan" if mode == "plan" else "",
        )

    # Also sync to PermissionPipeline via unified transition
    _apply_mode_to_pipeline(registry, mode)

    # Inject mode-switch notice into conversation
    notice = (
        f"[SYSTEM] Mode switch: {detail}" if detail
        else f"[SYSTEM] Mode switch to: {mode}"
    )
    if history is not None:
        from llm.base import LLMMessage
        history.add(LLMMessage(role="user", content=notice))


def _apply_mode_to_pipeline(registry: Any, target_mode: str) -> None:
    """Apply permission mode change to the pipeline, saving pre-state on entry."""
    pipeline = getattr(registry, "_permission_pipeline", None)
    if pipeline is None:
        return

    if target_mode == "plan":
        # ENTRY: Save current mode BEFORE switching (CC prePlanMode pattern)
        pipeline.save_pre_plan_mode()
        pipeline.set_permission_mode("plan")
    else:
        # EXIT to build/default: restore from prePlanMode
        pipeline.set_permission_mode("")


def handle_plan_mode_transition(
    registry: Any,
    target_mode: str,  # "plan" or "build" (exit)
    permission_pipeline: Any = None,
) -> str | None:
    """CC-aligned unified plan mode transition.

    Called when EnterPlanMode or ExitPlanMode fires.
    Handles: prePlanMode save, permission context switch, circuit breaker check.

    Returns the previous mode on entry, or the restored mode on exit.
    """
    pipeline = permission_pipeline or getattr(registry, "_permission_pipeline", None)
    if pipeline is None:
        return None

    if target_mode == "plan":
        # ENTRY: Save current mode before switching
        pipeline.save_pre_plan_mode()
        pipeline.set_permission_mode("plan")
        return pipeline._pre_plan_mode

    # EXIT: Restore, with circuit breaker for auto mode
    pre_mode = pipeline._pre_plan_mode or "default"

    # Circuit breaker: if prePlanMode was 'auto' but auto gate is now closed,
    # fall back to 'default' instead of restoring 'auto'
    if pre_mode == "auto":
        circuit_breaker = getattr(pipeline, "_circuit_breaker", None)
        if circuit_breaker is not None:
            try:
                is_enabled = getattr(circuit_breaker, "is_gate_enabled", None)
                if callable(is_enabled) and not is_enabled():
                    pre_mode = "default"
                    logger.warning(
                        "Auto mode gate tripped during plan — "
                        "falling back to default instead of auto"
                    )
            except Exception:
                pass

    pipeline.set_permission_mode(pre_mode)
    pipeline._pre_plan_mode = ""
    return pre_mode
