from __future__ import annotations

from pathlib import Path

from agent.core import AgentConfig, PlanExecuteAgent
from agent.event_log import EventLog
from agent.plan import PlanApproval, PlanExecuteConfig
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
    assert any("Tool 'find_files' is blocked by the user's single-file constraint" in error for error in errors)


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
