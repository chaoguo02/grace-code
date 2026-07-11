"""Tool registry factory — assembles the complete ToolRegistry with all
built-in tools and the permission pipeline.

Constitution: registry assembly belongs in entry/bootstrap/ — it's factory
logic, not CLI logic. cli.py should call build_registry(), not build it inline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def build_registry(
    cfg: Any,
    confirm_callback: Any = None,
    runtime: Any = None,
    memory_store: Any = None,
    external_store: Any = None,
    repo_path: Any = None,
    auto_approve: bool = False,
) -> Any:
    """Build the complete ToolRegistry with all built-in tools + permission pipeline."""
    from tools.base import ToolRegistry
    from tools.file_tool import FileReadTool, FileViewTool, FileWriteTool, FileReadCache
    from tools.file_edit_tool import FileEditTool
    from tools.git_tool import GitAddTool, GitCommitTool, GitDiffTool, GitStatusTool
    from tools.search_tool import FindFilesTool, FindSymbolTool, SearchTextTool
    from tools.shell_tool import ShellTool
    from tools.test_tool import PytestTool
    from tools.web_tool import WebSearchTool, WebFetchTool
    from tools.artifact_tool import ArtifactListTool, ArtifactReadTool, ArtifactStoreRef
    from tools.evidence_tool import ArtifactSearchTool, EvidenceGetTool, EvidenceLedgerRef, EvidenceListTool
    from tools.submit_plan_tool import SubmitReadPlanRef, SubmitReadPlanTool

    from hitl.pipeline import PermissionPipeline
    from hitl.settings_loader import load_permission_settings

    project_root = str(repo_path) if repo_path else None
    rules, _hook_configs = load_permission_settings(project_root or ".")

    perm_confirm = None
    if confirm_callback is not None:
        from entry.renderer import permission_prompt
        perm_confirm = permission_prompt

    settings_path = None
    if project_root:
        settings_path = str(Path(project_root) / ".forge-agent" / "settings.json")

    pipeline = PermissionPipeline(
        rules=rules, confirm_callback=perm_confirm,
        auto_approve=auto_approve, settings_path=settings_path,
        project_root=project_root,
    )

    web_cfg = cfg.tools.web
    artifact_store_ref = ArtifactStoreRef()
    evidence_ledger_ref = EvidenceLedgerRef()
    submit_plan_ref = SubmitReadPlanRef()

    _global_read_cache = FileReadCache()

    registry = (
        ToolRegistry(permission_pipeline=pipeline)
        .register(ShellTool(runtime=runtime))
        .register(FileReadTool(read_cache=_global_read_cache))
        .register(FileViewTool(read_cache=_global_read_cache))
        .register(FileWriteTool(read_cache=_global_read_cache, workspace_root=project_root))
        .register(FileEditTool(read_cache=_global_read_cache, workspace_root=project_root))
        .register(SearchTextTool())
        .register(FindFilesTool())
        .register(FindSymbolTool())
        .register(PytestTool(runtime=runtime))
        .register(GitStatusTool(runtime=runtime))
        .register(GitDiffTool(runtime=runtime))
        .register(GitAddTool(runtime=runtime))
        .register(GitCommitTool(runtime=runtime))
        .register(WebSearchTool(max_results=web_cfg.search_max_results))
        .register(WebFetchTool(max_chars=web_cfg.fetch_max_chars, timeout=web_cfg.fetch_timeout))
        .register(ArtifactListTool(artifact_store_ref))
        .register(ArtifactReadTool(artifact_store_ref))
        .register(ArtifactSearchTool(artifact_store_ref))
        .register(EvidenceListTool(evidence_ledger_ref))
        .register(EvidenceGetTool(evidence_ledger_ref))
        .register(SubmitReadPlanTool(submit_plan_ref))
    )
    registry._artifact_store_ref = artifact_store_ref
    registry._evidence_ledger_ref = evidence_ledger_ref
    registry._submit_plan_ref = submit_plan_ref

    if memory_store is not None:
        from tools.memory_tool import (
            MemoryReadTool, MemoryWriteTool, MemoryListTool, MemoryDeleteTool,
        )
        registry \
            .register(MemoryReadTool(memory_store)) \
            .register(MemoryWriteTool(memory_store)) \
            .register(MemoryListTool(memory_store)) \
            .register(MemoryDeleteTool(memory_store))

        if external_store is not None:
            from tools.memory_tool import MemorySearchTool
            registry.register(MemorySearchTool(external_store))

    return registry
