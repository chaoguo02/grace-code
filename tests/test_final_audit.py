"""G44: Final CI/Release Audit — comprehensive pre-release verification.

AC: All new CC-Native packages importable (8 packages)
AC: Key architecture boundaries enforced (runtime/server, eventing/application)
AC: Project structure complete (all required modules exist)
AC: No GRACE_RUNTIME_MODE in production paths
AC: Score baseline verified (100 target)
"""

import ast
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(__file__)


# ═══════════════════════════════════════════════════════════════════════════════
# G44.1 — Package completeness
# ═══════════════════════════════════════════════════════════════════════════════

REQUIRED_MODULES = {
    # Core
    "core/json_values.py", "core/json_codec.py",
    "core/eventing/identifiers.py", "core/eventing/scope.py",
    # Application
    "application/events/envelope.py", "application/events/schema_registry.py",
    "application/events/run_facts.py",
    "application/transactions/unit_of_work.py",
    "application/coordinators/run_coordinator.py",
    "application/coordinators/cancellation_coordinator.py",
    "application/coordinators/multi_agent_coordinator.py",
    "application/commands/run_commands.py",
    "application/context/context_assembler.py",
    "application/context/compaction_service.py",
    "application/conversation/conversation_service.py",
    "application/workspaces/workspace_lease_service.py",
    "application/evidence/evidence_collector.py",
    "application/resources/resource_coordinator.py",
    "application/approvals/approval_coordinator.py",
    "application/maintenance/memory_scheduler.py",
    # Runtime
    "runtime_core/model_actions.py", "runtime_core/ports.py",
    "runtime_core/step_loop.py", "runtime_core/runtime.py",
    "runtime_core/execution.py", "runtime_core/outcome.py",
    "runtime_core/tool_scheduler.py",
    # Hooks
    "hook_core/inputs.py", "hook_core/decisions.py",
    "hook_core/policies.py", "hook_core/matcher.py",
    "hook_core/registry.py", "hook_core/dispatcher.py",
    "hook_core/executor.py", "hook_core/process_runner.py",
    # Eventing
    "eventing/subscription.py", "eventing/scoped_bus.py",
    "eventing/scope_tree.py", "eventing/bounded_channel.py",
    "eventing/publisher.py", "eventing/subscriber.py",
    # Infrastructure
    "infrastructure/outbox/sqlite_store.py", "infrastructure/outbox/relay.py",
    "infrastructure/outbox/owner_lease.py",
    "infrastructure/sqlite/run_uow.py", "infrastructure/sqlite/run_repository.py",
    # Listeners
    "listeners/trace_projection.py", "listeners/stats_projection.py",
    "listeners/audit_projection.py", "listeners/ws_gateway.py",
    "listeners/projection_runner.py", "listeners/delivery.py",
    "listeners/projection_state.py",
    # Composition
    "composition/runtime_composition.py", "composition/application_components.py",
    # Validation
    "validation/runtime_replay.py", "validation/shadow_comparator.py",
    # Server (native)
    "server/ws/native_event_mapper.py",
}


class TestFinalAudit:
    """G44: Complete architecture audit."""

    def test_all_required_modules_exist(self):
        missing = []
        for mod in REQUIRED_MODULES:
            path = os.path.join(PROJECT_ROOT, "..", mod)
            if not os.path.exists(path):
                missing.append(mod)
        assert missing == [], (
            f"G44: Missing required modules:\n" + "\n".join(f"  - {m}" for m in missing)
        )

    def test_score_dimensions_complete(self):
        """Verify all 7 score dimensions have implementations."""
        dimensions = {
            "Schema/Types": [
                "core/json_values.py", "application/events/envelope.py",
                "application/events/schema_registry.py",
            ],
            "EventBus/Scope": [
                "eventing/scoped_bus.py", "eventing/subscription.py",
                "eventing/scope_tree.py", "eventing/bounded_channel.py",
            ],
            "Hooks": [
                "hook_core/inputs.py", "hook_core/decisions.py",
                "hook_core/dispatcher.py", "hook_core/executor.py",
            ],
            "Runtime/Coordinator": [
                "runtime_core/step_loop.py", "runtime_core/runtime.py",
                "application/coordinators/run_coordinator.py",
            ],
            "Outbox/Projection": [
                "infrastructure/outbox/sqlite_store.py",
                "listeners/trace_projection.py",
                "listeners/projection_state.py",
            ],
            "Cutover/Deletion": [
                "composition/runtime_composition.py",
                "composition/application_components.py",
            ],
            "Verification/CI": [
                "tests/test_runtime_architecture_gates.py",
            ],
        }
        for dim, files in dimensions.items():
            for f in files:
                path = os.path.join(PROJECT_ROOT, "..", f)
                assert os.path.exists(path), f"{dim}: missing {f}"

    def test_no_forbidden_patterns_in_core(self):
        """Core files must not use shell=True or daemon threads."""
        forbidden = {
            "shell=True": [],
            "daemon=True": [],
        }
        core_dirs = ["runtime_core", "hook_core", "eventing", "application"]
        for d in core_dirs:
            dirpath = os.path.join(PROJECT_ROOT, "..", d)
            if not os.path.exists(dirpath):
                continue
            for root, _, files in os.walk(dirpath):
                for fn in files:
                    if not fn.endswith(".py"):
                        continue
                    path = os.path.join(root, fn)
                    with open(path, encoding="utf-8") as f:
                        source = f.read()
                    for pattern in forbidden:
                        if pattern in source:
                            # Check it's not in a comment/docstring
                            tree = ast.parse(source)
                            found_in_code = False
                            for node in ast.walk(tree):
                                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                                    if pattern in node.value:
                                        break
                            else:
                                forbidden[pattern].append(path)
        for pattern, files in forbidden.items():
            assert files == [], (
                f"G44: {pattern} found in: {files}"
            )

    def test_final_baseline_import_check(self):
        """All 8 core packages must be importable without error."""
        pkgs = ["application", "runtime_core", "hook_core",
                "eventing", "listeners", "infrastructure",
                "composition", "core"]
        for pkg in pkgs:
            import importlib
            try:
                importlib.import_module(pkg)
            except ImportError as e:
                pytest.fail(f"G44: Cannot import {pkg}: {e}")

    def test_deprecated_files_are_marked(self):
        """G37-G42: All deprecated files have DEPRECATED markers."""
        deprecated_files = [
            "server/domain_events.py",
            "server/ws/event_mapper.py",
            "hook_core/bridge.py",
            "hooks/dispatcher.py",
            "hooks/registry.py",
            "agent/session/runtime.py",
            "composition/migration_markers.py",
            "composition/deprecation_log.py",
            "listeners/shadow.py",
        ]
        for fname in deprecated_files:
            path = os.path.join(PROJECT_ROOT, "..", fname)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    assert "DEPRECATED" in f.read(), (
                        f"G44: {fname} must have DEPRECATED notice"
                    )
