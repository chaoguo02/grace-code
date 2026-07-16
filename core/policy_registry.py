"""Policy-aware ToolRegistry wrapper."""

from __future__ import annotations

import time
from typing import Any

from core.policy import PhasePolicy, normalize_repo_path
from core.base import (
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

    # ── SK-05 / SK-06: Skill tool restrictions ──────────────────────

    def with_skill_restrictions(self, skill) -> "PolicyAwareToolRegistry":
        """Apply a skill's allowed-tools and disallowed-tools to this registry.

        SK-05: allowed-tools grants pre-approval (the listed tools don't prompt).
        SK-06: disallowed-tools removes tools from the available pool while active.

        Returns a new PolicyAwareToolRegistry with restrictions layered on.
        """
        result = self
        if skill.allowed_tools:
            result = result.with_allowed_tools(skill.allowed_tools)
        if skill.disallowed_tools:
            result = result._with_disallowed_tools(skill.disallowed_tools)
        return result

    def _with_disallowed_tools(self, disallowed: frozenset[str]) -> "PolicyAwareToolRegistry":
        """Return a registry with additional denied tools (SK-06)."""
        return PolicyAwareToolRegistry(
            base=self._base,
            phase_policy=self._phase_policy.with_denied_tools(disallowed),
            repo_path=self._repo_path,
            phase_name=self._phase_name,
            base_allowed_tools=self._base_allowed_tools,
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
        )

    def scoped(self, context: ExecutionContext) -> "PolicyAwareToolRegistry":
        """Rebind workspace-aware tools without losing phase authority."""
        return PolicyAwareToolRegistry(
            base=self._base.scoped(context),
            phase_policy=self._phase_policy,
            repo_path=context.repo_path or context.workspace_root,
            phase_name=self._phase_name,
            base_allowed_tools=self._base_allowed_tools,
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
        # Consume CC-aligned SkillContextModifier from tool result metadata
        if result.metadata and "skill_modifier" in result.metadata:
            self._apply_skill_modifier(result.metadata["skill_modifier"])
        self._record_timing(name, start, result)
        return result

    def _apply_skill_modifier(self, modifier) -> None:
        """Apply skill contextModifier: update PhasePolicy for SK-05/SK-06."""
        from skills.tool import SkillContextModifier
        if not isinstance(modifier, SkillContextModifier):
            return
        if modifier.allowed_tools or modifier.disallowed_tools:
            fake_skill = type("_Skill", (), {
                "allowed_tools": modifier.allowed_tools,
                "disallowed_tools": modifier.disallowed_tools,
            })()
            new_policy = self._phase_policy
            if modifier.allowed_tools:
                new_policy = new_policy.with_allowed_tools(modifier.allowed_tools)
            if modifier.disallowed_tools:
                new_policy = new_policy.with_denied_tools(modifier.disallowed_tools)
            # Rebuild registry with new policy
            rebuilt = PolicyAwareToolRegistry(
                base=self._base,
                phase_policy=new_policy,
                repo_path=self._repo_path,
                phase_name=self._phase_name,
                base_allowed_tools=self._base_allowed_tools,
            )
            self._phase_policy = rebuilt._phase_policy

    def _check_tool_call(self, name: str, params: dict[str, Any]) -> str | None:
        # ── Scoped rules (Claude Code pattern: Deny→Allow order) ──
        scoped_verdict = self._phase_policy.check_scoped_rules(name, params)
        if scoped_verdict is not None:
            return scoped_verdict

        # ── Permission mode check (CC-aligned) ──
        # Plan mode: only read-only tools allowed. Writing tools are still
        # visible to the model (registered in schema) but blocked at call time.
        # This is the same approach as CC: the model sees the tool definition
        # but gets a permission error when it tries to use it.
        if self._phase_policy.is_tool_blocked_by_permission_mode(name):
            return (
                f"Permission denied: '{name}' is blocked by permission mode "
                f"'{self._phase_policy.permission_mode}'. "
                f"Available tools: {', '.join(self.tool_names)}. "
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
