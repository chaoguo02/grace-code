"""Regression tests for the shared Read-before-Edit cache."""

from __future__ import annotations

from agent.session.runtime import _inject_shared_read_cache
from core.base import ExecutionContext, ToolRegistry
from tools.file_edit_tool import FileEditTool
from tools.file_tool import FileReadCache, FileReadTool


def test_runtime_cache_injection_uses_canonical_edit_tool_name(tmp_path):
    """Read and Edit must retain one cache after a registry rebuild.

    ``file_edit`` is an alias, while the registry key is ``Edit``.  The old
    name-based injection skipped Edit and caused every subsequent edit to fail
    the Read-before-Edit guard even after a successful Read.
    """
    bootstrap_cache = FileReadCache()
    runtime_cache = FileReadCache()
    registry = (
        ToolRegistry()
        .register(FileReadTool(bootstrap_cache, workspace_root=tmp_path))
        .register(FileEditTool(bootstrap_cache, workspace_root=str(tmp_path)))
    )

    injected = _inject_shared_read_cache(registry, runtime_cache)

    assert set(injected) == {"Read", "Edit"}
    assert registry._tools["Read"]._read_cache is runtime_cache
    assert registry._tools["Edit"]._read_cache is runtime_cache


def test_read_then_edit_succeeds_after_scoping_registry(tmp_path):
    target = tmp_path / "README.md"
    target.write_text("before\n", encoding="utf-8")

    bootstrap_cache = FileReadCache()
    runtime_cache = FileReadCache()
    registry = (
        ToolRegistry()
        .register(FileReadTool(bootstrap_cache, workspace_root=tmp_path))
        .register(FileEditTool(bootstrap_cache, workspace_root=str(tmp_path)))
    )
    _inject_shared_read_cache(registry, runtime_cache)
    scoped = registry.scoped(ExecutionContext(
        workspace_root=str(tmp_path),
        repo_path=str(tmp_path),
    ))

    read_result = scoped.execute_tool("Read", {"path": str(target)})
    edit_result = scoped.execute_tool("Edit", {
        "path": str(target),
        "old_str": "before",
        "new_str": "after",
    })

    assert read_result.success is True
    assert edit_result.success is True, edit_result.error
    assert target.read_text(encoding="utf-8") == "after\n"
