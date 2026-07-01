"""
hitl/pipeline.py

PermissionPipeline — 5-layer tool permission evaluation.

Layer 1: validateInput()       — L0 safety blacklist (absolute floor, not overridable)
Layer 2: PreToolUse Hooks      — user-defined shell scripts
Layer 3: Permission Rules      — deny > ask > allow with Tool(pattern) glob syntax
Layer 4: Interactive Prompt    — Allow Once / Always Allow / Deny (3-way)
Layer 5: checkPermissions()    — tool-specific checks (path sandbox)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from hitl.hooks import HookConfig, run_hook
from hitl.permission_rule import PermissionRule
from hitl.settings_loader import save_rule_to_settings

if TYPE_CHECKING:
    from tools.base import BaseTool


@dataclass
class PermissionResult:
    approved: bool
    layer: int = 0
    reason: str = ""
    feedback: str = ""
    rule: PermissionRule | None = None
    wait_ms: float = 0.0


@dataclass
class PermissionRequest:
    """Passed to the interactive prompt callback."""
    tool_name: str
    params: dict[str, Any]
    thought: str = ""


@dataclass
class PromptDecision:
    """Returned from the interactive prompt callback."""
    action: str  # "allow_once" | "always_allow" | "deny"
    note: str = ""
    inferred_rule: PermissionRule | None = None


# Type for the 3-way confirm callback
ConfirmCallback = Callable[[PermissionRequest], PromptDecision]


@dataclass
class PipelineStats:
    total: int = 0
    allowed: int = 0
    denied: int = 0
    prompted: int = 0
    hook_decided: int = 0
    total_wait_ms: float = 0.0

    def record(self, result: PermissionResult) -> None:
        self.total += 1
        if result.approved:
            self.allowed += 1
        else:
            self.denied += 1
        if result.layer == 4:
            self.prompted += 1
        elif result.layer == 2:
            self.hook_decided += 1
        self.total_wait_ms += result.wait_ms


class PermissionPipeline:
    """
    5-layer permission evaluation pipeline aligned with Claude Code's
    ToolPermissionPipeline architecture.
    """

    def __init__(
        self,
        *,
        rules: list[PermissionRule] | None = None,
        hooks: list[HookConfig] | None = None,
        confirm_callback: ConfirmCallback | None = None,
        auto_approve: bool = False,
        settings_path: str | None = None,
        project_root: str | None = None,
    ) -> None:
        self._deny_rules: list[PermissionRule] = []
        self._ask_rules: list[PermissionRule] = []
        self._allow_rules: list[PermissionRule] = []
        self._hooks = hooks or []
        self._confirm_callback = confirm_callback
        self._auto_approve = auto_approve
        self._settings_path = settings_path
        self._project_root = project_root
        self._session_rules: list[PermissionRule] = []
        self._stats = PipelineStats()

        for r in (rules or []):
            if r.tier == "deny":
                self._deny_rules.append(r)
            elif r.tier == "ask":
                self._ask_rules.append(r)
            else:
                self._allow_rules.append(r)

    @property
    def stats(self) -> PipelineStats:
        return self._stats

    @property
    def session_rules(self) -> list[PermissionRule]:
        return list(self._session_rules)

    def check(
        self,
        tool: "BaseTool",
        params: dict[str, Any],
        thought: str = "",
    ) -> PermissionResult:
        """Run the 5-layer pipeline. Returns PermissionResult."""
        tool_name = tool.name

        # Layer 1: validateInput — absolute safety floor
        result = self._layer1_validate(tool_name, params)
        if result is not None:
            self._stats.record(result)
            return result

        # Layer 2: PreToolUse Hooks
        result = self._layer2_hooks(tool_name, params)
        if result is not None:
            self._stats.record(result)
            return result

        # Layer 3: Permission Rules (deny > ask > allow)
        rule_decision = self._layer3_rules(tool_name, params)

        if rule_decision == "deny":
            result = PermissionResult(approved=False, layer=3, reason="denied by rule")
            self._stats.record(result)
            return result
        elif rule_decision == "allow":
            # Layer 5 still applies even for allowed rules
            result = self._layer5_check(tool_name, params)
            if result is not None:
                self._stats.record(result)
                return result
            result = PermissionResult(approved=True, layer=3, reason="allowed by rule")
            self._stats.record(result)
            return result

        # rule_decision == "ask" → Layer 4
        result = self._layer4_prompt(tool_name, params, thought)

        # If Layer 4 approves, still run Layer 5
        if result.approved:
            l5 = self._layer5_check(tool_name, params)
            if l5 is not None:
                self._stats.record(l5)
                return l5

        self._stats.record(result)
        return result

    # ─── Layer 1: validateInput ────────────────────────────────────────

    def _layer1_validate(self, tool_name: str, params: dict[str, Any]) -> PermissionResult | None:
        """Absolute safety floor. Cannot be overridden by rules or hooks."""
        if tool_name.lower() == "shell":
            from tools.shell_tool import _check_blocked
            cmd = params.get("cmd", "")
            blocked = _check_blocked(cmd)
            if blocked:
                return PermissionResult(
                    approved=False, layer=1,
                    reason=f"Blocked by safety floor: matched '{blocked}'",
                )
            # Also block null bytes and excessively long commands
            if "\x00" in cmd or len(cmd) > 10_000:
                return PermissionResult(
                    approved=False, layer=1,
                    reason="Blocked: malicious input detected",
                )
        return None

    # ─── Layer 2: PreToolUse Hooks ─────────────────────────────────────

    def _layer2_hooks(self, tool_name: str, params: dict[str, Any]) -> PermissionResult | None:
        """Run user-defined shell hooks. Hook exit codes: 0=approve, 1=abstain, 2=deny."""
        for hook in self._hooks:
            if hook.matches(tool_name, params):
                hook_result = run_hook(hook, tool_name, params, cwd=self._project_root)
                if hook_result.approves:
                    return PermissionResult(approved=True, layer=2, reason="Hook approved")
                elif hook_result.denies:
                    return PermissionResult(approved=False, layer=2, reason="Hook denied")
                # abstains → continue
        return None

    # ─── Layer 3: Permission Rules ─────────────────────────────────────

    def _layer3_rules(self, tool_name: str, params: dict[str, Any]) -> str:
        """
        Match rules with priority: deny > session_allow > ask > allow.

        Session rules (from "Always Allow") take precedence over static ask rules
        because they represent explicit user confirmation during this session.
        Static deny rules always win (safety invariant).
        """
        # Check deny rules first (highest priority, safety invariant)
        for rule in self._deny_rules:
            if rule.matches(tool_name, params):
                return "deny"

        # Session rules from "Always Allow" override static ask rules
        for rule in self._session_rules:
            if rule.matches(tool_name, params):
                return "allow"

        # Check ask rules
        for rule in self._ask_rules:
            if rule.matches(tool_name, params):
                return "ask"

        # Check static allow rules
        for rule in self._allow_rules:
            if rule.matches(tool_name, params):
                return "allow"

        # No rule matched → default is "ask"
        return "ask"

    # ─── Layer 4: Interactive Prompt ───────────────────────────────────

    def _layer4_prompt(
        self, tool_name: str, params: dict[str, Any], thought: str
    ) -> PermissionResult:
        """3-way interactive prompt or auto-approve bypass."""
        if self._auto_approve:
            return PermissionResult(approved=True, layer=4, reason="auto_approve")

        if self._confirm_callback is None:
            return PermissionResult(approved=True, layer=4, reason="no callback (headless)")

        request = PermissionRequest(tool_name=tool_name, params=params, thought=thought)

        t0 = time.time()
        decision = self._confirm_callback(request)
        wait_ms = (time.time() - t0) * 1000

        if decision.action == "always_allow":
            rule = decision.inferred_rule
            if rule is None:
                from hitl.pattern_inference import infer_permission_pattern
                rule = infer_permission_pattern(tool_name, params)
            self._session_rules.append(rule)
            if self._settings_path:
                try:
                    save_rule_to_settings(self._settings_path, rule)
                except Exception:
                    pass
            return PermissionResult(
                approved=True, layer=4, reason="always_allow", wait_ms=wait_ms
            )
        elif decision.action == "allow_once":
            return PermissionResult(
                approved=True, layer=4, reason="allow_once", wait_ms=wait_ms
            )
        else:
            return PermissionResult(
                approved=False, layer=4, reason="denied_by_user",
                feedback=decision.note, wait_ms=wait_ms,
            )

    # ─── Layer 5: checkPermissions ─────────────────────────────────────

    def _layer5_check(self, tool_name: str, params: dict[str, Any]) -> PermissionResult | None:
        """Tool-specific checks: path sandbox enforcement."""
        if self._project_root and tool_name.lower() in ("file_write", "file_edit"):
            path = params.get("path", "")
            if path:
                abs_path = os.path.normcase(os.path.abspath(path))
                abs_root = os.path.normcase(os.path.abspath(self._project_root))
                # Ensure the path is within project root (with separator boundary)
                if not (abs_path == abs_root or abs_path.startswith(abs_root + os.sep)):
                    return PermissionResult(
                        approved=False, layer=5,
                        reason=f"Path sandbox: '{path}' is outside project root",
                    )
        return None
