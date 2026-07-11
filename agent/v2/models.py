"""Agent V2 data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionRecord:
    id: str
    parent_id: str | None
    root_id: str
    agent_name: str
    mode: str
    title: str
    status: str
    repo_path: str
    summary: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionMessageRecord:
    id: int
    session_id: str
    role: str
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    created_at: str = ""


@dataclass(frozen=True)
class AgentDefinition:
    """Agent definition loaded from .md YAML frontmatter (Claude Code compatible)."""

    name: str
    description: str
    tools: frozenset[str] = frozenset()
    disallowed_tools: frozenset[str] = frozenset()
    allowed_subagents: frozenset[str] | None = None
    model: str = "inherit"
    isolation: str = "fork"
    background: bool = False
    max_turns: int = 50
    hidden: bool = False
    system_prompt: str = ""

    @property
    def mode(self) -> str:
        return "primary" if self.isolation == "none" else "subagent"


@dataclass(frozen=True)
class ForkResult:
    """Result from a forked subagent run."""

    agent_name: str
    session_id: str
    status: str  # completed | partial | failed
    summary: str
    error: str = ""
    artifacts: tuple[str, ...] = ()
    turns_used: int = 0
    tokens_used: int = 0
    terminated_by_loop: bool = False  # subagent was killed by loop detection
    structured_findings: tuple[dict[str, object], ...] = ()  # from SubmitFindingsTool
    failure_diagnosis: str = ""  # structured diagnosis when status is "failed"


# ── Built-in agent definitions (fallback when no .md files exist) ──

_DEFAULT_READONLY_TOOLS = frozenset({
    "Read", "Glob", "Grep", "file_view", "WebFetch", "WebSearch",
})

_DEFAULT_GENERAL_TOOLS = frozenset({
    "Read", "Glob", "Grep", "file_view", "Write", "Edit", "Bash", "WebFetch", "WebSearch",
})

_COORDINATOR_TOOLS = frozenset({"Task", "Read", "Glob", "Grep"})

_BUILTIN_AGENTS: dict[str, AgentDefinition] = {
    "build": AgentDefinition(
        name="build",
        description="Primary coding agent with full tool access. Can delegate to subagents.",
        tools=_DEFAULT_GENERAL_TOOLS,
        allowed_subagents=frozenset({"explore", "general", "code-reviewer"}),
        isolation="none",
        max_turns=100,
        system_prompt="",
    ),
    "plan": AgentDefinition(
        name="plan",
        description="Read-only planning agent. Explores codebase and produces structured plans.",
        tools=_DEFAULT_READONLY_TOOLS,
        allowed_subagents=frozenset({"explore", "general", "code-reviewer"}),
        isolation="none",
        max_turns=60,
        system_prompt="",
    ),
    "coordinator": AgentDefinition(
        name="coordinator",
        description="Coordinates work across specialized subagents with a restricted tool set.",
        tools=_COORDINATOR_TOOLS,
        allowed_subagents=frozenset({"explore", "general", "code-reviewer"}),
        isolation="none",
        max_turns=80,
        system_prompt="""You are a coordinator agent. Plan, delegate, synthesize, and verify.
- Use task to delegate execution to specialized subagents.
- Do not perform implementation work directly.
- Do not rubber-stamp weak subagent findings.
- Separate confirmed findings from unverified claims and observations.""",
    ),
    "explore": AgentDefinition(
        name="explore",
        description="Fast read-only agent for code exploration, search, and analysis. "
        "Use for: finding files, searching code, analyzing code for bugs, "
        "answering questions about the codebase. Uses file_read/search_text — NO shell.",
        tools=_DEFAULT_READONLY_TOOLS,
        disallowed_tools=frozenset({"Write", "Edit", "Bash", "Task"}),
        max_turns=50,
        system_prompt="""You are a read-only code analysis agent. Analyze code and return findings.
- Read files with file_read (NEVER use shell commands to read files).
- Search code with search_text (NEVER use grep or find in shell).
- Stop as soon as you can answer the question asked.
- Return: Files inspected, Key findings with line numbers, Evidence (actual code read).
- Do NOT edit code or leave follow-up work for the parent.
- Your final message IS your return value.""",
    ),
    "general": AgentDefinition(
        name="general",
        description="General-purpose coding subagent with full tool access "
        "including shell. Use ONLY when Write, Edit, or Bash is required. "
        "For read-only analysis, code search, or bug-finding, use 'explore' instead.",
        tools=_DEFAULT_GENERAL_TOOLS,
        disallowed_tools=frozenset({"Task"}),
        max_turns=60,
        system_prompt="""You are a coding subagent. Handle a single, well-scoped task.
- Read files with file_read, edit with file_edit, write with file_write.
- Use shell ONLY for running tests, builds, and git commands — NEVER for
  reading files (cat/type) or modifying files (sed/awk).
- Search → read → edit → verify.
- If finished: summarize concrete changes.
- If blocked: explain precisely what's missing.
- Your final message IS your return value.""",
    ),
    "code-reviewer": AgentDefinition(
        name="code-reviewer",
        description="Reviews code for correctness and quality.",
        tools=_DEFAULT_READONLY_TOOLS,
        disallowed_tools=frozenset({"Write", "Edit", "Bash", "Task", "WebFetch", "WebSearch"}),
        max_turns=40,
        hidden=True,
        system_prompt="""You are a code reviewer. Find bugs and quality issues.
- Focus on correctness first, then simplification.
- Do NOT rubber-stamp weak work.
- For each finding: file, line, summary, failure scenario.
- Do NOT edit code. Your final message IS your review.""",
    ),
}
