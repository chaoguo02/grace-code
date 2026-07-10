"""AgentTool — spawn a fork subagent to handle a delegated subtask.

Architecture: three-layer defense for subagent output quality.

  Layer 1 (prompt, ~80%): _SUBAGENT_PROTOCOL wraps every subagent prompt with
    mandatory analysis constraints, a 4-phase verification flow, anti-laziness
    rules, and a structured output format.
  Layer 2 (code, 100%): _validate_subagent_report() checks that the subagent
    followed the format protocol before the result reaches the parent.
  Layer 3 (parent prompt): runtime._build_runtime_messages() injects review
    instructions so the parent doesn't rubber-stamp subagent output.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

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

## Output Format (MANDATORY — three sections, in this exact order)

### Confirmed Bugs
Each entry MUST contain:
- **File and line**: e.g. `agent/task_tool.py:142`
- **Actual code**: the exact lines you read (use ``` fence)
- **Problem**: what's wrong
- **Verification**: how you confirmed this (which file you cross-referenced)

If you can't provide ALL four fields, move to Unverified Hypotheses.

### Improvement Suggestions
Style, clarity, robustness suggestions. NOT bugs.
Each must still include a file:line reference.

### Unverified Hypotheses
Claims you suspect but could NOT verify.
Each MUST explain: "Why unverified: <blocked by what>"
If you have no unverified claims, write "None." — do NOT omit this section.

---

## Your Task
"""


class AgentTool(BaseTool):
    """Dispatch a fork subagent. Claude Code `task` tool equivalent.

    The subagent runs in a fresh context (Fork model):
    - No parent conversation history.
    - Tools restricted to the agent definition's allowlist.
    - Its final message is the return value.

    Usage:
        AgentTool(runtime, parent_session_id)
    """

    def __init__(self, runtime: "SessionRuntime", parent_session_id: str, caller_agent_name: str | None = None) -> None:
        self._runtime = runtime
        self._parent_session_id = parent_session_id
        self._caller_agent_name = caller_agent_name

    # ── BaseTool interface ──

    @property
    def name(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        registry = self._runtime.agent_registry
        allowed = self._allowed_subagent_names()
        specs = registry.list_subagents()
        if allowed is not None:
            specs = [spec for spec in specs if spec.name in allowed]
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
            "- Put ALL necessary context in the prompt — the subagent has no access to this conversation.",
            "- The subagent's final summary is the only thing returned to you.",
            "- Use for independent, clearly-scoped work. Do simple tasks directly.",
            "- Never hand off understanding — you can delegate execution, not comprehension.",
        ]
        return "\n".join(lines)

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "subagent_type": {
                    "type": "string",
                    "description": "The type of subagent to spawn (e.g. 'explore', 'general', 'code-reviewer')",
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

        # Wrap user prompt with mandatory analysis protocol (Layer 1)
        prompt = _build_subagent_prompt(user_prompt)
        logger.debug(
            "Injecting subagent protocol (%d chars) into prompt for agent %s",
            len(_SUBAGENT_PROTOCOL), subagent_type,
        )
        logger.info(
            "Dispatching subagent '%s' for task: %s",
            subagent_type, description,
        )

        try:
            fork_result = self._runtime.fork_session(
                definition=definition,
                description=description,
                prompt=prompt,
            )
            output = _format_fork_result(subagent_type, fork_result)
            if fork_result.status == "partial":
                output = (
                    f"WARNING: Subagent reached max steps ({fork_result.turns_used} turns). "
                    "Result may be INCOMPLETE. Verify findings independently before relying on them.\n\n"
                    f"{output}"
                )
        except Exception as exc:
            logger.exception("Fork subagent '%s' crashed", subagent_type)
            return ToolResult(
                success=False, output="",
                error=f"Subagent '{subagent_type}' failed: {exc}",
            )

        is_failure = fork_result.status == "failed"

        # Layer 2: deterministic format validation (code layer, 100% execution)
        violations = _validate_subagent_report(fork_result.summary or "")
        if violations:
            violation_text = "\n".join(f"  • {v}" for v in violations)
            logger.warning(
                "Subagent '%s' report format violations: %s",
                subagent_type, violations,
            )
            output = (
                f"{output}\n\n"
                f"{VIOLATION_MARKER}:\n{violation_text}\n"
                f"Treat all findings as [UNVERIFIED] until independently confirmed."
            )

        return ToolResult(
            success=not is_failure,
            output=output,
            error=fork_result.error if is_failure else "",
        )

    def _allowed_subagent_names(self) -> frozenset[str] | None:
        if self._caller_agent_name is None:
            return None
        try:
            caller = self._runtime.agent_registry.get(self._caller_agent_name)
        except KeyError:
            return None
        return caller.allowed_subagents


def _format_fork_result(agent_type: str, result: "ForkResult") -> str:
    """Format ForkResult as an XML <task-notification> block."""
    lines = [
        "<task-notification>",
        f"  <agent-type>{agent_type}</agent-type>",
        f"  <session-id>{result.session_id}</session-id>",
        f"  <status>{result.status}</status>",
        f"  <turns-used>{result.turns_used}</turns-used>",
    ]
    if result.error:
        lines.append(f"  <error>{_xml_escape(result.error)}</error>")
    if result.failure_diagnosis:
        lines.append(f"  <failure-diagnosis>{_xml_escape(result.failure_diagnosis)}</failure-diagnosis>")
    lines.extend([
        "  <summary>",
        _xml_escape(str(result.summary or "").strip()),
        "  </summary>",
        "</task-notification>",
    ])
    return "\n".join(lines)


def _xml_escape(text: Any) -> str:
    return escape(str(text or ""))


def _build_subagent_prompt(user_prompt: str) -> str:
    """Wrap the user's task prompt with the mandatory subagent analysis protocol.

    The protocol is hardcoded in the tool, not in memory — tool behavior
    constraints belong to the tool, while memory is for project knowledge.
    """
    return f"{_SUBAGENT_PROTOCOL}\n{user_prompt}"


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2: Format validation (code layer — deterministic, 100% execution)
# ═══════════════════════════════════════════════════════════════════════════

_BUG_CLAIM_PATTERN = re.compile(
    r"\b(Confirmed Bugs?|Bug\s*[#\d]|🐞|bug:|issue:|error:)\b",
    re.IGNORECASE,
)
_FILE_LINE_PATTERN = re.compile(r"\S+\.(?:py|ts|js|rs|go|java|rb):\d+")
_SECTION_MARKERS = re.compile(
    r"(Confirmed Bugs?|Improvement Suggestions?|Unverified Hypothes)",
    re.IGNORECASE,
)
_CODE_FENCE_PATTERN = re.compile(r"```")

VIOLATION_MARKER = "⚠️ SUBAGENT REPORT FORMAT VIOLATIONS"


def _validate_subagent_report(summary: str) -> list[str]:
    """Validate subagent report format — metadata-level, NOT semantic.

    Checks whether the subagent followed the output format protocol:
    - Each Confirmed entry has file:line references + code snippets.
    - Report uses the three-section structure.

    NOTE: [UNVERIFIED] markers are NOT mandatory. Their presence depends
    on whether the subagent actually had unverifiable findings. A report
    where everything was confirmed should not have them.

    Returns a list of violation strings (empty = report format is clean).
    """
    text = summary.strip()
    if not text:
        return []

    # Only inspect reports that claim to have found bugs/issues
    if not _BUG_CLAIM_PATTERN.search(text):
        return []

    violations: list[str] = []
    has_file_lines = bool(_FILE_LINE_PATTERN.search(text))
    has_sections = len(_SECTION_MARKERS.findall(text)) >= 2
    has_code_fences = bool(_CODE_FENCE_PATTERN.search(text))

    # Confirmed section present → must provide evidence
    if not has_file_lines:
        violations.append(
            "Missing file:line references — Confirmed Bugs must cite "
            "specific code locations (e.g. agent/task_tool.py:142)"
        )
    if not has_code_fences:
        violations.append(
            "Missing code snippets (``` fences) — Confirmed Bugs "
            "must include the actual code read"
        )
    # Three-section structure (at least 2 of the 3 required markers)
    if not has_sections:
        violations.append(
            "Missing report structure — expected at least 2 of: "
            "Confirmed Bugs / Improvement Suggestions / Unverified Hypotheses"
        )

    return violations
