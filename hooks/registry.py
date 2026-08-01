"""
hooks/registry.py

Hook registry: stores external (command) and internal (Python callable) hooks,
loaded from settings.json or registered programmatically.

Config format in .grace/settings.json:
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "shell", "hooks": [{"type": "command", "command": "...", "timeout": 5}]}
    ],
    "PostToolUse": [
      {"matcher": "*", "hooks": [{"type": "command", "command": "..."}]}
    ]
  }
}
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from hooks.events import HookContext, HookEvent
from hooks.matcher import HookMatcher

logger = logging.getLogger(__name__)


class HookScheduling(str, Enum):
    AWAITED = "awaited"
    DETACHED = "detached"


class HookDecisionAuthority(str, Enum):
    POLICY = "policy"
    ADVISORY = "advisory"


class HookDataAuthority(str, Enum):
    READ_ONLY = "read_only"
    TRANSFORM = "transform"


class HookFailurePolicy(str, Enum):
    EVENT_DEFAULT = "event_default"
    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


@dataclass
class ExternalHookConfig:
    """A command-type hook loaded from settings.json."""

    type: str = "command"
    command: str = ""
    timeout: int = 60
    matcher: HookMatcher = field(default_factory=HookMatcher)
    agent_id: str = ""  # non-empty = scoped to a specific agent session
    hook_id: str = ""
    priority: int = 100
    scheduling: HookScheduling = HookScheduling.AWAITED
    decision_authority: HookDecisionAuthority = HookDecisionAuthority.POLICY
    data_authority: HookDataAuthority = HookDataAuthority.TRANSFORM
    failure_policy: HookFailurePolicy = HookFailurePolicy.EVENT_DEFAULT


@dataclass
class InternalHook:
    """A Python callable hook registered programmatically (no subprocess overhead)."""

    callback: Callable[[HookContext], Any]
    matcher: HookMatcher = field(default_factory=HookMatcher)
    hook_id: str = ""
    priority: int = 100
    scheduling: HookScheduling = HookScheduling.AWAITED
    decision_authority: HookDecisionAuthority = HookDecisionAuthority.POLICY
    data_authority: HookDataAuthority = HookDataAuthority.TRANSFORM
    failure_policy: HookFailurePolicy = HookFailurePolicy.EVENT_DEFAULT


class HookRegistry:
    """
    Central registry for all hooks (external commands + internal callables).

    External hooks are loaded from settings.json.
    Internal hooks are registered in code (e.g., ProactiveMemory observers).
    """

    def __init__(self) -> None:
        self._external: dict[HookEvent, list[ExternalHookConfig]] = defaultdict(list)
        self._internal: dict[HookEvent, list[InternalHook]] = defaultdict(list)

    def load_from_settings(self, settings_path: Path) -> None:
        """Load hook configurations from a settings.json file."""
        if not settings_path.exists():
            return
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load hooks from %s: %s", settings_path, exc)
            return

        hooks_section = data.get("hooks", {})
        if not isinstance(hooks_section, dict):
            return

        for event_name, entries in hooks_section.items():
            try:
                event = HookEvent(event_name)
            except ValueError:
                logger.debug("Unknown hook event in settings: %s", event_name)
                continue

            if not isinstance(entries, list):
                continue

            for entry in entries:
                self._load_entry(event, entry)

    def register_internal(self, event: HookEvent, hook: InternalHook) -> None:
        """Register a Python callable hook."""
        self._validate_contract(hook)
        self._internal[event].append(hook)

    def register_external(self, event: HookEvent, config: ExternalHookConfig) -> None:
        """Dynamically register an external hook (e.g. from agent frontmatter)."""
        self._validate_contract(config)
        self._external[event].append(config)

    def clone(self) -> "HookRegistry":
        """Return a deep copy suitable for per-session customization.

        The clone starts with the same internal + external hooks as the
        original but can be independently mutated (e.g. by adding
        agent-scoped hooks) without affecting shared state.
        """
        import copy
        cloned = HookRegistry()
        cloned._internal = copy.deepcopy(self._internal)
        cloned._external = copy.deepcopy(self._external)
        return cloned

    def unregister_external(self, event: HookEvent, config: ExternalHookConfig) -> None:
        """Remove a dynamically registered external hook."""
        try:
            self._external[event].remove(config)
        except (KeyError, ValueError):
            pass

    def find_external(
        self, event: HookEvent, matcher_subject: str, tool_input: dict[str, Any],
        *, agent_id: str = "",
    ) -> list[ExternalHookConfig]:
        """Find external hooks matching the event's declared subject.

        agent_id: if non-empty, only include hooks scoped to this agent
                  (no agent_id = global hook, applies to all agents).
        """
        return sorted([
            h for h in self._external.get(event, [])
            if h.matcher.matches(matcher_subject, tool_input)
            and (not h.agent_id or h.agent_id == agent_id)
        ], key=lambda hook: (hook.priority, hook.hook_id))

    def find_internal(
        self, event: HookEvent, matcher_subject: str, tool_input: dict[str, Any]
    ) -> list[InternalHook]:
        """Find internal hooks matching the event's declared subject."""
        return sorted([
            h for h in self._internal.get(event, [])
            if h.matcher.matches(matcher_subject, tool_input)
        ], key=lambda hook: (hook.priority, hook.hook_id))

    @staticmethod
    def _validate_contract(hook: ExternalHookConfig | InternalHook) -> None:
        if hook.scheduling is HookScheduling.DETACHED and (
            hook.decision_authority is HookDecisionAuthority.POLICY
            or hook.data_authority is HookDataAuthority.TRANSFORM
        ):
            raise ValueError(
                "Detached hooks must be advisory and read-only because their "
                "result is not awaited"
            )

    def _load_entry(self, event: HookEvent, entry: dict[str, Any]) -> None:
        """Parse a single hook entry from settings.json."""
        matcher_raw = entry.get("matcher", "*")
        if_cond = entry.get("if")
        matcher = HookMatcher(pattern=matcher_raw, if_condition=if_cond)

        hooks_list = entry.get("hooks", [])
        if not isinstance(hooks_list, list):
            return

        for hook_def in hooks_list:
            hook_type = hook_def.get("type", "command")
            if hook_type != "command":
                logger.debug("Unsupported hook type: %s", hook_type)
                continue
            command = hook_def.get("command", "")
            if not command:
                continue
            try:
                config = ExternalHookConfig(
                    type="command",
                    command=command,
                    timeout=int(hook_def.get("timeout", 60)),
                    matcher=matcher,
                    hook_id=str(hook_def.get("id", command)),
                    priority=int(hook_def.get("priority", 100)),
                    scheduling=HookScheduling(
                        hook_def.get(
                            "scheduling", HookScheduling.AWAITED.value,
                        )
                    ),
                    decision_authority=HookDecisionAuthority(
                        hook_def.get(
                            "decision_authority",
                            HookDecisionAuthority.POLICY.value,
                        )
                    ),
                    data_authority=HookDataAuthority(
                        hook_def.get(
                            "data_authority", HookDataAuthority.TRANSFORM.value,
                        )
                    ),
                    failure_policy=HookFailurePolicy(
                        hook_def.get(
                            "failure_policy",
                            HookFailurePolicy.EVENT_DEFAULT.value,
                        )
                    ),
                )
                self.register_external(event, config)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "Ignoring invalid %s hook configuration: %s",
                    event.value,
                    exc,
                )
