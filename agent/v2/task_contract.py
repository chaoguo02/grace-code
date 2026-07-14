"""Task Contract — immutable execution boundary defined by the entry layer.

The contract defines resource limits (max_steps, budget_tokens) for a task
execution. It does NOT define tool permissions — those are enforced at
execution time by PermissionPipeline and by path safety checks in file tools.

Security is NOT tool-list-based. It's enforcement-point-based:
  - PermissionPipeline: Allow / Deny / Ask at tool call time
  - is_path_safe(): hard check inside FileWrite/FileEdit tools
  - Bash sandbox: OS-level isolation for shell commands

Usage:
    contract = TaskContract.for_plan(agent_cfg)    # reduced budget
    contract = TaskContract.for_build(agent_cfg)   # full budget
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.core import AgentConfig
    from agent.v2.models import AgentDefinition


@dataclass(frozen=True)
class TaskContract:
    """Immutable execution contract. Created by the entry layer,
    enforced by the Runtime (budget, steps) and PermissionPipeline (auth)."""

    max_steps: int
    budget_tokens: int
    require_deliverables: dict[str, int] = field(default_factory=dict)

    # ── Factory presets ──────────────────────────────────────────────────

    @classmethod
    def for_plan(cls, cfg: "AgentConfig") -> "TaskContract":
        """Plan agent: fewer exploration steps, with the full token ceiling.

        Provider usage is cumulative across turns, so scaling both steps and
        tokens by the same ratio can exhaust a multi-turn plan before its final
        contract is rendered.
        """
        ratio = getattr(cfg, "plan_budget_ratio", 0.33)
        return cls(
            max_steps=max(5, int(cfg.max_steps * ratio)),
            budget_tokens=cfg.budget_tokens,
        )

    @classmethod
    def for_build(cls, cfg: "AgentConfig") -> "TaskContract":
        """Build agent: full budget."""
        return cls(
            max_steps=cfg.max_steps,
            budget_tokens=cfg.budget_tokens,
        )

    @classmethod
    def for_subagent(
        cls,
        definition: "AgentDefinition",
        cfg: "AgentConfig",
        *,
        parent_budget_tokens: int,
        parent_max_steps: int,
    ) -> "TaskContract":
        """Narrow parent resources using declarative child limits."""
        token_limits = [cfg.budget_tokens, parent_budget_tokens]
        if definition.max_tokens is not None:
            token_limits.append(definition.max_tokens)
        return cls(
            max_steps=min(cfg.max_steps, parent_max_steps, definition.max_turns),
            budget_tokens=min(token_limits),
            require_deliverables=dict(definition.completion_requires),
        )
