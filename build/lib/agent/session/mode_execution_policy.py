"""ModeExecutionPolicy — immutable per-run execution contract.

Every Run creates ONE ModeExecutionPolicy from an explicit product mode.
The policy is NOT derived from AgentDefinition — it comes from the user's
explicit product-mode selection in the Workbench UI.

Policy is bound to RunContext (not Session) so cross-run contamination
is impossible by construction: when a Run ends, its policy dies with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from core.resource_governor import ResourceGovernor


# ── Mode constants ──────────────────────────────────────────────────────────

ProductMode = Literal["plan", "build", "multi-agent"]
DelegationStrategy = Literal["serial", "bounded_parallel"]
VerificationRequirement = Literal[
    "not_required",
    "required_if_workspace_changed",
]

_VALID_PRIMARY: dict[str, str] = {
    "plan": "plan",
    "build": "build",
    "multi-agent": "orchestrator",
}


# ── Policy ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModeExecutionPolicy:
    """Immutable execution contract for one Run.

    Created by ``for_run()`` from the user's explicit product mode.
    Must NOT be derived from AgentDefinition or environment variables
    at the point of use — those are inputs to ``for_run()``, not
    alternatives to it.
    """

    product_mode: ProductMode
    primary_agent: str  # "plan" | "build" | "orchestrator"
    write_allowed: bool
    delegation_strategy: DelegationStrategy
    allowed_worker_types: frozenset[str]
    agent_batch_allowed: bool
    max_in_flight_workers: int
    max_spawn_depth: int = 1
    nested_delegation_allowed: bool = False
    verification_requirement: VerificationRequirement = "not_required"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.product_mode not in _VALID_PRIMARY:
            raise ValueError(
                f"Unknown product_mode: {self.product_mode!r}. "
                f"Must be one of: {sorted(_VALID_PRIMARY)}"
            )
        expected = _VALID_PRIMARY[self.product_mode]
        if self.primary_agent != expected:
            raise ValueError(
                f"product_mode={self.product_mode!r} requires "
                f"primary_agent={expected!r}, got {self.primary_agent!r}"
            )
        if self.max_spawn_depth < 1:
            raise ValueError("max_spawn_depth must be >= 1")
        if self.max_in_flight_workers < 1:
            raise ValueError("max_in_flight_workers must be >= 1")
        if self.schema_version < 1:
            raise ValueError("schema_version must be >= 1")

    @classmethod
    def for_run(
        cls,
        *,
        product_mode: str,
        primary_agent: str,
        governor: "ResourceGovernor | None" = None,
    ) -> "ModeExecutionPolicy":
        """Create the policy for one Run.

        Args:
            product_mode: Explicit user selection — "plan", "build", or "multi-agent".
            primary_agent: Must match product_mode:
                plan → "plan", build → "build", multi-agent → "orchestrator".
            governor: Optional ResourceGovernor for deriving runtime concurrency limits.

        Returns:
            A frozen policy that must be bound to the Run's RunContext.

        Raises:
            ValueError: If the product_mode/primary_agent combination is invalid.
        """
        product_mode = str(product_mode).strip().lower()
        primary_agent = str(primary_agent).strip().lower()

        if product_mode not in _VALID_PRIMARY:
            raise ValueError(
                f"Unknown product_mode: {product_mode!r}. "
                f"Must be one of: {sorted(_VALID_PRIMARY)}"
            )
        expected = _VALID_PRIMARY[product_mode]
        if primary_agent != expected:
            raise ValueError(
                f"product_mode={product_mode!r} requires "
                f"primary_agent={expected!r}, got {primary_agent!r}"
            )

        # ── Derive concurrency from governor when available ──
        gov_max = _governor_worker_limit(governor)

        if product_mode == "plan":
            return cls(
                product_mode="plan",
                primary_agent="plan",
                write_allowed=False,
                delegation_strategy="serial",
                allowed_worker_types=frozenset({"explore", "plan-researcher"}),
                agent_batch_allowed=False,
                max_in_flight_workers=1,
                max_spawn_depth=1,
                nested_delegation_allowed=False,
                verification_requirement="not_required",
            )

        if product_mode == "build":
            return cls(
                product_mode="build",
                primary_agent="build",
                write_allowed=True,
                delegation_strategy="serial",
                allowed_worker_types=frozenset({
                    "explore", "general", "plan-researcher",
                    "debugger", "test-runner", "code-reviewer",
                }),
                agent_batch_allowed=False,
                max_in_flight_workers=1,
                max_spawn_depth=1,
                nested_delegation_allowed=False,
                verification_requirement="required_if_workspace_changed",
            )

        # multi-agent
        return cls(
            product_mode="multi-agent",
            primary_agent="orchestrator",
            write_allowed=True,
            delegation_strategy="bounded_parallel",
            allowed_worker_types=frozenset({
                "explore", "general", "plan-researcher",
                "debugger", "test-runner", "code-reviewer",
                "security-reviewer",
            }),
            agent_batch_allowed=True,
            max_in_flight_workers=max(1, gov_max),
            max_spawn_depth=1,
            nested_delegation_allowed=False,
            verification_requirement="required_if_workspace_changed",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "product_mode": self.product_mode,
            "primary_agent": self.primary_agent,
            "write_allowed": self.write_allowed,
            "delegation_strategy": self.delegation_strategy,
            "allowed_worker_types": sorted(self.allowed_worker_types),
            "agent_batch_allowed": self.agent_batch_allowed,
            "max_in_flight_workers": self.max_in_flight_workers,
            "max_spawn_depth": self.max_spawn_depth,
            "nested_delegation_allowed": self.nested_delegation_allowed,
            "verification_requirement": self.verification_requirement,
            "schema_version": self.schema_version,
        }


# ── Helpers ─────────────────────────────────────────────────────────────────


def _governor_worker_limit(governor: "ResourceGovernor | None") -> int:
    """Read the governor's configured worker limit, falling back to env."""
    if governor is not None and governor.mode != "observe":
        from core.resource_governor import ResourceKind
        snap = governor.snapshot()
        ws = snap.snapshots.get(ResourceKind.WORKER_SLOT)
        if ws is not None and ws.limit > 0:
            return ws.limit

    from agent.session.multi_agent_config import MultiAgentFeatureConfig
    return MultiAgentFeatureConfig.from_environment().max_concurrent
