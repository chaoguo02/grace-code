"""Child-agent execution for fresh definitions and inherited parent snapshots."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.task import RunResult, RunStatus, Task, TerminationReason
from agent.v2.models import (
    AgentDefinition,
    AgentKind,
    AgentRunResult,
    AgentSpawnRequest,
    ContextOrigin,
    ForkStatus,
    WorktreeChange,
    WorktreeDisposition,
    WorkspaceMode,
)
from context.history import ConversationHistory
from hooks.events import HookEvent
from llm.base import LLMBackend, LLMMessage
from tools.base import ToolRegistry
from agent.v2.result_contract import SubagentReport, SubagentReportStatus
from agent.v2.run_context import AgentSpawnContext, CancellationToken

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent.policy import PhasePolicy
    from agent.v2.models import SessionRecord, WorktreeEvidence
    from agent.v2.runtime import SessionRuntime
    from agent.v2.task_contract import TaskContract

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


def run_child_agent(
    *,
    agent_id: str,
    request: AgentSpawnRequest,
    source_definition: AgentDefinition,
    repo_path: str,
    base_registry: ToolRegistry,
    backend: LLMBackend,
    log_dir: str,
    root_agent_config: AgentConfig | None = None,
    message_sink: Callable[[list[LLMMessage]], None] | None = None,
    contract: "TaskContract",
    cancellation_token: CancellationToken,
    parent_policy: "PhasePolicy",
    spawn_context: AgentSpawnContext | None = None,
    inherited_registry: ToolRegistry | None = None,
    event_callback: Callable[[Any], None] | None = None,
    persisted_messages: list[LLMMessage] | None = None,
    session_record: "SessionRecord | None" = None,
    session_runtime: "SessionRuntime | None" = None,
) -> AgentRunResult:
    """Run a typed child request while preserving its context-origin contract."""
    definition = source_definition
    prompt = request.prompt
    result_agent_name = (
        AgentKind.FORK.value
        if request.agent_kind is AgentKind.FORK
        else definition.name
    )
    logger.info(
        "Child agent '%s' (%s) starting: %s",
        result_agent_name, agent_id, prompt[:80],
    )

    if request.agent_kind is AgentKind.FORK:
        if (
            request.context_origin is ContextOrigin.PARENT_SNAPSHOT
            and spawn_context is None
        ):
            raise ValueError("Fork execution requires a live parent snapshot")
        if inherited_registry is None:
            raise ValueError("Fork execution requires the parent's tool contract")
    if request.context_origin is ContextOrigin.RESUMED and persisted_messages is None:
        raise ValueError("Resumed execution requires the persisted child transcript")

    if cancellation_token.is_cancelled:
        return AgentRunResult(
            agent_name=result_agent_name,
            session_id=agent_id,
            status=ForkStatus.CANCELLED,
            summary=f"Subagent cancelled: {cancellation_token.detail}",
            error=cancellation_token.detail,
        )

    # ── Phase 6.2: Git Worktree isolation ──
    from agent.v2.worktree_service import WorktreeIsolationError, create_worktree
    try:
        _worktree, _effective_repo_path = create_worktree(
            repo_path, result_agent_name, agent_id,
            isolation=request.workspace_mode,
        )
    except WorktreeIsolationError as exc:
        return AgentRunResult(
            agent_name=result_agent_name,
            session_id=agent_id,
            status=ForkStatus.FAILED,
            summary="Subagent isolation could not be established",
            error=str(exc),
            failure_diagnosis=str(exc),
        )

    # Named children use their definition; forks use the parent's exact
    # reconstructed tool contract supplied by SessionRuntime.
    if request.agent_kind is AgentKind.NAMED_SUBAGENT:
        from agent.v2.subagent_registry_factory import build_restricted_registry
        child_base_registry = base_registry.with_permission_request_origin(
            result_agent_name
        )
        wrapped_registry, _findings_accumulator = build_restricted_registry(
            definition,
            child_base_registry,
            repo_path=_effective_repo_path,
            parent_policy=parent_policy,
            session=session_record,
            agent_registry=(
                session_runtime.agent_registry
                if session_runtime is not None else None
            ),
            runtime=session_runtime,
            circuit_breaker=None,
        )
    else:
        if request.workspace_mode is WorkspaceMode.WORKTREE:
            from tools.base import ExecutionContext
            inherited_registry = inherited_registry.scoped(ExecutionContext(
                workspace_root=_effective_repo_path,
                repo_path=_effective_repo_path,
            ))
        from tools.submit_findings_tool import FindingsAccumulator
        wrapped_registry = inherited_registry
        _findings_accumulator = FindingsAccumulator()

    # Build agent config
    if root_agent_config is not None:
        from copy import copy
        cfg = copy(root_agent_config)
    else:
        cfg = AgentConfig()

    cfg.max_steps = contract.max_steps
    cfg.budget_tokens = contract.budget_tokens
    cfg.cancellation_token = cancellation_token
    cfg.stream = False
    cfg.stream_callback = None
    cfg.thought_callback = None
    cfg.compact_history = False
    cfg.stop_hook_event = HookEvent.SUBAGENT_STOP
    cfg.hook_session_id = (
        session_record.parent_id
        if session_record is not None and session_record.parent_id is not None
        else agent_id
    )
    cfg.hook_agent_id = agent_id
    cfg.hook_agent_type = result_agent_name
    if session_runtime is not None:
        cfg.hook_dispatcher = session_runtime.hook_dispatcher

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
    agent = ReActAgent(
        backend,
        wrapped_registry,
        cfg,
        inherited_context=(
            spawn_context.conversation
            if request.agent_kind is AgentKind.FORK and spawn_context is not None
            else None
        ),
    )

    # Child-local messages; inherited parent messages remain an immutable prefix.
    history_capacity = cfg.history_max_messages
    if request.context_origin is ContextOrigin.RESUMED:
        history_capacity += len(persisted_messages or [])
    history = ConversationHistory(max_messages=history_capacity)

    if request.agent_kind is AgentKind.NAMED_SUBAGENT:
        # Named agents start from their definition, never parent history.
        history.add_many(_build_system_messages(definition))

    if request.context_origin is ContextOrigin.RESUMED:
        history.add_many(persisted_messages or [])
    else:
        # The initial prompt is already persisted by SessionRuntime, but the
        # child-local history must also contain it for this invocation.
        history.add(LLMMessage(role="user", content=prompt))
    _persisted_history_boundary = len(history)

    agent._pending_history = history

    # Run
    task = Task(
        description=prompt,
        repo_path=_effective_repo_path,
        intent=definition.intent,
        max_steps=contract.max_steps,
        budget_tokens=contract.budget_tokens,
        metadata={
            "entrypoint": request.agent_kind.value,
            # Definition identity remains the parent's for a fork; the
            # orthogonal agent_kind field records that this run is a fork.
            "agent_name": definition.name,
            "agent_id": agent_id,
            "session_id": agent_id,
            "parent_session_id": (
                session_record.parent_id if session_record is not None else None
            ),
            "root_session_id": (
                session_record.root_id if session_record is not None else agent_id
            ),
            "agent_kind": request.agent_kind.value,
            "context_origin": request.context_origin.value,
            "workspace_mode": request.workspace_mode.value,
            "agent_depth": (
                session_record.agent_depth.value
                if session_record is not None else 1
            ),
            "worktree_path": _worktree.path if _worktree else "",
            "completion_requires": dict(contract.require_deliverables),
            "required_tools": sorted(definition.required_tools),
        },
    )

    _recent_actions: list[Any] = []
    _worktree_evidence: "WorktreeEvidence | None" = None
    _worktree_disposition = (
        WorktreeDisposition.CLEANED
        if _worktree is not None
        else WorktreeDisposition.NOT_APPLICABLE
    )

    # ── Result object fallback: never let a bare exception escape ──
    # The parent MUST receive a structured AgentRunResult regardless of what
    # happens inside the subagent. Initialize to a failed state so even
    # if the try block never executes, the contract is honored.
    result = RunResult(
        task_id=agent_id, status=RunStatus.FAILED,
        summary="Subagent did not start", steps_taken=0, total_tokens=0,
    )

    try:
        with EventLog.create(task, log_dir=log_dir) as event_log:
            if event_callback is not None:
                original_append = event_log._append

                def _append_and_emit(event):
                    original_append(event)
                    try:
                        event_callback(event)
                    except Exception:
                        logger.debug(
                            "Subagent event callback failed", exc_info=True,
                        )

                event_log._append = _append_and_emit
            result = agent.run(task, event_log)
            _recent_actions = _snapshot_recent_actions(event_log)

    except MemoryError:
        logger.critical("Subagent '%s' OOM — aborting", definition.name)
        result = RunResult(
            task_id=agent_id, status=RunStatus.FAILED,
            summary="Subagent ran out of memory",
            steps_taken=0, total_tokens=0,
            error="MemoryError: subagent exceeded available memory",
        )

    except Exception as exc:
        logger.exception("Subagent '%s' crashed: %s", definition.name, exc)
        result = RunResult(
            task_id=agent_id, status=RunStatus.FAILED,
            summary=f"Subagent crashed: {exc}",
            steps_taken=0, total_tokens=0, error=str(exc),
        )

    finally:
        if message_sink is not None:
            try:
                message_sink(history.to_list()[_persisted_history_boundary:])
            except Exception as exc:
                logger.exception(
                    "Subagent '%s' transcript persistence failed: %s",
                    definition.name, exc,
                )
                result = RunResult(
                    task_id=agent_id,
                    status=RunStatus.FAILED,
                    summary="Subagent transcript persistence failed",
                    steps_taken=result.steps_taken,
                    total_tokens=result.total_tokens,
                    error=str(exc),
                )
        if _worktree is not None:
            # Always finalize from Git facts, including crash/cancellation paths.
            # Only an objectively unchanged worktree may be removed.
            from agent.v2.worktree_service import finalize_worktree
            evidence = finalize_worktree(_worktree, repo_path)
            if evidence.change is not WorktreeChange.NONE:
                _worktree_evidence = evidence
                _worktree_disposition = WorktreeDisposition.PRESERVED

    # ── Contract: result is ALWAYS a valid RunResult at this point ──
    warnings: list[str] = []
    if _worktree_evidence is not None:
        if _worktree_evidence.change is WorktreeChange.UNKNOWN:
            warnings.append(
                "Subagent worktree inspection was inconclusive and the "
                f"worktree was preserved at {_worktree_evidence.path}: "
                f"{_worktree_evidence.error or 'unknown Git inspection error'}"
            )
    if result.status == RunStatus.MAX_STEPS:
        warnings.append("Subagent reached max steps; its result may be incomplete.")

    return _build_fork_result(
        result_agent_name, agent_id, result, _recent_actions,
        report=_findings_accumulator.combined_report(),
        warning=" ".join(warnings), worktree=_worktree_evidence,
        worktree_disposition=_worktree_disposition,
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
    report: SubagentReport | None = None,
    warning: str = "",
    worktree: "WorktreeEvidence | None" = None,
    worktree_disposition: WorktreeDisposition = WorktreeDisposition.NOT_APPLICABLE,
) -> AgentRunResult:
    status = ForkStatus.from_run_status(result.status)
    failure_diagnosis = ""
    if status is ForkStatus.FAILED:
        diagnosis = _build_structured_diagnosis(result, recent_actions or [])
        failure_diagnosis = diagnosis

    # ── P0-2: Enrich failure diagnosis with circuit breaker info ──
    if result.status == RunStatus.GAVE_UP:
        if result.termination_reason == TerminationReason.CIRCUIT_BREAKER:
            failure_diagnosis = (
                f"{failure_diagnosis}\n"
                f"circuit_breaker: TRIPPED\n"
                f"circuit_breaker_reason: {result.summary}"
            ).strip()
            status = ForkStatus.FAILED

    if (
        status is ForkStatus.COMPLETED
        and report is not None
        and report.status is SubagentReportStatus.PARTIAL
    ):
        status = ForkStatus.PARTIAL

    summary = (result.summary or "").strip()
    if not summary:
        summary = "Subagent finished without a summary."

    return AgentRunResult(
        agent_name=agent_name,
        session_id=agent_id,
        status=status,
        summary=summary,
        error=result.error or "",
        turns_used=result.steps_taken,
        tokens_used=result.total_tokens,
        report=report,
        failure_diagnosis=failure_diagnosis,
        warning=warning,
        worktree=worktree,
        worktree_disposition=worktree_disposition,
    )


def fork_subagent(
    *,
    agent_id: str,
    definition: AgentDefinition,
    prompt: str,
    repo_path: str,
    base_registry: ToolRegistry,
    backend: LLMBackend,
    log_dir: str,
    root_agent_config: AgentConfig | None = None,
    message_sink: Callable[[list[LLMMessage]], None] | None = None,
    contract: "TaskContract",
    cancellation_token: CancellationToken,
    parent_policy: "PhasePolicy",
    event_callback: Callable[[Any], None] | None = None,
) -> AgentRunResult:
    """Compatibility entrypoint for a fresh named child."""
    return run_child_agent(
        agent_id=agent_id,
        request=AgentSpawnRequest.named(
            definition=definition,
            description=prompt[:80] or definition.name,
            prompt=prompt,
        ),
        source_definition=definition,
        repo_path=repo_path,
        base_registry=base_registry,
        backend=backend,
        log_dir=log_dir,
        root_agent_config=root_agent_config,
        message_sink=message_sink,
        contract=contract,
        cancellation_token=cancellation_token,
        parent_policy=parent_policy,
        event_callback=event_callback,
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
    if result.status == RunStatus.GAVE_UP:
        lines.append("diagnosis: Agent exhausted its analysis without completing")
    elif result.status == RunStatus.FAILED:
        lines.append("diagnosis: Agent encountered an unrecoverable error")
    elif result.status == RunStatus.MAX_STEPS:
        lines.append("diagnosis: Agent ran out of turns — task may need splitting")
    else:
        lines.append(f"diagnosis: Agent terminated with status {result.status.value}")

    return "\n".join(lines)
