"""Runtime prompt builder — assembles runtime-injected messages for sessions.

Extracted from SessionRuntime._build_runtime_messages().
Constitution: this is prompt composition, not runtime orchestration.
SessionRuntime should call this, not own the prompt-building details.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from agent.session.models import DelegationMode, SessionMode

if TYPE_CHECKING:
    from agent.session.models import AgentDefinition
    from llm.base import LLMMessage


def build_runtime_messages(
    spec: "AgentDefinition",
    task_description: str,
    *,
    agent_registry=None,
    project_dir: str | None = None,
    skill_registry=None,
    mcp_integration=None,
    on_skill_preload: Any = None,
    inject_capability_context: bool = True,
) -> list["LLMMessage"]:
    """Build runtime-injected messages for a session.

    For all agents (primary + subagent), injects:
      - Preloaded skills content (if spec.skills is set)
      - Persistent memory context (if spec.memory is set)
    For primary agents additionally injects:
      - Plan mode injection (for analysis agents)
      - One unified capability context for Skills, MCP, and Subagents
    """
    from llm.base import LLMMessage
    messages: list[LLMMessage] = []

    # ── Skills preloading (CC-aligned: full SKILL.md content injected) ──
    if spec.skills:
        skill_contents = _load_skills(
            spec.skills,
            project_dir,
            skill_registry=skill_registry,
            on_skill_preload=on_skill_preload,
        )
        if skill_contents:
            messages.append(LLMMessage(
                role="user",
                content="[PRELOADED SKILLS]\n" + "\n---\n".join(skill_contents)
            ))

    # ── Persistent memory (CC-aligned: first 25KB of MEMORY.md injected) ──
    if spec.memory:
        memory_content = _load_agent_memory(spec, project_dir)
        if memory_content:
            messages.append(LLMMessage(
                role="user",
                content=f"[AGENT MEMORY]\n{memory_content}\n\n"
                        "Review your memory above for patterns and decisions "
                        "from previous sessions. Update it after completing work."
            ))

    capability_context = ""
    if inject_capability_context:
        # DEPRECATED: Capability context now lives in the system prompt
        # (ContextManager.build_request_messages → StructuredContext layer).
        # This path is never reached in normal operation because
        # SessionRuntime calls _build_runtime_messages with
        # inject_capability_context=False, and capability sections are
        # passed to ContextManager.build_request_messages() instead.
        from capabilities import build_capability_context
        capability_context = build_capability_context(
            spec=spec,
            skill_registry=skill_registry,
            mcp_integration=mcp_integration,
            agent_registry=agent_registry,
        )
    if capability_context:
        messages.append(LLMMessage(role="user", content=capability_context))

    if spec.mode != SessionMode.PRIMARY:
        return messages

    if spec.permission_mode == "plan":
        from prompts.builder import get_plan_mode_injection
        messages.append(LLMMessage(role="user", content=get_plan_mode_injection()))
        # Structured contract: Plan agent MUST output JSON
        messages.append(LLMMessage(role="user", content=(
            '## Output Format (MANDATORY)\n\n'
            'At the end of your plan, you MUST include a JSON contract block:\n\n'
            '```json\n'
            '{\n'
            '  "objective": "One sentence describing the business goal",\n'
            '  "execution_intent": "analysis",\n'
            '  "target_files": ["path/to/file1.py", "path/to/file2.py"],\n'
            '  "expected_behavior": "What the system should do after changes",\n'
            '  "verification_strategy": "pytest test_auth.py",\n'
            '  "potential_conflicts": ["risk 1", "risk 2"]\n'
            '}\n'
            '```\n\n'
            'Rules:\n'
            '- ALL six fields are required (potential_conflicts can be empty array)\n'
            '- execution_intent MUST be analysis for read-only answers, edit for changes\n'
            '- target_files MUST list files to inspect, create, or modify\n'
            '- If you cannot determine a field, write "NEEDS CLARIFICATION: <question>"\n'
            '- Do NOT call finish without this JSON block in your output'
        )))

    if spec.delegation_policy.mode is DelegationMode.DISABLED:
        return messages
    if agent_registry is None:
        raise ValueError("delegation prompt requires an agent registry")

    return messages


def _load_skills(
    skill_names: tuple[str, ...],
    project_dir: str | None,
    *,
    skill_registry=None,
    on_skill_preload: Any = None,
) -> list[str]:
    """Load SKILL.md content for preloading into agent context."""
    from pathlib import Path
    contents: list[str] = []
    if skill_registry is not None:
        for skill_name in skill_names:
            rendered = skill_registry.load_and_render(
                skill_name,
                project_dir=project_dir or "",
            )
            if rendered:
                contents.append(f"=== {skill_name} ===\n{rendered}")
                # ── Evidence: record preload activation ──
                if on_skill_preload is not None:
                    try:
                        meta = skill_registry.get_skill_meta(skill_name)
                        fingerprint = ""
                        mcp_deps: list[str] = []
                        if meta is not None:
                            from skills.activation import skill_fingerprint
                            fingerprint = skill_fingerprint(meta)
                            mcp_deps = sorted(meta.mcp_servers) if meta.mcp_servers else []
                        on_skill_preload(
                            skill_name,
                            source="preload",
                            fingerprint=fingerprint,
                            mcp_dependencies=mcp_deps,
                        )
                    except Exception:
                        pass
            else:
                import logging
                # Phase 1 #4: preload failure → ERROR + runtime notice
                _err_msg = f"(skill \"{skill_name}\" failed to load — file missing or malformed)"
                logging.getLogger(__name__).error(
                    "Skill %r not found or empty — runtime notice appended",
                    skill_name,
                )
                contents.append(_err_msg)
        return contents

    search_dirs: list[Path] = []
    if project_dir:
        search_dirs.append(Path(project_dir) / ".grace" / "skills")
    search_dirs.append(Path.home() / ".grace" / "skills")
    for skill_name in skill_names:
        loaded = False
        for base in search_dirs:
            skill_dir = base / skill_name
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                try:
                    text = skill_file.read_text(encoding="utf-8")
                    contents.append(f"=== {skill_name} ===\n{text}")
                    loaded = True
                except OSError:
                    pass
                break
        if not loaded:
            import logging
            logging.getLogger(__name__).warning(
                "Skill %r not found", skill_name,
            )
    return contents


def _load_agent_memory(spec: "AgentDefinition", project_dir: str | None) -> str:
    """Load MEMORY.md content for an agent's persistent memory scope."""
    from pathlib import Path
    scope = spec.memory
    name = spec.name
    if scope == "user":
        mem_dir = Path.home() / ".grace" / "agent-memory" / name
    elif scope == "project" and project_dir:
        mem_dir = Path(project_dir) / ".grace" / "agent-memory" / name
    elif scope == "local" and project_dir:
        mem_dir = Path(project_dir) / ".grace" / "agent-memory-local" / name
    else:
        return ""
    mem_file = mem_dir / "MEMORY.md"
    if mem_file.exists():
        try:
            return mem_file.read_text(encoding="utf-8")[:25_000]
        except OSError:
            pass
    return ""
