"""AgentTool — dispatch a typed named child or inherited-context fork.

Architecture: runtime-enforced subagent quality, with a light prompt layer.

  Layer 0 (runtime contract, primary): submit_findings / ReportFindings carries
    structured output. Runtime validates paths, line numbers, evidence, and
    completion requirements before the parent consumes the result.
  Layer 1 (prompt, secondary): _SUBAGENT_PROTOCOL supplies only the minimal
    analysis discipline that still helps the model stay on task.
  Layer 2 (parent review): runtime prompt building reminds the parent to inspect
    subagent evidence instead of rubber-stamping it.
"""

from __future__ import annotations

import logging
import copy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from agent.task import TaskIntent
from agent.session.models import (
    AgentKind, AgentSpawnRequest, BackgroundAgentHandle, DelegationScope,
    ExecutionPlacement, ForkStatus, WorkspaceMode,
)
from core.base import (
    ToolConcurrency, ToolEffect, ToolErrorType, ToolMetadata,
    ToolRetryDirective, ToolRole,
)
from core.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from agent.session.models import AgentRunResult
    from agent.session.runtime import SessionRuntime

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Layer 1: Subagent Analysis Protocol (hardcoded in tool prompt — prompt layer)
# ═══════════════════════════════════════════════════════════════════════════

_SUBAGENT_PROTOCOL = """
[SUBAGENT ANALYSIS PROTOCOL — CC-aligned]
You are a subagent running in a FRESH context — you see NONE of the parent's
conversation history. Your final message IS your return value.

## OUTPUT CONTRACT
- Target output: 1,000–2,000 tokens unless the parent explicitly asks for more.
- Be concise. The parent pays for every token you return. Prefer structured
  summaries over prose. If using ReportFindings/submit_findings, use it.
- If you could NOT complete the task: say so clearly, state what's missing,
  and provide whatever partial results you have. Do NOT fabricate completion.

## CONTEXT (what the parent already knows)
- The parent has already explored the codebase and formed an initial plan.
  Do NOT re-discover what the task description already states as known.
- Focus on the SPECIFIC gap the parent assigned to you — not the whole problem.

## TOOL DISCIPLINE
- Use Read/Grep/Glob BEFORE shell. Shell is ONLY for tests, builds, git,
  and package managers. NEVER use shell to read or search files.
- Respect rate limits on external APIs.

## BOUNDARIES
- Only report findings with concrete evidence (file paths, line numbers,
  actual code). Label unverified claims as "[unverified]".
- Do NOT expand scope beyond the assigned task.
- Do NOT edit code unless the parent explicitly asked you to — your job is
  to ANALYZE and REPORT.
- If your tool set does NOT include Write/Edit: you are READ-ONLY.

## PARALLEL DISPATCH (if you are one of several agents)
- Stay strictly within your assigned scope. Do NOT investigate what other
  agents were assigned — overlap wastes tokens and creates conflicts.
- If you discover something another agent should know: note it in your
  output so the parent can relay it. Do NOT try to coordinate yourself.

## Deliverable contract
"""


@dataclass(frozen=True)
class _SpawnPlanningFacts:
    """Typed runtime facts used to plan one child launch.

    This is intentionally narrower than ``AgentSpawnRequest``:
    it captures caller-provided spawn intent plus the resolved target
    definition/workspace facts that runtime policy needs for concurrency and
    AUTO placement decisions.
    """

    subagent_type: str
    is_fork: bool
    workspace_mode: WorkspaceMode
    definition: Any | None


@dataclass(frozen=True)
class _SpawnInvocationPlan:
    """Validated caller input plus resolved typed facts for one launch."""

    description: str
    user_prompt: str
    requested_placement: ExecutionPlacement
    facts: _SpawnPlanningFacts


class AgentTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.DELEGATE_WRITE}),
        roles=frozenset({ToolRole.DELEGATE}),
    )
    """Dispatch a named subagent or fork through one Runtime spawn path.

    Named children use their definition and fresh context. Forks inherit the
    parent request prefix, model, and tools. Foreground calls return the final
    result; background calls return a session handle and deliver completion later.

    Usage:
        AgentTool(runtime, parent_session_id, caller_agent_name=agent_name)
    """

    def __init__(
        self,
        runtime: "SessionRuntime",
        parent_session_id: str,
        *,
        caller_agent_name: str,
        circuit_breaker: Any = None,
    ) -> None:
        self._runtime = runtime
        self._parent_session_id = parent_session_id
        self._caller_agent_name = caller_agent_name
        self._circuit_breaker = circuit_breaker
        self._run_context = None
        delegation_scope = (
            runtime.agent_registry.get(caller_agent_name)
            .effective_delegation_scope
        )
        delegation_effect = (
            ToolEffect.DELEGATE_READ_ONLY
            if delegation_scope is DelegationScope.READ_ONLY
            else ToolEffect.DELEGATE_WRITE
        )
        self.metadata = ToolMetadata(
            effects=frozenset({delegation_effect}),
            roles=frozenset({ToolRole.DELEGATE}),
        )

    def with_run_context(self, context: Any) -> "AgentTool":
        """Bind the parent run's live budget and cancellation facts."""
        from agent.session.run_context import RunContext
        if not isinstance(context, RunContext):
            raise TypeError("AgentTool requires a RunContext")
        bound = copy.copy(self)
        bound._run_context = context
        return bound

    def _planning_facts_from_params(
        self, params: dict[str, Any],
    ) -> _SpawnPlanningFacts | None:
        """Best-effort typed spawn facts for runtime policy decisions.

        Returns ``None`` for invalid or unauthorized inputs so the caller can
        fall back to the safest serial/foreground behavior or report a richer
        execution-time validation error.
        """
        raw_subagent_type = params.get("subagent_type")
        if not isinstance(raw_subagent_type, str):
            return None
        subagent_type = raw_subagent_type.strip()
        if not subagent_type:
            return None
        allowed = self._allowed_subagent_names()
        if subagent_type not in allowed:
            return None

        is_fork = subagent_type == AgentKind.FORK.value
        try:
            workspace_mode = WorkspaceMode(
                params.get("isolation", WorkspaceMode.CURRENT.value)
            )
        except (TypeError, ValueError):
            return None

        if is_fork:
            return _SpawnPlanningFacts(
                subagent_type=subagent_type,
                is_fork=True,
                workspace_mode=workspace_mode,
                definition=None,
            )

        if not self._runtime.agent_registry.has(subagent_type):
            return None
        definition = self._runtime.agent_registry.get(subagent_type)
        if "isolation" in params:
            definition = replace(definition, workspace_mode=workspace_mode)
        else:
            workspace_mode = definition.workspace_mode
        return _SpawnPlanningFacts(
            subagent_type=subagent_type,
            is_fork=False,
            workspace_mode=workspace_mode,
            definition=definition,
        )

    def _plan_from_params(
        self, params: dict[str, Any],
    ) -> tuple[_SpawnInvocationPlan | None, ToolResult | None]:
        raw_subagent_type = params.get("subagent_type")
        raw_description = params.get("description")
        raw_prompt = params.get("prompt")
        raw_placement = params.get(
            "execution_placement", ExecutionPlacement.AUTO.value,
        )
        raw_isolation = params.get("isolation", WorkspaceMode.CURRENT.value)

        if (
            not isinstance(raw_subagent_type, str) or not raw_subagent_type.strip()
            or not isinstance(raw_description, str) or not raw_description.strip()
            or not isinstance(raw_prompt, str) or not raw_prompt.strip()
        ):
            return None, ToolResult(
                success=False,
                output="",
                error="task requires subagent_type, description, and prompt",
            )

        subagent_type = raw_subagent_type.strip()
        description = raw_description.strip()
        user_prompt = raw_prompt.strip()
        try:
            requested_placement = ExecutionPlacement(raw_placement)
        except (TypeError, ValueError):
            return None, ToolResult(
                success=False,
                output="",
                error=(
                    "execution_placement must be 'auto', 'foreground', or "
                    "'background'"
                ),
            )
        try:
            workspace_mode = WorkspaceMode(raw_isolation)
        except (TypeError, ValueError):
            return None, ToolResult(
                success=False,
                output="",
                error="isolation must be 'current' or 'worktree'",
            )

        allowed = self._allowed_subagent_names()
        is_fork = subagent_type == AgentKind.FORK.value
        if not is_fork and not self._runtime.agent_registry.has(subagent_type):
            return None, ToolResult(
                success=False,
                output="",
                error=(
                    f"Unknown subagent_type: {subagent_type!r}. "
                    f"Available: {sorted(allowed)}"
                ),
            )
        if subagent_type not in allowed:
            return None, ToolResult(
                success=False,
                output="",
                error=(
                    f"subagent_type {subagent_type!r} is not allowed for this "
                    f"agent. Available: {sorted(allowed)}"
                ),
            )

        planning_params = (
            {
                "subagent_type": subagent_type,
                "isolation": workspace_mode.value,
            }
            if is_fork or "isolation" in params
            else {"subagent_type": subagent_type}
        )
        facts = self._planning_facts_from_params(planning_params)
        if facts is None:
            return None, ToolResult.from_error(
                error_type=ToolErrorType.INVALID_INPUT,
                retry=ToolRetryDirective.DO_NOT_RETRY,
                detail=(
                    "Delegation parameters could not be resolved into typed "
                    "spawn facts"
                ),
            )
        return _SpawnInvocationPlan(
            description=description,
            user_prompt=user_prompt,
            requested_placement=requested_placement,
            facts=facts,
        ), None

    def _validate_run_context(self, *, is_fork: bool) -> ToolResult | None:
        run_context = getattr(self, "_run_context", None)
        if run_context is None:
            return ToolResult.from_error(
                error_type=ToolErrorType.INTERNAL,
                retry=ToolRetryDirective.DO_NOT_RETRY,
                detail="Delegation requires a Runtime-bound run context",
            )
        if run_context.phase_policy is None:
            return ToolResult.from_error(
                error_type=ToolErrorType.INTERNAL,
                retry=ToolRetryDirective.DO_NOT_RETRY,
                detail="Delegation requires the parent's effective phase policy",
            )
        if run_context.delegation_effects is None:
            return ToolResult.from_error(
                error_type=ToolErrorType.INTERNAL,
                retry=ToolRetryDirective.DO_NOT_RETRY,
                detail="Delegation requires the parent's effective tool effects",
            )
        if run_context.delegation_step_limit is None:
            return ToolResult.from_error(
                error_type=ToolErrorType.INTERNAL,
                retry=ToolRetryDirective.DO_NOT_RETRY,
                detail="Delegation requires the parent's effective step limit",
            )
        if is_fork and run_context.spawn_context is None:
            return ToolResult.from_error(
                error_type=ToolErrorType.UNAVAILABLE,
                retry=ToolRetryDirective.DO_NOT_RETRY,
                detail="Fork requires a valid live parent conversation snapshot",
            )
        if run_context.cancellation.is_cancelled:
            return ToolResult.from_error(
                error_type=ToolErrorType.INTERRUPTED,
                retry=ToolRetryDirective.DO_NOT_RETRY,
                detail=run_context.cancellation.detail,
            )
        if run_context.delegation_token_limit <= 0:
            return ToolResult.from_error(
                error_type=ToolErrorType.UNAVAILABLE,
                retry=ToolRetryDirective.DO_NOT_RETRY,
                detail=(
                    "Parent execution budget has no tokens available for "
                    "delegation"
                ),
            )
        return None

    def _build_spawn_request(
        self,
        *,
        plan: _SpawnInvocationPlan,
        prompt: str,
        execution_placement: ExecutionPlacement,
    ) -> AgentSpawnRequest:
        if plan.facts.is_fork:
            return AgentSpawnRequest.fork(
                description=plan.description,
                prompt=prompt,
                workspace_mode=plan.facts.workspace_mode,
                execution_placement=execution_placement,
            )
        return AgentSpawnRequest.named(
            definition=plan.facts.definition,
            description=plan.description,
            prompt=prompt,
            execution_placement=execution_placement,
        )

    def concurrency_mode(self, params: dict[str, Any]) -> ToolConcurrency:
        """Only shared-workspace read-only children are safe to fan out."""
        facts = self._planning_facts_from_params(params)
        if facts is None:
            return ToolConcurrency.SERIAL
        return self._concurrency_mode_for_facts(facts)

    # ── BaseTool interface ──

    aliases = ("task",)

    @property
    def name(self) -> str:
        return "Agent"

    def _get_available_subagent_specs(self) -> list[Any]:
        """Return subagent specs allowed by the declarative agent definition."""
        registry = self._runtime.agent_registry
        caller = registry.get(self._caller_agent_name)
        return registry.delegatable_by(caller)

    def _resolve_execution_placement(
        self,
        *,
        requested: ExecutionPlacement,
        facts: _SpawnPlanningFacts,
        run_context,
    ) -> ExecutionPlacement:
        """Resolve AUTO using only typed runtime facts.

        Policy:
        - explicit foreground/background always wins
        - definition.background remains a declarative default for named children
        - parallel fan-out does NOT by itself force background for named
          read-only children, because those results are often needed for the
          parent's next synthesis turn
        - isolated fork/worktree branches are the typed AUTO case that upgrades
          naturally to background under fan-out, because the parent can review
          them later via explicit resolution tools
        - everything else stays foreground
        """
        if requested is not ExecutionPlacement.AUTO:
            return requested

        base = AgentSpawnRequest.resolve_execution_placement(
            agent_kind=(
                AgentKind.FORK
                if facts.is_fork
                else AgentKind.NAMED_SUBAGENT
            ),
            requested=requested,
            definition=facts.definition,
        )
        if base is ExecutionPlacement.BACKGROUND:
            return base

        if run_context.delegation_width <= 1:
            return base

        if (
            facts.is_fork
            and facts.workspace_mode is WorkspaceMode.WORKTREE
            and self._concurrency_mode_for_facts(facts)
            is ToolConcurrency.PARALLEL_SAFE
        ):
            return ExecutionPlacement.BACKGROUND
        return base

    def _concurrency_mode_for_facts(
        self, facts: _SpawnPlanningFacts,
    ) -> ToolConcurrency:
        if facts.is_fork:
            if facts.workspace_mode is WorkspaceMode.WORKTREE:
                return ToolConcurrency.PARALLEL_SAFE
            caller = self._runtime.agent_registry.get(self._caller_agent_name)
            return (
                ToolConcurrency.PARALLEL_SAFE
                if caller.intent is TaskIntent.ANALYSIS
                else ToolConcurrency.SERIAL
            )
        if (
            facts.definition is not None
            and facts.definition.intent is TaskIntent.ANALYSIS
            and facts.workspace_mode is WorkspaceMode.CURRENT
        ):
            return ToolConcurrency.PARALLEL_SAFE
        return ToolConcurrency.SERIAL

    @property
    def description(self) -> str:
        specs = self._get_available_subagent_specs()
        subagents = [
            f"- {spec.name}: {spec.description}"
            for spec in specs
        ]
        subagents.append(
            f"- {AgentKind.FORK.value}: inherit this conversation, tools, and model"
        )
        lines = [
            "Launch a subagent to handle a complex, multi-step task autonomously.",
            "The child keeps its own tool history and returns one final message.",
            "",
            "Available subagent types:",
            *subagents,
            "",
            "Guidelines:",
            "- Select only from the Runtime-derived subagent list above.",
            "- Named subagents start fresh; include FULL context in the prompt:",
            "  what you already know, what you already tried, what failed.",
            "  Without this, the subagent will re-discover facts you already have.",
            "- fork inherits this conversation; in fork mode, include only the delta or specific ask.",
            "- The subagent's final summary is the only thing returned to you.",
            "  Ask for structured output (~1-2K tokens) — you pay for every token.",
            "- Use foreground when you need the result before continuing.",
            "- Use background only for independent work; completion arrives later.",
            "- Use for independent, clearly-scoped work. Do simple tasks directly.",
            "- For 2-3 independent read-only investigations, emit their task calls",
            "  in one response; the Runtime fans them out in parallel and returns",
            "  all results for synthesis.",
            "- Never hand off understanding — you can delegate execution, not comprehension.",
        ]
        return "\n".join(lines)

    @property
    def parameters_schema(self) -> dict[str, Any]:
        # P2: dynamic subagent_type description — only lists available types
        available_specs = self._get_available_subagent_specs()
        available_names = [s.name for s in available_specs]
        available_names.append(AgentKind.FORK.value)
        type_desc = (
            "Which subagent to spawn. CHOOSE CAREFULLY — wrong type causes loops. "
            "Currently available: " + ", ".join(
                f"'{n}'" for n in available_names
            ) + ". Select only from this Runtime-derived list."
        )
        return {
            "type": "object",
            "properties": {
                "subagent_type": {
                    "type": "string",
                    "description": type_desc,
                },
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "The task for the subagent. For named subagents, structure as: "
                        "1) OBJECTIVE — the outcome and how the result will be used. "
                        "2) CONTEXT — what you already know, already tried, what failed. "
                        "3) OUTPUT — exact deliverable shape and size (~1-2K tokens). "
                        "4) BOUNDARIES — scope limits, paths, what NOT to do. "
                        "For fork, include only the delta (conversation context is inherited)."
                    ),
                },
                "execution_placement": {
                    "type": "string",
                    "enum": [
                        ExecutionPlacement.AUTO.value,
                        ExecutionPlacement.FOREGROUND.value,
                        ExecutionPlacement.BACKGROUND.value,
                    ],
                    "description": (
                        "Use foreground when this result is required before the "
                        "next step; use background for independent concurrent work."
                    ),
                },
                "isolation": {
                    "type": "string",
                    "enum": [
                        WorkspaceMode.CURRENT.value,
                        WorkspaceMode.WORKTREE.value,
                    ],
                    "description": (
                        "Fork-only workspace placement. Use worktree for "
                        "parallel edits that must not touch the parent checkout."
                    ),
                },
            },
            "required": ["subagent_type", "description", "prompt"],
        }

    # ── Execution ──

    def execute(self, params: dict[str, Any]) -> ToolResult:
        plan, plan_error = self._plan_from_params(params)
        if plan_error is not None:
            return plan_error
        assert plan is not None

        run_context_error = self._validate_run_context(is_fork=plan.facts.is_fork)
        if run_context_error is not None:
            return run_context_error
        run_context = self._run_context
        assert run_context is not None

        execution_placement = self._resolve_execution_placement(
            requested=plan.requested_placement,
            facts=plan.facts,
            run_context=run_context,
        )

        prompt = (
            plan.user_prompt
            if plan.facts.is_fork
            else _build_subagent_prompt(plan.user_prompt, plan.facts.definition)
        )
        if (
            plan.facts.definition is not None
            and plan.facts.definition.required_tools
        ):
            logger.debug(
                "Injecting subagent protocol (%d chars) into prompt for agent %s",
                len(_SUBAGENT_PROTOCOL), plan.facts.subagent_type,
            )
        else:
            logger.debug(
                "Skipping subagent protocol for agent %s (no required_tools or fork)",
                plan.facts.subagent_type,
            )
        logger.info(
            "Dispatching subagent '%s' for task: %s",
            plan.facts.subagent_type, plan.description,
        )

        try:
            request = self._build_spawn_request(
                plan=plan,
                prompt=prompt,
                execution_placement=execution_placement,
            )
            dispatch_result = self._runtime.spawn_agent(
                parent_session_id=self._parent_session_id,
                request=request,
                budget_tokens=run_context.delegation_token_limit,
                parent_max_steps=run_context.delegation_step_limit,
                cancellation_token=run_context.cancellation,
                parent_policy=(
                    run_context.phase_policy
                    if plan.facts.is_fork
                    else run_context.phase_policy.with_allowed_effects(
                        run_context.delegation_effects
                    )
                ),
                spawn_context=run_context.spawn_context,
            )
            if isinstance(dispatch_result, BackgroundAgentHandle):
                return ToolResult(
                    success=True,
                    output=_format_background_handle(
                        plan.facts.subagent_type, dispatch_result,
                    ),
                    subagent_tokens_used=0,
                )
            fork_result = dispatch_result
            output = _format_fork_result(plan.facts.subagent_type, fork_result)
            if fork_result.status == ForkStatus.PARTIAL:
                output = (
                    f"WARNING: Subagent reached max steps ({fork_result.turns_used} turns). "
                    "Result may be INCOMPLETE. Verify findings independently before relying on them.\n\n"
                    f"{output}"
                )
        except Exception as exc:
            logger.exception("Subagent '%s' crashed", plan.facts.subagent_type)
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_subagent_failure()
            return ToolResult(
                success=False, output="",
                error=f"Subagent '{plan.facts.subagent_type}' failed: {exc}",
            )

        # ── Circuit breaker: track subagent success/failure rhythm ──
        if self._circuit_breaker is not None:
            if fork_result.status == ForkStatus.FAILED:
                self._circuit_breaker.record_subagent_failure()
            elif fork_result.status != ForkStatus.CANCELLED:
                self._circuit_breaker.record_subagent_success()

        is_failure = fork_result.status in {
            ForkStatus.FAILED, ForkStatus.CANCELLED,
        }

        return ToolResult(
            success=not is_failure,
            output=output,
            error=fork_result.error if is_failure else "",
            subagent_tokens_used=fork_result.tokens_used,
            structured_findings=tuple(
                finding.to_dict() for finding in fork_result.structured_findings
            ),
            metadata={
                "fork_result": fork_result.to_dict(),
                "subagent_type": plan.facts.subagent_type,
            },
        )

    def _allowed_subagent_names(self) -> frozenset[str]:
        caller = self._runtime.agent_registry.get(self._caller_agent_name)
        return frozenset({AgentKind.FORK.value}).union(
            child.name
            for child in self._runtime.agent_registry.delegatable_by(caller)
        )


def _format_fork_result(
    agent_type: str,
    result: "AgentRunResult",
    *,
    generation: int | None = None,
) -> str:
    """Render AgentRunResult as an XML <task-notification> block.

    When structured_findings are present (from submit_findings tool),
    they are displayed FIRST as the primary, reliable output. The text
    summary follows as supplementary context.
    """
    lines = [
        "<task-notification>",
        f"  <agent-type>{agent_type}</agent-type>",
        f"  <session-id>{result.session_id}</session-id>",
        f"  <status>{result.status.value}</status>",
        f"  <turns-used>{result.turns_used}</turns-used>",
        f"  <worktree-disposition>{result.worktree_disposition.value}</worktree-disposition>",
    ]
    if generation is not None:
        lines.insert(3, f"  <generation>{generation}</generation>")
    if result.error:
        lines.append(f"  <error>{_xml_escape(result.error)}</error>")
    if result.warning:
        lines.append(f"  <warning>{_xml_escape(result.warning)}</warning>")
    if result.worktree is not None:
        evidence = result.worktree
        lines.append(f"  <worktree change='{evidence.change.value}'>")
        lines.append(f"    <path>{_xml_escape(evidence.path)}</path>")
        lines.append(f"    <branch>{_xml_escape(evidence.branch)}</branch>")
        lines.append(f"    <base-branch>{_xml_escape(evidence.base_branch)}</base-branch>")
        lines.append(f"    <base-commit>{_xml_escape(evidence.base_commit)}</base-commit>")
        lines.append(f"    <revision>{_xml_escape(evidence.revision)}</revision>")
        for changed_file in evidence.changed_files:
            lines.append(f"    <changed-file>{_xml_escape(changed_file)}</changed-file>")
        if evidence.error:
            lines.append(f"    <inspection-error>{_xml_escape(evidence.error)}</inspection-error>")
        lines.append("  </worktree>")
    if result.failure_diagnosis:
        lines.append(f"  <failure-diagnosis>{_xml_escape(result.failure_diagnosis)}</failure-diagnosis>")

    # ── P1-5: Structured findings (primary output) ──
    if result.report is not None:
        lines.append(
            f"  <subagent-report status='{result.report.status.value}' "
            f"count='{len(result.report.findings)}'>"
        )
        if result.report.summary:
            lines.append(
                f"    <report-summary>{_xml_escape(result.report.summary)}</report-summary>"
            )
        for finding in result.report.findings:
            lines.append(
                f"    <finding severity='{finding.severity.value}' "
                f"category='{finding.category.value}'>"
            )
            lines.append(f"      <title>{_xml_escape(finding.title)}</title>")
            lines.append(f"      <description>{_xml_escape(finding.description)}</description>")
            if finding.file_path:
                lines.append(
                    f"      <location path='{_xml_escape(finding.file_path)}' "
                    f"line-start='{finding.line_start}' line-end='{finding.line_end}' />"
                )
            if finding.code_snippet:
                lines.append(
                    f"      <code-snippet>{_xml_escape(finding.code_snippet)}</code-snippet>"
                )
            if finding.verification:
                lines.append(
                    f"      <verification>{_xml_escape(finding.verification)}</verification>"
                )
            if finding.recommendation:
                lines.append(
                    f"      <recommendation>{_xml_escape(finding.recommendation)}</recommendation>"
                )
            lines.append("    </finding>")
        lines.append("  </subagent-report>")

    lines.extend([
        "  <summary>",
        _xml_escape(str(result.summary or "").strip()),
        "  </summary>",
        "</task-notification>",
    ])
    return "\n".join(lines)


def _format_background_handle(
    agent_type: str, handle: BackgroundAgentHandle,
) -> str:
    """Render an acknowledgement; it is not a completion result."""
    return "\n".join([
        "<task-notification>",
        f"  <agent-type>{_xml_escape(agent_type)}</agent-type>",
        f"  <session-id>{_xml_escape(handle.session_id)}</session-id>",
        f"  <generation>{handle.generation}</generation>",
        f"  <status>{handle.status.value}</status>",
        f"  <execution-placement>{handle.execution_placement.value}</execution-placement>",
        "  <message>Subagent started; completion will arrive separately.</message>",
        "</task-notification>",
    ])


def _xml_escape(text: Any) -> str:
    return escape(str(text or ""))


def _build_subagent_prompt(user_prompt: str, definition: "AgentDefinition | None") -> str:
    """Wrap the user's task prompt with the subagent analysis protocol.

    Structured-report subagents keep a small analysis protocol in prompt space.
    Evidence validation, completion requirements, and output structure are
    enforced at runtime via ReportFindings / submit_findings.
    """
    if definition is not None and definition.required_tools:
        return (
            f"{_SUBAGENT_PROTOCOL}\n"
            f"{_build_deliverable_contract(definition)}\n\n"
            f"## Your Task\n"
            f"{user_prompt}"
        )
    return user_prompt


def _build_deliverable_contract(definition: "AgentDefinition") -> str:
    """Render the Runtime-owned deliverable facts as a minimal prompt reminder."""
    lines: list[str] = []
    required_tools = sorted(definition.required_tools)
    if required_tools:
        lines.append(
            "- Required tools before finish: " + ", ".join(required_tools) + "."
        )
    completion_requires = {
        tool_name: count
        for tool_name, count in sorted(definition.completion_requires.items())
        if count > 0
    }
    if completion_requires:
        lines.append("- Runtime completion requirements:")
        for tool_name, count in completion_requires.items():
            lines.append(f"  - {tool_name}: call at least {count} time(s).")
    if definition.intent is TaskIntent.ANALYSIS:
        lines.append("- Do NOT edit code. Your job is analysis, not fixing.")
    lines.append(
        "- Runtime validates structured deliverables, evidence locations, and completion gates."
    )
    lines.append(
        "- If you found nothing, return the required deliverable with an explicit no-findings result."
    )
    return "\n".join(lines)
