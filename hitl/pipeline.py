"""
hitl/pipeline.py

PermissionPipeline - 6-layer tool permission evaluation (CC-aligned).

Layer 1: validateInput()       - L0 safety blacklist (absolute floor, not overridable)
Layer 2: PreToolUse Hooks      - user-defined shell scripts
Layer 3: Deny Rules + Ask      - deny > ask > allow with Tool(pattern) glob syntax
Layer 4: Permission Mode       - bypassPermissions / acceptEdits / plan / dontAsk
Layer 4.5: Prompt-based Perms  - CC-aligned ExitPlanMode allowedPrompts
Layer 5: Allow Rules + Path Sandbox
Layer 6: Interactive Callback  - TTY prompt (CLI) or WebConfirmCallback (headless Web)
                                  or AUTO bypass.  All paths are SYNCHRONOUS —
                                  the agent thread blocks until a decision arrives.
                                  This is the exact equivalent of CC's stdin-blocking
                                  control_request / control_response protocol.
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
    PROMPT_APPROVED = 6  # CC-aligned ExitPlanMode allowedPrompts


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
    """Returned from the interactive prompt callback.

    CC-aligned: ``updated_params`` allows the frontend to modify tool
    parameters before execution (equivalent to CC's ``updatedInput``
    field in the ``control_response`` message).
    """
    action: PromptAction
    note: str = ""
    inferred_rule: PermissionRule | None = None
    updated_params: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.action = PromptAction(self.action)


# Type for the 3-way confirm callback (CLI/TTY mode)
ConfirmCallback = Callable[[PermissionRequest], PromptDecision]

# Type for the headless Web confirm callback.
# Same signature as ConfirmCallback, but blocks the calling thread
# internally (via threading.Event) while waiting for a frontend
# decision — the exact equivalent of CC's stdin-blocking
# control_request / control_response protocol.
WebConfirmCallback = Callable[[PermissionRequest], PromptDecision]


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
        web_confirm_callback: WebConfirmCallback | None = None,
    ) -> None:
        self._deny_rules: list[PermissionRule] = []
        self._ask_rules: list[PermissionRule] = []
        self._allow_rules: list[PermissionRule] = []
        self._hook_dispatcher = hook_dispatcher
        self._confirm_callback = confirm_callback
        self._web_confirm_callback = web_confirm_callback
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
        # CC-aligned prompt-based permissions: model-declared prompts approved during
        # plan exit, auto-allowed in the subsequent build session.
        self._approved_prompts: list[dict[str, str]] = []

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

    def add_approved_prompts(self, prompts: list[dict[str, str]]) -> None:
        """Register model-declared prompts approved during plan exit (CC-aligned).

        Each prompt is ``{"tool": "...", "prompt": "..."}``.  After plan approval
        the build agent may invoke the listed tools with matching parameters
        without interactive confirmation.
        """
        if not isinstance(prompts, list):
            return
        for item in prompts:
            if isinstance(item, dict) and "tool" in item and "prompt" in item:
                self._approved_prompts.append({
                    "tool": str(item["tool"]),
                    "prompt": str(item["prompt"]),
                })

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    def set_circuit_breaker(self, circuit_breaker: Any) -> None:
        """Inject a CircuitBreaker after construction (session-scoped)."""
        self._circuit_breaker = circuit_breaker

    # ── Parent → Child state inheritance (CC subagent permission model) ──

    def get_inheritable_state(self) -> dict:
        """Snapshot the pipeline state that subagents should inherit.

        CC-aligned: child agents inherit parent's deny/allow rules,
        permission_mode (subject to bypassPermissions/plan constraints),
        and session_rules (Always Allow decisions).

        Returns a plain dict safe to serialise and pass across threads.
        """
        return {
            "deny_rules": list(self._deny_rules),
            "allow_rules": list(self._allow_rules),
            "ask_rules": list(self._ask_rules),
            "session_rules": list(self._session_rules),
            "permission_mode": self._permission_mode,
        }

    def apply_inherited_state(
        self, state: dict, *, child_permission_mode: str,
    ) -> None:
        """Apply parent pipeline state to this (child) pipeline.

        CC rules for inheritance:
        - deny rules: ALWAYS inherited (safety invariant, child can't relax)
        - allow rules: inherited (pre-approved tools carry over)
        - session_rules: inherited (Always Allow from this session)
        - permission_mode: bypassPermissions/plan from parent is forced;
          otherwise child's own mode is used, capped by parent mode.
        """
        # Deny rules: absolute floor — child MUST inherit
        for rule in state.get("deny_rules", []):
            if rule not in self._deny_rules:
                self._deny_rules.append(rule)

        # Allow rules: inherited for convenience
        for rule in state.get("allow_rules", []):
            if rule not in self._allow_rules:
                self._allow_rules.append(rule)

        # Ask rules: inherited
        for rule in state.get("ask_rules", []):
            if rule not in self._ask_rules:
                self._ask_rules.append(rule)

        # Session rules (Always Allow): inherited
        for rule in state.get("session_rules", []):
            if rule not in self._session_rules:
                self._session_rules.append(rule)

        # Permission mode: resolved by caller (respects CC constraints)
        self._permission_mode = child_permission_mode

    def scoped(self, project_root: str) -> "PermissionPipeline":
        """Clone session-local state and bind path checks to an effective project.

        Deep-copies mutable rule lists so that modifications in the scoped
        pipeline don't leak back to the original.
        """
        import copy

        scoped = copy.copy(self)
        scoped._project_root = os.path.abspath(project_root)
        # Deep-copy mutable lists to avoid shared-state bugs
        scoped._deny_rules = list(self._deny_rules)
        scoped._ask_rules = list(self._ask_rules)
        scoped._allow_rules = list(self._allow_rules)
        scoped._session_rules = list(self._session_rules)
        scoped._stats = PipelineStats()
        scoped._permission_mode = self._permission_mode
        # _web_confirm_callback is intentionally shared (thread-safe)
        return scoped

    def for_agent(self, agent_name: str) -> "PermissionPipeline":
        """Derive a child view while retaining the shared interactive channel."""
        import copy

        derived = copy.copy(self)
        derived._requesting_agent = agent_name.strip()
        # Deep-copy mutable lists
        derived._deny_rules = list(self._deny_rules)
        derived._ask_rules = list(self._ask_rules)
        derived._allow_rules = list(self._allow_rules)
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

    # Per-step storage for hook input modifications (CC: hooks can modify
    # input without making an approval decision).
    _pending_hook_updates: dict[str, Any] | None = None

    def check(
        self,
        tool: "BaseTool",
        params: dict[str, Any],
        thought: str = "",
    ) -> PermissionResult:
        """CC-aligned 6-layer permission evaluation.

        Layers:
        1. validateInput     — absolute safety floor (not overridable)
        2. PreToolUse Hooks  — user-defined scripts, can deny/allow/pass
        3. Permission Rules  — deny > session_allow > allow > ask
        4. Permission Mode   — bypassPermissions/acceptEdits/plan/dontAsk/default
        4.5 Prompt-based     — CC ExitPlanMode allowedPrompts
        5. Allow Rules       — static allow rules (fallback when Layer 3
                               returns None, e.g. no rule matched)
        6. canUseTool        — Web callback (headless) or TTY callback (CLI)

        When Layer 3 matches an ASK rule, it short-circuits directly to
        Layer 6 (interactive callback).  When no rule matches (None),
        execution continues through Layers 4-6.
        """
        tool_name = tool.name
        self._pending_hook_updates = None

        # Step 1: validateInput
        result = self._layer1_validate(tool, params)
        if result is not None:
            self._stats.record(result)
            return result

        # Step 2: PreToolUse Hooks
        result = self._layer2_hooks(tool_name, params)
        if result is not None:
            # Hook made an explicit allow/deny decision
            self._stats.record(result)
            return self._apply_tool_check(result, tool, params)

        # Apply any hook input modifications that were stashed
        if self._pending_hook_updates:
            params = {**params, **self._pending_hook_updates}

        # CC: total denial limit — session-level circuit breaker
        if self._total_denials >= 20:
            result = PermissionResult(
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.RULE,
                reason=(
                    "Session denial limit (20) reached. "
                    "Tool call blocked. You MUST review and change your approach."
                ),
            )
            self._stats.record(result)
            return self._apply_tool_check(result, tool, params)

        # Step 3: Permission Rules (deny → session_allow → allow → ask)
        tier = self._layer3_rules(tool_name, params)
        if tier is PermissionRuleTier.DENY:
            consecutive = self._denial_counters.get(tool_name, 0) + 1
            reason = f"denied by rule"
            if consecutive >= 3:
                reason += (
                    f" — Tool '{tool_name}' has been denied {consecutive} "
                    "consecutive times. You MUST change your approach."
                )
            if self._total_denials + 1 >= 20:
                reason += " Total denials have reached the session limit."
            result = PermissionResult(
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.RULE,
                reason=reason,
            )
            self._stats.record(result)
            return self._apply_tool_check(result, tool, params)

        if tier is PermissionRuleTier.ALLOW:
            # Session rule ("Always Allow") or static allow rule matched.
            # These take precedence over ask rules (checked in _layer3_rules).
            result = PermissionResult(
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.RULE,
                reason="allowed by rule",
            )
            self._stats.record(result)
            return self._apply_tool_check(result, tool, params)

        if tier is PermissionRuleTier.ASK:
            # ASK rules always require interactive confirmation —
            # approval_mode AUTO is ignored for these tools.
            result = self._layer6_callback(
                tool_name, params, thought, force_interactive=True,
            )
            self._stats.record(result)
            return self._apply_tool_check(result, tool, params)

        # tier is None — no rule matched at Layer 3.
        # Continue to Layer 4 (Permission Mode).
        # This path is reached for tools that have no deny/allow/ask
        # rule defined, e.g. Bash commands when shell rules are
        # not yet fixed (see Finding 6).

        # Step 4: Permission Mode
        mode_result = self._layer4_permission_mode(tool_name, params)
        if mode_result is not None:
            self._stats.record(mode_result)
            return self._apply_tool_check(mode_result, tool, params)

        # Step 4.5: Prompt-based Permissions (CC-aligned ExitPlanMode allowedPrompts)
        if self._approved_prompts:
            match = self._match_approved_prompt(tool_name, params)
            if match is not None:
                result = PermissionResult(
                    decision=PermissionDecision.ALLOW,
                    layer=PermissionLayer.PROMPT_APPROVED,
                    reason=f"Approved prompt: {match}",
                )
                self._stats.record(result)
                return self._apply_tool_check(result, tool, params)

        # Step 5: Allow Rules + Session Rules
        # (session rules — Always Allow — are checked first in _layer3_rules,
        #  so reaching here means no session rule matched)
        for rule in self._allow_rules:
            if rule.matches(tool_name, params):
                result = PermissionResult(
                    decision=PermissionDecision.ALLOW,
                    layer=PermissionLayer.RULE,
                    reason=f"allowed by rule: {rule.raw}",
                )
                self._stats.record(result)
                return self._apply_tool_check(result, tool, params)

        # Step 6: canUseTool Callback
        result = self._layer6_callback(tool_name, params, thought)
        self._stats.record(result)
        # Apply stashed hook updates to the final allow result
        if (result.decision is PermissionDecision.ALLOW
                and self._pending_hook_updates
                and not result.updated_params):
            result.updated_params = self._pending_hook_updates
        return self._apply_tool_check(result, tool, params)

    # --- Layer 1: validateInput ----------------------------------------

    # CC-aligned: protected paths that ALWAYS require interactive
    # confirmation, even in bypassPermissions mode.  These are
    # bypass-immune — no mode, hook, or rule can override them.
    _PROTECTED_DIRS: frozenset[str] = frozenset({
        ".git", ".forge-agent", ".grace", ".claude",
        ".vscode", ".idea",
    })

    _PROTECTED_FILES: frozenset[str] = frozenset({
        ".gitconfig", ".gitmodules",
        ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile",
        ".ripgreprc", ".mcp.json", ".claude.json",
        "settings.json", "settings.local.json",
    })

    @staticmethod
    def _is_protected_path(tool_name: str, params: dict[str, Any]) -> str | None:
        """Check if a file operation targets a protected path.

        Returns the protected component string if so, None otherwise.
        Only applies to Write/Edit tools — Read is always safe.
        """
        if tool_name not in ("Write", "Edit"):
            return None
        path = params.get("file_path") or params.get("path") or ""
        if not path:
            return None
        import os as _os
        parts = _os.path.normpath(str(path)).replace("\\", "/").split("/")
        for part in parts:
            part_lower = part.lower()
            if part_lower in PermissionPipeline._PROTECTED_DIRS:
                return f"Protected directory: {part}/"
            if part_lower in PermissionPipeline._PROTECTED_FILES:
                return f"Protected file: {part}"
        return None

    def _layer1_validate(
        self, tool: "BaseTool", params: dict[str, Any]
    ) -> PermissionResult | None:
        """Absolute safety floor. Cannot be overridden by rules or hooks."""
        # 1. Tool's own blacklist check
        reason = tool.permission_denial_reason(params)
        if reason:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.INPUT_VALIDATION,
                reason=reason,
            )
        # 2. CC-aligned: protected path check (bypass-immune)
        # Protected paths are DENIED at Layer 1 — no mode/hook/rule can override.
        # To edit these files, use an external editor or temporarily comment out
        # the protected paths list.
        protected = self._is_protected_path(tool.name, params)
        if protected:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.INPUT_VALIDATION,
                reason=f"Protected path blocked (bypass-immune): {protected}",
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
        # CONTINUE: no decision.  Stash any updated_input for later
        # application — the pipeline continues through Layers 3-6.
        # CC: hooks can modify input without making an approval decision;
        # deny rules and permission mode still apply.
        if dispatch_result.updated_input:
            self._pending_hook_updates = (
                dispatch_result.updated_input
                if self._pending_hook_updates is None
                else {**self._pending_hook_updates, **dispatch_result.updated_input}
            )
        return None

    # --- Layer 3: Permission Rules -------------------------------------

    def _layer3_rules(
        self, tool_name: str, params: dict[str, Any]
    ) -> PermissionRuleTier | None:
        """
        Match rules with CC-aligned priority: deny > session_allow > allow > ask.

        Session rules (from "Always Allow") take precedence over static ask rules
        because they represent explicit user confirmation during this session.
        Static deny rules always win (safety invariant).

        Allow rules are checked BEFORE ask rules so that users can override
        builtin ask defaults by adding tools to their settings.json allow list.

        Returns None when no rule matches, so the pipeline can continue to
        Layer 4 (permission mode) instead of forcing the ASK path.
        """
        # 1. Deny rules (highest priority, absolute safety floor)
        for rule in self._deny_rules:
            if rule.matches(tool_name, params):
                return PermissionRuleTier.DENY

        # 2. Session rules from "Always Allow" override everything below
        for rule in self._session_rules:
            if rule.matches(tool_name, params):
                return PermissionRuleTier.ALLOW

        # 3. Static allow rules (before ask — user can override builtin asks)
        for rule in self._allow_rules:
            if rule.matches(tool_name, params):
                return PermissionRuleTier.ALLOW

        # 4. Ask rules (soft default — prompts for interactive approval)
        for rule in self._ask_rules:
            if rule.matches(tool_name, params):
                return PermissionRuleTier.ASK

        # 5. No rule matched — continue to Layer 4 (permission mode)
        return None

    def _apply_tool_check(self, result, tool, params):
        if result.decision is PermissionDecision.ALLOW:
            l5 = self._layer5_check(tool, params)
            if l5 is not None:
                # Layer 5 (path sandbox) overrides the allow decision.
                # Use the denied result so the circuit breaker and denial
                # counters below see it (previously return l5 skipped them).
                result = l5
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

    # Tools that are read-only and safe to auto-approve in any mode.
    # CC: "Read-only: No approval required within the working directory."
    _READONLY_SAFE_TOOLS: frozenset[str] = frozenset({
        "Read", "Grep", "Glob", "WebSearch", "WebFetch",
        "Skill", "Task", "SendMessage", "WaitForAgent",
    })

    # CC acceptEdits: "common filesystem commands such as mkdir, touch, mv, cp"
    _FILESYSTEM_SAFE_COMMANDS: frozenset[str] = frozenset({
        "mkdir", "touch", "mv", "cp",
    })

    # CC bypassPermissions: root/home removal still prompts as circuit breaker
    _ROOT_REMOVAL_PATTERNS: tuple[str, ...] = (
        "rm -rf /", "rm -rf ~", "rm -r /", "rm -r ~",
        "rm -rf /*", "rm -rf ~/*",
    )

    def _layer4_permission_mode(self, tool_name, params=None):
        mode = self._permission_mode
        if not mode or mode in ("default", "manual"):
            return None

        if mode == "bypassPermissions":
            # CC: bypassPermissions skips prompts EXCEPT:
            #  - explicit ask rules (checked in Layer 3 before reaching here)
            #  - MCP tools with requiresUserInteraction (checked by tool metadata)
            #  - root/home removal — circuit breaker
            if tool_name == "Bash" and params:
                cmd = str(params.get("command", "")).strip()
                for pattern in self._ROOT_REMOVAL_PATTERNS:
                    if cmd.startswith(pattern) or pattern in cmd:
                        return None  # fall through to Layer 6 for approval
            # Check for MCP requiresUserInteraction
            # (tool metadata check — handled in _layer1_validate if set)
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
            # CC: also auto-approve common filesystem commands
            if tool_name == "Bash" and params:
                cmd = str(params.get("command", "")).strip()
                cmd_base = cmd.split()[0] if cmd else ""
                if cmd_base in self._FILESYSTEM_SAFE_COMMANDS:
                    return PermissionResult(
                        decision=PermissionDecision.ALLOW,
                        layer=PermissionLayer.RULE,
                        reason=f"acceptEdits: {cmd_base} auto-approved",
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

        if mode == "dontAsk":
            # CC-aligned: "Auto-denies tools unless pre-approved."
            # 1. Read-only tools always pass (CC default behaviour)
            if tool_name in self._READONLY_SAFE_TOOLS:
                return PermissionResult(
                    decision=PermissionDecision.ALLOW,
                    layer=PermissionLayer.RULE,
                    reason="dontAsk: read-only tool auto-approved",
                )
            # 2. Check allow rules + session rules (use full pattern matching)
            for rule in self._allow_rules + self._session_rules:
                if rule.matches(tool_name, params):
                    return PermissionResult(
                        decision=PermissionDecision.ALLOW,
                        layer=PermissionLayer.RULE,
                        reason=f"dontAsk: allowed by rule '{rule.raw}'",
                    )
            # 3. Everything else → deny at Layer 4 (never reaches Layer 6)
            return PermissionResult(
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.RULE,
                reason=(
                    f"dontAsk mode: '{tool_name}' denied. "
                    "Add it to permissions.allow in .forge-agent/settings.json "
                    "to pre-approve."
                ),
            )
        return None

    # ── Prompt-based Permissions (CC-aligned allowedPrompts) ──────────────

    _PROMPT_PRIMARY_PARAM: dict[str, str] = {
        "Bash": "command",
        "Write": "path",
        "Edit": "path",
        "Read": "path",
        "Grep": "pattern",
        "Glob": "pattern",
        "WebFetch": "url",
        "WebSearch": "query",
        "Skill": "skill",
    }

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Split *text* into lowercase word tokens, stripping common punctuation."""
        import re
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    def _match_approved_prompt(
        self, tool_name: str, params: dict[str, Any]
    ) -> str | None:
        """Return the approved prompt text if *params* match any stored prompt."""
        for entry in self._approved_prompts:
            if entry.get("tool", "") != tool_name:
                continue
            approved_prompt = entry.get("prompt", "")
            if not approved_prompt:
                continue
            prompt_tokens = self._tokenize(approved_prompt)
            primary_key = self._PROMPT_PRIMARY_PARAM.get(tool_name)
            if primary_key and primary_key in params:
                value = str(params[primary_key])
                value_tokens = self._tokenize(value)
                if prompt_tokens & value_tokens:
                    return approved_prompt
            # Also check all string params for substring matches
            for key, val in params.items():
                if isinstance(val, str):
                    val_tokens = self._tokenize(val)
                    if prompt_tokens & val_tokens:
                        return approved_prompt
        return None

    # ── Layer 6: Interactive Callback (CC-aligned) ──────────────────────

    def _layer6_callback(
        self, tool_name: str, params: dict[str, Any], thought: str,
        force_interactive: bool = False,
    ) -> PermissionResult:
        """CC-aligned interactive approval.

        Resolution order (first match wins):

        1. AUTO mode       → ALLOW (no prompt, for fully automated runs)
           SKIPPED when *force_interactive* is True — ask rules and
           session-level prompts always require user confirmation.
        2. Web callback    → block on threading.Event (headless Web —
                             exact equivalent of CC's stdin-blocking
                             control_request / control_response)
        3. TTY callback    → block on terminal input (CLI mode)
        4. No callback     → DENY (fail closed)
        """
        # Path 1: AUTO mode (skipped when ASK rule forces interactive approval)
        if not force_interactive and self._approval_mode is ToolApprovalMode.AUTO:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.INTERACTIVE,
                reason="auto_approve",
            )

        request = PermissionRequest(
            tool_name=tool_name,
            params=params,
            thought=thought,
            agent_name=self._requesting_agent,
        )

        # Path 2: Web headless callback (blocks on threading.Event internally)
        if self._web_confirm_callback is not None:
            t0 = time.time()
            decision = self._web_confirm_callback(request)
            wait_ms = (time.time() - t0) * 1000
            return self._apply_decision(decision, tool_name, params, wait_ms)

        # Path 3: TTY / CLI callback (blocks on terminal input)
        if self._confirm_callback is not None:
            t0 = time.time()
            with self._prompt_lock:
                decision = self._confirm_callback(request)
            wait_ms = (time.time() - t0) * 1000
            return self._apply_decision(decision, tool_name, params, wait_ms)

        # Path 4: No callback available → fail closed
        return PermissionResult(
            decision=PermissionDecision.DENY,
            layer=PermissionLayer.INTERACTIVE,
            reason="interactive approval unavailable in headless mode",
        )

    def _apply_decision(
        self,
        decision: PromptDecision,
        tool_name: str,
        params: dict[str, Any],
        wait_ms: float,
    ) -> PermissionResult:
        """Convert a PromptDecision into a PermissionResult.

        Handles ALWAYS_ALLOW rule persistence (CC's "Yes, don't ask again").
        """
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
                updated_params=decision.updated_params,
            )
        elif decision.action is PromptAction.ALLOW_ONCE:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                layer=PermissionLayer.INTERACTIVE,
                reason="allow_once",
                wait_ms=wait_ms,
                updated_params=decision.updated_params,
            )
        else:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                layer=PermissionLayer.INTERACTIVE,
                reason="denied_by_user",
                feedback=decision.note,
                wait_ms=wait_ms,
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
