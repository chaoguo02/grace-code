"""Typed, runtime-independent description of delegatable work.

The main model performs semantic decomposition.  These types give the runtime
facts it can validate without parsing prose: dependencies, file ownership,
coordination needs, risk, and budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePath

from agent.task import TaskIntent


class TaskPurpose(str, Enum):
    EXPLORATION = "exploration"
    PLANNING = "planning"
    DEBUGGING = "debugging"
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    VERIFICATION = "verification"
    SECURITY = "security"
    GENERAL = "general"


class ContextVolume(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class EvidenceLevel(str, Enum):
    NONE = "none"
    SUMMARY = "summary"
    FILE_LINE = "file_line"
    VERIFIED = "verified"


class CoordinationNeed(str, Enum):
    NONE = "none"
    PARENT_MEDIATED = "parent_mediated"
    PEER_TO_PEER = "peer_to_peer"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AgentTopology(str, Enum):
    SINGLE = "single"
    ONE_TO_ONE = "one_to_one"
    FAN_OUT_FAN_IN = "fan_out_fan_in"
    CHAIN = "chain"
    NESTED = "nested"
    TEAM = "team"


def _non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _strings(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    normalized = tuple(_non_empty(value, name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} must not contain duplicates")
    return normalized


def _paths(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    normalized = _strings(values, name)
    for value in normalized:
        path = PurePath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"{name} must contain repository-relative paths")
    return normalized


@dataclass(frozen=True)
class WorkItem:
    id: str
    goal: str
    domain: str
    candidate_agent: str = ""
    depends_on: tuple[str, ...] = ()
    expected_files: tuple[str, ...] = ()
    write_files: tuple[str, ...] = ()
    deliverable: str = "concise structured report"
    required: bool = True
    estimated_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("id", "goal", "domain", "deliverable"):
            object.__setattr__(self, name, _non_empty(getattr(self, name), name))
        if self.candidate_agent:
            object.__setattr__(
                self, "candidate_agent", self.candidate_agent.strip()
            )
        object.__setattr__(
            self, "depends_on", _strings(self.depends_on, "depends_on")
        )
        object.__setattr__(
            self, "expected_files", _paths(self.expected_files, "expected_files")
        )
        object.__setattr__(
            self, "write_files", _paths(self.write_files, "write_files")
        )
        if self.id in self.depends_on:
            raise ValueError("a work item cannot depend on itself")
        if self.estimated_tokens < 0:
            raise ValueError("estimated_tokens cannot be negative")

    @property
    def is_write(self) -> bool:
        return bool(self.write_files)


@dataclass(frozen=True)
class DependencyEdge:
    predecessor: str
    successor: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "predecessor", _non_empty(self.predecessor, "predecessor")
        )
        object.__setattr__(
            self, "successor", _non_empty(self.successor, "successor")
        )
        if self.predecessor == self.successor:
            raise ValueError("dependency edge cannot point to itself")


@dataclass(frozen=True)
class DelegationBudget:
    available_tokens: int
    worker_tokens: int = 0
    parent_reserve_tokens: int = 0
    recovery_reserve_tokens: int = 0
    max_workers: int = 0

    def __post_init__(self) -> None:
        values = (
            self.available_tokens,
            self.worker_tokens,
            self.parent_reserve_tokens,
            self.recovery_reserve_tokens,
            self.max_workers,
        )
        if any(not isinstance(value, int) for value in values):
            raise TypeError("delegation budget values must be integers")
        if any(value < 0 for value in values):
            raise ValueError("delegation budget values cannot be negative")
        allocated = (
            self.worker_tokens
            + self.parent_reserve_tokens
            + self.recovery_reserve_tokens
        )
        if allocated > self.available_tokens:
            raise ValueError("delegation budget exceeds available tokens")

    @property
    def remaining_tokens(self) -> int:
        return self.available_tokens - (
            self.worker_tokens
            + self.parent_reserve_tokens
            + self.recovery_reserve_tokens
        )


@dataclass(frozen=True)
class TaskShape:
    intent: TaskIntent
    purpose: TaskPurpose
    domains: tuple[str, ...]
    work_items: tuple[WorkItem, ...]
    dependency_edges: tuple[DependencyEdge, ...] = ()
    expected_files: tuple[str, ...] = ()
    write_files: tuple[str, ...] = ()
    context_volume: ContextVolume = ContextVolume.SMALL
    evidence_requirement: EvidenceLevel = EvidenceLevel.SUMMARY
    coordination_need: CoordinationNeed = CoordinationNeed.NONE
    risk: RiskLevel = RiskLevel.LOW
    user_requested_topology: AgentTopology | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent", TaskIntent(self.intent))
        object.__setattr__(self, "purpose", TaskPurpose(self.purpose))
        object.__setattr__(
            self, "context_volume", ContextVolume(self.context_volume)
        )
        object.__setattr__(
            self, "evidence_requirement", EvidenceLevel(self.evidence_requirement)
        )
        object.__setattr__(
            self, "coordination_need", CoordinationNeed(self.coordination_need)
        )
        object.__setattr__(self, "risk", RiskLevel(self.risk))
        if self.user_requested_topology is not None:
            object.__setattr__(
                self,
                "user_requested_topology",
                AgentTopology(self.user_requested_topology),
            )
        object.__setattr__(self, "domains", _strings(self.domains, "domains"))
        object.__setattr__(
            self, "expected_files", _paths(self.expected_files, "expected_files")
        )
        object.__setattr__(
            self, "write_files", _paths(self.write_files, "write_files")
        )
        if not isinstance(self.work_items, tuple):
            raise TypeError("work_items must be a tuple")
        if not isinstance(self.dependency_edges, tuple):
            raise TypeError("dependency_edges must be a tuple")
        ids = tuple(item.id for item in self.work_items)
        if len(ids) != len(set(ids)):
            raise ValueError("work item ids must be unique")
        known = set(ids)
        edges = set(self.dependency_edges)
        for item in self.work_items:
            unknown = set(item.depends_on) - known
            if unknown:
                raise ValueError(f"work item {item.id!r} has unknown dependencies")
            edges.update(DependencyEdge(parent, item.id) for parent in item.depends_on)
        if any(edge.predecessor not in known or edge.successor not in known for edge in edges):
            raise ValueError("dependency edge references an unknown work item")
        self._assert_acyclic(ids, edges)
        object.__setattr__(
            self,
            "dependency_edges",
            tuple(sorted(edges, key=lambda edge: (edge.predecessor, edge.successor))),
        )

    @staticmethod
    def _assert_acyclic(
        ids: tuple[str, ...], edges: set[DependencyEdge]
    ) -> None:
        successors = {item_id: [] for item_id in ids}
        indegree = {item_id: 0 for item_id in ids}
        for edge in edges:
            successors[edge.predecessor].append(edge.successor)
            indegree[edge.successor] += 1
        ready = [item_id for item_id, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            current = ready.pop()
            visited += 1
            for successor in successors[current]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)
        if visited != len(ids):
            raise ValueError("work item dependency graph must be acyclic")

    @property
    def has_dependencies(self) -> bool:
        return bool(self.dependency_edges)

    @property
    def has_writes(self) -> bool:
        return bool(self.write_files) or any(item.is_write for item in self.work_items)

    @property
    def has_write_conflicts(self) -> bool:
        seen: set[str] = set()
        for item in self.work_items:
            if item.is_write and not item.write_files:
                return True
            current = set(item.write_files)
            if seen.intersection(current):
                return True
            seen.update(current)
        return False

