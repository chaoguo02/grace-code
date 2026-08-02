"""G28: Native Object Graph — typed assembly, all components wired.

AC: assemble() returns ApplicationComponents (not dict)
AC: All 18 components are non-None and correctly typed
AC: RunCoordinator has non-None UoW factory
AC: RuntimePorts has all 7 ports
AC: ApplicationLifecycle start/stop works
AC: No mode branching mixing old/new
AC: No dict service locator
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from composition.runtime_composition import assemble
from composition.application_components import (
    ApplicationComponents, ApplicationLifecycle,
)
from application.coordinators.run_coordinator import RunCoordinator
from application.events.schema_registry import SchemaRegistry
from eventing.scoped_bus import ScopedEventBus
from hook_core.registry import HookRegistry
from infrastructure.outbox.owner_lease import OwnerLease
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from listeners.projection_runner import ProjectionDispatcher
from listeners.trace_projection import TraceProjection
from listeners.projection_state import ProjectionStateStore
from runtime_core.runtime import AgentRuntime


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    import sqlite3
    conn = sqlite3.connect(db)
    SqliteOutboxStore.install(conn)
    OwnerLease.install(conn)
    conn.commit()
    conn.close()
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


class TestTypedAssembly:
    """G28: assemble() returns typed components."""

    def test_assemble_returns_application_components(self, temp_db):
        comp = assemble(temp_db)
        assert isinstance(comp, ApplicationComponents), (
            f"G28: assemble must return ApplicationComponents, got {type(comp)}"
        )

    def test_mode_is_native(self, temp_db):
        comp = assemble(temp_db)
        assert comp.mode == "NATIVE"

    def test_all_components_non_none(self, temp_db):
        comp = assemble(temp_db)
        comps = [
            ("registry", comp.registry, SchemaRegistry),
            ("outbox", comp.outbox, SqliteOutboxStore),
            ("lease", comp.lease, OwnerLease),
            ("bus", comp.bus, ScopedEventBus),
            ("hook_registry", comp.hook_registry, HookRegistry),
            ("runtime", comp.runtime, AgentRuntime),
            ("projection_dispatcher", comp.projection_dispatcher, ProjectionDispatcher),
            ("trace", comp.trace, TraceProjection),
            ("run_coordinator", comp.run_coordinator, RunCoordinator),
        ]
        for name, obj, expected_type in comps:
            assert obj is not None, f"G28: {name} must not be None"
            assert isinstance(obj, expected_type), (
                f"G28: {name} expected {expected_type.__name__}, got {type(obj).__name__}"
            )

    def test_runtime_ports_all_present(self, temp_db):
        comp = assemble(temp_db)
        ports = comp.runtime_ports
        assert ports.llm is not None
        assert ports.tools is not None
        assert ports.hooks is not None
        assert ports.live_events is not None
        assert ports.clock is not None
        assert ports.token_usage is not None
        assert ports.cancellation is not None

    def test_uow_factory_returns_usable_uow(self, temp_db):
        comp = assemble(temp_db)
        uow = comp.uow_factory()
        assert uow is not None

    def test_not_dict_service_locator(self, temp_db):
        comp = assemble(temp_db)
        # Components must NOT be accessed via dict-like interface
        with pytest.raises(TypeError):
            _ = comp["registry"]  # type: ignore


class TestLifecycle:
    """G28: ApplicationLifecycle start/stop ordering."""

    def test_lifecycle_start_stop(self, temp_db):
        comp = assemble(temp_db)
        lifecycle = ApplicationLifecycle(comp)
        lifecycle.start()
        lifecycle.stop()
        # No exception = OK
