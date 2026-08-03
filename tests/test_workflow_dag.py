"""P1-3: Workflow DAG Engine — acceptance tests.

AC mappings:
  AC-1  Cycle detection returns errors
  AC-2  A→B, A→C → B and C execute in parallel (same layer)
  AC-3  on_error=skip → dependents get default value
  AC-4  on_error=retry + max_retries=2 → retries up to 2 times
"""

from __future__ import annotations

import pytest

from core.workflow import (
    WorkflowNode, WorkflowDefinition, WorkflowValidator, WorkflowExecutor,
    WorkflowNodeResult,
)


class TestWorkflowValidator:

    def test_valid_dag_passes(self):
        wf = WorkflowDefinition(name="test", nodes=[
            WorkflowNode(id="a", type="tool"),
            WorkflowNode(id="b", type="tool", depends_on=["a"]),
        ])
        assert WorkflowValidator.validate(wf) == []

    def test_missing_dependency(self):
        wf = WorkflowDefinition(name="test", nodes=[
            WorkflowNode(id="a", type="tool", depends_on=["does_not_exist"]),
        ])
        errors = WorkflowValidator.validate(wf)
        assert len(errors) > 0
        assert "does_not_exist" in errors[0]

    def test_cycle_detection(self):
        wf = WorkflowDefinition(name="test", nodes=[
            WorkflowNode(id="a", type="tool", depends_on=["b"]),
            WorkflowNode(id="b", type="tool", depends_on=["a"]),
        ])
        errors = WorkflowValidator.validate(wf)
        assert len(errors) > 0
        assert "cycle" in errors[0].lower()


class TestTopologicalSort:

    def test_linear_chain(self):
        wf = WorkflowDefinition(name="test", nodes=[
            WorkflowNode(id="a", type="tool"),
            WorkflowNode(id="b", type="tool", depends_on=["a"]),
            WorkflowNode(id="c", type="tool", depends_on=["b"]),
        ])
        layers = WorkflowValidator.topological_sort(wf)
        assert len(layers) == 3
        assert [n.id for n in layers[0]] == ["a"]

    def test_parallel_layer(self):
        """AC-2: A→B, A→C → B and C in same layer."""
        wf = WorkflowDefinition(name="test", nodes=[
            WorkflowNode(id="a", type="tool"),
            WorkflowNode(id="b", type="tool", depends_on=["a"]),
            WorkflowNode(id="c", type="tool", depends_on=["a"]),
        ])
        layers = WorkflowValidator.topological_sort(wf)
        assert len(layers) == 2
        layer1_ids = {n.id for n in layers[1]}
        assert layer1_ids == {"b", "c"}

    def test_diamond_dependency(self):
        wf = WorkflowDefinition(name="test", nodes=[
            WorkflowNode(id="a", type="tool"),
            WorkflowNode(id="b", type="tool", depends_on=["a"]),
            WorkflowNode(id="c", type="tool", depends_on=["a"]),
            WorkflowNode(id="d", type="tool", depends_on=["b", "c"]),
        ])
        layers = WorkflowValidator.topological_sort(wf)
        assert len(layers) == 3
        assert layers[2][0].id == "d"


class TestWorkflowExecutor:

    def test_execute_simple_workflow(self):
        wf = WorkflowDefinition(name="test", nodes=[
            WorkflowNode(id="step1", type="tool", config={"output": "done"}),
        ])
        executor = WorkflowExecutor()
        result = executor.execute(wf)
        assert result.success
        assert result.node_results["step1"].success

    def test_execute_with_variable_binding(self):
        wf = WorkflowDefinition(name="test", nodes=[
            WorkflowNode(id="producer", type="tool", config={"value": 42}),
            WorkflowNode(id="consumer", type="tool", config={"input": "$producer"}, depends_on=["producer"]),
        ])
        executor = WorkflowExecutor()
        result = executor.execute(wf)
        assert result.success
        # consumer received producer's output via $producer reference
        consumer_out = result.node_results["consumer"].output
        assert consumer_out is not None

    def test_error_skip_policy(self):
        """AC-3: on_error=skip → dependents still execute."""
        call_count = {"failing": 0}

        def custom_exec(node, vars):
            if node.id == "failing":
                call_count["failing"] += 1
                return WorkflowNodeResult(node_id=node.id, success=False, error="boom")
            return WorkflowNodeResult(node_id=node.id, success=True, output="ok")

        wf = WorkflowDefinition(name="test", nodes=[
            WorkflowNode(id="failing", type="tool", on_error="skip"),
            WorkflowNode(id="dependent", type="tool", depends_on=["failing"]),
        ])
        executor = WorkflowExecutor(node_executor=custom_exec)
        result = executor.execute(wf)
        # dependent should still execute (skip → variables["failing"] = None)
        assert result.node_results["dependent"].success

    def test_error_retry_policy(self):
        """AC-4: on_error=retry + max_retries=3 → retries up to 3 times."""
        call_count = {"retrying": 0}

        def custom_exec(node, vars):
            if node.id == "retrying":
                call_count["retrying"] += 1
                if call_count["retrying"] < 3:
                    return WorkflowNodeResult(node_id=node.id, success=False, error="fail")
                return WorkflowNodeResult(node_id=node.id, success=True, output="recovered")

            return WorkflowNodeResult(node_id=node.id, success=True, output="ok")

        wf = WorkflowDefinition(name="test", nodes=[
            WorkflowNode(id="retrying", type="tool", on_error="retry", max_retries=3),
        ])
        executor = WorkflowExecutor(node_executor=custom_exec)
        result = executor.execute(wf)
        assert result.success
        assert call_count["retrying"] == 3
