"""Render capability snapshots into structured prompt sections."""

from __future__ import annotations

from typing import Any

from capabilities.models import (
    CapabilityKind,
    CapabilityQuery,
    CapabilitySection,
    CapabilitySnapshot,
    CapabilityStatus,
)
from capabilities.sanitize import sanitize_error, sanitize_text


# ── Tool description formatting ─────────────────────────────────────────────
# These live here so that PromptAssembler does not need to depend on tool
# schema objects.  They are pure formatting functions — stateless, no I/O.


def format_tool_descriptions(tools: list[Any]) -> str:
    """Format tool schemas as a markdown bullet list for prompt injection."""
    if not tools:
        return "(no tools available)"
    sorted_tools = sorted(tools, key=lambda t: t.name)
    lines = [f"- **{tool.name}**: {tool.description}" for tool in sorted_tools]
    return "\n".join(lines)


def build_tool_contract_rules(tools: list[Any]) -> str:
    """Build mandatory tool usage rules from declarative ``prompt_contract`` metadata.

    Tool definitions own these rules; this function only formats them.

    TODO: ``{name}`` placeholders in prompt_contract strings are not
    substituted.  Not triggered by current ``base.md`` templates.  When
    templates evolve, use the ``_SafeDict`` pattern from
    ``PromptAssembler._render_local`` as a reference implementation.
    """
    rules: list[str] = []
    for tool in tools:
        for contract in getattr(tool, "prompt_contract", ()):
            rules.append(f"- **{tool.name}**: {contract}")
    if rules:
        return "\n## CRITICAL TOOL USAGE RULES\n" + "\n".join(rules)
    return ""


def build_platform_info() -> str:
    """Declare the platform truth to the LLM — control plane, not secret translation."""
    import platform as _platform
    system = _platform.system()
    if system == "Windows":
        return (
            "## Platform\n"
            "You are running on **Windows**. Available shell: **PowerShell**.\n"
            "- Use PowerShell commands. Do NOT use Linux commands (wc, grep, find, cat, ls).\n"
            "- wc → `(Get-Content file).Count`\n"
            "- grep → `Select-String`\n"
            "- find → `Get-ChildItem -Recurse`\n"
            "- cat → `Get-Content`\n"
            "- ls → `dir` or `Get-ChildItem`\n"
            "- which → `where` or `Get-Command`"
        )
    return (
        "## Platform\n"
        "You are running on **Linux/macOS**. Available shell: **bash**.\n"
    )


class CapabilityPromptRenderer:
    def render(
        self,
        snapshot: CapabilitySnapshot,
        query: CapabilityQuery | None = None,
    ) -> list[CapabilitySection]:
        effective_query = query or CapabilityQuery()
        sections: list[CapabilitySection] = []
        if CapabilityKind.SKILL in effective_query.kinds:
            skill_section = self._render_skills(snapshot)
            if skill_section is not None:
                sections.append(skill_section)
        if CapabilityKind.MCP_TOOL in effective_query.kinds or CapabilityKind.MCP_SERVER in effective_query.kinds:
            sections.extend(self._render_mcp(snapshot))
        if CapabilityKind.AGENT in effective_query.kinds:
            agent_section = self._render_agents(snapshot)
            if agent_section is not None:
                sections.append(agent_section)
        return sections

    def _render_skills(self, snapshot: CapabilitySnapshot) -> CapabilitySection | None:
        skills = snapshot.by_kind(CapabilityKind.SKILL)
        if not skills:
            return None

        user_invocable = [
            descriptor.metadata.name for descriptor in skills
            if descriptor.metadata.user_invocable
        ]
        lines: list[str] = []
        if user_invocable:
            names = ", ".join(f"/{name}" for name in sorted(user_invocable))
            lines.append(f"User-invocable: {names}")

        lines.append(
            "Use the `Skill` tool to load a skill (PREFERRED — saves context by injecting instructions without duplicating):"
        )
        for descriptor in sorted(skills, key=lambda item: item.metadata.name):
            meta = descriptor.metadata
            desc = meta.description or "(no description)"
            if meta.when_to_use:
                desc += f" (Use when: {meta.when_to_use})"
            if meta.path_scopes:
                desc += f" (Path scope: {', '.join(meta.path_scopes)})"
            if meta.mcp_servers:
                desc += f" (MCP: {', '.join(meta.mcp_servers)})"
            lines.append(f"- **{meta.name}**: {desc}")

        return CapabilitySection(
            title="Skills",
            content="\n".join(lines),
            priority=20,
            token_estimate=0,
            kind_filter=CapabilityKind.SKILL,
            descriptor_count=len(skills),
            source_fingerprint=snapshot.fingerprint,
        )

    def _render_mcp(self, snapshot: CapabilitySnapshot) -> list[CapabilitySection]:
        sections: list[CapabilitySection] = []
        servers = snapshot.by_kind(CapabilityKind.MCP_SERVER)
        tools = snapshot.by_kind(CapabilityKind.MCP_TOOL)
        failed_servers = [
            descriptor for descriptor in servers
            if descriptor.runtime.status is CapabilityStatus.FAILED
        ]
        connected_servers = [
            descriptor for descriptor in servers
            if descriptor.runtime.status is not CapabilityStatus.FAILED
        ]
        deferred_tools = [
            descriptor for descriptor in tools
            if descriptor.runtime.status is CapabilityStatus.DEFERRED
        ]
        loaded_tools = [
            descriptor for descriptor in tools
            if descriptor.runtime.status is CapabilityStatus.AVAILABLE
        ]

        discovery_lines = []
        if connected_servers or deferred_tools or loaded_tools:
            discovery_lines.extend([
                "MCP tools may be loaded or deferred. Deferred tools are not directly callable until activated.",
                "Use `ToolSearch` before assuming an external capability is unavailable.",
            ])
            if connected_servers:
                discovery_lines.append("Connected MCP servers:")
                for descriptor in sorted(connected_servers, key=lambda item: item.metadata.name):
                    discovery_lines.append(f"- {descriptor.metadata.name} — {sanitize_text(descriptor.metadata.description)}")
            if loaded_tools:
                discovery_lines.append("Loaded MCP tools:")
                for descriptor in sorted(loaded_tools, key=lambda item: item.metadata.name):
                    discovery_lines.append(
                        f"- {descriptor.metadata.name} — {sanitize_text(descriptor.metadata.description)}"
                    )
            if deferred_tools:
                discovery_lines.append("Deferred MCP tools:")
                for descriptor in sorted(deferred_tools, key=lambda item: item.metadata.name):
                    discovery_lines.append(
                        f"- {descriptor.metadata.name} — {sanitize_text(descriptor.metadata.description)}"
                    )
            sections.append(CapabilitySection(
                title="MCP Tool Discovery",
                content="\n".join(discovery_lines),
                priority=30,
                token_estimate=0,
                kind_filter=CapabilityKind.MCP_TOOL,
                descriptor_count=len(connected_servers) + len(loaded_tools) + len(deferred_tools),
                source_fingerprint=snapshot.fingerprint,
            ))

        if failed_servers:
            failure_lines = ["Failed MCP servers:"]
            for descriptor in sorted(failed_servers, key=lambda item: item.metadata.name):
                error = sanitize_error(descriptor.runtime.error or descriptor.runtime.reason or "connection failed")
                failure_lines.append(f"- {descriptor.metadata.name} — {error}")
            sections.append(CapabilitySection(
                title="MCP Failures",
                content="\n".join(failure_lines),
                priority=10,
                token_estimate=0,
                kind_filter=CapabilityKind.MCP_SERVER,
                descriptor_count=len(failed_servers),
                source_fingerprint=snapshot.fingerprint,
            ))

        return sections

    def _render_agents(self, snapshot: CapabilitySnapshot) -> CapabilitySection | None:
        agents = snapshot.by_kind(CapabilityKind.AGENT)
        if not agents:
            return None

        lines = [
            "[Available Subagents]",
            "Available subagent types:",
        ]
        for descriptor in sorted(agents, key=lambda item: item.metadata.name):
            meta = descriptor.metadata
            workspace = meta.workspace_mode or "current"
            lines.append(f"- **{meta.name}** (workspace={workspace}): {meta.description}")

        if any(descriptor.metadata.delegation_scope == "read_only" for descriptor in agents):
            lines.append(
                "- This session has a read-only delegation scope. Never delegate edits, shell execution, or any other write-capable work."
            )

        lines.extend([
            "- Select only from the listed types. To delegate, call Agent(subagent_type=\"explore\").",
            "",
            "Delegation rules (Runtime-enforced where possible):",
            "- Subagents run in FRESH context — include ALL needed context in the prompt.",
            "- Each task MUST specify SCOPE (1-3 files), CONSTRAINTS (at least one negative), and DELIVERABLE (exact output expected).",
            "- Do simple or tightly coupled work yourself. Delegate only when specialization, context isolation, or parallelism has a clear benefit.",
            "- Use Agent for one bounded worker. For 2-4 independent tasks or a small dependency chain, use AgentBatch once with explicit task ids, scope, dependencies, expected files, write files, and deliverables.",
            "- If ProposeAgentTeam is available, use it only when peers must message one another or coordinate through a shared task board. A proposal never implies user approval or teammate activation.",
            "- Independent read-only or isolated-worktree tasks may fan out. Dependent tasks run in later waves; shared-workspace edits are serial.",
            "- Select the specialist whose description matches the task; do not route every investigation to the same generic worker.",
            "- Runtime enforces retry limits, loop detection, and circuit breaking — no need to count retries yourself.",
            "- When a subagent fails, read its <failure-diagnosis> and either retry once or handle the work yourself.",
            "- After a subagent completes, you can resume it with a follow-up task using SendMessage(to=\"{session_id}\") — the <resume-hint> in each completion notification shows the session ID.",
            "",
            "Result review:",
            "- Prefer structured findings from submit_findings (<subagent-report> XML).",
            "- Claims without file path + line + code evidence → mark [UNVERIFIED].",
            "- Never verbatim-forward — re-express in your own words.",
        ])

        if any(descriptor.metadata.workspace_mode == "worktree" for descriptor in agents):
            lines.extend([
                "",
                "Worktree Result Protocol (MANDATORY):",
                "- A worktree child edits an isolated Git worktree; its changes are NOT automatically present in the parent workspace.",
                "- If task-notification reports worktree-disposition=preserved, call subagent_worktree_inspect with that child session id.",
                "- Apply an acceptable result with subagent_worktree_apply using the exact revision returned by inspection, then verify the parent workspace.",
                "- If you do not apply it, report the preserved path and revision. Never claim that preserved changes landed in the parent workspace. First call subagent_worktree_retain with the inspected revision so the decision is recorded as an objective state transition.",
                "- Discard only when the result is definitively unwanted; discarding is permanent and also requires the inspected revision.",
            ])

        return CapabilitySection(
            title="Subagents",
            content="\n".join(lines),
            priority=25,
            token_estimate=0,
            kind_filter=CapabilityKind.AGENT,
            descriptor_count=len(agents),
            source_fingerprint=snapshot.fingerprint,
        )
