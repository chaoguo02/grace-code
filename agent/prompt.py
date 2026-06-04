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
# Planning prompt（PlanExecuteAgent 用）
# ---------------------------------------------------------------------------

_PLANNING_SYSTEM_TEMPLATE = """\
You are a task planning agent. Your job is to analyze a coding task and break \
it down into a sequence of clear, actionable subtasks.

## Instructions
1. Analyze the task carefully — what needs to be done, in what order
2. Break it down into sequential subtasks — each subtask should be a self-contained unit
3. Order matters: later subtasks may depend on earlier ones
4. Each subtask should produce a concrete, verifiable outcome
5. Include a final subtask for verification (running tests, checking results)

## Output Format
Respond ONLY with a JSON object. No markdown, no extra text.

```json
{{
  "reasoning": "<brief explanation of your overall approach>",
  "plan": [
    {{
      "id": "1",
      "description": "<what to do — will be given directly to a coding agent>",
      "expected_outcome": "<what success looks like for this step>"
    }}
  ]
}}
```

## Rules
- Keep subtask descriptions specific and self-contained
- 3-7 subtasks is typical; do not exceed 10
- Each subtask must produce a verifiable result
- If the task is trivial, 1-2 subtasks is acceptable\
"""


def build_planning_prompt(task_description: str) -> str:
    """返回规划专用的 system prompt。"""
    return _PLANNING_SYSTEM_TEMPLATE.format()


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