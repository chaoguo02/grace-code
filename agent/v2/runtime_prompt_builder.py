"""Runtime prompt builder — assembles runtime-injected messages for v2 sessions.

Extracted from SessionRuntime._build_runtime_messages().
Constitution: this is prompt composition, not runtime orchestration.
SessionRuntime should call this, not own the prompt-building details.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.v2.models import DelegationMode, SessionMode, WorkspaceMode

if TYPE_CHECKING:
    from agent.v2.models import AgentDefinition
    from llm.base import LLMMessage


def build_runtime_messages(
    spec: "AgentDefinition",
    task_description: str,
    *,
    agent_registry=None,
) -> list["LLMMessage"]:
    """Build runtime-injected messages for a v2 session.

    For primary agents, injects:
      - Plan mode injection (for plan agents)
      - Subagent delegation rules + available subagent list

    For sub-agents (spec.mode != "primary"), returns empty list.
    """
    if spec.mode != SessionMode.PRIMARY:
        return []

    from llm.base import LLMMessage
    messages: list[LLMMessage] = []

    if spec.permission_mode == "plan":
        from agent.prompt import get_plan_mode_injection
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
    from agent.v2.models import DelegationScope
    delegation_boundary = (
        "- This session has a read-only delegation scope. Never delegate "
        "edits, shell execution, or any other write-capable work.\n"
        if spec.effective_delegation_scope is DelegationScope.READ_ONLY
        else ""
    )
    content = (
        "[Available Subagents]\n"
        "You have a `task` tool to delegate subtasks to fresh-context subagents. "
        "Each agent's declared workspace mode is shown below.\n"
        f"Available subagent types:\n{subagent_descriptions}\n\n"
        "Task routing guide (MUST follow — wrong agent type causes loops):\n"
        f"{delegation_boundary}"
        "- Select only from the available subagent types listed above.\n"
        "When in doubt, use 'explore'. It has no shell and cannot accidentally modify files.\n\n"
        "Delegation isolation rules:\n"
        "- Each subagent runs in a FRESH context — it sees NONE of your conversation history.\n"
        "- workspace=current uses the parent project working tree. Only read-only "
        "current-workspace tasks may fan out in parallel.\n"
        "- workspace=worktree uses a separate Git worktree and requires explicit "
        "parent-side result handling.\n"
        "- Put ALL necessary context in the prompt: constraints, key facts, file paths, expected output.\n"
        "- The Runtime returns the subagent's final message plus any validated structured report.\n"
        "- Use subagents for independent, clearly-scoped work.\n"
        "- For 2-3 independent read-only investigations, emit their task calls together "
        "in one response; the Runtime will fan them out and return all results for synthesis.\n"
        "- Do simple tasks directly without delegating.\n"
        "- Never hand off understanding — you can delegate execution, not comprehension.\n"
        "- When the user explicitly asks to use the task tool or delegate, call it instead of answering directly.\n\n"
        "Atomic Task Boundaries (MANDATORY — prevent subagent failure at the source):\n"
        "Every task prompt you write MUST specify:\n"
        "1. SCOPE: which files to touch (limit to 1-3 files per subagent).\n"
        "2. CONSTRAINTS: what NOT to do. Always include at least one explicit\n"
        "   negative constraint (\"Do NOT modify files\", \"Do NOT run tests\",\n"
        "   \"Only read — do not write\", \"Stop after finding the root cause\").\n"
        "3. DELIVERABLE: the exact output format expected.\n"
        "A well-scoped subagent task finishes in 2-5 turns. If you think it\n"
        "needs more, SPLIT it into 2-3 smaller tasks and delegate each separately.\n"
        "Broad tasks like \"analyze this repo\" or \"fix the bugs\" will fail.\n\n"
        "Subagent Output Review Protocol (MANDATORY — you are the final arbiter):\n"
        "1. INSPECT before you relay. Every Confirmed Bug from a subagent MUST have:\n"
        "   - A specific file path and line number.\n"
        "   - A code snippet (``` fence) showing actual code read.\n"
        "   - A verification description (how the finding was confirmed).\n"
        "   If any of these is missing → DOWNGRADE to [UNVERIFIED]. Do NOT present as fact.\n"
        "2. CHECK findings structure. Prefer structured findings from the "
        "submit_findings tool (in the <subagent-report> XML block). "
        "These are Runtime-validated and reliable. Text-only summaries are "
        "supplementary — treat text claims without structured backing as unverified.\n"
        "3. SPOT DESIGN PATTERNS. Before accepting a bug report, ask yourself: "
        "\"Is this reported behavior actually documented as intentional?\" "
        "Examples of intentional patterns the subagent may misreport:\n"
        "   - partial status with success=True (constrained run, WARNING is prepended)\n"
        "   - Any behavior explained in comments, docstrings, or rules.\n"
        "4. NEVER verbatim-forward a subagent report. Always re-express findings "
        "in your own words after applying the checks above.\n"
        "5. STRUCTURE your final output as:\n"
        "   - Confirmed Issues (you or subagent verified with code evidence)\n"
        "   - Unverified Claims (subagent reported but lacks evidence → marked [UNVERIFIED])\n"
        "   - Design Observations (stylistic notes, not bugs)\n\n"
        "Subagent Failure Recovery:\n"
        "The Runtime enforces retry limits, loop detection, and circuit breaking.\n"
        "When a subagent fails, read the <failure-diagnosis> block and the status.\n"
        "- If the error looks transient (timeout, network) → retry once with the same task.\n"
        "- Otherwise → handle the work yourself or report to the user.\n"
        "- The system will stop you if you retry too many times — no need to count.\n"
        f"{worktree_review_protocol}"
    )
    messages.append(LLMMessage(role="user", content=content))
    return messages
