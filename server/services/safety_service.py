"""Runtime-backed safety and authority projection for the Web UI."""

from __future__ import annotations

from collections import Counter
from enum import Enum
from typing import Any

from hitl.permission_rule import RULE_SOURCE_PRIORITY


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class SafetyService:
    """Expose permission facts without executing or simulating a tool call."""

    TRACE_LIMIT = 5000

    def __init__(self, agent_service: Any) -> None:
        self._service = agent_service

    def get_snapshot(self, session_id: str = "") -> dict[str, Any]:
        rules = self._rules()
        return {
            "layers": self._layers(),
            "rules": rules,
            "rule_summary": {
                "total": len(rules),
                "by_tier": dict(Counter(rule["tier"] for rule in rules)),
                "by_source": dict(Counter(rule["source"] for rule in rules)),
                "precedence": ["deny", "ask", "allow"],
                "source_priority": dict(RULE_SOURCE_PRIORITY),
            },
            "tools": self._tools(rules),
            "modes": self._modes(),
            "session": self._session_snapshot(session_id) if session_id else None,
            "invariants": [
                {
                    "name": "Input validation is absolute",
                    "detail": "Unsafe input cannot be allowed by a rule, mode, hook, or user response.",
                },
                {
                    "name": "Deny rules are bypass-immune",
                    "detail": "Deny is evaluated before ask and allow across all rule sources.",
                },
                {
                    "name": "Child agents cannot relax parent authority",
                    "detail": "Deny rules are inherited and elevated modes are capped by the parent.",
                },
                {
                    "name": "Write paths remain project-scoped",
                    "detail": "Tool-specific path checks reject write targets outside the project root.",
                },
                {
                    "name": "Interactive decisions are synchronous",
                    "detail": "The agent thread blocks until allow, deny, cancellation, or timeout.",
                },
            ],
            "disclosure": {
                "source": "live_registry_rules_and_persisted_trace",
                "tool_calls_executed": False,
                "rule_simulation_performed": False,
                "historical_responses_may_be_unrecorded": True,
            },
        }

    @staticmethod
    def _layers() -> list[dict[str, Any]]:
        return [
            {
                "order": "1",
                "id": "input_validation",
                "label": "Input validation",
                "authority": "absolute",
                "can_allow": False,
                "can_deny": True,
                "detail": "Tool validation and the non-overridable safety blacklist.",
            },
            {
                "order": "2",
                "id": "pre_tool_hook",
                "label": "PreToolUse hooks",
                "authority": "proposal",
                "can_allow": False,
                "can_deny": True,
                "detail": "Hooks may block or propose updated input; updates are revalidated.",
            },
            {
                "order": "3",
                "id": "rules",
                "label": "Deny / ask rules",
                "authority": "policy",
                "can_allow": False,
                "can_deny": True,
                "detail": "Deny wins over ask and allow, independent of source priority.",
            },
            {
                "order": "4",
                "id": "permission_mode",
                "label": "Permission mode",
                "authority": "session",
                "can_allow": True,
                "can_deny": True,
                "detail": "Plan, dontAsk, acceptEdits, default, and bypassPermissions semantics.",
            },
            {
                "order": "4.5",
                "id": "approved_prompts",
                "label": "Plan-approved prompts",
                "authority": "scoped grant",
                "can_allow": True,
                "can_deny": False,
                "detail": "Structured ExitPlanMode grants carried into the approved build.",
            },
            {
                "order": "5",
                "id": "allow_and_sandbox",
                "label": "Allow rules + path sandbox",
                "authority": "policy",
                "can_allow": True,
                "can_deny": True,
                "detail": "Static/session grants are checked without weakening project boundaries.",
            },
            {
                "order": "6",
                "id": "interactive",
                "label": "Human decision",
                "authority": "interactive",
                "can_allow": True,
                "can_deny": True,
                "detail": "A blocking Web or TTY control request resolves the remaining decision.",
            },
        ]

    def _rules(self) -> list[dict[str, Any]]:
        rules = list(getattr(self._service, "_loaded_rules", []) or [])
        return sorted(
            [
                {
                    "raw": str(getattr(rule, "raw", "")),
                    "tool_name": str(getattr(rule, "tool_name", "")),
                    "pattern": getattr(rule, "pattern", None),
                    "tier": str(_value(getattr(rule, "tier", "unknown"))),
                    "source": str(getattr(rule, "source", "unknown")),
                    "source_priority": RULE_SOURCE_PRIORITY.get(
                        str(getattr(rule, "source", "")),
                        0,
                    ),
                }
                for rule in rules
            ],
            key=lambda item: (
                {"deny": 0, "ask": 1, "allow": 2}.get(item["tier"], 3),
                -item["source_priority"],
                item["raw"],
            ),
        )

    def _tools(self, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        registry = self._service._registry
        tools = []
        for name in sorted(registry.tool_names):
            metadata = registry.metadata_for(name)
            effects = sorted(
                str(_value(effect))
                for effect in (metadata.effects if metadata else ())
            )
            path_access = str(
                _value(metadata.path_access) if metadata else "none"
            )
            matching_rules = [
                rule for rule in rules
                if self._rule_targets_tool(rule["tool_name"], name)
            ]
            always_interactive = bool(
                metadata and metadata.requires_user_interaction
            )
            risk = self._risk_level(effects, path_access)
            if always_interactive:
                control = "always_interactive"
            elif any(rule["tier"] == "ask" for rule in matching_rules):
                control = "ask_rule"
            elif any(rule["tier"] == "deny" for rule in matching_rules):
                control = "deny_guarded"
            elif risk in {"high", "critical"}:
                control = "mode_or_rule_guarded"
            else:
                control = "policy_evaluated"
            tools.append({
                "name": name,
                "risk": risk,
                "control": control,
                "effects": effects,
                "path_access": path_access,
                "path_parameter": (
                    str(metadata.path_parameter) if metadata else ""
                ),
                "requires_user_interaction": always_interactive,
                "required_permissions": sorted(
                    metadata.required_permissions if metadata else ()
                ),
                "matching_rules": [
                    {
                        "raw": rule["raw"],
                        "tier": rule["tier"],
                        "source": rule["source"],
                    }
                    for rule in matching_rules
                ],
            })
        return tools

    @staticmethod
    def _rule_targets_tool(rule_tool: str, tool_name: str) -> bool:
        normalized = tool_name.lower()
        return (
            rule_tool in {"*", normalized}
            or (normalized == "bash" and rule_tool == "shell")
        )

    @staticmethod
    def _risk_level(effects: list[str], path_access: str) -> str:
        effect_set = set(effects)
        if effect_set & {"write_vcs", "network", "delegate_write"}:
            return "critical"
        if effect_set & {
            "write_workspace",
            "write_agent_state",
            "execute",
        } or path_access in {"write", "workspace_wide"}:
            return "high"
        if effect_set & {"test", "produce_deliverable", "delegate_read_only"}:
            return "medium"
        if effect_set & {
            "read_workspace",
            "read_vcs",
            "read_agent_state",
            "discover_workspace",
        }:
            return "low"
        return "unknown"

    @staticmethod
    def _modes() -> list[dict[str, str]]:
        return [
            {
                "name": "default",
                "posture": "balanced",
                "detail": "Rules first; unresolved calls may ask the user.",
            },
            {
                "name": "acceptEdits",
                "posture": "edit-forward",
                "detail": "Workspace edits may proceed while deny/ask safety remains.",
            },
            {
                "name": "plan",
                "posture": "read-only",
                "detail": "Mutation is blocked even when an allow rule exists.",
            },
            {
                "name": "dontAsk",
                "posture": "fail-closed",
                "detail": "Unapproved actions are denied instead of prompting.",
            },
            {
                "name": "bypassPermissions",
                "posture": "elevated",
                "detail": "Ordinary prompts are bypassed; absolute and deny boundaries remain.",
            },
        ]

    def _session_snapshot(self, session_id: str) -> dict[str, Any]:
        session = self._service.session_service.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        try:
            definition = self._service._agent_registry.get(session.agent_name)
        except KeyError:
            definition = None
        default_mode = (
            getattr(definition, "permission_mode", "") if definition else ""
        ) or "default"
        pending_mode = str(
            getattr(
                self._service._runtime,
                "_pending_perm_modes",
                {},
            ).get(session_id, "")
        )
        broker = self._service._runtime.get_approval_broker(session_id)
        events = self._service._storage.list_trace_events(
            session_id,
            limit=self.TRACE_LIMIT,
        )
        approvals = self._approval_history(events)
        parent = (
            self._service.session_service.get_session(session.parent_id)
            if getattr(session, "parent_id", None) else None
        )
        return {
            "session_id": session_id,
            "agent_name": getattr(session, "agent_name", ""),
            "agent_kind": str(_value(getattr(session, "agent_kind", ""))),
            "default_mode": default_mode,
            "pending_mode": pending_mode,
            "effective_next_mode": pending_mode or default_mode,
            "project_root": str(getattr(session, "repo_path", "")),
            "parent_session_id": getattr(session, "parent_id", None),
            "parent_agent_name": getattr(parent, "agent_name", "") if parent else "",
            "deny_rules_inherited": parent is not None,
            "pending_approval_count": (
                broker.pending_count if broker is not None else 0
            ),
            "approvals": approvals,
            "approval_summary": {
                "total": len(approvals),
                "allowed": sum(
                    1 for item in approvals
                    if item["decision"] in {"allow_once", "always_allow"}
                ),
                "denied": sum(
                    1 for item in approvals if item["decision"] == "deny"
                ),
                "timed_out": sum(
                    1 for item in approvals if item["status"] == "timed_out"
                ),
                "response_not_recorded": sum(
                    1 for item in approvals
                    if item["status"] == "response_not_recorded"
                ),
                "average_wait_ms": self._average_wait(approvals),
            },
            "trace_truncated": len(events) >= self.TRACE_LIMIT,
        }

    @staticmethod
    def _approval_history(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        required: dict[str, dict[str, Any]] = {}
        resolved: dict[str, dict[str, Any]] = {}
        timed_out: set[str] = set()
        for event in events:
            event_type = str(event.get("type") or "")
            request_id = str(event.get("request_id") or "")
            if not request_id:
                continue
            if event_type == "approval_required":
                required[request_id] = event
            elif event_type == "approval_resolved":
                resolved[request_id] = event
            elif event_type == "approval_timeout":
                timed_out.add(request_id)

        history = []
        for request_id, request in required.items():
            response = resolved.get(request_id, {})
            params = request.get("params")
            params = params if isinstance(params, dict) else {}
            if request_id in timed_out:
                status = "timed_out"
                decision = "deny"
            elif response:
                status = "resolved"
                decision = str(response.get("decision") or "")
            else:
                status = "response_not_recorded"
                decision = ""
            history.append({
                "request_id": request_id,
                "tool_name": str(request.get("tool_name") or ""),
                "decision_reason": str(
                    request.get("decision_reason") or ""
                ),
                "permission_mode": str(
                    request.get("permission_mode") or ""
                ),
                "risk_level": str(request.get("risk_level") or ""),
                "params_keys": sorted(str(key) for key in params),
                "target": SafetyService._target_summary(params),
                "status": status,
                "decision": decision,
                "note": str(response.get("note") or ""),
                "updated_input": bool(response.get("updated_input")),
                "wait_ms": float(response.get("wait_ms") or 0),
                "requested_at": str(request.get("timestamp") or ""),
                "resolved_at": str(response.get("timestamp") or ""),
                "sequence": int(request.get("sequence") or 0),
            })
        history.sort(key=lambda item: item["sequence"], reverse=True)
        return history

    @staticmethod
    def _target_summary(params: dict[str, Any]) -> str:
        for key in (
            "path",
            "file_path",
            "target_file",
            "command",
            "url",
            "pattern",
        ):
            if key in params and params[key] is not None:
                value = str(params[key]).replace("\n", " ")
                return f"{key}: {value[:160]}"
        return ""

    @staticmethod
    def _average_wait(approvals: list[dict[str, Any]]) -> float:
        waits = [
            float(item["wait_ms"])
            for item in approvals
            if item["status"] == "resolved" and item["wait_ms"] > 0
        ]
        return sum(waits) / len(waits) if waits else 0.0
