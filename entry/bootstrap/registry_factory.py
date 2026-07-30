"""Tool registry factory — assembles the complete ToolRegistry with all
built-in tools and the permission pipeline.

Constitution: registry assembly belongs in entry/bootstrap/ — it's factory
logic, not CLI logic. cli.py should call build_registry(), not build it inline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from hitl.pipeline import ToolApprovalMode

logger = logging.getLogger(__name__)


def initialize_mcp_integration(cfg: Any, registry: Any) -> Any | None:
    """Initialize the canonical MCP runtime and attach it to a registry.

    CLI and Web share this boundary so transport handling, tool adapters,
    ToolSearch wiring, and lifecycle semantics stay consistent.
    """
    server_configs = getattr(cfg, "mcp_servers", None)
    if not server_configs:
        return None

    from agent.session import MCPToolIntegration
    from tools.workflow_tool import ToolSearchTool, WaitForMcpServersTool

    integration = MCPToolIntegration({"mcp_servers": server_configs})
    integration.initialize()
    integration.register_into(registry)
    registry.add_closeable(integration)
    for tool in registry.tools:
        if isinstance(tool, (ToolSearchTool, WaitForMcpServersTool)):
            tool.set_mcp_context(registry, integration)
    return integration


def build_registry(
    cfg: Any,
    confirm_callback: Any = None,
    runtime: Any = None,
    memory_store: Any = None,
    external_store: Any = None,
    repo_path: Any = None,
    skill_registry: Any = None,
    approval_mode: ToolApprovalMode = ToolApprovalMode.PROMPT,
) -> Any:
    """Build the complete ToolRegistry with all built-in tools + permission pipeline."""
    from core.base import ToolRegistry
    from tools.pool import assemble_tool_pool
    from tools.factory import build_tool
    from tools.file_tool import FileReadTool, FileViewTool, FileWriteTool, FileReadCache
    from tools.file_edit_tool import FileEditTool
    from tools.git_tool import GitAddTool, GitCommitTool, GitDiffTool, GitStatusTool
    from tools.search_tool import FindFilesTool, FindSymbolTool, SearchTextTool
    from tools.shell_tool import ShellTool
    from tools.test_tool import PytestTool
    from tools.web_tool import WebSearchTool, WebFetchTool
    from tools.artifact_tool import ArtifactListTool, ArtifactReadTool, ArtifactStoreRef
    from tools.evidence_tool import ArtifactSearchTool, EvidenceGetTool, EvidenceLedgerRef, EvidenceListTool
    from tools.submit_analysis_tool import SubmitAnalysisTool
    from tools.plan_mode_tool import EnterPlanModeTool, ExitPlanModeTool
    from tools.worktree_session_tool import EnterWorktreeTool, ExitWorktreeTool
    from tools.workflow_tool import ToolSearchTool, WaitForMcpServersTool, WorkflowTool
    from core.process import LocalRuntime

    from hitl.pipeline import PermissionPipeline
    from hitl.settings_loader import load_permission_settings

    project_root = str(repo_path) if repo_path else None
    runtime = runtime or LocalRuntime(workspace_root=project_root or Path.cwd())
    rules, _hook_configs = load_permission_settings(project_root or ".")

    perm_confirm = None
    if confirm_callback is not None:
        from entry.renderer import permission_prompt
        perm_confirm = permission_prompt

    settings_path = None
    if project_root:
        settings_path = str(Path(project_root) / ".grace" / "settings.json")

    pipeline = PermissionPipeline(
        rules=rules, confirm_callback=perm_confirm,
        approval_mode=approval_mode, settings_path=settings_path,
        project_root=project_root,
    )

    web_cfg = cfg.tools.web
    artifact_store_ref = ArtifactStoreRef()
    evidence_ledger_ref = EvidenceLedgerRef()

    _global_read_cache = FileReadCache()

    if skill_registry is None and project_root:
        from skills.registry import SkillRegistry
        skill_registry = SkillRegistry.for_project(project_root)

    from skills.buffer import SkillContextBuffer
    skill_buffer = SkillContextBuffer() if skill_registry is not None else None
    registry = ToolRegistry(
        permission_pipeline=pipeline,
        artifact_store_ref=artifact_store_ref,
        evidence_ledger_ref=evidence_ledger_ref,
        skill_registry=skill_registry,
        skill_buffer=skill_buffer,
    )

    local_tools = [build_tool(tool=tool) for tool in [
        ShellTool(runtime=runtime),
        FileReadTool(read_cache=_global_read_cache),
        FileViewTool(read_cache=_global_read_cache),
        FileWriteTool(
            read_cache=_global_read_cache,
            workspace_root=project_root,
        ),
        FileEditTool(
            read_cache=_global_read_cache,
            workspace_root=project_root,
        ),
        SearchTextTool(),
        FindFilesTool(),
        FindSymbolTool(),
        PytestTool(runtime=runtime, workspace_root=project_root),
        GitStatusTool(runtime=runtime),
        GitDiffTool(runtime=runtime),
        GitAddTool(runtime=runtime),
        GitCommitTool(runtime=runtime),
        WebSearchTool(max_results=web_cfg.search_max_results),
        WebFetchTool(
            max_chars=web_cfg.fetch_max_chars,
            timeout=web_cfg.fetch_timeout,
        ),
        ArtifactListTool(artifact_store_ref),
        ArtifactReadTool(artifact_store_ref),
        ArtifactSearchTool(artifact_store_ref),
        EvidenceListTool(evidence_ledger_ref),
        EvidenceGetTool(evidence_ledger_ref),
        SubmitAnalysisTool(),
        EnterPlanModeTool(),
        ExitPlanModeTool(),
        EnterWorktreeTool(),
        ExitWorktreeTool(),
        WorkflowTool(),
        ToolSearchTool(),
        WaitForMcpServersTool(),
    ]]

    if skill_registry is not None:
        if skill_registry.list_skills():
            from skills.tool import SkillTool
            local_tools.append(build_tool(tool=SkillTool(
                skill_registry,
                buffer=skill_buffer,
                runtime=runtime,
            )))

    if memory_store is not None:
        from tools.memory_tool import (
            MemoryReadTool, MemoryWriteTool, MemoryListTool, MemoryDeleteTool,
        )
        local_tools.extend([
            build_tool(tool=MemoryReadTool(memory_store)),
            build_tool(tool=MemoryWriteTool(memory_store)),
            build_tool(tool=MemoryListTool(memory_store)),
            build_tool(tool=MemoryDeleteTool(memory_store)),
        ])

        if external_store is not None:
            from tools.memory_tool import MemorySearchTool
            local_tools.append(build_tool(
                tool=MemorySearchTool(external_store),
            ))

    registry.register_many(assemble_tool_pool(local_tools))
    return registry
