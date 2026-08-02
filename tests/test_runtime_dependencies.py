from dataclasses import FrozenInstanceError

import pytest

from agent.session.runtime import RuntimeDependencies, SessionRuntime


def _dependencies() -> RuntimeDependencies:
    marker = object()
    return RuntimeDependencies(
        store=marker,
        backend=marker,
        base_registry=marker,
        agent_registry=marker,
        root_agent_config=marker,
        log_dir="logs",
    )


def test_runtime_dependencies_are_frozen() -> None:
    dependencies = _dependencies()
    with pytest.raises(FrozenInstanceError):
        dependencies.log_dir = "other"  # type: ignore[misc]


def test_dependency_bundle_cannot_be_mixed_with_legacy_arguments() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        SessionRuntime(dependencies=_dependencies(), log_dir="other")
