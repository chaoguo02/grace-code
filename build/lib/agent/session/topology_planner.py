"""Deterministic topology selection and safety downgrade rules."""

from __future__ import annotations

from dataclasses import dataclass

from agent.session.task_shape import (
    AgentTopology,
    CoordinationNeed,
    DelegationBudget,
    TaskShape,
    WorkItem,
)


@dataclass(frozen=True)
class TopologyPolicy:
    max_fanout: int = 3
    max_concurrent_subagents: int = 4
    max_spawn_per_session: int = 64
    max_subagent_spawn_depth: int = 1
    current_depth: int = 0
    spawned_count: int = 0
    active_count: int = 0
    available_tokens: int = 100_000
    minimum_worker_tokens: int = 2_000
    parent_reserve_ratio: float = 0.25
    recovery_reserve_ratio: float = 0.10
    nested_enabled: bool = False
    worktree_writes: bool = False

    def __post_init__(self) -> None:
        integer_fields = (
            "max_fanout",
            "max_concurrent_subagents",
            "max_spawn_per_session",
            "max_subagent_spawn_depth",
            "current_depth",
            "spawned_count",
            "active_count",
            "available_tokens",
            "minimum_worker_tokens",
        )
        if any(getattr(self, name) < 0 for name in integer_fields):
            raise ValueError("topology policy limits cannot be negative")
        for ratio in (self.parent_reserve_ratio, self.recovery_reserve_ratio):
            if not 0 <= ratio < 1:
                raise ValueError("budget reserve ratios must be between zero and one")
        if self.parent_reserve_ratio + self.recovery_reserve_ratio >= 1:
            raise ValueError("budget reserves must leave room for workers")


@dataclass(frozen=True)
class RoutingDecision:
    topology: AgentTopology
    work_items: tuple[WorkItem, ...]
    reason_code: str
    explanation: str
    estimated_budget: DelegationBudget
    downgraded_from: AgentTopology | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "topology", AgentTopology(self.topology))
        if self.downgraded_from is not None:
            object.__setattr__(
                self, "downgraded_from", AgentTopology(self.downgraded_from)
            )
        if not self.reason_code.strip() or not self.explanation.strip():
            raise ValueError("routing decision requires a reason and explanation")


class TopologyPlanner:
    """Validate a model-proposed shape and choose the cheapest safe topology."""

    def plan(
        self, shape: TaskShape, policy: TopologyPolicy | None = None
    ) -> RoutingDecision:
        policy = policy or TopologyPolicy()
        requested = shape.user_requested_topology
        candidate = requested or self._infer(shape, policy)
        selected, reason, explanation = self._validate(candidate, shape, policy)
        items = self._selected_items(shape.work_items, selected, policy)
        if (
            selected is AgentTopology.FAN_OUT_FAN_IN
            and len(items) < len(shape.work_items)
        ):
            reason = "fanout_reduced_by_limits"
            explanation = (
                f"Fan-out was reduced from {len(shape.work_items)} to "
                f"{len(items)} workers by concurrency, spawn, or budget limits."
            )
        budget = self._budget(items, selected, policy)
        downgraded = candidate if selected is not candidate else None
        return RoutingDecision(
            topology=selected,
            work_items=items,
            reason_code=reason,
            explanation=explanation,
            estimated_budget=budget,
            downgraded_from=downgraded,
        )

    @staticmethod
    def _infer(shape: TaskShape, policy: TopologyPolicy) -> AgentTopology:
        count = len(shape.work_items)
        if count == 0:
            return AgentTopology.SINGLE
        if shape.coordination_need is CoordinationNeed.PEER_TO_PEER:
            # Peer messaging is intentionally unsupported. The primary owns
            # coordination and aggregates isolated worker results.
            return AgentTopology.FAN_OUT_FAN_IN
        if shape.has_dependencies:
            return AgentTopology.CHAIN
        if count == 1:
            return AgentTopology.ONE_TO_ONE
        if policy.nested_enabled and shape.context_volume.value == "large" and count > policy.max_fanout:
            return AgentTopology.NESTED
        return AgentTopology.FAN_OUT_FAN_IN

    def _validate(
        self,
        candidate: AgentTopology,
        shape: TaskShape,
        policy: TopologyPolicy,
    ) -> tuple[AgentTopology, str, str]:
        count = len(shape.work_items)
        if candidate is AgentTopology.SINGLE:
            return candidate, "single_is_sufficient", "The primary can complete the task directly."
        if count == 0:
            return AgentTopology.SINGLE, "no_work_items", "No bounded worker task was supplied."
        if policy.current_depth >= policy.max_subagent_spawn_depth:
            return AgentTopology.SINGLE, "spawn_depth_exhausted", "The configured spawn depth is exhausted."
        if policy.spawned_count >= policy.max_spawn_per_session:
            return AgentTopology.SINGLE, "spawn_count_exhausted", "The session spawn allowance is exhausted."
        if policy.active_count >= policy.max_concurrent_subagents:
            return AgentTopology.SINGLE, "concurrency_exhausted", "No subagent concurrency slot is available."
        affordable = self._affordable_workers(policy)
        if affordable == 0:
            return AgentTopology.SINGLE, "delegation_budget_insufficient", "Reserved parent and recovery budget leaves no worker budget."
        if candidate is AgentTopology.NESTED:
            if not policy.nested_enabled or policy.max_subagent_spawn_depth - policy.current_depth < 2:
                fallback = AgentTopology.CHAIN if shape.has_dependencies else AgentTopology.FAN_OUT_FAN_IN
                selected, _, explanation = self._validate(fallback, shape, policy)
                return selected, "nested_delegation_disabled", f"Nested delegation was downgraded: {explanation}"
            return candidate, "nested_coordination_benefit", "A coordinator can isolate and aggregate several leaf contexts."
        if shape.has_dependencies and candidate is AgentTopology.FAN_OUT_FAN_IN:
            return AgentTopology.CHAIN, "dependencies_require_chain", "Work-item dependencies prohibit concurrent fan-out."
        if candidate is AgentTopology.FAN_OUT_FAN_IN:
            if count < 2:
                return AgentTopology.ONE_TO_ONE, "single_worker_item", "Only one bounded worker task exists."
            if shape.has_write_conflicts or (shape.has_writes and not policy.worktree_writes):
                return AgentTopology.CHAIN, "write_conflict_requires_serial", "Write ownership is overlapping, unknown, or not isolated in worktrees."
            if affordable < 2:
                return AgentTopology.ONE_TO_ONE, "budget_reduced_worker_count", "The budget supports only one worker."
            return candidate, "independent_work_items", "Independent work items can run concurrently and return to one primary."
        if candidate is AgentTopology.CHAIN:
            return candidate, "ordered_or_conflicting_work", "Dependencies or write ownership require parent-mediated serial execution."
        return AgentTopology.ONE_TO_ONE, "bounded_specialist_task", "One bounded task benefits from a specialist context."

    @staticmethod
    def _capacity(policy: TopologyPolicy) -> int:
        return max(
            0,
            min(
                policy.max_fanout,
                policy.max_concurrent_subagents - policy.active_count,
                policy.max_spawn_per_session - policy.spawned_count,
            ),
        )

    @staticmethod
    def _worker_pool(policy: TopologyPolicy) -> int:
        reserved = int(
            policy.available_tokens
            * (policy.parent_reserve_ratio + policy.recovery_reserve_ratio)
        )
        return max(0, policy.available_tokens - reserved)

    def _affordable_workers(self, policy: TopologyPolicy) -> int:
        if policy.minimum_worker_tokens == 0:
            return self._capacity(policy)
        return min(
            self._capacity(policy),
            self._worker_pool(policy) // policy.minimum_worker_tokens,
        )

    def _selected_items(
        self,
        items: tuple[WorkItem, ...],
        topology: AgentTopology,
        policy: TopologyPolicy,
    ) -> tuple[WorkItem, ...]:
        if topology is AgentTopology.SINGLE:
            return ()
        if topology is AgentTopology.ONE_TO_ONE:
            return items[:1]
        if topology is AgentTopology.FAN_OUT_FAN_IN:
            return items[: self._affordable_workers(policy)]
        return items

    def _budget(
        self,
        items: tuple[WorkItem, ...],
        topology: AgentTopology,
        policy: TopologyPolicy,
    ) -> DelegationBudget:
        if topology is AgentTopology.SINGLE:
            return DelegationBudget(available_tokens=policy.available_tokens)
        parent = int(policy.available_tokens * policy.parent_reserve_ratio)
        recovery = int(policy.available_tokens * policy.recovery_reserve_ratio)
        pool = max(0, policy.available_tokens - parent - recovery)
        requested = sum(item.estimated_tokens for item in items)
        workers = min(pool, requested if requested else len(items) * policy.minimum_worker_tokens)
        return DelegationBudget(
            available_tokens=policy.available_tokens,
            worker_tokens=workers,
            parent_reserve_tokens=parent,
            recovery_reserve_tokens=recovery,
            max_workers=len(items),
        )
