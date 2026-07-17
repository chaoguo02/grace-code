"""
hitl/pipeline.py

PermissionPipeline - 5-layer tool permission evaluation.

Layer 1: validateInput()       - L0 safety blacklist (absolute floor, not overridable)
Layer 2: PreToolUse Hooks      - user-defined shell scripts
Layer 3: Permission Rules      - deny > ask > allow with Tool(pattern) glob syntax
Layer 4: Interactive Prompt    - Allow Once / Always Allow / Deny (3-way)
Layer 5: checkPermissions()    - tool-specific checks (path sandbox)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import Enum, IntEnum
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable

from hitl.permission_rule import PermissionRule, PermissionRuleTier
from hitl.settings_loader import save_rule_to_settings

if TYPE_CHECKING:
    from core.base import BaseTool


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
    updated_params: dict[str, Any] | None = None

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
    agent_name: str = ""


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
        self._requesting_agent = ""
        self._prompt_lock = RLock()
        self._permission_mode: str = ""
        self._pre_plan_mode: str = ""
        self._denial_counters: dict[str, int] = {}
        self._total_denials: int = 0

        for r in (rules or []):
            if r.tier is PermissionRuleTier.DENY:
                self._deny_rules.append(r)
            elif r.tier is PermissionRuleTier.ASK:
                self._ask_rules.append(r)
            else:
                self._allow_rules.append(r)

    def set_permission_mode(self, mode: str) -> None:
        """Set the active permission mode (CC-aligned Step 4)."""
        self._permission_mode = mode

    def save_pre_plan_mode(self) -> None:
        """Save current mode before entering plan (CC-aligned prePlanMode)."""
        self._pre_plan_mode = self._permission_mode

    def restore_pre_plan_mode(self) -> str:
        """Restore mode after exiting plan. Returns the restored mode."""
        restored = self._pre_plan_mode or self._permission_mode
        self._permission_mode = restored
        self._pre_plan_mode = ""
        return restored

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

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
        scoped._permission_mode = self._permission_mode
        return scoped

    def for_agent(self, agent_name: str) -> "PermissionPipeline":
        """Derive a child view while retaining the shared interactive channel."""
        import copy

        derived = copy.copy(self)
        derived._requesting_agent = agent_name.strip()
        derived._session_rules = list(self._session_rules)
        derived._stats = PipelineStats()
        derived._permission_mode = self._permission_mode
        return derived

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
        """CC-aligned 6-step permission evaluation.

        if self._total_denials >= 20:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.RULE,
                reason=f"Session denial limit (20) reached. Tool call blocked. You MUST review and change your approach.",
            )

        Step 1: validateInput - absolute safety floor
        Step 2: PreToolUse Hooks
        Step 3: Deny Rules + Ask Rules
        Step 4: Permission Mode
        Step 5: Allow Rules
        Step 6: canUseTool Callback
        """
        tool_name = tool.name

        # Step 1: validateInput
        result = self._layer1_validate(tool, params)
        if result is not None:
            self._stats.record(result)
            return result

        # Step 2: PreToolUse Hooks
        result = self._layer2_hooks(tool_name, params)
        if result is not None:
            self._stats.record(result)
            return result

        # Step 3: Deny Rules + Ask Rules (bypass-proof)
        for rule in self._deny_rules:
            if rule.matches(tool_name, params):
                consecutive = self._denial_counters.get(tool_name, 0) + 1
                reason = f"denied by rule: {rule.raw}"
                if consecutive >= 3:
                    reason += " Tool '" + tool_name + "' has been denied " + str(consecutive) + " consecutive times. You MUST change your approach."
                if self._total_denials + 1 >= 20:
                    reason += " Total denials have reached the session limit."
                result = PermissionResult(
                    decision=PermissionDecision.DENY,
                    layer=PermissionLayer.RULE,
                    reason=reason,
                )
                self._stats.record(result)
                return result
        for rule in self._ask_rules:
            if rule.matches(tool_name, params):
                result = self._layer6_callback(tool_name, params, thought)
                self._stats.record(result)
                return self._apply_tool_check(result, tool, params)

        # Step 4: Permission Mode
        mode_result = self._layer4_permission_mode(tool_name)
        if mode_result is not None:
            self._stats.record(mode_result)
            return self._apply_tool_check(mode_result, tool, params)

        # Step 5: Allow Rules + Session Rules
        for rule in self._session_rules:
            if rule.matches(tool_name, params):
                result = PermissionResult(
                    decision=PermissionDecision.ALLOW,
                    layer=PermissionLayer.RULE,
                    reason="session allow rule",
                )
                return self._apply_tool_check(result, tool, params)
        for rule in self._allow_rules:
            if rule.matches(tool_name, params):
                result = PermissionResult(
                    decision=PermissionDecision.ALLOW,
                    layer=PermissionLayer.RULE,
                    reason=f"allowed by rule: {rule.raw}",
                )
                return self._apply_tool_check(result, tool, params)

        # Step 6: canUseTool Callback
        result = self._layer6_callback(tool_name, params, thought)
        self._stats.record(result)
        return self._apply_tool_check(result, tool, params)

    # --- Layer 1: validateInput ----------------------------------------

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

    # --- Layer 2: PreToolUse Hooks -------------------------------------

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
            result = PermissionResult(
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.PRE_TOOL_HOOK,
                reason="Hook approved",
            )
            if dispatch_result.updated_input:
                result.updated_params = dispatch_result.updated_input
            return result
        # CONTINUE: no decision, but may have updated_input from hooks
        if dispatch_result.updated_input:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.PRE_TOOL_HOOK,
                reason="Hook approved (input modified)",
                updated_params=dispatch_result.updated_input,
            )
        return None

    # --- Layer 3: Permission Rules -------------------------------------

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

        # No rule matched - default is "ask"
        return PermissionRuleTier.ASK

    # --- Layer 6: canUseTool Callback ----------------------------------

    

    def _apply_tool_check(self, result, tool, params):
        if result.decision is PermissionDecision.ALLOW:
            l5 = self._layer5_check(tool, params)
            if l5 is not None:
                self._stats.record(l5)
                return l5
        if result.decision is PermissionDecision.ALLOW:
            if getattr(self, '_circuit_breaker', None) is not None:
                self._circuit_breaker.record_approval()
        else:
            self._total_denials += 1
            if tool is not None and hasattr(tool, 'name'):
                self._denial_counters[tool.name] = self._denial_counters.get(tool.name, 0) + 1
            if getattr(self, '_circuit_breaker', None) is not None:
                self._circuit_breaker.record_denial()
        return result

    # --- Layer 4: Permission Mode (CC-aligned Step 4) ---

    def _layer4_permission_mode(self, tool_name):
        mode = self._permission_mode
        if not mode or mode == "default":
            return None
        if mode == "bypassPermissions":
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.RULE,
                reason="bypassPermissions mode",
            )
        if mode == "acceptEdits":
            if tool_name in {"Write", "Edit"}:
                return PermissionResult(
                    decision=PermissionDecision.ALLOW,
                    layer=PermissionLayer.RULE,
                    reason="acceptEdits: %s auto-approved" % tool_name,
                )
            return None
        if mode == "plan":
            if tool_name in {"Write", "Edit", "Bash"}:
                return PermissionResult(
                    decision=PermissionDecision.DENY,
                    layer=PermissionLayer.RULE,
                    reason="plan mode: %s is read-only" % tool_name,
                )
            return None
        return None

    def _layer6_callback(
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
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.INTERACTIVE,
                reason="interactive approval unavailable in headless mode",
            )

        request = PermissionRequest(
            tool_name=tool_name,
            params=params,
            thought=thought,
            agent_name=self._requesting_agent,
        )

        t0 = time.time()
        with self._prompt_lock:
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

    # --- Layer 5: checkPermissions -------------------------------------

    def _layer5_check(
        self, tool: "BaseTool", params: dict[str, Any]
    ) -> PermissionResult | None:
        """Tool-specific checks: path sandbox enforcement."""
        from core.base import PathAccess

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
