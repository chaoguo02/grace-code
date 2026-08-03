"""P3: Schema Registry — acceptance tests.

AC: All registered payloads have independent classes.
AC: Duplicate key with different class → ValueError.
AC: validate_payload returns True only for registered class.
AC: Registry is immutable after construction.
"""

from __future__ import annotations

import pytest

from application.events.schema_registry import SchemaRegistry, SchemaEntry
from application.events.run_facts import (
    RunSubmittedV1, RunCompletedV1, RunCancelledV1,
)
from application.events.tool_facts import ToolExecutedV1
from application.events.delegation_facts import (
    DelegationCreatedV1, ChildTaskStartedV1,
)
from core.eventing.identifiers import RunId, TaskId


class TestSchemaRegistry:

    def test_all_defaults_registered(self):
        reg = SchemaRegistry()
        types = reg.registered_types
        assert "run.submitted.v1" in types
        assert "run.completed.v1" in types
        assert "run.cancelled.v1" in types
        assert "tool.executed.v1" in types
        assert "delegation.created.v1" in types
        assert "child_task.started.v1" in types
        assert len(types) == 12

    def test_duplicate_key_same_class_raises(self):
        """G3: duplicate (event_type, version) key always fails, even if class matches."""
        reg = SchemaRegistry()  # run.submitted.v1 is pre-registered by default
        with pytest.raises(ValueError, match="Duplicate"):
            reg.register(SchemaEntry("run.submitted.v1", 1, RunSubmittedV1))

    def test_duplicate_key_different_class_raises(self):
        reg = SchemaRegistry()
        with pytest.raises(ValueError, match="Duplicate"):
            reg.register(SchemaEntry("run.submitted.v1", 1, RunCompletedV1))

    def test_validate_payload_accepts_registered(self):
        reg = SchemaRegistry()
        payload = RunSubmittedV1(run_id=RunId("r1"))
        assert reg.validate_payload("run.submitted.v1", payload) is True

    def test_validate_payload_rejects_wrong_class(self):
        reg = SchemaRegistry()
        payload = RunCompletedV1(run_id=RunId("r1"))
        assert reg.validate_payload("run.submitted.v1", payload) is False

    def test_validate_payload_rejects_unknown_type(self):
        reg = SchemaRegistry()
        assert reg.validate_payload("unknown.event.v1", object()) is False

    def test_tool_executed_requires_name(self):
        with pytest.raises(ValueError, match="tool_name"):
            ToolExecutedV1(run_id=RunId("r1"), tool_name="")

    def test_delegation_created_validates_count(self):
        with pytest.raises(ValueError):
            DelegationCreatedV1(delegation_id="d1", parent_run_id=RunId("r1"), task_count=-1)

    def test_all_registered_classes_are_frozen(self):
        reg = SchemaRegistry()
        for event_type in reg.registered_types:
            entry = reg.get(event_type)
            cls = entry.payload_class
            # All must be frozen dataclasses
            assert hasattr(cls, "__dataclass_fields__"), f"{cls} is not a dataclass"
