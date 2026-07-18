"""Runtime prompt builder — assembles runtime-injected messages for v2 sessions.

Extracted from SessionRuntime._build_runtime_messages().
Constitution: this is prompt composition, not runtime orchestration.
SessionRuntime should call this, not own the prompt-building details.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.session.models import DelegationMode, SessionMode, WorkspaceMode

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
) -> list["LLMMessage"]:
    """Build runtime-injected messages for a v2 session.

    For all agents (primary + subagent), injects:
      - Preloaded skills content (if spec.skills is set)
      - Persistent memory context (if spec.memory is set)
    For primary agents additionally injects:
      - Plan mode injection (for analysis agents)
      - Subagent delegation rules + available subagent list
      - Available Skills listing (CC-aligned Phase 1)
    """
    from llm.base import LLMMessage
    messages: list[LLMMessage] = []

    # ── Skills preloading (CC-aligned: full SKILL.md content injected) ──
    if spec.skills:
        skill_contents = _load_skills(spec.skills, project_dir)
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

    # Dynamically generate subagent descriptions from the registry
    available_subagents = (
        agent_registry.delegatable_by(spec)
    )
    if not available_subagents:
        return messages
    subagent_descriptions = "\n".join(
        f"- **{s.name}** (workspace={s.workspace_mode.value}): {s.description}"
        for s in available_subagents
    )
    has_worktree_subagent = any(
        child.workspace_mode is WorkspaceMode.WORKTREE
        for child in available_subagents
    )
    worktree_review_protocol = (
        "\nWorktree Result Protocol (MANDATORY):\n"
        "- A worktree child edits an isolated Git worktree; its changes are NOT "
        "automatically present in the parent workspace.\n"
        "- If task-notification reports worktree-disposition=preserved, call "
        "subagent_worktree_inspect with that child session id.\n"
        "- Apply an acceptable result with subagent_worktree_apply using the exact "
        "revision returned by inspection, then verify the parent workspace.\n"
        "- If you do not apply it, report the preserved path and revision. Never "
        "claim that preserved changes landed in the parent workspace. First call "
        "subagent_worktree_retain with the inspected revision so the decision is "
        "recorded as an objective state transition.\n"
        "- Discard only when the result is definitively unwanted; discarding is "
        "permanent and also requires the inspected revision.\n"
        if has_worktree_subagent
        else ""
    )
    from agent.session.models import DelegationScope
    delegation_boundary = (
        "- This session has a read-only delegation scope. Never delegate "
        "edits, shell execution, or any other write-capable work.\n"
        if spec.effective_delegation_scope is DelegationScope.READ_ONLY
        else ""
    )
    content = (
        "[Available Subagents]\n"
        f"Available subagent types:\n{subagent_descriptions}\n"
        f"{delegation_boundary}"
        "- Select only from the listed types. To delegate, call Agent(subagent_type=\"explore\").\n\n"
        "Delegation rules (Runtime-enforced where possible):\n"
        "- Subagents run in FRESH context — include ALL needed context in the prompt.\n"
        "- Each task MUST specify SCOPE (1-3 files), CONSTRAINTS (at least one "
        "negative), and DELIVERABLE (exact output expected).\n"
        "- For 2-3 independent read-only tasks, emit them together; the Runtime "
        "fans them out in parallel.\n"
        "- Runtime enforces retry limits, loop detection, and circuit breaking — "
        "no need to count retries yourself.\n"
        "- When a subagent fails, read its <failure-diagnosis> and either retry "
        "once or handle the work yourself.\n\n"
        "Result review:\n"
        "- Prefer structured findings from submit_findings (<subagent-report> XML).\n"
        "- Claims without file path + line + code evidence → mark [UNVERIFIED].\n"
        "- Never verbatim-forward — re-express in your own words.\n"
        f"{worktree_review_protocol}"
    )
    messages.append(LLMMessage(role="user", content=content))

    # CC-aligned: inject Available Skills listing (Phase 1)
    if skill_registry is not None:
        skill_listing = skill_registry.format_for_prompt(llm_invocable_only=True)
        if skill_listing:
            messages.append(LLMMessage(role="user", content=skill_listing))

    return messages


def _load_skills(skill_names: tuple[str, ...], project_dir: str | None) -> list[str]:
    """Load SKILL.md content for preloading into agent context."""
    from pathlib import Path
    contents: list[str] = []
    search_dirs: list[Path] = []
    if project_dir:
        search_dirs.append(Path(project_dir) / ".forge-agent" / "skills")
    search_dirs.append(Path.home() / ".forge-agent" / "skills")
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
        mem_dir = Path.home() / ".forge-agent" / "agent-memory" / name
    elif scope == "project" and project_dir:
        mem_dir = Path(project_dir) / ".forge-agent" / "agent-memory" / name
    elif scope == "local" and project_dir:
        mem_dir = Path(project_dir) / ".forge-agent" / "agent-memory-local" / name
    else:
        return ""
    mem_file = mem_dir / "MEMORY.md"
    if mem_file.exists():
        try:
            return mem_file.read_text(encoding="utf-8")[:25_000]
        except OSError:
            pass
    return ""
