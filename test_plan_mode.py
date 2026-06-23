from __future__ import annotations

from pathlib import Path

from agent.completion import CompletionValidator
from agent.core import AgentConfig, PlanExecuteAgent
from agent.event_log import EventLog
from agent.factory import classify_task_intent
from agent.plan import PlanApproval, PlanExecuteConfig
from agent.policy import build_task_policy
from agent.policy_registry import PolicyAwareToolRegistry
from agent.task import Action, ActionType, RunStatus, Task, ToolCall
from llm.base import MockBackend
from tools.base import BaseTool, ToolRegistry, ToolResult


class RecordingTool(BaseTool):
    def __init__(self, name: str, output: str = "ok") -> None:
        self._name = name
        self.output = output
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"recording {self._name}"

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    def execute(self, params: dict) -> ToolResult:
        self.calls.append(params)
        return ToolResult(success=True, output=self.output)


def make_registry() -> tuple[ToolRegistry, RecordingTool, RecordingTool]:
    read_tool = RecordingTool("file_read", "# Forge Agent\nA local autonomous coding agent.")
    write_tool = RecordingTool("file_write", "written")
    registry = ToolRegistry().register(read_tool).register(write_tool)
    return registry, read_tool, write_tool


def make_log(tmp_path: Path, task: Task) -> EventLog:
    return EventLog.create(task, log_dir=str(tmp_path / "logs"))


def test_policy_distinguishes_read_and_write_scope(tmp_path: Path) -> None:
    read_policy = build_task_policy(Task("只允许读取 README，不要查看其他文件", str(tmp_path), intent="analysis"))
    assert read_policy.execution.allowed_read_paths == frozenset({"README.md"})
    assert read_policy.execution.allowed_write_paths is None
    assert read_policy.completion.required_reads == frozenset({"README.md"})

    write_policy = build_task_policy(Task("只允许修改 README，不要查看或修改其他文件", str(tmp_path), intent="edit"))
    assert write_policy.execution.allowed_read_paths == frozenset({"README.md"})
    assert write_policy.execution.allowed_write_paths == frozenset({"README.md"})
    assert write_policy.completion.required_writes == frozenset({"README.md"})


def test_policy_normalizes_absolute_and_relative_paths(tmp_path: Path) -> None:
    target = tmp_path / "config" / "default.yaml"
    absolute_policy = build_task_policy(Task(f"only read {target}", str(tmp_path), intent="analysis"))
    relative_policy = build_task_policy(Task("only read config/default.yaml", str(tmp_path), intent="analysis"))

    assert absolute_policy.execution.allowed_read_paths == frozenset({"config/default.yaml"})
    assert relative_policy.execution.allowed_read_paths == absolute_policy.execution.allowed_read_paths


def test_policy_registry_hides_and_blocks_denied_tools(tmp_path: Path) -> None:
    read_tool = RecordingTool("file_read", "read")
    web_tool = RecordingTool("web_search", "web")
    registry = ToolRegistry().register(read_tool).register(web_tool)
    policy = build_task_policy(Task("只允许读取 README，不要联网", str(tmp_path), intent="analysis"))
    wrapped = PolicyAwareToolRegistry(registry, policy.execution, str(tmp_path), "execution")

    schema_names = {schema.name for schema in wrapped.get_schemas()}
    assert "file_read" in schema_names
    assert "web_search" not in schema_names

    result = wrapped.execute_tool("web_search", {"query": "README"})
    assert not result.success
    assert "blocked by task constraints" in (result.error or "")
    assert web_tool.calls == []


def test_completion_validator_requires_logged_read(tmp_path: Path) -> None:
    task = Task("只允许读取 README", str(tmp_path), intent="analysis")
    policy = build_task_policy(task)
    log = make_log(tmp_path, task)
    try:
        log.log_task_start(task)
        verdict = CompletionValidator().validate(log, policy, str(tmp_path))
    finally:
        log.close()

    assert not verdict.success
    assert "without reading required source file" in verdict.reason


def test_completion_validator_accepts_logged_write(tmp_path: Path) -> None:
    task = Task("只允许修改 README", str(tmp_path), intent="edit")
    policy = build_task_policy(task)
    log = make_log(tmp_path, task)
    try:
        log.log_task_start(task)
        log.log_action(1, Action(ActionType.TOOL_CALL, "write", [ToolCall("file_write", {"path": "README.md"})]))
        log.log_observation(1, ToolResult(success=True, output="ok").to_observation("file_write"))
        verdict = CompletionValidator().validate(log, policy, str(tmp_path))
    finally:
        log.close()

    assert verdict.success


def test_analysis_plan_cannot_finish_without_reading_allowed_file(tmp_path: Path) -> None:
    registry, read_tool, write_tool = make_registry()
    backend = MockBackend([
        Action(ActionType.FINISH, "plan", message="### Goal\nRead pyproject after approval."),
        Action(ActionType.FINISH, "fake answer", message="I planned again instead of reading."),
    ])
    task = Task(
        "请从 pyproject.toml 中找出项目名。只允许读取 pyproject.toml，不要查看其他文件。",
        str(tmp_path),
        intent="analysis",
        max_steps=9,
        budget_tokens=9000,
    )
    cfg = PlanExecuteConfig(
        plan_subtask_log_dir=str(tmp_path / "subtasks"),
        plan_approval_callback=lambda plan: PlanApproval(approved=True),
    )
    agent = PlanExecuteAgent(backend, registry, AgentConfig(stream=False), cfg)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.GAVE_UP
    assert result.summary == "Approved analysis plan finished without reading the allowed source file."
    assert read_tool.calls == []
    assert write_tool.calls == []


def test_analysis_plan_executes_with_readonly_tools(tmp_path: Path) -> None:

    registry, read_tool, write_tool = make_registry()
    backend = MockBackend([
        Action(ActionType.FINISH, "plan", message="### Goal\nAnswer from README after approval.\n\n### Constraints\nOnly read README.\n\n### Steps\nRead README and answer.\n\n### Verification\nCite README."),
        Action(ActionType.TOOL_CALL, "read", [ToolCall("file_read", {"path": str(tmp_path / "README.md")})]),
        Action(ActionType.FINISH, "answer", message="Forge Agent — a local autonomous coding agent."),
    ])
    task = Task(
        description="查看 README 的项目名称，并用一句话总结它是什么。只允许读取 README。",
        repo_path=str(tmp_path),
        intent="analysis",
        max_steps=9,
        budget_tokens=9000,
    )
    cfg = PlanExecuteConfig(
        plan_subtask_log_dir=str(tmp_path / "subtasks"),
        plan_approval_callback=lambda plan: PlanApproval(approved=True),
    )
    agent = PlanExecuteAgent(backend, registry, AgentConfig(stream=False), cfg)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.SUCCESS
    assert result.summary == "Forge Agent — a local autonomous coding agent."
    assert read_tool.calls == [{"path": str(tmp_path / "README.md")}]
    assert write_tool.calls == []
    assert "Forge Agent — a local autonomous coding agent." not in backend.received_messages[0][-1].content
    execution_prompt = backend.received_messages[1][-1].content
    assert "No tools are available during planning" not in execution_prompt
    assert "must read the approved source file now" in execution_prompt


def test_analysis_planning_phase_has_no_tools(tmp_path: Path) -> None:
    registry, read_tool, write_tool = make_registry()
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "premature read", [ToolCall("file_read", {"path": "README.md"})]),
        Action(ActionType.FINISH, "plan", message="### Goal\nRead README after approval."),
        Action(ActionType.TOOL_CALL, "read", [ToolCall("file_read", {"path": "README.md"})]),
        Action(ActionType.FINISH, "done", message="Answered from README."),
    ])
    task = Task(
        "请把查看 README 的项目名称拆成计划后执行。只允许读取 README，不要查看其他文件。",
        str(tmp_path),
        intent="analysis",
        max_steps=12,
        budget_tokens=12000,
    )
    cfg = PlanExecuteConfig(
        plan_subtask_log_dir=str(tmp_path / "subtasks"),
        plan_approval_callback=lambda plan: True,
    )
    agent = PlanExecuteAgent(backend, registry, AgentConfig(stream=False), cfg)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.SUCCESS
    assert read_tool.calls == [{"path": "README.md"}]
    assert write_tool.calls == []
    subtask_logs = sorted((tmp_path / "subtasks").glob("*.jsonl"))
    assert subtask_logs
    blocked_log = subtask_logs[-1].read_text(encoding="utf-8")
    assert "Unknown tool 'file_read'. Available tools: none" in blocked_log



def test_analysis_execution_cannot_use_disallowed_tool(tmp_path: Path) -> None:
    registry, _read_tool, write_tool = make_registry()
    backend = MockBackend([
        Action(ActionType.FINISH, "plan", message="### Goal\nAnswer read-only.\n\n### Steps\nRead allowed file."),
        Action(ActionType.TOOL_CALL, "bad discovery", [ToolCall("find_files", {"pattern": "README*"})]),
        Action(ActionType.TOOL_CALL, "read", [ToolCall("file_read", {"path": "README.md"})]),
        Action(ActionType.FINISH, "done", message="No discovery happened."),
    ])
    task = Task("只允许读取 README，不要查看其他文件", str(tmp_path), intent="analysis", max_steps=9, budget_tokens=9000)
    cfg = PlanExecuteConfig(
        plan_subtask_log_dir=str(tmp_path / "subtasks"),
        plan_approval_callback=lambda plan: True,
    )
    agent = PlanExecuteAgent(backend, registry, AgentConfig(stream=False), cfg)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.SUCCESS
    assert write_tool.calls == []
    errors = [event.payload["observation"].get("error", "") for event in log.replay() if event.event_type.value == "observation"]
    assert any("Tool 'find_files' is blocked by task constraints" in error for error in errors)


def test_edit_scope_blocks_other_file_reads(tmp_path: Path) -> None:
    read_tool = RecordingTool("file_read", "read")
    write_tool = RecordingTool("file_write", "written")
    registry = ToolRegistry().register(read_tool).register(write_tool)
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "bad planning read", [ToolCall("file_read", {"path": "pyproject.toml"})]),
        Action(ActionType.FINISH, "plan", message="### Goal\nOnly edit README."),
        Action(ActionType.TOOL_CALL, "bad execution read", [ToolCall("file_read", {"path": "pyproject.toml"})]),
        Action(ActionType.TOOL_CALL, "write README", [ToolCall("file_write", {"path": "README.md"})]),
        Action(ActionType.FINISH, "done", message="Blocked unrelated read."),
    ])
    task = Task(
        "请把 README 里的说明改得更简洁。只允许修改 README，不要查看或修改其他文件。",
        str(tmp_path),
        intent="edit",
        max_steps=12,
        budget_tokens=12000,
    )
    cfg = PlanExecuteConfig(
        plan_subtask_log_dir=str(tmp_path / "subtasks"),
        plan_approval_callback=lambda plan: True,
    )
    agent = PlanExecuteAgent(backend, registry, AgentConfig(stream=False), cfg)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.SUCCESS
    assert read_tool.calls == []
    assert write_tool.calls == [{"path": "README.md"}]

    errors = [event.payload["observation"].get("error", "") for event in log.replay() if event.event_type.value == "observation"]
    subtask_logs = sorted((tmp_path / "subtasks").glob("*.jsonl"))
    plan_log_text = subtask_logs[-1].read_text(encoding="utf-8")
    assert "allow only: README.md" in plan_log_text
    assert any("allow only: README.md" in error for error in errors)



def test_edit_scope_requires_path_for_git_diff(tmp_path: Path) -> None:
    diff_tool = RecordingTool("git_diff", "diff")
    write_tool = RecordingTool("file_write", "written")
    registry = ToolRegistry().register(diff_tool).register(write_tool)
    backend = MockBackend([
        Action(ActionType.FINISH, "plan", message="### Goal\nOnly edit README."),
        Action(ActionType.TOOL_CALL, "bad diff", [ToolCall("git_diff", {})]),
        Action(ActionType.TOOL_CALL, "good diff", [ToolCall("git_diff", {"path": "README.md"})]),
        Action(ActionType.TOOL_CALL, "write", [ToolCall("file_write", {"path": "README.md"})]),
        Action(ActionType.FINISH, "done", message="Diff constrained."),
    ])
    task = Task(
        "请把 README 里的说明改得更简洁。只允许修改 README，不要查看或修改其他文件。",
        str(tmp_path),
        intent="edit",
        max_steps=12,
        budget_tokens=12000,
    )
    cfg = PlanExecuteConfig(
        plan_subtask_log_dir=str(tmp_path / "subtasks"),
        plan_approval_callback=lambda plan: True,
    )
    agent = PlanExecuteAgent(backend, registry, AgentConfig(stream=False), cfg)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.SUCCESS
    assert diff_tool.calls == [{"path": "README.md"}]
    assert write_tool.calls == [{"path": "README.md"}]
    errors = [event.payload["observation"].get("error", "") for event in log.replay() if event.event_type.value == "observation"]
    assert any("git_diff is blocked by task constraints unless a permitted path is provided" in error for error in errors)



def test_chinese_readme_addition_is_classified_as_edit() -> None:
    assert classify_task_intent("请给 README 增加一个“本地测试”小节") == "edit"


def test_edit_plan_cannot_finish_without_write(tmp_path: Path) -> None:
    registry, _read_tool, write_tool = make_registry()
    backend = MockBackend([
        Action(ActionType.FINISH, "plan", message="### Goal\nEdit README."),
        Action(ActionType.FINISH, "fake done", message="I planned again instead of writing."),
    ])
    task = Task("请修改 README。只允许修改 README。", str(tmp_path), intent="edit", max_steps=9, budget_tokens=9000)
    cfg = PlanExecuteConfig(
        plan_subtask_log_dir=str(tmp_path / "subtasks"),
        plan_approval_callback=lambda plan: PlanApproval(approved=True),
    )
    agent = PlanExecuteAgent(backend, registry, AgentConfig(stream=False), cfg)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.GAVE_UP
    assert result.summary == "Approved edit plan finished without performing any file write."
    assert write_tool.calls == []



def test_edit_plan_executes_with_full_tools(tmp_path: Path) -> None:
    registry, _read_tool, write_tool = make_registry()
    backend = MockBackend([
        Action(ActionType.FINISH, "plan", message="### Goal\nChange README.\n\n### Steps\nWrite README."),
        Action(ActionType.TOOL_CALL, "write", [ToolCall("file_write", {"path": "README.md"})]),
        Action(ActionType.FINISH, "done", message="README updated."),
    ])
    task = Task("更新 README", str(tmp_path), intent="edit", max_steps=9, budget_tokens=9000)
    cfg = PlanExecuteConfig(
        plan_subtask_log_dir=str(tmp_path / "subtasks"),
        plan_approval_callback=lambda plan: PlanApproval(approved=True),
    )
    agent = PlanExecuteAgent(backend, registry, AgentConfig(stream=False), cfg)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.SUCCESS
    assert result.summary == "README updated."
    assert write_tool.calls == [{"path": "README.md"}]


def test_revise_approval_replans_before_execute(tmp_path: Path) -> None:
    registry, read_tool, write_tool = make_registry()
    backend = MockBackend([
        Action(ActionType.FINISH, "plan", message="### Goal\nPlan too broad."),
        Action(ActionType.FINISH, "revised plan", message="### Goal\nPlan only README."),
        Action(ActionType.TOOL_CALL, "read", [ToolCall("file_read", {"path": "README.md"})]),
        Action(ActionType.FINISH, "done", message="Answered after revised plan."),
    ])
    approvals = iter([
        PlanApproval(approved=True, action="revise", feedback="Need a narrower plan"),
        PlanApproval(approved=True),
    ])
    task = Task("查看 README", str(tmp_path), intent="analysis", max_steps=12, budget_tokens=12000)
    cfg = PlanExecuteConfig(
        plan_subtask_log_dir=str(tmp_path / "subtasks"),
        plan_approval_callback=lambda plan: next(approvals),
        max_replans=1,
    )
    agent = PlanExecuteAgent(backend, registry, AgentConfig(stream=False), cfg)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.SUCCESS
    assert result.summary == "Answered after revised plan."
    assert read_tool.calls == [{"path": "README.md"}]
    assert write_tool.calls == []
    assert backend.call_count == 4
    assert "Need a narrower plan" in backend.received_messages[1][-1].content
