"""
CC-Native Workflow DAG Engine (P1-3).

Design: layer-by-layer parallel execution with cycle detection,
variable binding, and error handling (fail/skip/retry).

Decoupled from: LLM Backend, MCP Transport, Session Store, HITL.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── WorkflowNode ────────────────────────────────────────────────────────────

@dataclass
class WorkflowNode:
    id: str
    type: str                      # "skill" | "tool" | "agent" | "condition"
    config: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    on_error: str = "fail"         # "fail" | "skip" | "retry"
    max_retries: int = 1
    timeout_s: float | None = None


# ── WorkflowDefinition ──────────────────────────────────────────────────────

@dataclass
class WorkflowDefinition:
    name: str
    version: str = "1.0"
    nodes: list[WorkflowNode] = field(default_factory=list)


# ── WorkflowValidator ───────────────────────────────────────────────────────

class WorkflowValidator:
    """Static DAG validation: cycle detection, reference check, schema."""

    @staticmethod
    def validate(workflow: WorkflowDefinition) -> list[str]:
        errors: list[str] = []
        node_ids = {n.id for n in workflow.nodes}

        # Reference check: depends_on must reference existing nodes
        for node in workflow.nodes:
            for dep in node.depends_on:
                if dep not in node_ids:
                    errors.append(
                        f"Node '{node.id}' depends on '{dep}' which does not exist"
                    )

        # Cycle detection via topological sort
        if not errors:
            try:
                WorkflowValidator.topological_sort(workflow)
            except ValueError as exc:
                errors.append(str(exc))

        return errors

    @staticmethod
    def topological_sort(workflow: WorkflowDefinition) -> list[list[WorkflowNode]]:
        """Topological sort → layers. Raises ValueError on cycle.

        Returns list of layers where each layer is a list of nodes
        that can execute in parallel.
        """
        node_map = {n.id: n for n in workflow.nodes}
        in_degree: dict[str, int] = {n.id: 0 for n in workflow.nodes}
        dependents: dict[str, list[str]] = defaultdict(list)

        for node in workflow.nodes:
            for dep in node.depends_on:
                in_degree[node.id] += 1
                dependents[dep].append(node.id)

        # Kahn's algorithm with layer grouping
        queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
        layers: list[list[WorkflowNode]] = []

        while queue:
            layer: list[WorkflowNode] = []
            for _ in range(len(queue)):
                nid = queue.popleft()
                layer.append(node_map[nid])
                for dep_id in dependents[nid]:
                    in_degree[dep_id] -= 1
                    if in_degree[dep_id] == 0:
                        queue.append(dep_id)
            if layer:
                layers.append(layer)

        if sum(len(l) for l in layers) != len(workflow.nodes):
            remaining = {
                nid for nid, deg in in_degree.items() if deg > 0
            }
            raise ValueError(
                f"Cycle detected in workflow. "
                f"Nodes still with dependencies: {remaining}"
            )

        return layers


# ── WorkflowExecutor ────────────────────────────────────────────────────────

@dataclass
class WorkflowNodeResult:
    node_id: str
    success: bool
    output: Any = None
    error: str = ""
    retries: int = 0


@dataclass
class WorkflowResult:
    success: bool
    node_results: dict[str, WorkflowNodeResult] = field(default_factory=dict)
    error: str = ""


class WorkflowExecutor:
    """CC-aligned DAG executor — layer-by-layer, parallel within layer."""

    def __init__(
        self,
        node_executor: Callable[
            [WorkflowNode, dict[str, Any]], WorkflowNodeResult,
        ] | None = None,
    ) -> None:
        self._node_executor = node_executor or self._default_executor

    def execute(
        self,
        workflow: WorkflowDefinition,
        inputs: dict[str, Any] | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        """Execute DAG layer by layer.

        1. Validate → topological sort → layers
        2. For each layer: execute nodes in parallel
        3. Feed outputs to dependent nodes
        4. Handle errors per node's on_error policy
        """
        errors = WorkflowValidator.validate(workflow)
        if errors:
            return WorkflowResult(
                success=False,
                error="; ".join(errors),
            )

        try:
            layers = WorkflowValidator.topological_sort(workflow)
        except ValueError as exc:
            return WorkflowResult(success=False, error=str(exc))

        node_results: dict[str, WorkflowNodeResult] = {}
        variables: dict[str, Any] = dict(inputs or {})

        for layer in layers:
            layer_results: dict[str, WorkflowNodeResult] = {}

            # Execute layer in parallel (thread pool)
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=min(8, len(layer))) as pool:
                futures = {
                    pool.submit(self._execute_node, node, variables, context): node
                    for node in layer
                }
                for future in as_completed(futures):
                    node = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = WorkflowNodeResult(
                            node_id=node.id, success=False, error=str(exc),
                        )
                    layer_results[node.id] = result

            node_results.update(layer_results)

            # Feed outputs to variables for dependent nodes
            for node in layer:
                r = layer_results[node.id]
                if r.success:
                    variables[node.id] = r.output
                else:
                    self._handle_error(node, r, layer_results, variables)

        success = all(r.success for r in node_results.values())
        return WorkflowResult(success=success, node_results=node_results)

    def _execute_node(
        self,
        node: WorkflowNode,
        variables: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> WorkflowNodeResult:
        """Execute one node with retry policy."""
        last_error = ""
        for attempt in range(1, node.max_retries + 1):
            try:
                result = self._node_executor(node, variables)
                if result.success:
                    result.retries = attempt - 1
                    return result
                last_error = result.error
            except Exception as exc:
                last_error = str(exc)
            if node.on_error != "retry":
                break

        return WorkflowNodeResult(
            node_id=node.id, success=False, error=last_error,
        )

    @staticmethod
    def _handle_error(
        node: WorkflowNode,
        result: WorkflowNodeResult,
        layer_results: dict[str, WorkflowNodeResult],
        variables: dict[str, Any],
    ) -> None:
        if node.on_error == "skip":
            variables[node.id] = None  # dependents get default
        elif node.on_error == "fail":
            pass  # error already recorded, dependents will fail
        # "retry" is handled in _execute_node

    @staticmethod
    def _default_executor(
        node: WorkflowNode,
        variables: dict[str, Any],
    ) -> WorkflowNodeResult:
        """Default executor: resolve variable references, return config."""
        resolved = {}
        for k, v in node.config.items():
            if isinstance(v, str) and v.startswith("$"):
                ref = v[1:]
                resolved[k] = variables.get(ref, v)
            else:
                resolved[k] = v
        return WorkflowNodeResult(
            node_id=node.id, success=True, output=resolved,
        )
