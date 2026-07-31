"""Runtime-backed architecture snapshot for the Web architecture explorer."""

from __future__ import annotations

from collections import Counter
from enum import Enum
import logging
from pathlib import Path
from typing import Any

from capabilities import (
    CapabilityIndex,
    CapabilityKind,
    CapabilityQuery,
    CapabilityStatus,
)
from capabilities.providers.mcp_provider import McpCapabilityProvider
from capabilities.providers.skill_provider import SkillCapabilityProvider
from capabilities.sanitize import sanitize_error, sanitize_text

logger = logging.getLogger(__name__)


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class ArchitectureService:
    """Describe configured capabilities and overlay one persisted session."""

    def __init__(self, agent_service: Any) -> None:
        self._service = agent_service

    def get_snapshot(self, session_id: str = "") -> dict[str, Any]:
        # Build a capability fingerprint from available providers.
        # Providers are lightweight adapters — building the index here
        # does no I/O and is cheap even when _skills() / _mcp() also
        # construct their own per-kind indexes internally.
        providers: list[Any] = []
        skill_registry = getattr(self._service._registry, "skill_registry", None)
        if skill_registry is not None:
            providers.append(
                SkillCapabilityProvider(skill_registry, llm_invocable_only=False),
            )
        mcp = self._service._mcp_integration
        if mcp is not None:
            providers.append(McpCapabilityProvider(mcp))

        fingerprint = ""
        if providers:
            cap_snapshot = CapabilityIndex(providers).snapshot(
                CapabilityQuery(visible_to_model=None),
            )
            fingerprint = cap_snapshot.fingerprint

        return {
            "components": self._components(),
            "edges": self._edges(),
            "agents": self._agents(),
            "tools": self._tools(),
            "skills": self._skills(),
            "mcp": self._mcp(),
            "memory": self._memory(),
            "hitl": self._hitl(),
            "runtime": self._runtime(),
            "session_overlay": (
                self._session_overlay(session_id) if session_id else None
            ),
            "disclosure": {
                "configured_facts": "live_runtime_registries",
                "session_facts": "persisted_session_steps_and_trace",
                "prompt_contents_included": False,
            },
            "fingerprint": fingerprint,
        }

    def _components(self) -> list[dict[str, Any]]:
        memory_enabled = bool(
            getattr(self._service._memory_context, "enabled", False)
        )
        mcp = self._service._mcp_integration
        mcp_failed = bool(getattr(mcp, "failed_servers", {}) if mcp else {})
        return [
            self._component("web", "Web workbench", "interface", "available",
                            "Chat, approvals, plans, inspectors, and review decisions"),
            self._component("runtime", "Session runtime", "orchestration", "available",
                            "Owns session lifecycle, delegation, cancellation, and resumes"),
            self._component("agents", "Agent registry", "orchestration", "available",
                            "Validated primary and named-subagent definitions"),
            self._component("context", "Context manager", "reasoning", "available",
                            "Builds provider messages, budgets history, and compacts context"),
            self._component("model", "LLM backend", "reasoning", "available",
                            "Receives prepared messages and visible tool schemas"),
            self._component("tools", "Tool registry", "capability", "available",
                            "Declarative schemas with Runtime execution metadata"),
            self._component("hitl", "HITL policy", "safety", "available",
                            "Permission rules, approval broker, hooks, and path boundaries"),
            self._component("subagents", "Subagent scheduler", "orchestration", "available",
                            "Fresh or inherited contexts with scoped workspaces and budgets"),
            self._component("skills", "Skill registry", "capability", "available",
                            "On-demand instruction packages with model and tool modifiers"),
            self._component(
                "mcp", "MCP integration", "capability",
                "warning" if mcp_failed else (
                    "available" if mcp and mcp.is_initialized else "disabled"
                ),
                "External tool discovery with per-server failure isolation",
            ),
            self._component(
                "memory", "Memory system", "context",
                "available" if memory_enabled else "disabled",
                "Recall, injection decisions, persistence, and consolidation",
            ),
            self._component("storage", "SQLite fact store", "persistence", "available",
                            "Sessions, traces, runs, reviews, stats, and context snapshots"),
            self._component("observability", "Observability", "persistence", "available",
                            "Run scores, validation artifacts, logs, and regression baselines"),
        ]

    @staticmethod
    def _component(
        component_id: str,
        label: str,
        layer: str,
        status: str,
        responsibility: str,
    ) -> dict[str, str]:
        return {
            "id": component_id,
            "label": label,
            "layer": layer,
            "status": status,
            "responsibility": responsibility,
        }

    @staticmethod
    def _edges() -> list[dict[str, str]]:
        return [
            {"source": "web", "target": "runtime", "label": "commands + decisions", "kind": "control"},
            {"source": "runtime", "target": "agents", "label": "resolve definition", "kind": "control"},
            {"source": "runtime", "target": "context", "label": "assemble request", "kind": "data"},
            {"source": "context", "target": "memory", "label": "recall + inject", "kind": "data"},
            {"source": "context", "target": "skills", "label": "active instructions", "kind": "data"},
            {"source": "context", "target": "model", "label": "messages", "kind": "data"},
            {"source": "tools", "target": "model", "label": "visible schemas", "kind": "data"},
            {"source": "model", "target": "hitl", "label": "tool request", "kind": "control"},
            {"source": "hitl", "target": "tools", "label": "allow / deny", "kind": "control"},
            {"source": "tools", "target": "mcp", "label": "external calls", "kind": "control"},
            {"source": "runtime", "target": "subagents", "label": "delegate contract", "kind": "control"},
            {"source": "subagents", "target": "runtime", "label": "typed result", "kind": "data"},
            {"source": "runtime", "target": "storage", "label": "persist facts", "kind": "data"},
            {"source": "runtime", "target": "observability", "label": "scores + traces", "kind": "data"},
            {"source": "storage", "target": "web", "label": "reload state", "kind": "data"},
        ]

    def _agents(self) -> list[dict[str, Any]]:
        registry = self._service._agent_registry
        repo_agents = Path(self._service.repo_path) / ".grace" / "agents"
        agents: list[dict[str, Any]] = []
        for definition in registry.list_all():
            try:
                delegates = [
                    child.name for child in registry.delegatable_by(definition)
                ]
            except (KeyError, ValueError):
                delegates = []
            agents.append({
                "name": definition.name,
                "description": definition.description,
                "kind": _value(definition.agent_kind),
                "intent": _value(definition.intent),
                "visibility": _value(definition.visibility),
                "workspace_mode": _value(definition.workspace_mode),
                "context_policy": (
                    "primary"
                    if _value(definition.agent_kind) == "primary"
                    else "fresh_or_resumed"
                ),
                "permission_mode": definition.permission_mode or "inherit",
                "model": definition.model,
                "effort": definition.effort or "inherit",
                "max_turns": definition.max_turns,
                "max_tokens": definition.max_tokens,
                "background": definition.background,
                "memory_scope": definition.memory or "disabled",
                "tools": sorted(registry.tool_names_for(definition.name)),
                "disallowed_tools": sorted(
                    registry.disallowed_tool_names_for(definition.name)
                ),
                "required_tools": sorted(definition.required_tools),
                "completion_requires": dict(definition.completion_requires),
                "skills": list(definition.skills),
                "mcp_servers": [
                    entry if isinstance(entry, str) else next(iter(entry), "")
                    for entry in definition.mcp_servers
                ],
                "delegates": delegates,
                "source": (
                    "project"
                    if (repo_agents / f"{definition.name}.md").is_file()
                    else "discovered_default"
                ),
            })
        return agents

    def _tools(self) -> list[dict[str, Any]]:
        registry = self._service._registry
        visible_names = {schema.name for schema in registry.get_schemas()}
        tools: list[dict[str, Any]] = []
        for name in sorted(registry.tool_names):
            metadata = registry.metadata_for(name)
            tool = getattr(registry, "_tools", {}).get(name)
            description = str(getattr(tool, "description", "") or "")
            roles = sorted(_value(role) for role in (metadata.roles if metadata else ()))
            effects = sorted(_value(effect) for effect in (metadata.effects if metadata else ()))
            tools.append({
                "name": name,
                "description": description[:240],
                "deferred": name not in visible_names,
                "roles": roles,
                "effects": effects,
                "path_access": _value(metadata.path_access) if metadata else "none",
                "requires_user_interaction": bool(
                    metadata and metadata.requires_user_interaction
                ),
                "required_permissions": sorted(
                    metadata.required_permissions if metadata else ()
                ),
                "category": self._tool_category(name, roles, effects),
            })
        return tools

    @staticmethod
    def _tool_category(
        name: str,
        roles: list[str],
        effects: list[str],
    ) -> str:
        lower = name.lower()
        if name.startswith("mcp__"):
            return "mcp"
        if "delegate" in roles or "delegate_read_only" in effects or "delegate_write" in effects:
            return "delegation"
        if "memory" in lower or "persist_memory" in roles:
            return "memory"
        if "write_workspace" in effects or "write_vcs" in effects:
            return "mutation"
        if "test" in effects or "execute" in effects:
            return "execution"
        if any(effect.startswith(("read_", "discover_")) for effect in effects):
            return "read"
        return "control"

    def _skills(self) -> list[dict[str, Any]]:
        registry = self._service._registry.skill_registry
        if registry is None:
            return []

        query = CapabilityQuery(
            kinds=frozenset({CapabilityKind.SKILL}),
            visible_to_model=None,
        )
        index = CapabilityIndex([
            SkillCapabilityProvider(registry, llm_invocable_only=False),
        ])
        snapshot = index.snapshot(query)

        # Build name → metadata lookup for UI-only fields the provider
        # intentionally excludes (they are not relevant to prompt context).
        reg_map = dict(registry.list_skill_entries())

        result: list[dict[str, Any]] = []
        for descriptor in snapshot.by_kind(CapabilityKind.SKILL):
            metadata = reg_map.get(descriptor.metadata.name)
            if metadata is None:
                continue
            result.append({
                "name": descriptor.metadata.name,
                "display_name": getattr(metadata, "display_name", descriptor.metadata.name),
                "description": sanitize_text(descriptor.metadata.description),
                "model_invocable": descriptor.metadata.model_invocable,
                "user_invocable": descriptor.metadata.user_invocable,
                "context": getattr(metadata, "context", None) or "current",
                "agent": getattr(metadata, "agent", None),
                "model": getattr(metadata, "model", None) or "inherit",
                "effort": getattr(metadata, "effort", None) or "inherit",
                "allowed_tools": sorted(descriptor.metadata.allowed_tools),
                "disallowed_tools": sorted(descriptor.metadata.disallowed_tools),
                "path_scopes": list(descriptor.metadata.path_scopes),
            })
        return result

    def _mcp(self) -> dict[str, Any]:
        integration = self._service._mcp_integration
        if integration is None:
            return {
                "initialized": False,
                "servers": [],
                "tool_names": [],
                "failed_servers": [],
            }

        query = CapabilityQuery(
            kinds=frozenset({CapabilityKind.MCP_SERVER, CapabilityKind.MCP_TOOL}),
            visible_to_model=None,
        )
        index = CapabilityIndex([McpCapabilityProvider(integration)])
        snapshot = index.snapshot(query)

        server_descriptors = snapshot.by_kind(CapabilityKind.MCP_SERVER)
        tool_descriptors = snapshot.by_kind(CapabilityKind.MCP_TOOL)

        # Group tool names per server from MCP_TOOL descriptors
        server_tool_map: dict[str, list[str]] = {}
        for d in tool_descriptors:
            if d.metadata.server_name:
                server_tool_map.setdefault(d.metadata.server_name, []).append(
                    d.metadata.name,
                )

        servers: list[dict[str, Any]] = []
        for d in server_descriptors:
            servers.append({
                "name": d.metadata.name,
                "status": (
                    "failed"
                    if d.runtime.status is CapabilityStatus.FAILED
                    else "connected"
                ),
                "tools": sorted(server_tool_map.get(d.metadata.name, [])),
                "error": sanitize_error(d.runtime.error),
            })

        return {
            "initialized": integration.is_initialized,
            "servers": servers,
            "tool_names": sorted(d.metadata.name for d in tool_descriptors),
            "failed_servers": [
                {"name": name, "error": sanitize_error(error)}
                for name, error in sorted(integration.failed_servers.items())
            ],
        }

    def _memory(self) -> dict[str, Any]:
        store = self._service._memory_store
        summaries = []
        if store is not None:
            try:
                summaries = store.list_memories()
            except Exception:
                logger.debug("Unable to count memories", exc_info=True)
        return {
            "enabled": bool(
                getattr(self._service._memory_context, "enabled", False)
            ),
            "store_available": store is not None,
            "semantic_retrieval": self._service._memory_retriever is not None,
            "recall_service": self._service._memory_recall_service is not None,
            "memory_count": len(summaries),
            "consolidation_hook": self._service._hook_dispatcher is not None,
        }

    def _hitl(self) -> dict[str, Any]:
        rules = list(getattr(self._service, "_loaded_rules", []) or [])
        tiers = Counter(
            str(_value(getattr(rule, "tier", "unknown")))
            for rule in rules
        )
        return {
            "base_approval_mode": "auto",
            "rule_count": len(rules),
            "rules_by_tier": dict(sorted(tiers.items())),
            "approval_broker": True,
            "plan_approval": True,
            "path_sandbox": True,
            "hooks_enabled": self._service._hook_dispatcher is not None,
        }

    def _runtime(self) -> dict[str, Any]:
        config = self._service._config
        prompts = getattr(config, "prompts", None)
        return {
            "provider": config.llm.provider,
            "model": config.llm.model,
            "max_steps": config.agent.max_steps,
            "execution_token_budget": config.agent.budget_tokens,
            "request_context_budget": config.context.request_budget_tokens,
            "history_window": config.context.history_window,
            "prompt_source": getattr(prompts, "source", ""),
            "prompt_label": getattr(prompts, "label", ""),
            "prompt_version": getattr(prompts, "version", ""),
            "streaming_tool_execution": True,
        }

    def _session_overlay(self, session_id: str) -> dict[str, Any]:
        session = self._service.session_service.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        root_id = getattr(session, "root_id", "") or session.id
        tree = self._service.session_service.get_session_tree(root_id)
        session_ids = self._flatten_tree_ids(tree)
        tool_counts: Counter[str] = Counter()
        approval_count = 0
        subagent_events = 0
        for current_id in session_ids:
            for step in self._service._stats_service.get_session_steps(current_id):
                name = str(step.get("tool_name") or "")
                if name:
                    tool_counts[name] += 1
            try:
                events = self._service._storage.list_trace_events(
                    current_id,
                    limit=1000,
                )
            except Exception:
                events = []
            approval_count += sum(
                1 for event in events
                if event.get("type") == "approval_required"
            )
            subagent_events += sum(
                1 for event in events
                if event.get("type") == "subagent_start"
            )

        snapshots = self._service._stats_service.get_context_snapshots(
            session_id,
            limit=1,
        )
        latest_context = snapshots[-1] if snapshots else None
        recalls = []
        recall_service = self._service._memory_recall_service
        if recall_service is not None:
            try:
                recalls = recall_service.list_recalls(session_id, limit=100)
            except Exception:
                logger.debug("Unable to read memory recalls", exc_info=True)
        return {
            "selected_session_id": session_id,
            "root_session_id": root_id,
            "agent_name": getattr(session, "agent_name", ""),
            "status": _value(getattr(session, "status", "unknown")),
            "mode": _value(getattr(session, "mode", "unknown")),
            "agent_kind": _value(getattr(session, "agent_kind", "unknown")),
            "context_origin": _value(
                getattr(session, "context_origin", "unknown")
            ),
            "execution_placement": _value(
                getattr(session, "execution_placement", "unknown")
            ),
            "workspace_mode": _value(
                getattr(session, "workspace_mode", "unknown")
            ),
            "tree": tree,
            "session_count": len(session_ids),
            "agent_names": sorted({
                node["agent_name"]
                for node in self._flatten_tree(tree)
                if node.get("agent_name")
            }),
            "tool_usage": [
                {"name": name, "count": count}
                for name, count in tool_counts.most_common()
            ],
            "mcp_usage": [
                {"name": name, "count": count}
                for name, count in tool_counts.most_common()
                if name.startswith("mcp__")
            ],
            "approval_count": approval_count,
            "subagent_start_count": subagent_events,
            "memory_recall_count": len(recalls),
            "memory_injected_count": sum(
                1 for recall in recalls if recall.get("injected")
            ),
            "latest_context": latest_context,
        }

    @classmethod
    def _flatten_tree_ids(cls, tree: dict[str, Any] | None) -> list[str]:
        return [
            str(node["id"])
            for node in cls._flatten_tree(tree)
            if node.get("id")
        ]

    @classmethod
    def _flatten_tree(
        cls,
        tree: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not tree:
            return []
        nodes = [tree]
        for child in tree.get("children", []):
            nodes.extend(cls._flatten_tree(child))
        return nodes
