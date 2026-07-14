"""AgentTool — spawn a fork subagent to handle a delegated subtask.

Architecture: three-layer defense for subagent output quality.

  Layer 0 (JSON Schema, ~95%): submit_findings tool with structured JSON Schema.
    Subagent calls this tool → Runtime validates → parent receives typed data.
    Replaces fragile regex parsing with deterministic schema enforcement.
  Layer 1 (prompt, ~5%): _SUBAGENT_PROTOCOL wraps code-reviewer prompts with
    mandatory analysis constraints, a 4-phase verification flow, and
    anti-laziness rules. Text-only output still accepted as fallback.
  Layer 2: Removed — replaced by Layer 0 (submit_findings JSON Schema).
    No more regex parsing. No more format guessing.
    followed the format protocol before the result reaches the parent.
  Layer 3 (parent prompt): runtime._build_runtime_messages() injects review
    instructions so the parent doesn't rubber-stamp subagent output.
"""

from __future__ import annotations

import logging
import copy
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from agent.task import TaskIntent
from agent.v2.models import AgentIsolation, DelegationScope, ForkStatus
from tools.base import (
    ToolConcurrency, ToolEffect, ToolErrorType, ToolMetadata,
    ToolRetryDirective, ToolRole,
)
from tools.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from agent.v2.models import ForkResult
    from agent.v2.runtime import SessionRuntime

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Layer 1: Subagent Analysis Protocol (hardcoded in tool prompt — prompt layer)
# ═══════════════════════════════════════════════════════════════════════════

# Known design decisions that look like bugs but are intentional.
# Used to preempt a class of false positives. Append new patterns as they
# emerge — the list evolves with the codebase.
_KNOWN_DESIGN_DECISIONS = [
    'partial status with success=True is NOT a bug (constrained run, '
    'WARNING is prepended to the output)',
]

_SUBAGENT_PROTOCOL = f"""
[SUBAGENT ANALYSIS PROTOCOL]
You are a code analysis subagent. This protocol governs how you produce reports.
Violating these rules makes your report unreliable — the parent agent will flag
it with "[SUBAGENT REPORT FORMAT VIOLATIONS]" warnings.

## Analysis Constraints

1. READ BEFORE YOU CLAIM. Every bug report MUST cite actual code you read
   (use Read/Grep tools). If you haven't read the line, DON'T report it.

2. CHECK DESIGN INTENT before calling something a bug. If behavior looks
   suspicious, search for comments, docstrings, tests, or rules that may
   explain it as intentional. Many things that "look wrong" at first glance
   are documented design choices — your job is to find the documentation
   before filing the report, not after.

3. CROSS-REFERENCE at least 2 related files before filing a Confirmed Bug.
   Read the dependency (type definitions, interfaces) AND at least one
   consumer (who calls this code, who consumes this return value).

4. KNOWN DESIGN DECISIONS — these are NOT bugs, do NOT report them:
   {chr(10).join('   - ' + d for d in _KNOWN_DESIGN_DECISIONS)}
   - Any behavior explained by comments, docstrings, or tests in the source.
   If you encounter a pattern that looks suspicious but might be intentional:
   (a) search for related comments/tests/rules, (b) if still unsure, put it
   under "Unverified Hypotheses" with a note explaining your uncertainty.

5. If you CANNOT read a file needed to verify a hypothesis, that finding
   MUST go into "Unverified Hypotheses", not "Confirmed Bugs".

## Mandatory Analysis Flow (execute in order)

### Phase 1 — Read Target
Read the target file(s) named in your task. Record observations.
Do NOT report anything yet.

### Phase 2 — Cross-Validate (at least 2 related files per finding)
For each potential finding from Phase 1:
- Read the dependency (type definition, interface, parent class).
- Read at least one consumer (caller, test, configuration).
- If you cannot read any of these, mark the finding as UNVERIFIED.
- If the cross-reference shows the behavior is intentional, DROP the finding.

### Phase 3 — Self-Challenge
For each remaining candidate:
- "Could this be intentional design?" — search for related comments/tests/rules.
- "Is there an architectural reason for this?" — read the module docstring.
- "Am I assuming or am I observing?" — if assuming, move to Unverified.
- If your answer to any challenge is "I'm not sure", DOWNGRADE to Unverified.

### Phase 4 — Produce Report
Output in the exact format specified below. Only Confirmed findings that
survived Phase 2 + Phase 3 go into "Confirmed" section.

## Anti-Laziness: Prohibited Phrases

If you find yourself writing any of these, STOP — you are skipping verification:

| Prohibited                           | Instead                              |
|--------------------------------------|--------------------------------------|
| "从代码结构来看应该是..."              | Read the definition file. Confirm.   |
| "这个字段可能是可选的"                  | Read the model definition. Confirm.  |
| "调用方可能会..."                     | Read the caller code. Confirm.       |
| "看起来没问题"                         | "I read X and confirmed Y at line N" |
| "应该是设计如此"                        | "Design intent confirmed by docstring at X:123 — ..." |
| "我没有权限读取 X 文件"                 | Then DON'T report Confirmed findings about X. |
| "从命名来看..."                        | Read the actual implementation.      |

## How to Submit Your Findings (REQUIRED)

**You MUST call the `submit_findings` tool before finishing.** This is NOT optional.
The tool accepts a structured JSON report. Calling it means:
- Your findings are validated at the Runtime level (no format guessing)
- The parent agent receives typed data (no regex parsing)
- File paths, line numbers, and severities are machine-readable

You can call `submit_findings` multiple times (e.g., once per investigation phase).
Call it with status='no_findings' and empty findings if you found nothing.

---

## Your Task
"""


class AgentTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.DELEGATE_WRITE}),
        roles=frozenset({ToolRole.DELEGATE}),
    )
    """Dispatch a fork subagent. Claude Code `task` tool equivalent.

    The subagent runs in a fresh context (Fork model):
    - No parent conversation history.
    - Tools restricted to the agent definition's allowlist.
    - Its final message is the return value.

    Usage:
        AgentTool(runtime, parent_session_id)
    """

    def __init__(self, runtime: "SessionRuntime", parent_session_id: str, caller_agent_name: str | None = None, circuit_breaker: Any = None) -> None:
        self._runtime = runtime
        self._parent_session_id = parent_session_id
        self._caller_agent_name = caller_agent_name
        self._circuit_breaker = circuit_breaker
        self._run_context = None
        delegation_scope = DelegationScope.ANY
        if caller_agent_name is not None:
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
        from agent.v2.run_context import RunContext
        if not isinstance(context, RunContext):
            raise TypeError("AgentTool requires a RunContext")
        bound = copy.copy(self)
        bound._run_context = context
        return bound

    def concurrency_mode(self, params: dict[str, Any]) -> ToolConcurrency:
        """Only shared-workspace read-only children are safe to fan out."""
        subagent_type = params.get("subagent_type")
        if not isinstance(subagent_type, str):
            return ToolConcurrency.SERIAL
        subagent_type = subagent_type.strip()
        allowed = self._allowed_subagent_names()
        if not subagent_type or (allowed is not None and subagent_type not in allowed):
            return ToolConcurrency.SERIAL
        if not self._runtime.agent_registry.has(subagent_type):
            return ToolConcurrency.SERIAL
        definition = self._runtime.agent_registry.get(subagent_type)
        if (
            definition.intent is TaskIntent.ANALYSIS
            and definition.isolation is AgentIsolation.FORK
        ):
            return ToolConcurrency.PARALLEL_SAFE
        return ToolConcurrency.SERIAL

    # ── BaseTool interface ──

    @property
    def name(self) -> str:
        return "task"

    def _get_available_subagent_specs(self) -> list[Any]:
        """Return subagent specs allowed by the declarative agent definition."""
        registry = self._runtime.agent_registry
        if self._caller_agent_name is None:
            return registry.list_subagents()
        caller = registry.get(self._caller_agent_name)
        return registry.delegatable_by(caller)

    @property
    def description(self) -> str:
        specs = self._get_available_subagent_specs()
        subagents = [
            f"- {spec.name}: {spec.description}"
            for spec in specs
        ]
        lines = [
            "Launch a subagent to handle a complex, multi-step task autonomously.",
            "The subagent runs in an isolated context and returns one final message.",
            "",
            "Available subagent types:",
            *subagents,
            "",
            "Guidelines:",
            "- Select only from the Runtime-derived subagent list above.",
            "- Put ALL necessary context in the prompt — the subagent has no access to this conversation.",
            "- The subagent's final summary is the only thing returned to you.",
            "- Use for independent, clearly-scoped work. Do simple tasks directly.",
            "- Never hand off understanding — you can delegate execution, not comprehension.",
        ]
        return "\n".join(lines)

    @property
    def parameters_schema(self) -> dict[str, Any]:
        # P2: dynamic subagent_type description — only lists available types
        available_specs = self._get_available_subagent_specs()
        available_names = [s.name for s in available_specs]
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
                    "description": "The full task for the subagent. Include ALL context, constraints, and expected output format.",
                },
            },
            "required": ["subagent_type", "description", "prompt"],
        }

    # ── Execution ──

    def execute(self, params: dict[str, Any]) -> ToolResult:
        raw_subagent_type = params.get("subagent_type")
        raw_description = params.get("description")
        raw_prompt = params.get("prompt")

        # Validate
        if (
            not isinstance(raw_subagent_type, str) or not raw_subagent_type.strip()
            or not isinstance(raw_description, str) or not raw_description.strip()
            or not isinstance(raw_prompt, str) or not raw_prompt.strip()
        ):
            return ToolResult(
                success=False, output="",
                error="task requires subagent_type, description, and prompt",
            )

        subagent_type = raw_subagent_type.strip()
        description = raw_description.strip()
        user_prompt = raw_prompt.strip()
        allowed = self._allowed_subagent_names()
        if allowed is not None and subagent_type not in allowed:
            return ToolResult(
                success=False, output="",
                error=f"subagent_type {subagent_type!r} is not allowed for this agent. "
                      f"Available: {sorted(allowed)}",
            )
        if not self._runtime.agent_registry.has(subagent_type):
            available = allowed if allowed is not None else {s.name for s in self._runtime.agent_registry.list_subagents()}
            return ToolResult(
                success=False, output="",
                error=f"Unknown subagent_type: {subagent_type!r}. "
                      f"Available: {sorted(available)}",
            )

        definition = self._runtime.agent_registry.get(subagent_type)

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
        if run_context.cancellation.is_cancelled:
            return ToolResult.from_error(
                error_type=ToolErrorType.INTERRUPTED,
                retry=ToolRetryDirective.DO_NOT_RETRY,
                detail=run_context.cancellation.detail,
            )
        child_token_limit = run_context.delegation_token_limit
        if child_token_limit <= 0:
            return ToolResult.from_error(
                error_type=ToolErrorType.UNAVAILABLE,
                retry=ToolRetryDirective.DO_NOT_RETRY,
                detail="Parent execution budget has no tokens available for delegation",
            )

        # Wrap user prompt with agent-type-appropriate protocol (Layer 1)
        prompt = _build_subagent_prompt(user_prompt, subagent_type)
        if subagent_type == "code-reviewer":
            logger.debug(
                "Injecting subagent protocol (%d chars) into prompt for agent %s",
                len(_SUBAGENT_PROTOCOL), subagent_type,
            )
        else:
            logger.debug(
                "Skipping subagent protocol for agent %s (not code-reviewer)",
                subagent_type,
            )
        logger.info(
            "Dispatching subagent '%s' for task: %s",
            subagent_type, description,
        )

        try:
            fork_result = self._runtime.fork_session(
                parent_session_id=self._parent_session_id,
                definition=definition,
                description=description,
                prompt=prompt,
                budget_tokens=child_token_limit,
                parent_max_steps=run_context.delegation_step_limit,
                cancellation_token=run_context.cancellation,
                parent_policy=run_context.phase_policy.with_allowed_effects(
                    run_context.delegation_effects
                ),
            )
            output = _format_fork_result(subagent_type, fork_result)
            if fork_result.status == ForkStatus.PARTIAL:
                output = (
                    f"WARNING: Subagent reached max steps ({fork_result.turns_used} turns). "
                    "Result may be INCOMPLETE. Verify findings independently before relying on them.\n\n"
                    f"{output}"
                )
        except Exception as exc:
            logger.exception("Fork subagent '%s' crashed", subagent_type)
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_subagent_failure()
            return ToolResult(
                success=False, output="",
                error=f"Subagent '{subagent_type}' failed: {exc}",
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
        )

    def _allowed_subagent_names(self) -> frozenset[str] | None:
        if self._caller_agent_name is None:
            return None
        caller = self._runtime.agent_registry.get(self._caller_agent_name)
        return frozenset(
            child.name
            for child in self._runtime.agent_registry.delegatable_by(caller)
        )


def _format_fork_result(agent_type: str, result: "ForkResult") -> str:
    """Format ForkResult as an XML <task-notification> block.

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


def _xml_escape(text: Any) -> str:
    return escape(str(text or ""))


def _build_subagent_prompt(user_prompt: str, subagent_type: str) -> str:
    """Wrap the user's task prompt with the subagent analysis protocol.

    Only code-reviewer subagents get the full verification protocol.
    For explore/general, pass the prompt cleanly — their system prompt
    already has tool selection rules from _SUBAGENT_SUMMARY_RULE.
    """
    if subagent_type == "code-reviewer":
        return f"{_SUBAGENT_PROTOCOL}\n{user_prompt}"
    return user_prompt
