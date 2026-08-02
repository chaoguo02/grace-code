"""
CC-Native TokenPlanner — stateless budget allocation.

Design (P0_1 Batch 1):
  Single .plan() call per context assembly.  No multi-call pattern.
  Stateless — create a new instance per request or reuse.
  Decoupled from any Counter implementation — only receives integers.

Allocation (CC-aligned):
  system      ~12%  — system prompt
  repo_map    ~12%  — repository structure (capped at 12K)
  observation ~10%  — recent tool results
  history     remainder — conversation (decays with consumed tokens)
  output_room 4_096 — reserved for model response
"""

from __future__ import annotations

from dataclasses import dataclass

# ── BudgetPlan ──────────────────────────────────────────────────────────────

@dataclass
class BudgetPlan:
    """Per-request token budget allocation.

    Invariant: system + repo_map + history + observation + output_room == total
    """
    total: int
    system: int
    repo_map: int
    history: int
    observation: int
    output_room: int
    consumed_so_far: int = 0


# ── TokenPlanner ────────────────────────────────────────────────────────────

@dataclass
class TokenPlanner:
    """Stateless budget planner — CC-aligned allocation strategy.

    All tunables are constructor-injected so the planner can be tested
    in isolation with no environment or config-file dependency.

    Usage:
        planner = TokenPlanner()
        plan = planner.plan(model_window=200_000, consumed_tokens=50_000)
    """

    # Default fractions (CC-aligned)
    system_fraction: float = 0.12
    repo_map_fraction: float = 0.12
    observation_fraction: float = 0.10
    output_room_default: int = 4096

    # Hard caps
    repo_map_max: int = 12_000

    # Decay: when consumed_tokens reaches model_window * decay_trigger_ratio,
    # history budget shrinks to decay_floor * remaining.
    decay_trigger_ratio: float = 1.0 / 3.0   # consumed >= 1/3 window → decay
    decay_floor: float = 0.30                  # history never drops below 30%

    # ── public API ──────────────────────────────────────────────────────

    def plan(
        self,
        model_window: int,
        consumed_tokens: int = 0,
        *,
        output_room: int | None = None,
        system_fraction: float | None = None,
    ) -> BudgetPlan:
        """Allocate token budget for one request.

        Args:
            model_window: The model's max context window (e.g. 200_000).
            consumed_tokens: Tokens already consumed this session (drives decay).
            output_room: Override for reserved output tokens.
            system_fraction: Override for system prompt fraction.

        Returns:
            BudgetPlan with per-section allocations.

        Invariant:
            plan.system + plan.repo_map + plan.history
            + plan.observation + plan.output_room == plan.total
        """
        room = output_room if output_room is not None else self.output_room_default
        total = model_window - room

        sf = system_fraction if system_fraction is not None else self.system_fraction

        system = int(total * sf)
        repo_map = min(int(total * self.repo_map_fraction), self.repo_map_max)
        observation = int(total * self.observation_fraction)

        # History gets the remainder — then apply decay
        history = total - system - repo_map - observation

        # Consumption-based decay
        if consumed_tokens > 0 and model_window > 0:
            trigger = int(model_window * self.decay_trigger_ratio)
            if consumed_tokens >= trigger:
                remaining = total - consumed_tokens
                floor = int(total * self.decay_floor)
                history = max(floor, min(history, max(1, remaining)))

        # safety: ensure all values non-negative
        history = max(0, history)

        return BudgetPlan(
            total=total,
            system=system,
            repo_map=repo_map,
            history=history,
            observation=observation,
            output_room=room,
            consumed_so_far=consumed_tokens,
        )
