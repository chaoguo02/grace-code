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
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any, Callable

from hitl.permission_rule import PermissionRule, PermissionRuleTier
from hitl.settings_loader import save_rule_to_settings

if TYPE_CHECKING:
    from tools.base import BaseTool


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class ToolApprovalMode(str, Enum):
    PROMPT = "prompt"
    AUTO = "auto"


class PermissionLayer(IntEnum):
    NOT_APPLICABLE = 0
    INPUT_VALIDATION = 1
    PRE_TOOL_HOOK = 2
    RULE = 3
    INTERACTIVE = 4
    TOOL_CHECK = 5


class PromptAction(str, Enum):
    ALLOW_ONCE = "allow_once"
    ALWAYS_ALLOW = "always_allow"
    DENY = "deny"


@dataclass
class PermissionResult:
    decision: PermissionDecision
    layer: PermissionLayer = PermissionLayer.NOT_APPLICABLE
    reason: str = ""
    feedback: str = ""
    rule: PermissionRule | None = None
    wait_ms: float = 0.0

    def __post_init__(self) -> None:
        self.decision = PermissionDecision(self.decision)
        self.layer = PermissionLayer(self.layer)

    @property
    def approved(self) -> bool:
        """Compatibility view; Runtime control flow uses ``decision``."""
        return self.decision is PermissionDecision.ALLOW


@dataclass
class PermissionRequest:
    """Passed to the interactive prompt callback."""
    tool_name: str
    params: dict[str, Any]
    thought: str = ""


@dataclass
class PromptDecision:
    """Returned from the interactive prompt callback."""
    action: PromptAction
    note: str = ""
    inferred_rule: PermissionRule | None = None

    def __post_init__(self) -> None:
        self.action = PromptAction(self.action)


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
        if result.decision is PermissionDecision.ALLOW:
            self.allowed += 1
        else:
            self.denied += 1
        if result.layer is PermissionLayer.INTERACTIVE:
            self.prompted += 1
        elif result.layer is PermissionLayer.PRE_TOOL_HOOK:
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
        hook_dispatcher: Any = None,
        confirm_callback: ConfirmCallback | None = None,
        approval_mode: ToolApprovalMode = ToolApprovalMode.PROMPT,
        settings_path: str | None = None,
        project_root: str | None = None,
        circuit_breaker: Any = None,
    ) -> None:
        self._deny_rules: list[PermissionRule] = []
        self._ask_rules: list[PermissionRule] = []
        self._allow_rules: list[PermissionRule] = []
        self._hook_dispatcher = hook_dispatcher
        self._confirm_callback = confirm_callback
        self._approval_mode = ToolApprovalMode(approval_mode)
        self._settings_path = settings_path
        self._project_root = project_root
        self._session_rules: list[PermissionRule] = []
        self._stats = PipelineStats()
        self._circuit_breaker = circuit_breaker

        for r in (rules or []):
            if r.tier is PermissionRuleTier.DENY:
                self._deny_rules.append(r)
            elif r.tier is PermissionRuleTier.ASK:
                self._ask_rules.append(r)
            else:
                self._allow_rules.append(r)

    def set_circuit_breaker(self, circuit_breaker: Any) -> None:
        """Inject a CircuitBreaker after construction (session-scoped)."""
        self._circuit_breaker = circuit_breaker

    def scoped(self, project_root: str) -> "PermissionPipeline":
        """Clone session-local state and bind path checks to an effective project."""
        import copy

        scoped = copy.copy(self)
        scoped._project_root = os.path.abspath(project_root)
        scoped._session_rules = list(self._session_rules)
        scoped._stats = PipelineStats()
        return scoped

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
        result = self._layer1_validate(tool, params)
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

        if rule_decision is PermissionRuleTier.DENY:
            result = PermissionResult(
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.RULE,
                reason="denied by rule",
            )
            self._stats.record(result)
            return result
        elif rule_decision is PermissionRuleTier.ALLOW:
            # Layer 5 still applies even for allowed rules
            result = self._layer5_check(tool, params)
            if result is not None:
                self._stats.record(result)
                return result
            result = PermissionResult(
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.RULE,
                reason="allowed by rule",
            )
            self._stats.record(result)
            return result

        # rule_decision == "ask" → Layer 4
        result = self._layer4_prompt(tool_name, params, thought)

        # If Layer 4 approves, still run Layer 5
        if result.decision is PermissionDecision.ALLOW:
            l5 = self._layer5_check(tool, params)
            if l5 is not None:
                self._stats.record(l5)
                return l5

        self._stats.record(result)

        # ── Circuit breaker: track denial rhythm ──
        if self._circuit_breaker is not None:
            if result.decision is PermissionDecision.ALLOW:
                self._circuit_breaker.record_approval()
            else:
                self._circuit_breaker.record_denial()

        return result

    # ─── Layer 1: validateInput ────────────────────────────────────────

    def _layer1_validate(
        self, tool: "BaseTool", params: dict[str, Any]
    ) -> PermissionResult | None:
        """Absolute safety floor. Cannot be overridden by rules or hooks."""
        reason = tool.permission_denial_reason(params)
        if reason:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.INPUT_VALIDATION,
                reason=reason,
            )
        return None

    # ─── Layer 2: PreToolUse Hooks ─────────────────────────────────────

    def _layer2_hooks(self, tool_name: str, params: dict[str, Any]) -> PermissionResult | None:
        """Run PreToolUse hooks via HookDispatcher. Exit 0=approve, 2=deny."""
        if self._hook_dispatcher is None:
            return None

        from hooks.events import HookContext, HookEvent

        ctx = HookContext(
            event=HookEvent.PRE_TOOL_USE,
            tool_name=tool_name,
            tool_input=params,
        )
        dispatch_result = self._hook_dispatcher.dispatch(HookEvent.PRE_TOOL_USE, ctx)
        from hooks.protocol import HookControl
        if dispatch_result.control is HookControl.BLOCK:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.PRE_TOOL_HOOK,
                reason=dispatch_result.reason or "Blocked by hook",
            )
        if dispatch_result.control is HookControl.APPROVE:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.PRE_TOOL_HOOK,
                reason="Hook approved",
            )
        return None

    # ─── Layer 3: Permission Rules ─────────────────────────────────────

    def _layer3_rules(
        self, tool_name: str, params: dict[str, Any]
    ) -> PermissionRuleTier:
        """
        Match rules with priority: deny > session_allow > ask > allow.

        Session rules (from "Always Allow") take precedence over static ask rules
        because they represent explicit user confirmation during this session.
        Static deny rules always win (safety invariant).
        """
        # Check deny rules first (highest priority, safety invariant)
        for rule in self._deny_rules:
            if rule.matches(tool_name, params):
                return PermissionRuleTier.DENY

        # Session rules from "Always Allow" override static ask rules
        for rule in self._session_rules:
            if rule.matches(tool_name, params):
                return PermissionRuleTier.ALLOW

        # Check ask rules
        for rule in self._ask_rules:
            if rule.matches(tool_name, params):
                return PermissionRuleTier.ASK

        # Check static allow rules
        for rule in self._allow_rules:
            if rule.matches(tool_name, params):
                return PermissionRuleTier.ALLOW

        # No rule matched → default is "ask"
        return PermissionRuleTier.ASK

    # ─── Layer 4: Interactive Prompt ───────────────────────────────────

    def _layer4_prompt(
        self, tool_name: str, params: dict[str, Any], thought: str
    ) -> PermissionResult:
        """3-way interactive prompt or auto-approve bypass."""
        if self._approval_mode is ToolApprovalMode.AUTO:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.INTERACTIVE,
                reason="auto_approve",
            )

        if self._confirm_callback is None:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.INTERACTIVE,
                reason="no callback (headless)",
            )

        request = PermissionRequest(tool_name=tool_name, params=params, thought=thought)

        t0 = time.time()
        decision = self._confirm_callback(request)
        wait_ms = (time.time() - t0) * 1000

        if decision.action is PromptAction.ALWAYS_ALLOW:
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
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.INTERACTIVE,
                reason="always_allow",
                wait_ms=wait_ms,
            )
        elif decision.action is PromptAction.ALLOW_ONCE:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.INTERACTIVE,
                reason="allow_once",
                wait_ms=wait_ms,
            )
        else:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.INTERACTIVE,
                reason="denied_by_user",
                feedback=decision.note, wait_ms=wait_ms,
            )

    # ─── Layer 5: checkPermissions ─────────────────────────────────────

    def _layer5_check(
        self, tool: "BaseTool", params: dict[str, Any]
    ) -> PermissionResult | None:
        """Tool-specific checks: path sandbox enforcement."""
        from tools.base import PathAccess

        metadata = tool.metadata
        if (
            self._project_root
            and metadata.path_access is PathAccess.WRITE
            and metadata.path_parameter
        ):
            path = params.get(metadata.path_parameter, "")
            if path:
                abs_root = os.path.normcase(os.path.abspath(self._project_root))
                abs_path = os.path.normcase(os.path.abspath(
                    path if os.path.isabs(path) else os.path.join(abs_root, path)
                ))
                # Ensure the path is within project root (with separator boundary)
                if not (abs_path == abs_root or abs_path.startswith(abs_root + os.sep)):
                    return PermissionResult(
                        decision=PermissionDecision.DENY,
                        layer=PermissionLayer.TOOL_CHECK,
                        reason=f"Path sandbox: '{path}' is outside project root",
                    )
        return None
