"""Fork subagent — Claude Code style child agent with fresh context."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.policy import PhasePolicy
from agent.policy_registry import PolicyAwareToolRegistry
from agent.task import RunResult, RunStatus, Task
from agent.v2.models import AgentDefinition, ForkResult
from context.history import ConversationHistory
from llm.base import LLMBackend, LLMMessage
from tools.base import ToolRegistry

logger = logging.getLogger(__name__)

_SUBAGENT_SUMMARY_RULE = """Your final answer is returned to the parent as a tool result.
The parent only sees your final message — not your full reasoning or tool history.
Make your final summary standalone and directly useful.

TOOL SELECTION RULES (violating these → wasted turns → loop detection):
1. USE THE DEDICATED TOOL FIRST. If a tool exists specifically for an operation,
   do NOT use shell/zsh/bash for that operation. Examples:
   - Read files → file_read (NOT cat/type/head/tail in shell)
   - Edit files → file_edit (NOT sed/awk in shell)
   - Write files → file_write (NOT echo/cat > in shell)
   - Search code → search_text (NOT grep -r in shell)
   - Find files → find_files (NOT find/ls in shell)
2. Shell is ONLY for: running tests, building, git operations, package
   managers, and other operations that have NO dedicated tool.
3. If you catch yourself about to type a shell command to read or search a
   file, STOP — use the dedicated tool instead.

CRITICAL RULES:
1. Only report findings that you can back up with specific file paths and line numbers.
2. If you cannot verify something, explicitly say "UNVERIFIED" rather than stating it as fact.
3. Never repeat information from the task prompt as if it were your own finding.
4. If the task cannot be completed within your constraints, report what you found AND what you couldn't verify.
5. The task tool may return success=True with status "partial". This is by design — it means the task was constrained (for example, max steps reached) but produced usable output. Do NOT report "partial with success=True" as a bug or logic error. Only flag it if the output is missing the required WARNING prefix or structured status tag.

VERIFICATION DISCIPLINE (mandatory — violations make your report unreliable):
A. Read before you claim. Every bug report MUST cite the actual code you read, not what you assume is there. Use Read/Grep to get the exact lines. Never reason from "likely" or "probably" — if you haven't read the line, say so.
B. Check design intent before calling something a bug. If behavior looks suspicious, search for comments, docstrings, tests, or rules (like the ones above) that may explain it as intentional. "success=True with status partial" is a documented design pattern — do not flag it.
C. Cross-reference at least 2 related files before filing a bug. Bugs are intersectional: a suspicious line in A may be intentional when you read B that consumes it. For code-review tasks, read both the target file AND at least one consumer or dependency.
D. Organize your report into exactly three sections:
   ## Confirmed Bugs (only findings verified by reading actual code — include file:line quote)
   ## Improvement Suggestions (style, clarity, robustness — not bugs)
   ## Unverified Hypotheses (claims you could not verify — EXPLICITLY mark each as guesswork)
E. Before submitting, re-read your Confirmed Bugs section. Delete any entry where you cannot point to a specific line of code you actually read.

SELF-CHECK before submitting: "Did I read the actual lines I'm citing? Did I check whether this behavior is intentional? Did I cross-reference at least one consumer?" If any answer is NO, move that finding to Unverified Hypotheses.
"""


@dataclass
class _ForkContext:
    """Internal context for a fork subagent run."""
    agent_id: str
    definition: AgentDefinition
    prompt: str
    repo_path: str
    log_dir: str
    tool_registry: ToolRegistry
    backend: LLMBackend
    hook_dispatcher: Any = None


def fork_subagent(
    *,
    definition: AgentDefinition,
    prompt: str,
    repo_path: str,
    base_registry: ToolRegistry,
    backend: LLMBackend,
    log_dir: str,
    root_agent_config: AgentConfig | None = None,
    hook_dispatcher: Any = None,
) -> ForkResult:
    """Run a subagent in a forked context.

    The subagent gets:
    - A fresh conversation context (no parent history)
    - Tools restricted to its definition's allowlist
    - Its own system prompt (from the agent definition's body)
    - The prompt as the first user message

    Returns a ForkResult with the subagent's final summary.
    """
    agent_id = uuid.uuid4().hex[:12]
    logger.info("Fork subagent '%s' (%s) starting: %s", definition.name, agent_id, prompt[:80])

    # Build restricted tool registry for this subagent
    from agent.v2.agent_registry import AgentRegistryV2
    registry_v2 = AgentRegistryV2()
    allowed_tools = registry_v2.tool_names_for(definition.name)

    restricted_registry = base_registry.filtered(allowed_tools)

    # ── Session-global FileReadCache (P1-1) ──
    # Subagents share the parent's mtime-verified cache. If the file hasn't
    # been modified (mtime unchanged), re-reading provides zero new information.
    # Write tools invalidate cache entries, guaranteeing freshness.
    # No per-agent cache isolation needed — mtime is the source of truth.

    # ── P1-5: Structured findings tool (fresh accumulator per subagent) ──
    from tools.submit_findings_tool import FindingsAccumulator, SubmitFindingsTool
    _findings_accumulator = FindingsAccumulator()
    _submit_findings_tool = SubmitFindingsTool(accumulator=_findings_accumulator)
    restricted_registry.register(_submit_findings_tool)

    # Phase-policy wrap
    wrapped_registry = PolicyAwareToolRegistry(
        base=restricted_registry,
        phase_policy=PhasePolicy(allowed_tools=frozenset(restricted_registry.tool_names)),
        repo_path=repo_path,
        phase_name=f"fork-{definition.name}",
    )

    # Build agent config
    if root_agent_config is not None:
        from copy import copy
        cfg = copy(root_agent_config)
    else:
        cfg = AgentConfig()

    cfg.max_steps = definition.max_turns
    cfg.stream = False
    cfg.stream_callback = None
    cfg.thought_callback = None
    cfg.compact_history = False

    # ── P0-2: Per-subagent Circuit Breaker ──
    # Each subagent gets its OWN circuit breaker cloned from the parent's.
    # This prevents subagents from looping indefinitely — the subagent
    # breaker trips independently of the parent's.
    if root_agent_config is not None and root_agent_config.circuit_breaker is not None:
        cfg.circuit_breaker = root_agent_config.circuit_breaker.clone_for_subagent()
        # Set a per-subagent time limit (e.g., 120s default)
        if cfg.circuit_breaker.config.max_elapsed_seconds == 0.0:
            cfg.circuit_breaker.config.max_elapsed_seconds = 120.0
    else:
        from agent.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
        cfg.circuit_breaker = CircuitBreaker(config=CircuitBreakerConfig(
            max_elapsed_seconds=120.0,
        ))

    # Build agent
    agent = ReActAgent(backend, wrapped_registry, cfg)

    # Fresh context — no parent history
    history = ConversationHistory(max_messages=cfg.history_max_messages)

    # System prompt from agent definition (the body after frontmatter)
    system_messages = _build_system_messages(definition)
    history.add_many(system_messages)

    # User prompt
    history.add(LLMMessage(role="user", content=prompt))

    agent._pending_history = history

    # Fire SubagentStart hook
    _fire_hook(hook_dispatcher, "SubagentStart", session_id=agent_id)

    # Run
    task = Task(
        description=prompt,
        repo_path=repo_path,
        intent="analysis",
        max_steps=cfg.max_steps,
        budget_tokens=cfg.budget_tokens,
        metadata={
            "entrypoint": "fork",
            "agent_name": definition.name,
            "agent_id": agent_id,
            "isolation": definition.isolation,
        },
    )

    _recent_actions: list[Any] = []
    try:
        with EventLog.create(task, log_dir=log_dir) as event_log:
            result = agent.run(task, event_log)
            _recent_actions = _snapshot_recent_actions(event_log)
    finally:
        _fire_hook(hook_dispatcher, "SubagentStop", session_id=agent_id)

    return _build_fork_result(
        definition.name, agent_id, result, _recent_actions,
        structured_findings=tuple(_findings_accumulator.all_findings()),
    )


def _build_system_messages(definition: AgentDefinition) -> list[LLMMessage]:
    messages: list[LLMMessage] = []

    # Agent-specific system prompt (from .md body)
    if definition.system_prompt:
        messages.append(LLMMessage(
            role="system",
            content=definition.system_prompt,
        ))

    # Universal subagent rules
    messages.append(LLMMessage(
        role="user",
        content=(
            f"[Subagent: {definition.name}]\n"
            f"{_SUBAGENT_SUMMARY_RULE}"
        ),
    ))

    return messages


def _snapshot_recent_actions(event_log: EventLog, window: int = 10) -> list[dict[str, Any]]:
    """Extract the last N tool-call actions from the event log.

    Returns a list of {name, params} dicts, most recent first.
    Used by _build_fork_result to enrich failure diagnosis.
    """
    actions = event_log.get_actions()
    recent: list[dict[str, Any]] = []
    for action in reversed(actions):
        if action.tool_calls:
            for tc in action.tool_calls:
                recent.append({"name": tc.name, "params": tc.params})
                if len(recent) >= window:
                    return recent
    return recent


def _build_fork_result(
    agent_name: str, agent_id: str, result: RunResult,
    recent_actions: list[dict[str, Any]] | None = None,
    structured_findings: tuple[dict[str, object], ...] = (),
) -> ForkResult:
    status = "completed"
    failure_diagnosis = ""
    if result.status == RunStatus.MAX_STEPS:
        status = "partial"
    elif not result.is_success():
        status = "failed"
        diagnosis = _build_structured_diagnosis(result, recent_actions or [])
        failure_diagnosis = diagnosis

    # ── P0-2: Enrich failure diagnosis with circuit breaker info ──
    terminated_by_loop = False
    if result.status == RunStatus.GAVE_UP:
        if "Circuit breaker tripped" in (result.summary or ""):
            failure_diagnosis = (
                f"{failure_diagnosis}\n"
                f"circuit_breaker: TRIPPED\n"
                f"circuit_breaker_reason: {result.summary}"
            ).strip()
            status = "failed"
        if "loop" in (result.summary or "").lower() or "Loop detected" in (result.summary or ""):
            terminated_by_loop = True
            failure_diagnosis = (
                f"{failure_diagnosis}\n"
                f"loop_detected: true"
            ).strip()

    summary = (result.summary or "").strip()
    if not summary:
        summary = "Subagent finished without a summary."

    return ForkResult(
        agent_name=agent_name,
        session_id=agent_id,
        status=status,
        summary=summary,
        error=result.error or "",
        turns_used=result.steps_taken,
        tokens_used=result.total_tokens,
        terminated_by_loop=terminated_by_loop,
        structured_findings=structured_findings,
        failure_diagnosis=failure_diagnosis,
    )


def _build_structured_diagnosis(
    result: RunResult,
    recent_actions: list[dict[str, Any]],
) -> str:
    """Build a structured failure diagnosis block for the parent agent.

    Claude Code style: key-value pairs so the parent can parse without
    scanning prose. Outputs:

        failure_type: <status>
        steps_consumed: <N>
        last_action: <tool_name(params_summary)>
        repeated_count: <N>
        error: <text>
        diagnosis: <one-line summary>
    """
    lines = [
        f"failure_type: {result.status.value}",
        f"steps_consumed: {result.steps_taken}",
    ]

    # Last action — most recent tool call
    if recent_actions:
        last = recent_actions[0]
        params_str = ", ".join(
            f"{k}={str(v)[:60]}" for k, v in list(last["params"].items())[:3]
        )
        lines.append(f"last_action: {last['name']}({params_str})")

    # Repeated count — consecutive identical tool calls
    names = [a["name"] for a in recent_actions]
    repeated = 1
    for i in range(1, len(names)):
        if names[i] == names[0]:
            repeated += 1
        else:
            break
    if repeated > 1:
        lines.append(f"repeated_count: {repeated}")

    if result.error:
        lines.append(f"error: {result.error}")

    # One-line diagnosis summary
    if result.status == RunStatus.GAVE_UP and "Loop detected" in (result.summary or ""):
        lines.append("diagnosis: Agent entered a loop repeating the same action")
    elif result.status == RunStatus.GAVE_UP:
        lines.append("diagnosis: Agent exhausted its analysis without completing")
    elif result.status == RunStatus.FAILED:
        lines.append("diagnosis: Agent encountered an unrecoverable error")
    elif result.status == RunStatus.MAX_STEPS:
        lines.append("diagnosis: Agent ran out of turns — task may need splitting")
    else:
        lines.append(f"diagnosis: Agent terminated with status {result.status.value}")

    return "\n".join(lines)


def _fire_hook(dispatcher: Any, event_name: str, session_id: str = "") -> None:
    if dispatcher is None:
        return
    try:
        from hooks.events import HookContext, HookEvent
        evt = HookEvent(event_name)
        ctx = HookContext(event=evt, session_id=session_id)
        dispatcher.dispatch(evt, ctx)
    except Exception:
        logger.debug("Hook %s failed for session %s", event_name, session_id, exc_info=True)
