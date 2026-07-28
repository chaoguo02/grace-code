"""Policy-aware ToolRegistry wrapper."""

from __future__ import annotations

import re as _re
import time
from typing import Any

from core.policy import PhasePolicy, normalize_repo_path
from core.base import (
    ExecutionContext,
    PathAccess,
    ToolDependency,
    ToolEffect,
    ToolMetadata,
    ToolOutcome,
    ToolRegistry,
    ToolResult,
)


def _extract_shell_file_targets(command: str, args: list[str]) -> list[tuple[str, str]]:
    """Extract file paths targeted by shell redirections and common commands.

    Returns a list of (path, direction) tuples where direction is 'read' or 'write'.
    This allows the policy layer to apply allowed_read_paths to read targets
    and allowed_write_paths to write targets independently.
    """
    targets: list[tuple[str, str]] = []
    _full = f"{command} {' '.join(str(a) for a in args)}"

    # Output redirections: > file, >> file, 2> file, 1> file, &> file
    for m in _re.finditer(r'(?:[12]?&?>>?)\s*(\S+)', _full):
        _path = m.group(1).strip('"\'"')
        if _path and not _path.startswith('(') and not _path.startswith('/dev/'):
            targets.append((_path, 'write'))

    # Input redirections: < file
    for m in _re.finditer(r'(?<!\d)<\s*(\S+)', _full):
        _path = m.group(1).strip('"\'"')
        if _path and not _path.startswith('/dev/'):
            targets.append((_path, 'read'))

    # Pipe-to-file: tee file
    for m in _re.finditer(r'\btee\s+(\S+)', _full):
        _path = m.group(1).strip('"\'"')
        if _path and not _path.startswith('-'):
            targets.append((_path, 'write'))

    # dd output: dd of=/path/to/file
    for m in _re.finditer(r'\bdd\b.*?\bof=(\S+)', _full):
        _path = m.group(1).strip('"\'"')
        if _path and not _path.startswith('/dev/'):
            targets.append((_path, 'write'))

    # Common read commands: target is the last non-flag argument
    # Common read commands: target is the LAST non-flag argument before
    # the first shell redirection marker (>, >>, <, |).
    _READ_CMDS = {'cat', 'head', 'tail', 'less', 'more', 'wc'}
    _cmd_base = command.split()[0] if command.strip() else ""
    if _cmd_base in _READ_CMDS:
        _parts = _full.split()
        # Find first redirection token boundary
        _first_redir = len(_parts)
        for _i, _tok in enumerate(_parts):
            if _tok.startswith(('>', '<', '|')) or _tok in {'tee', 'dd'}:
                _first_redir = _i
                break
        # Only scan tokens before the first redirection
        for _p in reversed(_parts[1:_first_redir]):
            if not _p.startswith('-'):
                targets.append((_p.strip('"').strip("'"), 'read'))
                break

    # Common destructive commands: target is the LAST non-flag argument
    # before the first shell redirection marker.
    _DESTRUCTIVE_CMDS = {'rm', 'rmdir', 'chmod', 'chown', 'mv', 'cp'}
    _cmd_base = command.split()[0] if command.strip() else ""
    if _cmd_base in _DESTRUCTIVE_CMDS:
        _parts = _full.split()
        _first_redir = len(_parts)
        for _i, _tok in enumerate(_parts):
            if _tok.startswith(('>', '<', '|')) or _tok in {'tee', 'dd'}:
                _first_redir = _i
                break
        for _p in reversed(_parts[1:_first_redir]):
            if not _p.startswith('-'):
                targets.append((_p.strip('"').strip("'"), 'write'))
                break

    return targets


class PolicyAwareToolRegistry(ToolRegistry):
    """ToolRegistry wrapper that applies a phase-specific task policy."""

    def __init__(
        self,
        base: ToolRegistry,
        phase_policy: PhasePolicy,
        repo_path: str,
        phase_name: str,
        base_allowed_tools: set[str] | frozenset[str] | None = None,
        modifier_owner: "PolicyAwareToolRegistry | None" = None,
    ) -> None:
        super().__init__(hook_dispatcher=base.hook_dispatcher)
        self._base = base
        self._phase_policy = phase_policy
        self._repo_path = repo_path
        self._phase_name = phase_name
        self._base_allowed_tools = frozenset(base_allowed_tools) if base_allowed_tools is not None else None
        self._modifier_owner = modifier_owner
        self._skill_runtime_overrides: dict[str, str] = {}
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

    def configure_permission_session(self, config: Any) -> None:
        self._base.configure_permission_session(config)

    def permission_control_signal(self) -> Any:
        return self._base.permission_control_signal()

    def attach_hook_dispatcher(self, dispatcher: Any) -> None:
        self._base.attach_hook_dispatcher(dispatcher)

    @property
    def hook_dispatcher(self) -> Any:
        return self._base.hook_dispatcher

    def permission_inheritable_state(self) -> dict:
        return self._base.permission_inheritable_state()

    def apply_inherited_permission_state(
        self, state: dict, *, child_permission_mode: str,
    ) -> None:
        self._base.apply_inherited_permission_state(
            state,
            child_permission_mode=child_permission_mode,
        )

    def with_allowed_tools(self, allowed_tools: set[str] | frozenset[str]) -> "PolicyAwareToolRegistry":
        return PolicyAwareToolRegistry(
            base=self._base,
            phase_policy=self._phase_policy.with_allowed_tools(allowed_tools),
            repo_path=self._repo_path,
            phase_name=self._phase_name,
            base_allowed_tools=allowed_tools,
            modifier_owner=self._modifier_owner or self,
        )

    def with_pre_approved_tools(
        self, tools: set[str] | frozenset[str],
    ) -> "PolicyAwareToolRegistry":
        """Return a registry with additional approval-free tools."""
        return PolicyAwareToolRegistry(
            base=self._base,
            phase_policy=self._phase_policy.with_pre_approved_tools(
                frozenset(tools),
            ),
            repo_path=self._repo_path,
            phase_name=self._phase_name,
            base_allowed_tools=self._base_allowed_tools,
            modifier_owner=self._modifier_owner or self,
        )

    # ── SK-05 / SK-06: Skill tool restrictions ──────────────────────

    def with_skill_restrictions(self, skill) -> "PolicyAwareToolRegistry":
        """Apply a skill's allowed-tools and disallowed-tools to this registry.

        SK-05: allowed-tools grants pre-approval (the listed tools don't prompt).
        SK-06: disallowed-tools removes tools from the available pool while active.

        Returns a new PolicyAwareToolRegistry with restrictions layered on.
        """
        result = self
        # CC-aligned: allowed-tools = pre-approve (skip prompt), NOT filter visibility
        if skill.allowed_tools:
            result = result.with_pre_approved_tools(skill.allowed_tools)
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
            modifier_owner=self._modifier_owner or self,
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
            modifier_owner=self._modifier_owner or self,
        )

    def scoped(self, context: ExecutionContext) -> "PolicyAwareToolRegistry":
        """Rebind workspace-aware tools without losing phase authority."""
        return PolicyAwareToolRegistry(
            base=self._base.scoped(context),
            phase_policy=self._phase_policy,
            repo_path=context.repo_path or context.workspace_root,
            phase_name=self._phase_name,
            base_allowed_tools=self._base_allowed_tools,
            modifier_owner=self._modifier_owner or self,
        )

    def _is_tool_enabled(self, name: str) -> bool:
        metadata = self._base.metadata_for(name)
        if metadata is None:
            return False
        if metadata.dependency == ToolDependency.ARTIFACT_STORE:
            return self._artifact_store_ref is not None and self._artifact_store_ref.store is not None
        if metadata.dependency == ToolDependency.EVIDENCE_LEDGER:
            return self._evidence_ledger_ref is not None and self._evidence_ledger_ref.ledger is not None
        return True

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

    def get_schemas(self):
        schemas = [
            schema
            for name, tool in self._tools.items()
            if self._is_tool_visible(name) and self._is_tool_enabled(name)
            if not (schema := tool.to_llm_schema()).deferred
        ]
        schemas.sort(key=lambda s: s.name)
        return schemas

    @property
    def tool_names(self) -> list[str]:
        return [
            name for name in self._tools.keys()
            if self._is_tool_visible(name) and self._is_tool_enabled(name)
        ]

    def execute_tool(
        self,
        name: str,
        params: dict[str, Any],
        thought: str = "",
        *,
        invocation_id: str = "",
    ) -> ToolResult:
        start = time.perf_counter()
        violation = self._check_tool_call(name, params)
        if violation:
            result = ToolResult(success=False, output="", error=violation, outcome=ToolOutcome.BLOCKED)
            self._record_timing(name, start, result)
            return result
        result = self._base.execute_tool(
            name,
            params,
            thought=thought,
            invocation_id=invocation_id,
        )
        # Consume CC-aligned SkillContextModifier from tool result metadata
        if result.metadata and "skill_modifier" in result.metadata:
            self._apply_skill_modifier(result.metadata["skill_modifier"])
        self._record_timing(name, start, result)
        return result

    def _apply_skill_modifier(self, modifier) -> None:
        """Persist a skill modifier on this run's registry and bound clones."""
        from skills.tool import SkillContextModifier
        if not isinstance(modifier, SkillContextModifier):
            return

        owner = self._modifier_owner or self
        targets = (self,) if owner is self else (self, owner)
        for target in targets:
            new_policy = target._phase_policy
            if modifier.allowed_tools:
                new_policy = new_policy.with_pre_approved_tools(modifier.allowed_tools)
            if modifier.disallowed_tools:
                new_policy = new_policy.with_denied_tools(modifier.disallowed_tools)
            target._phase_policy = new_policy
            target._skill_runtime_overrides = {
                **target._skill_runtime_overrides,
                **{
                    key: value
                    for key, value in {
                        "model": modifier.model,
                        "effort": modifier.effort,
                        "context": modifier.context,
                    }.items()
                    if value
                },
            }

    @property
    def skill_runtime_overrides(self) -> dict[str, str]:
        """Active non-policy Skill overrides for the current run."""
        owner = self._modifier_owner or self
        return dict(owner._skill_runtime_overrides)

    def _check_tool_call(self, name: str, params: dict[str, Any]) -> str | None:
        # ── Scoped rules (Claude Code pattern: Deny→Allow order) ──
        scoped_verdict = self._phase_policy.check_scoped_rules(name, params)
        if scoped_verdict is not None:
            return scoped_verdict

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

        # ── Bash command target extraction (defense-in-depth) ──
        # Shell commands are opaque to the policy layer by default.
        # Extract file targets from shell redirections so strict_file_scope
        # and allowed_write_paths can constrain Bash side-effects.
        if name == "Bash" and self._phase_policy.strict_file_scope:
            _cmd = str(params.get("command", "") or "")
            _args = params.get("args", []) or []
            _targets = _extract_shell_file_targets(_cmd, _args)

            # ── Read path check (C0, symmetric with B1 write check) ──
            _read_allowed = self._phase_policy.allowed_read_paths
            for _target, _direction in _targets:
                if _direction != 'read':
                    continue
                _normalized = normalize_repo_path(_target, self._repo_path)
                if _read_allowed is not None and _normalized not in _read_allowed:
                    return (
                        f"[RUNTIME BLOCK] BASH READ PATH DENIED: '{_normalized}' is "
                        f"outside the allowed read scope. "
                        f"Allowed: {', '.join(sorted(_read_allowed)) or '(none)'}. "
                        f"Use Read or Grep for files within the allowed scope."
                    )

            # ── Write path check (B1, preserved) ──
            _write_allowed = self._phase_policy.allowed_write_paths
            for _target, _direction in _targets:
                if _direction != 'write':
                    continue
                _normalized = normalize_repo_path(_target, self._repo_path)
                if _write_allowed is not None and _normalized not in _write_allowed:
                    return (
                        f"[RUNTIME BLOCK] BASH PATH DENIED: '{_normalized}' is "
                        f"outside the allowed write scope in strict_file_scope mode. "
                        f"Allowed: {', '.join(sorted(_write_allowed)) or '(none)'}"
                    )
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
