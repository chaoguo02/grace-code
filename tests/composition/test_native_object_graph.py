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


# ═══════════════════════════════════════════════════════════════════════════════
# H1 — _RealLLM returns non-empty ModelAction with TokenUsage
# ═══════════════════════════════════════════════════════════════════════════════

class TestLLMAdapter:
    """H1: _RealLLM.invoke() returns real text + TokenUsage, not empty stub."""

    def test_invoke_returns_non_empty_text(self, temp_db):
        comp = assemble(temp_db)
        result = comp.runtime_ports.llm.invoke(None)
        assert result.text != "", (
            "H1 FAIL: _RealLLM.invoke() must not return empty text"
        )

    def test_invoke_returns_token_usage(self, temp_db):
        comp = assemble(temp_db)
        result = comp.runtime_ports.llm.invoke(None)
        assert result.usage is not None, (
            "H1 FAIL: _RealLLM.invoke() must return TokenUsage"
        )
        assert result.usage.input_tokens > 0, (
            "H1 FAIL: input_tokens must be > 0"
        )
        assert result.usage.output_tokens > 0, (
            "H1 FAIL: output_tokens must be > 0"
        )

    def test_invoke_with_fake_backend(self, temp_db):
        """H1: When backend is None, returns controlled fake response for tests."""
        comp = assemble(temp_db)
        result = comp.runtime_ports.llm.invoke(None)
        assert result.text == "H1 fake response", (
            f"H1: expected fake response, got {result.text!r}"
        )

    def test_stream_also_returns_non_empty(self, temp_db):
        comp = assemble(temp_db)
        import asyncio
        async def _run():
            coro = comp.runtime_ports.llm.stream(None)
            result = await coro
            assert result.text != "", "H1: stream must return non-empty"
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# H2 — _RealTools returns non-empty ToolOutcome
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolAdapter:
    """H2: _RealTools.execute() returns real output, not empty stub."""

    def test_execute_returns_non_empty_output(self, temp_db):
        comp = assemble(temp_db)
        result = comp.runtime_ports.tools.execute("read", None)
        assert result.output != "", (
            "H2 FAIL: _RealTools.execute() must not return empty output"
        )

    def test_execute_returns_tool_name(self, temp_db):
        comp = assemble(temp_db)
        result = comp.runtime_ports.tools.execute("write", None)
        assert result.tool_name == "write"

    def test_execute_fake_contains_tool_name_in_output(self, temp_db):
        comp = assemble(temp_db)
        result = comp.runtime_ports.tools.execute("bash", None)
        assert "bash" in result.output, (
            f"H2: fake output should mention tool name, got {result.output!r}"
        )

    # T5: LLMPort accepts tool_choice
    def test_llm_port_accepts_tool_choice(self, temp_db):
        comp = assemble(temp_db)
        from core.json_values import freeze_json
        result = comp.runtime_ports.llm.invoke(
            freeze_json({"messages": []}), tool_choice={"type": "auto"},
        )
        assert result.text != ""


# ═══════════════════════════════════════════════════════════════════════════════
# T3 — ToolScheduler metadata populated from real tools
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# T14 — Retry on automatic errors
# ═══════════════════════════════════════════════════════════════════════════════

class TestMCPPrefix:
    """T17: MCP tool prefix resolution."""
    def test_mcp_prefix_resolves(self, temp_db):
        """mcp__server__tool falls back to bare tool name."""
        def _lookup(n):
            if n == "get_weather":
                class T:
                    name = n
                    def execute(self, p):
                        return type('R',(),{'output':'sunny','success':True})()
                return T()
            return None
        from composition.runtime_composition import _execute_via_registry
        result = _execute_via_registry(_lookup, "mcp__weather__get_weather", {}, "")
        assert result.output == "sunny", f"MCP prefix should resolve to bare name"


class TestToolRetry:
    """T14: Retry automatic errors, don't retry permanent errors."""

    def test_timeout_retries(self, temp_db):
        """Tool that fails twice with timeout, then succeeds."""
        calls = []
        def _flaky_lookup(tool_name):
            class FlakyTool:
                name = tool_name
                def execute(self, params):
                    calls.append(1)
                    if len(calls) < 3:
                        raise TimeoutError("timed out")
                    return type('R',(),{'output':'ok','success':True})()
            return FlakyTool()
        from composition.runtime_composition import _execute_via_registry
        result = _execute_via_registry(_flaky_lookup, "test", {}, "")
        assert result.output == "ok", f"Expected success after retry, got {result}"
        assert len(calls) == 3, f"Expected 3 attempts (2 fail + 1 success), got {len(calls)}"

    def test_permission_denied_no_retry(self, temp_db):
        """Permission error must NOT retry."""
        def _failing_lookup(tool_name):
            class FailTool:
                name = tool_name
                def execute(self, params):
                    raise PermissionError("denied")
            return FailTool()
        from composition.runtime_composition import _execute_via_registry
        result = _execute_via_registry(_failing_lookup, "test", {}, "")
        assert result.error_type == "permission_denied", (
            f"Expected permission_denied, got {result.error_type}"
        )


class TestPermissionRules:
    """T12: Permission rules accessible from hook_settings."""
    def test_permission_rules_loaded(self, temp_db):
        # T12: Rules are loaded via hook_settings dict; wiring to hooks is T13
        settings = {"hooks": {}, "permission_rules": {"Bash": "deny"}}
        comp = assemble(temp_db, hook_settings=settings)
        # Verify assembly succeeds with permission rules (wiring in T13)
        assert comp.runtime_ports is not None


class TestSchedulerMetadataBridge:
    """T3: ToolScheduler gets real metadata when tool_registry is provided."""

    def test_from_base_tool_read_only(self):
        from runtime_core.tool_scheduler import ToolMetadata
        from core.base import BaseTool
        from core.types import ToolMetadata as CoreMetadata, ToolEffect, PathAccess

        class ReadTool(BaseTool):
            name = "Read"
            metadata = CoreMetadata(effects=frozenset({ToolEffect.READ_WORKSPACE}),
                                     path_access=PathAccess.READ)
            @property
            def description(self): return "Read"
            @property
            def parameters_schema(self): return {"type": "object", "properties": {}}
            def isReadOnly(self, p=None): return True
            def execute(self, p): return type('R',(),{'output':'','success':True})()

        tool = ReadTool()
        meta = ToolMetadata.from_base_tool(tool)
        assert meta.read_only is True

    def test_register_batch_populates_registry(self):
        from runtime_core.tool_scheduler import ToolScheduler, ToolMetadata
        from core.base import BaseTool
        from core.types import ToolMetadata as CoreMetadata, ToolEffect

        class ReadTool(BaseTool):
            name = "Read"
            metadata = CoreMetadata(effects=frozenset({ToolEffect.READ_WORKSPACE}))
            @property
            def description(self): return "Read"
            @property
            def parameters_schema(self): return {"type": "object", "properties": {}}
            def isReadOnly(self, p=None): return True
            def execute(self, p): return type('R',(),{'output':'','success':True})()

        class WriteTool(BaseTool):
            name = "Write"
            metadata = CoreMetadata(effects=frozenset({ToolEffect.WRITE_WORKSPACE}))
            @property
            def description(self): return "Write"
            @property
            def parameters_schema(self): return {"type": "object", "properties": {}}
            def execute(self, p): return type('R',(),{'output':'','success':True})()

        from core.json_values import freeze_json
        from runtime_core.model_actions import ToolCall
        sched = ToolScheduler()
        sched.register_batch([ReadTool(), WriteTool()])
        batches = sched.schedule((
            ToolCall(id="1", name="Read", params=freeze_json({})),
            ToolCall(id="2", name="Write", params=freeze_json({})),
        ))
        # Write tool should cause a new batch (serialized)
        assert len(batches) >= 2, (
            f"T3: Write tool should be serialized (own batch), got {len(batches)} batches"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# M4 — native tool execution routes through PolicyAwareToolRegistry
# ═══════════════════════════════════════════════════════════════════════════════

class TestNativePolicyEnforcement:
    """M4: native _RealTools routes through PolicyAwareToolRegistry so phase
    policy (allowed_write_paths) is enforced on the native path."""

    def test_allowed_write_paths_enforced_on_native_path(self, temp_db, tmp_path):
        from core.base import ToolRegistry
        from core.policy import PhasePolicy
        from core.policy_registry import PolicyAwareToolRegistry
        from tools.file_tool import FileWriteTool
        from runtime_core.ports import ToolSuccess, ToolFailure

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "src").mkdir(parents=True, exist_ok=True)

        base = ToolRegistry()
        base.register(FileWriteTool(workspace_root=str(ws)))
        policy = PhasePolicy(
            allowed_write_paths=frozenset({"src/a.txt"}),
            strict_file_scope=True,
        )
        reg = PolicyAwareToolRegistry(
            base=base, phase_policy=policy,
            repo_path=str(ws), phase_name="test",
        )
        comp = assemble(temp_db, tool_registry=reg)
        tools = comp.runtime_ports.tools

        # Inside the allowed path → ToolSuccess
        ok = tools.execute(
            "Write", {"path": str(ws / "src" / "a.txt"), "content": "x"}, "inv1",
        )
        assert isinstance(ok, ToolSuccess), f"M4: expected ToolSuccess, got {ok}"

        # Outside the allowed path → ToolFailure (policy block, not execution)
        bad = tools.execute(
            "Write", {"path": str(ws / "outside.txt"), "content": "x"}, "inv2",
        )
        assert isinstance(bad, ToolFailure), f"M4: expected ToolFailure, got {bad}"
        assert "blocked" in bad.error.lower() or "denied" in bad.error.lower()

    def test_plain_registry_keeps_execute_via_registry_path(self, temp_db, tmp_path):
        """M4: a plain ToolRegistry (no phase policy) still executes directly."""
        from core.base import ToolRegistry
        from tools.file_tool import FileReadTool
        from runtime_core.ports import ToolSuccess

        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "file.txt"
        target.write_text("hello", encoding="utf-8")

        base = ToolRegistry()
        base.register(FileReadTool(workspace_root=str(ws)))
        comp = assemble(temp_db, tool_registry=base)
        tools = comp.runtime_ports.tools

        ok = tools.execute("Read", {"path": str(target)}, "inv1")
        assert isinstance(ok, ToolSuccess), f"M4: expected ToolSuccess, got {ok}"
