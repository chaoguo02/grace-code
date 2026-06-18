"""
agent/prompt.py

System prompt 模板管理（薄兼容层）。

本文件保留所有旧函数签名，内部委托给 prompt.assembler.PromptAssembler。
外部调用者（core.py, plan.py, multi_agent.py）无需修改。

Prompt 内容现在存储为 prompts/ 目录下的 .md 文件，
支持三层覆盖：内置 → ~/.forge-agent/prompts/ → .forge-agent/prompts/
"""

from __future__ import annotations

from prompts.assembler import PromptAssembler
from llm.base import LLMToolSchema


# ---------------------------------------------------------------------------
# 模块级 assembler 实例（懒初始化，无 project_dir 时仅使用内置）
# ---------------------------------------------------------------------------

_assembler: PromptAssembler | None = None


def _get_assembler() -> PromptAssembler:
    """获取模块级 PromptAssembler 实例。"""
    global _assembler
    if _assembler is None:
        _assembler = PromptAssembler()
    return _assembler


def set_project_dir(project_dir: str) -> None:
    """设置项目目录，启用项目级 prompt 覆盖。应在 agent 初始化时调用。"""
    global _assembler
    _assembler = PromptAssembler(project_dir=project_dir)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_NO_REPO_SUMMARY = "(Repository summary not yet available — use find_files and file_read to explore)"


def build_system_prompt_core(
    repo_path: str,
    tools: list[LLMToolSchema],
    repo_summary: str | None = None,
) -> str:
    """
    渲染 system prompt 的稳定部分（每次调用不变）。

    包含：核心指令、工具列表、repo 摘要。
    这部分可以加 cache_control。
    """
    return _get_assembler().render_system_core(repo_path, tools, repo_summary)


def build_system_prompt_variable(
    memory_section: str = "",
    auto_memory_enabled: bool = False,
) -> str:
    """
    渲染 system prompt 的变化部分。

    记忆索引已移至独立的 user message（不影响 system prompt cache）。
    此处仅保留 auto memory 工具使用指导（稳定内容，可缓存）。
    """
    parts = []
    if memory_section:
        parts.append(f"## Memory\n{memory_section}")
    if auto_memory_enabled:
        parts.append(_get_assembler().render("memory/auto-memory.md"))
    return "\n\n".join(parts)


def build_system_prompt(
    repo_path: str,
    tools: list[LLMToolSchema],
    repo_summary: str | None = None,
    memory_section: str = "",
    auto_memory_enabled: bool = False,
) -> str:
    """
    渲染完整的 system prompt（稳定 + 变化部分拼接）。
    """
    core = build_system_prompt_core(repo_path, tools, repo_summary)
    variable = build_system_prompt_variable(memory_section, auto_memory_enabled)
    if variable:
        return core.rstrip() + "\n\n" + variable
    return core


def _format_tool_descriptions(tools: list[LLMToolSchema]) -> str:
    """把工具列表格式化为易读的描述块（按 name 排序，确保 cache 稳定）。"""
    return PromptAssembler._format_tool_descriptions(tools)


# ---------------------------------------------------------------------------
# Reflection prompts
# ---------------------------------------------------------------------------

REFLECTION_TEST_FAILED = None  # 延迟加载
REFLECTION_NO_EDIT = None
REFLECTION_LOOP_DETECTED = None


def reflection_test_failed() -> str:
    return _get_assembler().render_reflection("test-failed")


def reflection_no_edit(n: int) -> str:
    return _get_assembler().render_reflection("no-edit", n=n)


def reflection_loop_detected(n: int) -> str:
    return _get_assembler().render_reflection("loop-detected", n=n)


# ---------------------------------------------------------------------------
# Task prompt
# ---------------------------------------------------------------------------

_ISSUE_SECTION_TEMPLATE = """
## GitHub Issue
URL: {issue_url}
"""


def build_task_prompt(
    description: str,
    repo_path: str,
    issue_url: str | None = None,
    intent: str = "edit",
) -> str:
    """构建任务描述的用户消息。"""
    issue_section = ""
    if issue_url:
        issue_section = _ISSUE_SECTION_TEMPLATE.format(issue_url=issue_url)

    template = "task-analysis.md" if intent == "analysis" else "task.md"
    return _get_assembler().render(
        template,
        repo_path=repo_path,
        description=description.strip(),
        issue_section=issue_section,
    )


# ---------------------------------------------------------------------------
# Planning prompt
# ---------------------------------------------------------------------------

_PLANNING_SYSTEM_TEMPLATE = """\
You are a task planner. Break the user's coding task into a short, concrete \
sequence of subtasks. Each subtask will be executed by a coding agent with \
access to file read/write, shell, search, and test tools.

## Rules
- 2-5 subtasks is ideal; never exceed 7
- Each description MUST mention specific files or functions to act on
- NO vague descriptions — avoid "analyze the codebase", "explore", "understand"
- Instead use: "Read src/parser.py and find the Tokenizer class", \
"Edit src/parser.py: fix the __init__ method to handle empty input"
- The last subtask MUST verify the fix (run tests, check output)
- Keep reasoning under 200 characters — just the approach

## Output Format
Respond with exactly one line in this format:

TASK_COMPLETE: {{"reasoning": "<brief>", "plan": [{{"id": "1", "description": "...", "expected_outcome": "..."}}]}}\
"""


def build_system_prompt_structured(
    repo_path: str,
    tools: list[LLMToolSchema],
    repo_summary: str | None = None,
    memory_section: str = "",
    auto_memory_enabled: bool = False,
    enable_caching: bool = False,
) -> "str | list[dict]":
    """
    渲染 system prompt，返回适合含 cache_control 的格式。

    当 enable_caching=True 时，返回 content blocks 列表（Anthropic 模式）。
    否则返回普通字符串。
    """
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
    """返回规划专用的 system prompt（legacy JSON 模式，保留兼容）。"""
    return _PLANNING_SYSTEM_TEMPLATE


def get_plan_mode_injection() -> str:
    """返回 Plan Mode Phase 1 的 prompt 注入（只读探索阶段）。"""
    return _get_assembler().render("modes/plan.md")


def get_plan_execution_injection() -> str:
    """返回 Plan Mode Phase 2 的 prompt 注入（执行阶段）。"""
    return _get_assembler().render("modes/plan-execute.md")


# ---------------------------------------------------------------------------
# DAG Plan prompts
# ---------------------------------------------------------------------------

def get_dag_plan_prompt() -> str:
    """返回 DAG Plan 阶段的 prompt 注入。"""
    return _get_assembler().render("modes/plan-dag.md")


def build_dag_subtask_prompt(
    subtask_id: str,
    description: str,
    expected_outcome: str,
    upstream_context: str,
) -> str:
    """构建单个 subtask 的执行 prompt。"""
    upstream_section = ""
    if upstream_context:
        upstream_section = (
            "## Upstream Results (from completed dependencies)\n"
            f"{upstream_context}\n\n"
        )
    return _get_assembler().render_agent_prompt(
        "dag-subtask",
        subtask_id=subtask_id,
        description=description,
        expected_outcome=expected_outcome or "(not specified)",
        upstream_section=upstream_section,
    )


# ---------------------------------------------------------------------------
# Multi-Agent prompt 模板
# ---------------------------------------------------------------------------

def build_sub_agent_system_prompt(tools: list) -> str:
    """构建 sub-agent 专用精简 system prompt（不含 repo_map、workflow 指导）。"""
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
    """构建 Coordinator 的任务 prompt。"""
    return _get_assembler().render(
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
    """构建 Coordinator prompt（简化入口）。"""
    return _get_assembler().render(
        "modes/coordinator.md",
        task_description=task_description,
        repo_path=repo_path or ".",
        sub_agent_budget=sub_agent_budget or "(auto)",
        max_retries=max_retries,
    )


def build_sub_agent_prompt(
    role: str,
    task_prompt: str,
    upstream_context: str = "",
) -> str:
    """构建子 Agent 的 prompt。"""
    upstream_section = ""
    if upstream_context:
        upstream_section = (
            "## Upstream Context (results from prior agents)\n"
            f"{upstream_context}\n\n"
        )
    return _get_assembler().render_agent_prompt(
        "sub-agent",
        role=role.capitalize(),
        task_prompt=task_prompt,
        upstream_section=upstream_section,
    )
