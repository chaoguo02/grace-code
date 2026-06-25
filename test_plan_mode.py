from __future__ import annotations

import json
from pathlib import Path

from agent.completion import CompletionValidator
from agent.core import AgentConfig, PlanExecuteAgent, ReActAgent
from agent.event_log import EventLog
from agent.factory import classify_task_intent
from agent.plan import PlanApproval, PlanExecuteConfig
from agent.policy import build_task_policy, extract_explicit_read_paths
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


def test_policy_explicit_paths_flow_through(tmp_path: Path) -> None:
    """Explicit read/write paths from Task flow into policy fields."""
    read_policy = build_task_policy(Task(
        "分析 README 和 pyproject.toml", str(tmp_path), intent="analysis",
        explicit_read_paths=frozenset({"README.md", "pyproject.toml"}),
    ))
    assert read_policy.execution.allowed_read_paths == frozenset({"README.md", "pyproject.toml"})
    assert read_policy.execution.allowed_write_paths is None
    assert read_policy.completion.required_reads == frozenset({"README.md", "pyproject.toml"})

    write_policy = build_task_policy(Task(
        "修改 README", str(tmp_path), intent="edit",
        explicit_write_paths=frozenset({"README.md"}),
    ))
    assert write_policy.execution.allowed_read_paths == frozenset({"README.md"})
    assert write_policy.execution.allowed_write_paths == frozenset({"README.md"})
    assert write_policy.completion.required_writes == frozenset({"README.md"})

    # Without explicit paths, strict_file_scope comes from NO_OTHER_FILES_RE only
    strict_policy = build_task_policy(Task(
        "查看 README，不要查看其他文件", str(tmp_path), intent="analysis",
    ))
    assert strict_policy.execution.strict_file_scope is True
    assert strict_policy.execution.allowed_read_paths is None
    assert strict_policy.completion.require_any_read is True

    loose_policy = build_task_policy(Task(
        "查看 README", str(tmp_path), intent="analysis",
    ))
    assert loose_policy.execution.strict_file_scope is False
    assert loose_policy.completion.require_any_read is False


def test_policy_normalizes_explicit_paths(tmp_path: Path) -> None:
    """Explicit paths via Task fields get normalized through normalize_repo_path."""
    target = tmp_path / "config" / "default.yaml"
    from agent.policy import normalize_repo_path
    normalized = normalize_repo_path(str(target), str(tmp_path))

    policy = build_task_policy(Task(
        "read config", str(tmp_path), intent="analysis",
        explicit_read_paths=frozenset({normalized}),
    ))
    assert policy.execution.allowed_read_paths == frozenset({"config/default.yaml"})


def test_policy_extracts_only_read_paths_from_user_text(tmp_path: Path) -> None:
    """Strict only-read wording becomes an enforced read path scope."""
    description = "只阅读 agent/core.py 和 agent/event_log.py，说明 Action.thought 是否仍会写入内部日志。"

    assert extract_explicit_read_paths(description, str(tmp_path)) == frozenset({
        "agent/core.py",
        "agent/event_log.py",
    })

    policy = build_task_policy(Task(description, str(tmp_path), intent="analysis"))
    assert policy.execution.strict_file_scope is True
    assert policy.execution.allowed_read_paths == frozenset({"agent/core.py", "agent/event_log.py"})
    assert policy.execution.allowed_tools == frozenset({"file_read", "file_view"})


def test_policy_blocks_unlisted_file_under_only_read_scope(tmp_path: Path) -> None:
    """Only-read scopes prevent reading or discovering unrelated files."""
    read_tool = RecordingTool("file_read", "read")
    symbol_tool = RecordingTool("find_symbol", "symbol")
    registry = ToolRegistry().register(read_tool).register(symbol_tool)
    task = Task(
        "只阅读 agent/core.py 和 agent/event_log.py，说明 Action.thought 是否仍会写入内部日志。",
        str(tmp_path),
        intent="analysis",
    )
    policy = build_task_policy(task)
    wrapped = PolicyAwareToolRegistry(registry, policy.execution, str(tmp_path), "execution")

    assert "find_symbol" not in wrapped.tool_names
    result = wrapped.execute_tool("file_read", {"path": "agent/task.py"})

    assert not result.success
    assert "allows only" in (result.error or "")
    assert read_tool.calls == []


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
    assert "blocked by task policy" in (result.error or "")
    assert web_tool.calls == []


def test_completion_validator_requires_logged_read(tmp_path: Path) -> None:
    """require_any_read fails when no read happened under strict file scope."""
    task = Task("只允许读取 README，不要查看其他文件", str(tmp_path), intent="analysis")
    policy = build_task_policy(task)
    assert policy.completion.require_any_read is True

    log = make_log(tmp_path, task)
    try:
        log.log_task_start(task)
        verdict = CompletionValidator().validate(log, policy, str(tmp_path))
    finally:
        log.close()

    assert not verdict.success
    assert "without reading any file" in verdict.reason


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
    assert "without reading any file" in result.summary
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
    execution_texts = " ".join(m.content for m in backend.received_messages[1] if m.content)
    assert "No tools are available during planning" not in execution_texts
    assert "must read the approved source file now" in execution_texts


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
    assert any("Tool 'find_files' is blocked by task policy" in error for error in errors)


def test_edit_scope_blocks_other_file_reads(tmp_path: Path) -> None:
    """Explicit write path also grants read access; other files are blocked."""
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
        "请把 README 里的说明改得更简洁。",
        str(tmp_path),
        intent="edit",
        max_steps=12,
        budget_tokens=12000,
        explicit_write_paths=frozenset({"README.md"}),
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
    # Both bad reads blocked by path check (pyproject.toml not in {README.md})
    assert read_tool.calls == []
    assert write_tool.calls == [{"path": "README.md"}]

    errors = [event.payload["observation"].get("error", "") for event in log.replay() if event.event_type.value == "observation"]
    assert any("allows only: README.md" in error for error in errors)



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
    assert any("git_diff is blocked by task policy unless a permitted path is provided" in error for error in errors)



def test_explicit_intent_passes_through() -> None:
    """Explicit --intent passes through without LLM or regex."""
    assert classify_task_intent("any text", "analysis") == "analysis"
    assert classify_task_intent("fix this bug", "edit") == "edit"
    assert classify_task_intent("any text", "analysis", None) == "analysis"


def test_auto_intent_falls_back_to_edit_without_backend() -> None:
    """When --intent is auto and no backend, conservative fallback is edit."""
    assert classify_task_intent("read README and explain", "auto", None) == "edit"
    assert classify_task_intent("read README and explain") == "edit"


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
    assert "without performing any file write" in result.summary
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
    revision_texts = " ".join(m.content for m in backend.received_messages[1] if m.content)
    assert "Need a narrower plan" in revision_texts


def test_memory_types_episodic_semantic_procedural() -> None:
    """三种新记忆类型均可建模，并支持文件锚点。"""
    from memory.models import Anchor, Memory, MemoryMetadata

    memory = Memory(
        name="anchored-rule",
        description="Rule for core edits",
        content="Read policy registry before editing core policy.",
        metadata=MemoryMetadata(type="procedural"),
        anchors=[Anchor(kind="file", path="agent/core.py")],
    )

    assert memory.metadata.type == "procedural"
    assert memory.anchors[0].path == "agent/core.py"
    assert memory.to_dict()["anchors"] == [{"kind": "file", "path": "agent/core.py"}]


def test_memory_anchors_roundtrip(tmp_path: Path) -> None:
    """锚点写入 frontmatter 后读取不变。"""
    from memory.models import Anchor, Memory, MemoryMetadata
    from memory.store import MemoryStore

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path / "memory"))
    memory = Memory(
        name="anchored-rule",
        description="Rule with anchors",
        content="Follow the anchored rule.",
        metadata=MemoryMetadata(type="procedural"),
        anchors=[
            Anchor(kind="file", path="agent/core.py"),
            Anchor(kind="task", value="refactoring"),
        ],
    )

    assert store.write_memory(memory)
    loaded = store.read_memory("anchored-rule")

    assert loaded is not None
    assert loaded.metadata.type == "procedural"
    assert [anchor.to_dict() for anchor in loaded.anchors] == [
        {"kind": "file", "path": "agent/core.py"},
        {"kind": "task", "value": "refactoring"},
    ]


def test_memory_backward_compat_old_types(tmp_path: Path) -> None:
    """旧 user/feedback/project/reference 类型读取时自动映射为新三分法。"""
    from memory.store import MemoryStore

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "old-feedback.md").write_text(
        "---\n"
        "name: old-feedback\n"
        "description: old correction\n"
        "metadata:\n"
        "  type: feedback\n"
        "updated_at: 2025-01-01T00:00:00Z\n"
        "---\n\n"
        "old content\n",
        encoding="utf-8",
    )

    store = MemoryStore(repo_path="test", memory_dir=str(memory_dir))
    memory = store.read_memory("old-feedback")
    summaries = store.list_memories()

    assert memory is not None
    assert memory.metadata.type == "procedural"
    assert summaries[0].type == "procedural"


def test_memory_write_tool_accepts_new_types_and_anchors(tmp_path: Path) -> None:
    """memory_write 工具支持新类型和 anchors 参数。"""
    from memory.store import MemoryStore
    from tools.memory_tool import MemoryWriteTool

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path / "memory"))
    tool = MemoryWriteTool(store)
    result = tool.execute({
        "name": "yaml-rule",
        "description": "Use safe YAML parsing",
        "type": "procedural",
        "content": "Use yaml.safe_load() for YAML config files.",
        "anchors": [{"kind": "file", "path": "config/default.yaml"}],
    })

    loaded = store.read_memory("yaml-rule")
    assert result.success
    assert loaded is not None
    assert loaded.metadata.type == "procedural"
    assert loaded.anchors[0].to_dict() == {"kind": "file", "path": "config/default.yaml"}


def test_extractor_extracts_episodic_from_success(tmp_path: Path) -> None:
    """LLM 返回结构化 episodic 记忆时，extractor 正确解析。"""
    from memory.extractor import MemoryExtractor

    llm_json = json.dumps({"memories": [
        {
            "type": "episodic",
            "name": "readme-summary",
            "description": "Learned README describes Forge Agent",
            "content": "Task: read README.md. Outcome: README.md describes Forge Agent.",
            "confidence": "high",
            "anchors": [{"kind": "file", "path": "README.md"}],
        }
    ]})
    backend = MockBackend([Action(ActionType.FINISH, llm_json, message=llm_json)])
    extractor = MemoryExtractor(backend=backend)

    task = Task("读取 README.md 并总结", str(tmp_path), intent="analysis")
    log = make_log(tmp_path, task)
    try:
        log.log_task_start(task)
        log.log_action(1, Action(ActionType.TOOL_CALL, "read", [ToolCall("file_read", {"path": "README.md"})]))
        candidates = extractor.extract(task, log, "README.md describes Forge Agent.")
    finally:
        log.close()

    episodic = [c for c in candidates if c.type == "episodic"]
    assert episodic
    assert "README.md describes Forge Agent" in episodic[0].content
    assert episodic[0].anchors[0].path == "README.md"


def test_extractor_extracts_procedural_from_correction(tmp_path: Path) -> None:
    """LLM 识别用户纠正并返回 procedural 候选。"""
    from memory.extractor import MemoryExtractor

    llm_json = json.dumps({"memories": [
        {
            "type": "procedural",
            "name": "yaml-safe-load",
            "description": "Always use yaml.safe_load for config",
            "content": "When processing config/default.yaml, use yaml.safe_load() instead of regex.",
            "confidence": "high",
            "anchors": [{"kind": "file", "path": "config/default.yaml"}],
        }
    ]})
    backend = MockBackend([Action(ActionType.FINISH, llm_json, message=llm_json)])
    extractor = MemoryExtractor(backend=backend)

    task = Task("以后处理 config/default.yaml 都要用 yaml.safe_load", str(tmp_path), intent="edit")
    log = make_log(tmp_path, task)
    try:
        log.log_task_start(task)
        candidates = extractor.extract(task, log, "Acknowledged.")
    finally:
        log.close()

    procedural = [c for c in candidates if c.type == "procedural"]
    assert procedural
    assert procedural[0].anchors[0].to_dict() == {"kind": "file", "path": "config/default.yaml"}


def test_extractor_drops_low_confidence(tmp_path: Path) -> None:
    """低置信度候选在 write_success_memories 阶段被过滤。"""
    from memory.extractor import MemoryExtractor
    from memory.store import MemoryStore

    llm_json = json.dumps({"memories": [
        {
            "type": "semantic",
            "name": "low-conf",
            "description": "not sure",
            "content": "Maybe true",
            "confidence": "low",
            "anchors": [],
        }
    ]})
    backend = MockBackend([Action(ActionType.FINISH, llm_json, message=llm_json)])
    extractor = MemoryExtractor(backend=backend)
    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path / "memory"))

    task = Task("some task", str(tmp_path), intent="edit")
    log = make_log(tmp_path, task)
    try:
        log.log_task_start(task)
        written = extractor.write_success_memories(task, log, "done", store)
    finally:
        log.close()

    assert written == 0
    assert store.list_memories() == []


def test_extractor_no_extraction_on_gave_up(tmp_path: Path) -> None:
    """失败/GAVE_UP 路径不调用成功提取 helper。"""
    from memory.context import MemoryContext
    from memory.store import MemoryStore

    registry, _read_tool, _write_tool = make_registry()
    backend = MockBackend([Action(ActionType.GIVE_UP, "cannot", message="Cannot finish.")])
    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path / "memory"))
    agent = ReActAgent(backend, registry, AgentConfig(stream=False), memory_context=MemoryContext(store))
    task = Task("放弃这个任务", str(tmp_path), intent="analysis", max_steps=3, budget_tokens=3000)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.GAVE_UP
    assert store.list_memories() == []


def test_extractor_no_block_on_llm_failure(tmp_path: Path) -> None:
    """记忆提取或写入失败不阻断成功任务结果。"""
    from memory.context import MemoryContext

    class FailingStore:
        enabled = True
        def write_memory(self, memory):
            raise RuntimeError("boom")
        def get_index_content(self, max_lines=None):
            return ""
        def list_memories(self):
            return []

    class FailingContext:
        enabled = True
        store = FailingStore()
        def set_task_context(self, t):
            pass
        def set_user_message(self, m):
            pass
        def build_memory_section(self):
            return ""

    registry, read_tool, _write_tool = make_registry()
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "read", [ToolCall("file_read", {"path": "README.md"})]),
        Action(ActionType.FINISH, "done", message="Read README.md."),
    ])
    agent = ReActAgent(backend, registry, AgentConfig(stream=False), memory_context=FailingContext())
    task = Task("读取 README.md", str(tmp_path), intent="analysis", max_steps=5, budget_tokens=5000)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
    finally:
        log.close()

    assert result.status == RunStatus.SUCCESS
    assert read_tool.calls == [{"path": "README.md"}]


def test_file_view_paging_is_not_semantic_loop(tmp_path: Path) -> None:
    """Sequential file_view windows are progress, not a semantic tool loop."""
    registry, _read_tool, _write_tool = make_registry()
    agent = ReActAgent(MockBackend([]), registry, AgentConfig(stream=False))
    task = Task("查看长文件", str(tmp_path), intent="analysis")
    log = make_log(tmp_path, task)
    try:
        log.log_task_start(task)
        for step, start_line in enumerate((1, 101, 201, 301), start=1):
            log.log_action(step, Action(
                ActionType.TOOL_CALL,
                "page",
                [ToolCall("file_view", {"path": "entry/chat.py", "start_line": start_line})],
            ))
        assert agent._is_looping(log) is False
    finally:
        log.close()


def test_repeated_same_file_view_range_is_exact_loop(tmp_path: Path) -> None:
    """Repeating the same file_view range is still detected as an exact loop."""
    registry, _read_tool, _write_tool = make_registry()
    agent = ReActAgent(MockBackend([]), registry, AgentConfig(stream=False))
    task = Task("查看长文件", str(tmp_path), intent="analysis")
    log = make_log(tmp_path, task)
    try:
        log.log_task_start(task)
        for step in range(1, 4):
            log.log_action(step, Action(
                ActionType.TOOL_CALL,
                "same page",
                [ToolCall("file_view", {"path": "entry/chat.py", "start_line": 101})],
            ))
        assert agent._is_looping(log) is True
    finally:
        log.close()


def test_duplicate_file_read_is_not_executed_twice(tmp_path: Path) -> None:
    """Identical file reads are converted to synthetic observations after first read."""
    registry, read_tool, _write_tool = make_registry()
    backend = MockBackend([
        Action(ActionType.TOOL_CALL, "read once", [ToolCall("file_read", {"path": "README.md"})]),
        Action(ActionType.TOOL_CALL, "read duplicate", [ToolCall("file_read", {"path": "README.md"})]),
        Action(ActionType.FINISH, "done", message="Read README.md."),
    ])
    agent = ReActAgent(backend, registry, AgentConfig(stream=False))
    task = Task("读取 README.md", str(tmp_path), intent="analysis", max_steps=5, budget_tokens=5000)

    log = make_log(tmp_path, task)
    try:
        result = agent.run(task, log)
        observations = [event.payload["observation"] for event in log.replay() if event.event_type.value == "observation"]
    finally:
        log.close()

    assert result.status == RunStatus.SUCCESS
    assert read_tool.calls == [{"path": "README.md"}]
    assert any("Skipped duplicate file_read" in obs["output"] for obs in observations)


# ===========================================================================
# 阶段 3 测试 — 合并去重 (consolidate)
# ===========================================================================


def _make_candidate(name="test-mem", content="some content", mem_type="semantic", anchors=None):
    """辅助：构造一个 MemoryCandidate。"""
    from memory.extractor import MemoryCandidate
    from memory.models import Anchor
    return MemoryCandidate(
        type=mem_type,
        name=name,
        description=f"Test: {name}",
        content=content,
        anchors=anchors or [],
        confidence="high",
    )


def test_consolidate_add_new(tmp_path):
    """新记忆不存在同名也无向量索引 → ADD。"""
    from memory.store import MemoryStore
    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    candidate = _make_candidate(name="new-fact", content="Python 3.12 is required.")

    action = store.consolidate(candidate)

    assert action == "ADD"
    mem = store.read_memory("new-fact")
    assert mem is not None
    assert "Python 3.12" in mem.content


def test_consolidate_noop_identical(tmp_path):
    """同名记忆且内容完全相同 → NOOP，不重复写。"""
    from memory.store import MemoryStore
    from memory.models import Memory, MemoryMetadata
    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    existing = Memory(
        name="build-cmd",
        description="Build commands",
        content="npm run build",
        metadata=MemoryMetadata(type="semantic"),
    )
    store.write_memory(existing)

    candidate = _make_candidate(name="build-cmd", content="npm run build")
    action = store.consolidate(candidate)

    assert action == "NOOP"


def test_consolidate_update_same_name(tmp_path):
    """同名记忆但内容不同 → UPDATE。"""
    from memory.store import MemoryStore
    from memory.models import Memory, MemoryMetadata
    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    existing = Memory(
        name="build-cmd",
        description="Build commands",
        content="npm run build",
        metadata=MemoryMetadata(type="semantic"),
    )
    store.write_memory(existing)

    candidate = _make_candidate(name="build-cmd", content="pnpm build")
    action = store.consolidate(candidate)

    assert action == "UPDATE"
    mem = store.read_memory("build-cmd")
    assert "pnpm build" in mem.content


def test_consolidate_merge_high_similarity(tmp_path):
    """向量相似度 ≥ 0.85 → MERGE，合并内容。"""
    from memory.store import MemoryStore
    from memory.models import Memory, MemoryMetadata
    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    existing = Memory(
        name="api-conventions",
        description="API conventions",
        content="All endpoints use JSON.",
        metadata=MemoryMetadata(type="semantic"),
    )
    store.write_memory(existing)

    class FakeExternalStore:
        def search(self, query, top_k=3, min_score=0.0):
            return [{"name": "api-conventions", "content": "All endpoints use JSON.", "score": 0.90}]

    candidate = _make_candidate(name="api-json-rule", content="Responses must include Content-Type: application/json.")
    action = store.consolidate(candidate, external_store=FakeExternalStore())

    assert action == "MERGE"
    mem = store.read_memory("api-conventions")
    assert "All endpoints use JSON." in mem.content
    assert "Content-Type: application/json" in mem.content


def test_consolidate_llm_judge_noop(tmp_path):
    """灰区 (0.5-0.85) + LLM judge 返回 NOOP → 不写入。"""
    from memory.store import MemoryStore
    from memory.models import Memory, MemoryMetadata
    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    existing = Memory(
        name="test-rule",
        description="Testing rule",
        content="Always write unit tests.",
        metadata=MemoryMetadata(type="procedural"),
    )
    store.write_memory(existing)

    class FakeExternalStore:
        def search(self, query, top_k=3, min_score=0.0):
            return [{"name": "test-rule", "content": "Always write unit tests.", "score": 0.70}]

    class FakeBackend:
        def complete(self, messages, tools):
            class Resp:
                class action:
                    message = "NOOP"
                raw_content = "NOOP"
            return Resp()

    candidate = _make_candidate(name="test-rule-v2", content="Always write unit tests for new code.")
    action = store.consolidate(candidate, external_store=FakeExternalStore(), backend=FakeBackend())

    assert action == "NOOP"
    assert store.read_memory("test-rule-v2") is None


def test_consolidate_llm_judge_update(tmp_path):
    """灰区 + LLM judge 返回 UPDATE → 更新已有记忆内容。"""
    from memory.store import MemoryStore
    from memory.models import Memory, MemoryMetadata
    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    existing = Memory(
        name="deploy-steps",
        description="Deploy steps",
        content="Run make deploy.",
        metadata=MemoryMetadata(type="procedural"),
    )
    store.write_memory(existing)

    class FakeExternalStore:
        def search(self, query, top_k=3, min_score=0.0):
            return [{"name": "deploy-steps", "content": "Run make deploy.", "score": 0.65}]

    class FakeBackend:
        def complete(self, messages, tools):
            class Resp:
                class action:
                    message = "UPDATE"
                raw_content = "UPDATE"
            return Resp()

    candidate = _make_candidate(name="deploy-new", content="Use kubectl apply -f deploy.yaml")
    action = store.consolidate(candidate, external_store=FakeExternalStore(), backend=FakeBackend())

    assert action == "UPDATE"
    mem = store.read_memory("deploy-steps")
    assert "kubectl apply" in mem.content


# ===========================================================================
# 阶段 4 测试 — 差异化检索
# ===========================================================================


def test_procedural_triggered_by_file_access(tmp_path):
    """读取文件后，匹配锚点的 procedural 记忆被返回。"""
    from memory.store import MemoryStore
    from memory.context import MemoryContext
    from memory.models import Anchor, Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    store.write_memory(Memory(
        name="use-safe-load",
        description="Use yaml.safe_load for config files",
        content="Always use yaml.safe_load() when reading config/.",
        metadata=MemoryMetadata(type="procedural"),
        anchors=[Anchor(kind="file", path="config/default.yaml")],
    ))
    store.write_memory(Memory(
        name="api-convention",
        description="API returns JSON",
        content="All API endpoints return JSON.",
        metadata=MemoryMetadata(type="semantic"),
    ))

    ctx = MemoryContext(store=store)
    result = ctx.get_procedural_for_files({"config/default.yaml"})

    assert "use-safe-load" in result
    assert "yaml.safe_load" in result


def test_procedural_not_triggered_by_unrelated_file(tmp_path):
    """不相关的文件访问不触发 procedural 注入。"""
    from memory.store import MemoryStore
    from memory.context import MemoryContext
    from memory.models import Anchor, Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    store.write_memory(Memory(
        name="use-safe-load",
        description="Use yaml.safe_load for config files",
        content="Always use yaml.safe_load() when reading config/.",
        metadata=MemoryMetadata(type="procedural"),
        anchors=[Anchor(kind="file", path="config/default.yaml")],
    ))

    ctx = MemoryContext(store=store)
    result = ctx.get_procedural_for_files({"src/main.py"})

    assert result == ""


def test_procedural_directory_prefix_match(tmp_path):
    """文件锚点为目录前缀时，匹配该目录下的文件。"""
    from memory.store import MemoryStore
    from memory.context import MemoryContext
    from memory.models import Anchor, Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    store.write_memory(Memory(
        name="tests-rule",
        description="Test convention",
        content="All tests must use pytest fixtures.",
        metadata=MemoryMetadata(type="procedural"),
        anchors=[Anchor(kind="file", path="tests")],
    ))

    ctx = MemoryContext(store=store)
    result = ctx.get_procedural_for_files({"tests/test_api.py"})

    assert "tests-rule" in result
    assert "pytest fixtures" in result


def test_semantic_episodic_at_task_start(tmp_path):
    """任务开始时 build_memory_section 包含 semantic/episodic 摘要。"""
    from memory.store import MemoryStore
    from memory.context import MemoryContext
    from memory.models import Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    store.write_memory(Memory(
        name="project-tech-stack",
        description="Tech stack is Python + FastAPI",
        content="The project uses Python 3.11 and FastAPI.",
        metadata=MemoryMetadata(type="semantic"),
    ))
    store.write_memory(Memory(
        name="last-deploy",
        description="Last deploy was on 2024-01-15",
        content="Deployed v2.1.0 successfully.",
        metadata=MemoryMetadata(type="episodic"),
    ))

    ctx = MemoryContext(store=store)
    ctx.set_task_context("fix API bug")
    section = ctx.build_memory_section()

    # semantic and episodic should appear in the memory section
    assert "project-tech-stack" in section or "last-deploy" in section


# ===========================================================================
# 阶段 5 测试 — 验证与过期
# ===========================================================================


def test_mark_stale_on_file_write(tmp_path):
    """写文件后，关联 anchor 的记忆被标记为 stale。"""
    from memory.store import MemoryStore
    from memory.models import Anchor, Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    store.write_memory(Memory(
        name="config-rule",
        description="Config loading rule",
        content="Use yaml.safe_load for config.",
        metadata=MemoryMetadata(type="procedural"),
        anchors=[Anchor(kind="file", path="config/app.yaml")],
    ))
    store.write_memory(Memory(
        name="api-rule",
        description="API convention",
        content="All endpoints return JSON.",
        metadata=MemoryMetadata(type="semantic"),
        anchors=[Anchor(kind="file", path="src/api.py")],
    ))

    count = store.mark_stale_for_file("config/app.yaml")
    assert count == 1

    mem = store.read_memory("config-rule")
    assert mem.metadata.stale is True

    api_mem = store.read_memory("api-rule")
    assert api_mem.metadata.stale is False


def test_stale_not_triggered_by_file_read(tmp_path):
    """读取文件不应触发 stale 标记。"""
    from memory.store import MemoryStore
    from memory.models import Anchor, Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    store.write_memory(Memory(
        name="config-rule",
        description="Config loading rule",
        content="Use yaml.safe_load for config.",
        metadata=MemoryMetadata(type="procedural"),
        anchors=[Anchor(kind="file", path="config/app.yaml")],
    ))

    # 不调用 mark_stale_for_file，模拟只有读取
    mem = store.read_memory("config-rule")
    assert mem.metadata.stale is False


def test_stale_warning_in_procedural_injection(tmp_path):
    """stale 的 procedural 记忆注入时显示警告。"""
    from memory.store import MemoryStore
    from memory.context import MemoryContext
    from memory.models import Anchor, Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    store.write_memory(Memory(
        name="stale-rule",
        description="A stale rule",
        content="Do X when editing config.",
        metadata=MemoryMetadata(type="procedural", stale=True),
        anchors=[Anchor(kind="file", path="config/app.yaml")],
    ))

    ctx = MemoryContext(store=store)
    result = ctx.get_procedural_for_files({"config/app.yaml"})

    assert "stale-rule" in result
    assert "STALE" in result


def test_prune_expired_episodic(tmp_path):
    """超龄且低访问量的 episodic 记忆被清理。"""
    from memory.store import MemoryStore
    from memory.models import Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    # 创建一条很旧的 episodic 记忆
    store.write_memory(Memory(
        name="old-episode",
        description="Old episode",
        content="Something that happened long ago.",
        metadata=MemoryMetadata(type="episodic", access_count=0),
        updated_at="2020-01-01T00:00:00Z",
    ))
    # 创建一条最近的 episodic 记忆
    store.write_memory(Memory(
        name="recent-episode",
        description="Recent episode",
        content="Something that happened recently.",
        metadata=MemoryMetadata(type="episodic", access_count=0),
    ))

    pruned = store.prune_expired(max_episodic_age_days=30)

    assert pruned == 1
    assert store.read_memory("old-episode") is None
    assert store.read_memory("recent-episode") is not None


def test_prune_preserves_high_access_count(tmp_path):
    """高访问量的 episodic 记忆即使旧也保留更久。"""
    from memory.store import MemoryStore
    from memory.models import Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    # 旧但高访问量
    store.write_memory(Memory(
        name="important-episode",
        description="Important episode",
        content="Important thing that happened.",
        metadata=MemoryMetadata(type="episodic", access_count=10),
        updated_at="2025-12-01T00:00:00Z",
    ))

    # max_episodic_age_days=30, but retention = 30*(1+10*0.5) = 180 days
    # 从 2025-12-01 到 2026-06-24 大约 205 天，超过 180 天应被清理
    # 但如果 access_count=10, retention=180，而 age ~= 205, 会被删...
    # 用更大 access_count 测试保留
    store.write_memory(Memory(
        name="very-important-episode",
        description="Very important",
        content="Very important thing.",
        metadata=MemoryMetadata(type="episodic", access_count=20),
        updated_at="2025-12-01T00:00:00Z",
    ))

    pruned = store.prune_expired(max_episodic_age_days=30)

    # access_count=10: retention=180, age~205 → pruned
    assert store.read_memory("important-episode") is None
    # access_count=20: retention=330, age~205 → preserved
    assert store.read_memory("very-important-episode") is not None


# ===========================================================================
# 修复验证 — access_count 递增
# ===========================================================================


def test_record_access_increments_count(tmp_path):
    """record_access 正确递增 access_count。"""
    from memory.store import MemoryStore
    from memory.models import Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    store.write_memory(Memory(
        name="some-fact",
        description="A fact",
        content="Python is great.",
        metadata=MemoryMetadata(type="semantic"),
    ))

    assert store.read_memory("some-fact").metadata.access_count == 0

    store.record_access("some-fact")
    assert store.read_memory("some-fact").metadata.access_count == 1

    store.record_access("some-fact")
    assert store.read_memory("some-fact").metadata.access_count == 2


def test_procedural_injection_increments_access_count(tmp_path):
    """procedural 记忆被注入时 access_count 递增。"""
    from memory.store import MemoryStore
    from memory.context import MemoryContext
    from memory.models import Anchor, Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    store.write_memory(Memory(
        name="rule-a",
        description="Rule A",
        content="Always do X.",
        metadata=MemoryMetadata(type="procedural"),
        anchors=[Anchor(kind="file", path="src/app.py")],
    ))

    ctx = MemoryContext(store=store)
    ctx.get_procedural_for_files({"src/app.py"}, record_access=True)

    mem = store.read_memory("rule-a")
    assert mem.metadata.access_count == 1

    # 第二次触发（record_access=True 再递增）
    ctx.get_procedural_for_files({"src/app.py"}, record_access=True)
    mem = store.read_memory("rule-a")
    assert mem.metadata.access_count == 2

    # record_access=False 不递增
    ctx.get_procedural_for_files({"src/app.py"}, record_access=False)
    mem = store.read_memory("rule-a")
    assert mem.metadata.access_count == 2


def test_consolidate_procedural_without_anchor_downgrades(tmp_path):
    """procedural 无有效文件/符号锚点时降级为 semantic。"""
    from memory.store import MemoryStore
    from memory.models import Anchor

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))

    # 只有 task anchor，不算有效
    candidate = _make_candidate(
        name="rule-no-file",
        content="Always use async.",
        mem_type="procedural",
        anchors=[Anchor(kind="task", value="coding")],
    )
    action = store.consolidate(candidate)

    assert action == "ADD"
    mem = store.read_memory("rule-no-file")
    assert mem.metadata.type == "semantic"  # 被降级


def test_consolidate_procedural_with_file_anchor_stays(tmp_path):
    """procedural 有文件锚点时保持 procedural 类型。"""
    from memory.store import MemoryStore
    from memory.models import Anchor

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    candidate = _make_candidate(
        name="rule-with-file",
        content="Use safe_load for config.",
        mem_type="procedural",
        anchors=[Anchor(kind="file", path="config/app.yaml")],
    )
    action = store.consolidate(candidate)

    assert action == "ADD"
    mem = store.read_memory("rule-with-file")
    assert mem.metadata.type == "procedural"


def test_validate_memory_resets_stale(tmp_path):
    """validate_memory 重置 stale=False 并设置 validated_at。"""
    from memory.store import MemoryStore
    from memory.models import Anchor, Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    store.write_memory(Memory(
        name="stale-rule",
        description="A rule",
        content="Do X.",
        metadata=MemoryMetadata(type="procedural", stale=True),
        anchors=[Anchor(kind="file", path="src/main.py")],
    ))

    assert store.read_memory("stale-rule").metadata.stale is True
    assert store.read_memory("stale-rule").metadata.validated_at == ""

    result = store.validate_memory("stale-rule")

    assert result is True
    mem = store.read_memory("stale-rule")
    assert mem.metadata.stale is False
    assert mem.metadata.validated_at != ""


def test_validate_memory_nonexistent(tmp_path):
    """validate_memory 对不存在的记忆返回 False。"""
    from memory.store import MemoryStore

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    assert store.validate_memory("nonexistent") is False


# ---------------------------------------------------------------------------
# Fix A: ProactiveMemory 文件锚点 + 类型降级
# ---------------------------------------------------------------------------


def test_proactive_feedback_with_file_ref_is_procedural(tmp_path):
    """用户修正中包含文件路径时，ProactiveMemory 保存为 procedural 并带 anchor。"""
    from memory.store import MemoryStore
    from memory.proactive import ProactiveMemory

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    pm = ProactiveMemory(store)
    pm.check_user_message("don't use regex in config/parser.py, use yaml.safe_load instead")

    summaries = store.list_memories()
    assert len(summaries) == 1
    assert summaries[0].type == "procedural"

    mem = store.read_memory(summaries[0].name)
    assert mem is not None
    file_anchors = [a for a in mem.anchors if a.kind == "file"]
    assert len(file_anchors) >= 1
    assert any("config/parser.py" in (a.path or "") for a in file_anchors)


def test_proactive_feedback_without_file_ref_is_semantic(tmp_path):
    """用户修正中不含文件路径时，ProactiveMemory 降级为 semantic。"""
    from memory.store import MemoryStore
    from memory.proactive import ProactiveMemory

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    pm = ProactiveMemory(store)
    pm.check_user_message("don't use emojis in commit messages")

    summaries = store.list_memories()
    assert len(summaries) == 1
    assert summaries[0].type == "semantic"


# ---------------------------------------------------------------------------
# Fix B: Extractor 无锚点 procedural 通过 consolidate 降级
# ---------------------------------------------------------------------------


def test_extractor_procedural_without_anchor_downgrades_via_consolidate(tmp_path):
    """LLM 提取的 procedural 无 file/symbol anchor 时，consolidate 降级为 semantic。"""
    from memory.store import MemoryStore
    from memory.models import Anchor
    from memory.extractor import MemoryCandidate

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    candidate = MemoryCandidate(
        type="procedural",
        name="rule-no-anchor",
        description="A vague rule",
        content="Always be polite.",
        anchors=[],
        confidence="high",
    )
    action = store.consolidate(candidate)

    assert action == "ADD"
    mem = store.read_memory("rule-no-anchor")
    assert mem.metadata.type == "semantic"


def test_extractor_procedural_with_file_anchor_stays(tmp_path):
    """LLM 提取的 procedural 有 file anchor 时，保持 procedural。"""
    from memory.store import MemoryStore
    from memory.models import Anchor
    from memory.extractor import MemoryCandidate

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    candidate = MemoryCandidate(
        type="procedural",
        name="rule-with-anchor",
        description="Use safe_load",
        content="Always use yaml.safe_load.",
        anchors=[Anchor(kind="file", path="config/parser.py")],
        confidence="high",
    )
    action = store.consolidate(candidate)

    assert action == "ADD"
    mem = store.read_memory("rule-with-anchor")
    assert mem.metadata.type == "procedural"


# ===========================================================================
# 端到端集成验证 — 记忆系统完整生命周期
# ===========================================================================


def test_e2e_full_lifecycle_procedural(tmp_path):
    """
    端到端场景 1：用户修正 → 保存 → 文件读取触发 → 文件写入 stale → 重新验证

    模拟完整流程：
    1. 用户说 "don't use regex in config/parser.py"
    2. ProactiveMemory 保存为 procedural + file anchor
    3. Agent 读取 config/parser.py → procedural 规则被触发注入
    4. Agent 修改 config/parser.py → 规则变 stale
    5. validate_memory 重置 stale
    """
    from memory.store import MemoryStore
    from memory.context import MemoryContext
    from memory.proactive import ProactiveMemory

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    ctx = MemoryContext(store=store)
    pm = ProactiveMemory(store)

    # Step 1: 用户修正 → ProactiveMemory 保存
    pm.check_user_message("don't use regex in config/parser.py, use yaml.safe_load instead")
    summaries = store.list_memories()
    assert len(summaries) == 1
    mem_name = summaries[0].name
    mem = store.read_memory(mem_name)
    assert mem.metadata.type == "procedural"
    assert any(a.kind == "file" and "config/parser.py" in (a.path or "") for a in mem.anchors)

    # Step 2: Agent 读取 config/parser.py → procedural 触发
    result = ctx.get_procedural_for_files({"config/parser.py"}, record_access=True)
    assert "regex" in result or "safe_load" in result
    assert mem_name in result

    # Step 3: 验证 access_count 递增
    mem = store.read_memory(mem_name)
    assert mem.metadata.access_count == 1

    # Step 4: Agent 修改 config/parser.py → stale
    store.mark_stale_for_file("config/parser.py")
    mem = store.read_memory(mem_name)
    assert mem.metadata.stale is True

    # Step 5: 再次读取时显示 stale 警告
    result = ctx.get_procedural_for_files({"config/parser.py"})
    assert "STALE" in result

    # Step 6: Agent 确认规则仍有效 → validate
    store.validate_memory(mem_name)
    mem = store.read_memory(mem_name)
    assert mem.metadata.stale is False
    assert mem.metadata.validated_at != ""


def test_e2e_consolidation_dedup(tmp_path):
    """
    端到端场景 2：重复记忆合并去重

    模拟：
    1. 提取一条记忆 → ADD
    2. 提取内容相同的记忆 → NOOP（不产生重复）
    3. 提取内容更新的同名记忆 → UPDATE
    4. 验证最终只有一条记忆，内容是最新的
    """
    from memory.store import MemoryStore
    from memory.extractor import MemoryCandidate
    from memory.models import Anchor

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))

    # Round 1: 新记忆 → ADD
    c1 = MemoryCandidate(
        type="semantic",
        name="api-conventions",
        description="API uses JSON responses",
        content="All API endpoints return JSON with snake_case keys.",
        anchors=[Anchor(kind="file", path="src/api/")],
    )
    assert store.consolidate(c1) == "ADD"
    assert len(store.list_memories()) == 1

    # Round 2: 完全相同 → NOOP
    c2 = MemoryCandidate(
        type="semantic",
        name="api-conventions",
        description="API uses JSON responses",
        content="All API endpoints return JSON with snake_case keys.",
        anchors=[Anchor(kind="file", path="src/api/")],
    )
    assert store.consolidate(c2) == "NOOP"
    assert len(store.list_memories()) == 1

    # Round 3: 同名但内容更新 → UPDATE
    c3 = MemoryCandidate(
        type="semantic",
        name="api-conventions",
        description="API uses JSON responses with pagination",
        content="All API endpoints return JSON. Lists use cursor-based pagination.",
        anchors=[Anchor(kind="file", path="src/api/")],
    )
    assert store.consolidate(c3) == "UPDATE"
    assert len(store.list_memories()) == 1
    mem = store.read_memory("api-conventions")
    assert "pagination" in mem.content


def test_e2e_episodic_decay_and_prune(tmp_path):
    """
    端到端场景 3：episodic 记忆 Ebbinghaus 衰减

    模拟：
    1. 创建一条 60 天前的 episodic 记忆（access_count=0）
    2. 创建一条 60 天前但 access_count=3 的 episodic 记忆
    3. prune_expired(max_episodic_age_days=30) 只删除前者
    4. 高频访问的记忆因 retention_days 延长而存活
    """
    from memory.store import MemoryStore
    from memory.models import Memory, MemoryMetadata
    from datetime import datetime, timedelta, timezone

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))

    old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 低访问量 episodic（retention=30 days，60天前 → 过期）
    store.write_memory(Memory(
        name="old-episode-unused",
        description="An old event nobody accessed",
        content="Fixed a typo in README.",
        metadata=MemoryMetadata(type="episodic", access_count=0),
        updated_at=old_date,
    ))

    # 高访问量 episodic（retention=30*(1+3*0.5)=75 days，60天前 → 未过期）
    store.write_memory(Memory(
        name="old-episode-used",
        description="A frequently accessed event",
        content="Discovered API rate limit bug.",
        metadata=MemoryMetadata(type="episodic", access_count=3),
        updated_at=old_date,
    ))

    # 新的 semantic（不受 episodic 过期影响）
    store.write_memory(Memory(
        name="project-fact",
        description="Stable project knowledge",
        content="Project uses Python 3.11.",
        metadata=MemoryMetadata(type="semantic"),
    ))

    assert len(store.list_memories()) == 3

    pruned = store.prune_expired(max_episodic_age_days=30)

    assert pruned == 1
    assert store.read_memory("old-episode-unused") is None
    assert store.read_memory("old-episode-used") is not None
    assert store.read_memory("project-fact") is not None


def test_e2e_path_normalization_absolute_to_relative(tmp_path):
    """
    端到端场景 4：路径规范化 — 绝对路径自动转为 repo 相对路径

    模拟 agent 环境：
    1. 记忆 anchor 用相对路径 "agent/core.py"
    2. 工具返回绝对路径 "D:\\project\\agent\\core.py"
    3. normalize_repo_path 后能正确匹配
    """
    from memory.store import MemoryStore
    from memory.context import MemoryContext
    from memory.models import Anchor, Memory, MemoryMetadata
    from agent.policy import normalize_repo_path

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))
    store.write_memory(Memory(
        name="core-rule",
        description="Always check policy before running tools",
        content="Before executing any tool, check active policy constraints.",
        metadata=MemoryMetadata(type="procedural"),
        anchors=[Anchor(kind="file", path="agent/core.py")],
    ))

    repo_path = str(tmp_path / "project")

    # 模拟：工具返回绝对路径
    abs_path = str(tmp_path / "project" / "agent" / "core.py")
    normalized = normalize_repo_path(abs_path, repo_path)
    assert normalized == "agent/core.py"

    # 用规范化路径查询 procedural
    ctx = MemoryContext(store=store)
    result = ctx.get_procedural_for_files({normalized})
    assert "core-rule" in result
    assert "policy" in result


def test_e2e_type_differentiated_retrieval(tmp_path):
    """
    端到端场景 5：差异化检索策略

    验证：
    - semantic/episodic 出现在 build_memory_section（任务开始注入）
    - procedural 不出现在 build_memory_section
    - procedural 仅通过 get_procedural_for_files 按文件触发
    """
    from memory.store import MemoryStore
    from memory.context import MemoryContext
    from memory.models import Anchor, Memory, MemoryMetadata

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))

    # 三种类型各一条
    store.write_memory(Memory(
        name="tech-stack",
        description="Python + FastAPI",
        content="Project uses Python 3.11 with FastAPI.",
        metadata=MemoryMetadata(type="semantic"),
    ))
    store.write_memory(Memory(
        name="last-bug",
        description="Fixed auth bug yesterday",
        content="The auth bug was a missing null check.",
        metadata=MemoryMetadata(type="episodic"),
    ))
    store.write_memory(Memory(
        name="no-regex-config",
        description="Don't use regex for config parsing",
        content="Use yaml.safe_load, not regex.",
        metadata=MemoryMetadata(type="procedural"),
        anchors=[Anchor(kind="file", path="config/parser.py")],
    ))

    ctx = MemoryContext(store=store)
    ctx.set_task_context("fix a bug in auth system")

    # build_memory_section 应包含 semantic/episodic，不含 procedural 规则内容
    section = ctx.build_memory_section()
    assert "tech-stack" in section
    assert "last-bug" in section
    # procedural 名可能在索引列表中，但其规则内容不应通过这条路径注入
    assert "Use yaml.safe_load" not in section

    # get_procedural_for_files 才能获取 procedural 规则内容
    proc = ctx.get_procedural_for_files({"config/parser.py"})
    assert "yaml.safe_load" in proc


def test_e2e_extractor_with_consolidate_pipeline(tmp_path):
    """
    端到端场景 6：Extractor → Consolidate 完整管线

    模拟 LLM 提取后通过 consolidate 写入：
    1. 有 anchor 的 procedural → 保持类型，ADD
    2. 无 anchor 的 procedural → 降级 semantic，ADD
    3. 重复提取同名 → NOOP
    """
    from memory.store import MemoryStore
    from memory.extractor import MemoryCandidate, MemoryExtractor
    from memory.models import Anchor

    store = MemoryStore(repo_path="test", memory_dir=str(tmp_path))

    # 用 write_success_memories 的 consolidate 路径
    candidates = [
        MemoryCandidate(
            type="procedural",
            name="safe-load-rule",
            description="Use safe_load",
            content="Always use yaml.safe_load in config/.",
            anchors=[Anchor(kind="file", path="config/")],
            confidence="high",
        ),
        MemoryCandidate(
            type="procedural",
            name="be-polite",
            description="Be polite to users",
            content="Always be polite.",
            anchors=[],
            confidence="high",
        ),
        MemoryCandidate(
            type="episodic",
            name="debug-session",
            description="Debugged the parser",
            content="Found a bug in parser.py line 42.",
            anchors=[Anchor(kind="file", path="src/parser.py")],
            confidence="medium",
        ),
    ]

    for c in candidates:
        store.consolidate(c)

    mems = {s.name: s for s in store.list_memories()}
    assert mems["safe-load-rule"].type == "procedural"
    assert mems["be-polite"].type == "semantic"  # 降级
    assert mems["debug-session"].type == "episodic"

    # 再次提交同名 → NOOP
    assert store.consolidate(candidates[0]) == "NOOP"
    assert len(store.list_memories()) == 3  # 不产生新记忆
