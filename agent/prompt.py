"""
agent/prompt.py

System prompt template management compatibility layer.

This module preserves the historical function signatures used across the
codebase while delegating prompt loading/rendering to PromptAssembler.
It also records prompt usage metadata so Langfuse traces can link each
generation to the prompt source, name, label, and version used at runtime.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from config.schema import PromptConfig
from agent.task import TaskIntent
from llm.base import LLMToolSchema
from prompts.assembler import PromptAssembler, PromptRenderResult


_NO_REPO_SUMMARY = "(Repository summary not yet available - use find_files and file_read to explore)"

_ISSUE_SECTION_TEMPLATE = """
## GitHub Issue
URL: {issue_url}
"""

_PLANNING_SYSTEM_TEMPLATE = """\
You are a task planner. Break the user's coding task into a short, concrete \
sequence of subtasks. Each subtask will be executed by a coding agent with \
access to file read/write, shell, search, and test tools.

## Rules
- 2-5 subtasks is ideal; never exceed 7
- Each description MUST mention specific files or functions to act on
- NO vague descriptions - avoid "analyze the codebase", "explore", "understand"
- Instead use: "Read src/parser.py and find the Tokenizer class", \
"Edit src/parser.py: fix the __init__ method to handle empty input"
- The last subtask MUST verify the fix (run tests, check output)
- Keep reasoning under 200 characters - just the approach

## Output Format
Respond with exactly one line in this format:

TASK_COMPLETE: {{"reasoning": "<brief>", "plan": [{{"id": "1", "description": "...", "expected_outcome": "..."}}]}}\
"""


_assembler: PromptAssembler | None = None
_project_dir: str | None = None
_prompt_config: PromptConfig | None = None
_prompt_usage_var: ContextVar[list[dict[str, Any]]] = ContextVar("prompt_usage", default=[])


def _build_assembler() -> PromptAssembler:
    return PromptAssembler(project_dir=_project_dir, config=_prompt_config)


def _get_assembler() -> PromptAssembler:
    global _assembler
    if _assembler is None:
        _assembler = _build_assembler()
    return _assembler


def set_project_dir(project_dir: str | None) -> None:
    """Set the active project directory for prompt overrides."""
    global _assembler, _project_dir
    _project_dir = project_dir
    _assembler = _build_assembler()


def set_prompt_config(config: PromptConfig | None) -> None:
    """Set prompt loading configuration."""
    global _assembler, _prompt_config
    _prompt_config = config
    _assembler = _build_assembler()


def reset_prompt_usage() -> None:
    """Reset prompt usage tracking for a new top-level task/round."""
    _prompt_usage_var.set([])


def get_prompt_usage_metadata() -> list[dict[str, Any]]:
    """Return prompt usage metadata accumulated since the last reset/consume."""
    current = _prompt_usage_var.get()
    return [dict(item) for item in current]


def consume_prompt_usage_metadata() -> list[dict[str, Any]]:
    """Return and clear prompt usage metadata for the next generation trace."""
    current = get_prompt_usage_metadata()
    reset_prompt_usage()
    return current


def _record_prompt_usage(metadata: dict[str, Any] | None) -> None:
    if not metadata:
        return

    normalized = {key: value for key, value in metadata.items() if value is not None}
    current = list(_prompt_usage_var.get())
    fingerprint = json.dumps(normalized, ensure_ascii=True, sort_keys=True, default=str)
    existing = {
        json.dumps(item, ensure_ascii=True, sort_keys=True, default=str)
        for item in current
    }
    if fingerprint in existing:
        return
    current.append(normalized)
    _prompt_usage_var.set(current)


def _render_result(relative_path: str, **variables: Any) -> PromptRenderResult:
    result = _get_assembler().render_result(relative_path, **variables)
    _record_prompt_usage(result.metadata)
    return result


def _render_prompt(relative_path: str, **variables: Any) -> str:
    return _render_result(relative_path, **variables).text


def build_system_prompt_core(
    repo_path: str,
    tools: list[LLMToolSchema],
    repo_summary: str | None = None,
) -> str:
    result = _get_assembler().render_system_core_result(repo_path, tools, repo_summary)
    _record_prompt_usage(result.metadata)
    return result.text


def build_system_prompt_variable(
    memory_section: str = "",
    auto_memory_enabled: bool = False,
) -> str:
    parts = []
    if memory_section:
        parts.append(f"## Memory\n{memory_section}")
    if auto_memory_enabled:
        parts.append(_render_prompt("memory/auto-memory.md"))
    return "\n\n".join(parts)


def build_system_prompt(
    repo_path: str,
    tools: list[LLMToolSchema],
    repo_summary: str | None = None,
    memory_section: str = "",
    auto_memory_enabled: bool = False,
) -> str:
    core = build_system_prompt_core(repo_path, tools, repo_summary)
    variable = build_system_prompt_variable(memory_section, auto_memory_enabled)
    if variable:
        return core.rstrip() + "\n\n" + variable
    return core


def _format_tool_descriptions(tools: list[LLMToolSchema]) -> str:
    return PromptAssembler._format_tool_descriptions(tools)


def reflection_test_failed() -> str:
    return _render_prompt("reflection/test-failed.md")


def reflection_no_edit(n: int) -> str:
    return _render_prompt("reflection/no-edit.md", n=n)


def reflection_loop_detected(n: int) -> str:
    return _render_prompt("reflection/loop-detected.md", n=n)


def build_task_prompt(
    description: str,
    repo_path: str,
    issue_url: str | None = None,
    intent: TaskIntent | str = TaskIntent.EDIT,
) -> str:
    issue_section = ""
    if issue_url:
        issue_section = _ISSUE_SECTION_TEMPLATE.format(issue_url=issue_url)

    typed_intent = TaskIntent(intent)
    template = "task-analysis.md" if typed_intent is TaskIntent.ANALYSIS else "task.md"
    return _render_prompt(
        template,
        repo_path=repo_path,
        description=description.strip(),
        issue_section=issue_section,
    )


def build_system_prompt_structured(
    repo_path: str,
    tools: list[LLMToolSchema],
    repo_summary: str | None = None,
    memory_section: str = "",
    auto_memory_enabled: bool = False,
    enable_caching: bool = False,
) -> "str | list[dict]":
    core = build_system_prompt_core(repo_path, tools, repo_summary)
    variable = build_system_prompt_variable(memory_section, auto_memory_enabled)

    if not enable_caching:
        if variable:
            return core.rstrip() + "\n\n" + variable
        return core

    blocks = [{"type": "text", "text": core, "cache_control": {"type": "ephemeral"}}]
    if variable:
        blocks.append({"type": "text", "text": variable})
    return blocks


def build_planning_prompt(task_description: str) -> str:
    return _PLANNING_SYSTEM_TEMPLATE


def get_plan_mode_injection() -> str:
    return _render_prompt("modes/plan.md")


def get_plan_execution_injection() -> str:
    return _render_prompt("modes/plan-execute.md")


def get_dag_plan_prompt() -> str:
    return _render_prompt("modes/plan-dag.md")


def build_dag_subtask_prompt(
    subtask_id: str,
    description: str,
    expected_outcome: str,
    upstream_context: str,
) -> str:
    upstream_section = ""
    if upstream_context:
        upstream_section = (
            "## Upstream Results (from completed dependencies)\n"
            f"{upstream_context}\n\n"
        )
    return _render_prompt(
        "agents/dag-subtask.md",
        subtask_id=subtask_id,
        description=description,
        expected_outcome=expected_outcome or "(not specified)",
        upstream_section=upstream_section,
    )


def build_sub_agent_system_prompt(tools: list) -> str:
    tool_desc = _format_tool_descriptions(tools)
    return (
        "You are a focused coding assistant. Use the tools below to complete your task.\n\n"
        f"## Available Tools\n{tool_desc}"
    )


def build_coordinator_system_prompt(
    task_description: str,
    repo_path: str,
    total_budget: int,
    sub_agent_budget: int,
    max_retries: int,
) -> str:
    return _render_prompt(
        "modes/coordinator.md",
        task_description=task_description,
        repo_path=repo_path,
        sub_agent_budget=sub_agent_budget,
        max_retries=max_retries,
    )


def build_coordinator_prompt(
    task_description: str,
    max_agents: int = 8,
    repo_path: str = "",
    sub_agent_budget: int = 0,
    max_retries: int = 2,
) -> str:
    return _render_prompt(
        "modes/coordinator.md",
        task_description=task_description,
        repo_path=repo_path or ".",
        sub_agent_budget=sub_agent_budget or "(auto)",
        max_retries=max_retries,
    )


def build_long_term_context(
    memory_context=None,
    skills_prompt: str = "",
    repo_path: str = ".",
) -> str | None:
    """Build long-term memory context. Delegates to memory/injection_service."""
    from memory.injection_service import build_injection_context
    return build_injection_context(
        memory_context=memory_context,
        skills_prompt=skills_prompt,
        repo_path=repo_path,
    )


def build_sub_agent_prompt(
    role: str,
    task_prompt: str,
    upstream_context: str = "",
) -> str:
    upstream_section = ""
    if upstream_context:
        upstream_section = (
            "## Upstream Context (results from prior agents)\n"
            f"{upstream_context}\n\n"
        )
    return _render_prompt(
        "agents/sub-agent.md",
        role=role.capitalize(),
        task_prompt=task_prompt,
        upstream_section=upstream_section,
    )
