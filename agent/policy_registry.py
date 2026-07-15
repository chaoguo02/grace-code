"""Policy-aware ToolRegistry wrapper."""

from __future__ import annotations

import time
from typing import Any

from agent.policy import PhasePolicy, normalize_repo_path
from tools.base import (
    ExecutionContext,
    PathAccess,
    ToolDependency,
    ToolEffect,
    ToolMetadata,
    ToolRegistry,
    ToolResult,
)


class PolicyAwareToolRegistry(ToolRegistry):
    """ToolRegistry wrapper that applies a phase-specific task policy."""

    def __init__(
        self,
        base: ToolRegistry,
        phase_policy: PhasePolicy,
        repo_path: str,
        phase_name: str,
        base_allowed_tools: set[str] | frozenset[str] | None = None,
        plan_mode_allowed: frozenset[str] | None = None,
    ) -> None:
        super().__init__(
            hitl_manager=getattr(base, "_hitl_manager", None),
            permission_pipeline=getattr(base, "_permission_pipeline", None),
            hook_dispatcher=getattr(base, "_hook_dispatcher", None),
            capability_registry=getattr(base, "_capability_registry", None),
        )
        self._base = base
        self._phase_policy = phase_policy
        self._repo_path = repo_path
        self._phase_name = phase_name
        self._base_allowed_tools = frozenset(base_allowed_tools) if base_allowed_tools is not None else None
        # plan_mode_allowed：非只读工具仍注册（模型能看到定义），但调用时返回权限错误
        # ref: Claude Code hasPermissionsToUseToolInner() — plan 模式所有写操作直接拒绝
        self._plan_mode_allowed = plan_mode_allowed
        self._artifact_store_ref = getattr(base, "_artifact_store_ref", None)
        self._evidence_ledger_ref = getattr(base, "_evidence_ledger_ref", None)
        for name, tool in base._tools.items():
            if self._is_tool_visible(name):
                self._tools[name] = tool

    @property
    def phase_policy(self) -> PhasePolicy:
        return self._phase_policy

    @property
    def constraints(self) -> PhasePolicy:
        return self._phase_policy

    def with_allowed_tools(self, allowed_tools: set[str] | frozenset[str]) -> "PolicyAwareToolRegistry":
        return PolicyAwareToolRegistry(
            base=self._base,
            phase_policy=self._phase_policy.with_allowed_tools(allowed_tools),
            repo_path=self._repo_path,
            phase_name=self._phase_name,
            base_allowed_tools=allowed_tools,
        )

    def with_phase_policy(self, phase_policy: PhasePolicy) -> "PolicyAwareToolRegistry":
        """Layer a per-task policy without mutating the reusable registry."""
        return PolicyAwareToolRegistry(
            base=self,
            phase_policy=phase_policy,
            repo_path=self._repo_path,
            phase_name=self._phase_name,
            base_allowed_tools=frozenset(self.tool_names),
        )

    def with_run_context(self, context: Any) -> "PolicyAwareToolRegistry":
        """Preserve policy while binding Runtime resources to capable tools."""
        return PolicyAwareToolRegistry(
            base=self._base.with_run_context(context),
            phase_policy=self._phase_policy,
            repo_path=self._repo_path,
            phase_name=self._phase_name,
            base_allowed_tools=self._base_allowed_tools,
            plan_mode_allowed=self._plan_mode_allowed,
        )

    def scoped(self, context: ExecutionContext) -> "PolicyAwareToolRegistry":
        """Rebind workspace-aware tools without losing phase authority."""
        return PolicyAwareToolRegistry(
            base=self._base.scoped(context),
            phase_policy=self._phase_policy,
            repo_path=context.repo_path or context.workspace_root,
            phase_name=self._phase_name,
            base_allowed_tools=self._base_allowed_tools,
            plan_mode_allowed=self._plan_mode_allowed,
        )

    def _is_tool_visible(self, name: str) -> bool:
        if self._base_allowed_tools is not None and name not in self._base_allowed_tools:
            return False
        if self._phase_policy.allowed_tools is not None and name not in self._phase_policy.allowed_tools:
            return False
        if name in self._phase_policy.denied_tools:
            return False
        metadata = self._base.metadata_for(name)
        if metadata is None:
            return False
        if (
            self._phase_policy.allowed_effects is not None
            and not metadata.effects.issubset(self._phase_policy.allowed_effects)
        ):
            return False
        if metadata.effects & self._phase_policy.denied_effects:
            return False
        if self._phase_policy.strict_file_scope:
            if ToolEffect.UNKNOWN in metadata.effects:
                return False
            if metadata.effects & {
                ToolEffect.NETWORK,
                ToolEffect.READ_AGENT_STATE,
                ToolEffect.WRITE_AGENT_STATE,
            }:
                return False
            if (
                self._phase_policy.allowed_read_paths is not None
                and ToolEffect.DISCOVER_WORKSPACE in metadata.effects
            ):
                return False
            if (
                self._phase_policy.allowed_write_paths is not None
                and metadata.path_access == PathAccess.WORKSPACE_WIDE
            ):
                return False
        return True

    def _is_tool_enabled(self, name: str) -> bool:
        metadata = self._base.metadata_for(name)
        if metadata is None:
            return False
        if metadata.dependency == ToolDependency.ARTIFACT_STORE:
            return self._artifact_store_ref is not None and self._artifact_store_ref.store is not None
        if metadata.dependency == ToolDependency.EVIDENCE_LEDGER:
            return self._evidence_ledger_ref is not None and self._evidence_ledger_ref.ledger is not None
        return True

    def get_schemas(self):
        schemas = [
            tool.to_llm_schema()
            for name, tool in self._tools.items()
            if self._is_tool_enabled(name)
        ]
        schemas.sort(key=lambda s: s.name)
        return schemas

    @property
    def tool_names(self) -> list[str]:
        return [name for name in self._tools.keys() if self._is_tool_enabled(name)]

    def execute_tool(self, name: str, params: dict[str, Any], thought: str = "") -> ToolResult:
        start = time.perf_counter()
        violation = self._check_tool_call(name, params)
        if violation:
            result = ToolResult(success=False, output="", error=violation)
            self._record_timing(name, start, result)
            return result
        result = self._base.execute_tool(name, params, thought=thought)
        self._record_timing(name, start, result)
        return result

    def _check_tool_call(self, name: str, params: dict[str, Any]) -> str | None:
        # ── Plan Mode 权限拦截 ──
        # 模型能看到写工具的定义，但调用时返回明确的权限错误。
        # ref: Claude Code plan 模式 — 写操作直接拒绝，模型感知到限制后自行调整行为。
        if self._plan_mode_allowed is not None and name not in self._plan_mode_allowed:
            return (
                f"Permission denied: '{name}' is not available in plan mode. "
                f"Plan mode only allows read-only operations. "
                f"Available tools: {', '.join(sorted(self._plan_mode_allowed))}. "
                f"You are in plan mode — explore the codebase and produce a "
                f"structured implementation plan instead of attempting writes."
            )
        if name not in self._tools:
            # Distinguish: "tool doesn't exist" vs "tool exists but blocked by policy"
            _base_names = self._base.tool_names if hasattr(self._base, "tool_names") else set()
            if name not in _base_names:
                return f"Unknown tool '{name}'. Available tools: {', '.join(self.tool_names) or 'none'}"
            return f"Tool '{name}' is blocked by task policy in {self._phase_name} phase. Available tools: {', '.join(self.tool_names) or 'none'}"
        if not self._is_tool_enabled(name):
            return f"Tool '{name}' is not available in the current environment. Available tools: {', '.join(self.tool_names) or 'none'}"
        if name in self._phase_policy.denied_tools:
            return f"Tool '{name}' is blocked by task policy."

        metadata = self._base.metadata_for(name) or ToolMetadata()
        raw_path = params.get(metadata.path_parameter, "") if metadata.path_parameter else ""
        if metadata.path_access == PathAccess.READ:
            return self._check_path(name, raw_path, self._phase_policy.allowed_read_paths, "read")
        if metadata.path_access == PathAccess.WRITE:
            return self._check_path(name, raw_path, self._phase_policy.allowed_write_paths, "write")
        if metadata.path_access == PathAccess.DIFF and self._phase_policy.strict_file_scope:
            allowed = self._phase_policy.allowed_write_paths or self._phase_policy.allowed_read_paths
            if not raw_path:
                return f"{name} is blocked by task policy unless a permitted path is provided."
            if allowed is not None:
                return self._check_path(name, raw_path, allowed, "diff")
            return None
        if metadata.path_access == PathAccess.DISCOVER and self._phase_policy.allowed_read_paths is not None:
            return self._check_path(name, raw_path, self._phase_policy.allowed_read_paths, "search")
        return None

    def _check_path(
        self,
        tool_name: str,
        raw_path: Any,
        allowed_paths: frozenset[str] | None,
        action: str,
    ) -> str | None:
        if allowed_paths is None:
            return None
        requested = normalize_repo_path(str(raw_path or ""), self._repo_path)
        if requested in allowed_paths:
            return None
        return (
            f"[RUNTIME BLOCK] PATH ACCESS DENIED: '{requested}' is outside the "
            f"allowed {action} scope. You MUST choose a path within: "
            f"{', '.join(sorted(allowed_paths)) or '(none)'}. "
            f"This is a hard Runtime constraint — not a suggestion."
        )
