"""
agent/prompt.py

System prompt 模板管理。

职责：
- 维护 agent 的 system prompt 模板
- 根据运行时信息（工具列表、repo 概况）渲染最终 prompt
- 提供 Reflection prompt 模板

设计原则：
- prompt 集中在这里，修改 prompt 不需要改 core.py
- 模板用 str.format() 而不是 jinja2，减少依赖
- 每个 prompt 都有对应的函数，便于测试和调整
"""

from __future__ import annotations

from llm.base import LLMToolSchema


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATE = """\
You are an autonomous coding agent. Your goal is to understand a coding task, \
explore the repository, make the necessary code changes, and verify they work correctly.

## Workflow
1. **Explore**: Understand the repository structure and the problem
2. **Plan**: Identify what needs to change and why
3. **Edit**: Make precise, minimal changes using the available tools
4. **Verify**: Run tests to confirm the fix works
5. **Finish**: Call finish with a clear summary of what you changed

## Rules
- Think step by step before each action (use the thought field)
- After editing files, always run tests to verify your changes
- If tests fail, read the error carefully and fix the root cause, not the symptom
- If you are stuck after several attempts, reflect on your approach and try differently
- Make the smallest change that fixes the problem
- When done, call finish. If you truly cannot solve it, call give_up with an explanation
- **When to use web tools**: use web_search to look up API documentation, library usage, \
error messages, or best practices that are not in the local codebase. \
Use web_fetch to read a specific page in detail after a search. \
Do NOT use web tools for tasks that can be solved with local tools (grep, file_read, etc.)

## Repository
Path: {repo_path}
{repo_summary}

## Available tools
{tool_descriptions}
"""

_NO_REPO_SUMMARY = "(Repository summary not yet available — use find_files and file_read to explore)"


def build_system_prompt(
    repo_path: str,
    tools: list[LLMToolSchema],
    repo_summary: str | None = None,
) -> str:
    """
    渲染完整的 system prompt。

    Args:
        repo_path:    repo 根目录路径
        tools:        已注册工具的 schema 列表
        repo_summary: repo-map 生成的摘要（Day 5 接入，当前传 None）

    Returns:
        渲染好的 system prompt 字符串
    """
    tool_descriptions = _format_tool_descriptions(tools)
    summary = repo_summary or _NO_REPO_SUMMARY

    return _SYSTEM_TEMPLATE.format(
        repo_path=repo_path,
        repo_summary=summary,
        tool_descriptions=tool_descriptions,
    )


def _format_tool_descriptions(tools: list[LLMToolSchema]) -> str:
    """把工具列表格式化为易读的描述块。"""
    if not tools:
        return "(no tools available)"
    lines = []
    for tool in tools:
        lines.append(f"- **{tool.name}**: {tool.description}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reflection prompts
# ---------------------------------------------------------------------------

REFLECTION_TEST_FAILED = """\
[REFLECTION] The tests just failed. Before your next action, consider:
1. Read the full error message above carefully — what is the root cause?
2. Is your last edit correct? Did it introduce a new bug?
3. Do you need to look at more context before editing again?

Be specific about what you will do differently. What is your next action?\
"""

REFLECTION_NO_EDIT = """\
[REFLECTION] You have taken {n} steps without editing any file.
You may be stuck in an exploration loop. Consider:
1. Do you have enough context to make a change now?
2. If yes — make the edit
3. If no — identify exactly what you still need, get it in one targeted step, then edit

What specific action will move the task forward?\
"""

REFLECTION_LOOP_DETECTED = """\
[REFLECTION] You have repeated the same action {n} times in a row.
This suggests you are stuck. Stop and reconsider:
1. What are you trying to achieve with this action?
2. Why isn't it working?
3. What completely different approach could you try?

Do not repeat the same action again.\
"""


def reflection_test_failed() -> str:
    return REFLECTION_TEST_FAILED


def reflection_no_edit(n: int) -> str:
    return REFLECTION_NO_EDIT.format(n=n)


def reflection_loop_detected(n: int) -> str:
    return REFLECTION_LOOP_DETECTED.format(n=n)


# ---------------------------------------------------------------------------
# Task prompt（用户消息，描述任务）
# ---------------------------------------------------------------------------

_TASK_TEMPLATE = """\
Please fix the following issue in the repository at {repo_path}.

## Task
{description}
{issue_section}
## Instructions
- Start by exploring the repository to understand the codebase
- Make the minimal changes necessary to fix the issue
- Run the tests to verify your fix works
- When complete, call finish with a summary of your changes\
"""

_ISSUE_SECTION_TEMPLATE = """
## GitHub Issue
URL: {issue_url}
"""


# ---------------------------------------------------------------------------
# Planning prompt（PlanExecuteAgent Phase 1 用 — 只读探索）
# ---------------------------------------------------------------------------

_PLAN_MODE_INJECTION = """\
[PLAN MODE] You are in planning mode — a read-only exploration phase.

Your job is to explore the codebase, understand the problem, and produce a \
clear implementation plan. You MUST NOT make any edits, run any shell commands, \
or otherwise modify the system.

## Available tools (read-only only)
You can use: file_read, file_view, find_files, find_symbol, search_text, \
git_status, git_diff, web_search, web_fetch

You MUST NOT use: file_write, shell, pytest, git_add, git_commit

## Workflow
1. Explore the relevant code to understand the current state
2. Identify what needs to change and where
3. When ready, call **finish** with your implementation plan as the message

## Plan format
Your plan (the finish message) should be structured markdown:

### Analysis
What you found: key files, functions, current behavior

### Changes
What needs to change: specific files, functions, edits to make

### Verification
How to verify: what tests to run, expected outcomes

Be specific — name files, functions, line numbers. This plan will be shown to \
the user for approval before execution begins.\
"""

_PLAN_EXECUTION_INJECTION = """\
[EXECUTION MODE] The user has approved your plan. Execute it now.

You have full tool access. Make the changes described in your plan, then verify \
they work. When complete, call finish with a summary of what you changed.\
"""

# Legacy template for backward compatibility (old JSON-based planning)
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


def build_planning_prompt(task_description: str) -> str:
    """返回规划专用的 system prompt（legacy JSON 模式，保留兼容）。"""
    return _PLANNING_SYSTEM_TEMPLATE


def get_plan_mode_injection() -> str:
    """返回 Plan Mode Phase 1 的 prompt 注入（只读探索阶段）。"""
    return _PLAN_MODE_INJECTION


def get_plan_execution_injection() -> str:
    """返回 Plan Mode Phase 2 的 prompt 注入（执行阶段）。"""
    return _PLAN_EXECUTION_INJECTION


def build_task_prompt(
    description: str,
    repo_path: str,
    issue_url: str | None = None,
) -> str:
    """
    构建任务描述的用户消息（对话的第一条 user 消息）。

    Args:
        description: 任务描述（自然语言）
        repo_path:   repo 根目录
        issue_url:   GitHub issue URL（可选）
    """
    issue_section = ""
    if issue_url:
        issue_section = _ISSUE_SECTION_TEMPLATE.format(issue_url=issue_url)

    return _TASK_TEMPLATE.format(
        repo_path=repo_path,
        description=description.strip(),
        issue_section=issue_section,
    )