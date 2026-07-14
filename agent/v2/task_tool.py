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
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from agent.v2.models import ForkStatus
from tools.base import ToolEffect, ToolMetadata
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
    metadata = ToolMetadata(effects=frozenset({ToolEffect.DELEGATE}))
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

    # ── BaseTool interface ──

    @property
    def name(self) -> str:
        return "task"

    def _get_available_subagent_specs(self) -> list[Any]:
        """Return subagent specs allowed by the declarative agent definition."""
        registry = self._runtime.agent_registry
        allowed = self._allowed_subagent_names()
        specs = registry.list_subagents()
        if allowed is not None:
            specs = [spec for spec in specs if spec.name in allowed]

        return specs

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
            else:
                self._circuit_breaker.record_subagent_success()

        is_failure = fork_result.status == ForkStatus.FAILED

        return ToolResult(
            success=not is_failure,
            output=output,
            error=fork_result.error if is_failure else "",
            subagent_tokens_used=fork_result.tokens_used,
            structured_findings=fork_result.structured_findings,
        )

    def _allowed_subagent_names(self) -> frozenset[str] | None:
        if self._caller_agent_name is None:
            return None
        try:
            caller = self._runtime.agent_registry.get(self._caller_agent_name)
        except KeyError:
            return None
        return frozenset(
            child.name
            for child in self._runtime.agent_registry.list_subagents()
            if caller.permits_subagent(child)
        )


def _format_fork_result(agent_type: str, result: "ForkResult") -> str:
    """Format ForkResult as an XML <task-notification> block.

    When structured_findings are present (from submit_findings tool),
    they are displayed FIRST as the primary, reliable output. The text
    summary follows as supplementary context.
    """
    import json as _json

    lines = [
        "<task-notification>",
        f"  <agent-type>{agent_type}</agent-type>",
        f"  <session-id>{result.session_id}</session-id>",
        f"  <status>{result.status.value}</status>",
        f"  <turns-used>{result.turns_used}</turns-used>",
    ]
    if result.error:
        lines.append(f"  <error>{_xml_escape(result.error)}</error>")
    if result.warning:
        lines.append(f"  <warning>{_xml_escape(result.warning)}</warning>")
    if result.merge_conflict:
        lines.append(f"  <merge-conflict>true</merge-conflict>")
    if result.failure_diagnosis:
        lines.append(f"  <failure-diagnosis>{_xml_escape(result.failure_diagnosis)}</failure-diagnosis>")

    # ── P1-5: Structured findings (primary output) ──
    if result.structured_findings:
        lines.append(f"  <structured-findings count='{len(result.structured_findings)}'>")
        for f in result.structured_findings:
            finding_json = _json.dumps(f, ensure_ascii=False, default=str)
            lines.append(f"    <finding>{_xml_escape(finding_json)}</finding>")
        lines.append("  </structured-findings>")

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
