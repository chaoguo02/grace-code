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


@dataclass(frozen=True)
class TaskContract:
    """Immutable execution contract. Created by the entry layer,
    enforced by the Runtime (budget, steps) and PermissionPipeline (auth)."""

    max_steps: int
    budget_tokens: int
    intent: str = "edit"
    require_deliverables: dict[str, int] = field(default_factory=dict)
    ttl_seconds: int | None = None   # TaskLedger cache TTL

    # ── Factory presets ──────────────────────────────────────────────────

    @classmethod
    def for_plan(cls, cfg: "AgentConfig") -> "TaskContract":
        """Plan agent: reduced budget for exploration phase."""
        ratio = getattr(cfg, "plan_budget_ratio", 0.33)
        return cls(
            max_steps=max(5, int(cfg.max_steps * ratio)),
            budget_tokens=max(5000, int(cfg.budget_tokens * ratio)),
            intent="analysis",
        )

    @classmethod
    def for_build(cls, cfg: "AgentConfig") -> "TaskContract":
        """Build agent: full budget."""
        return cls(
            max_steps=cfg.max_steps,
            budget_tokens=cfg.budget_tokens,
            intent="edit",
        )

    @classmethod
    def for_explore(cls, cfg: "AgentConfig") -> "TaskContract":
        """Explore subagent: limited budget."""
        return cls(
            max_steps=min(cfg.max_steps, 50),
            budget_tokens=min(cfg.budget_tokens, 40000),
            intent="analysis",
        )

    @classmethod
    def for_code_review(cls, cfg: "AgentConfig") -> "TaskContract":
        """Code reviewer: limited budget, structured output required."""
        return cls(
            max_steps=min(cfg.max_steps, 40),
            budget_tokens=min(cfg.budget_tokens, 30000),
            intent="analysis",
            require_deliverables={"submit_findings": 1},
        )
