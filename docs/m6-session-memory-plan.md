# M6: SessionMemory 接入 + 子代理升级 Plan (v2)

> 调研反馈：CC 的 session memory 子代理只允许 `FileEditTool`（一个工具、一个文件）。
> 不是"更丰富的工具"，而是**更严格的沙箱** + **fork 继承上下文**。
> v2 方案简化：不给 Read/Grep/Glob，保持 file_write 单工具沙箱。

---

## 修正后的方案

### 核心变更（相比 v1）

| 项目 | v1（错误） | v2（修正） |
|------|-----------|-----------|
| 子代理工具 | Read/Grep/Glob/FileWrite | **仅 FileWrite** |
| spawn 方式 | named subagent | named subagent（保持） |
| notes 注入 context | `injection_service.py` 改 | `injection_service.py` 改（保持） |
| 配置开关 | AgentConfig 新增 | AgentConfig 新增（保持） |

---

## 改动详解

### Step 1: AgentConfig 新增 session_notes 开关

**文件**: `agent/core.py` — AgentConfig

```python
@dataclass
class AgentConfig:
    ...
    session_notes: bool = False
    """Enable session memory notes. CC-aligned: sessionMemory.ts"""
```

### Step 2: ReActAgent 接收 session_notes_path

**文件**: `agent/core.py:847`

```python
def __init__(
    self,
    backend, registry, config,
    memory_context=None,
    session_memory_tracker=None,  # 已存在，但永远为 None
    session_notes_path=None,      # 新增：notes 文件路径
    controller_factory=None,
    ...
) -> None:
    self._session_memory_tracker = session_memory_tracker
    self._session_notes_path = session_notes_path
```

`_build_long_term_context()` 中传入 notes 内容：

```python
def _build_long_term_context(self) -> str | None:
    ...
    self._long_term_context = build_injection_context(
        memory_context=self._memory_context,
        skills_prompt=...,
        repo_path=...,
        session_context=self._session_context,
        session_notes_path=self._session_notes_path,  # 新增
    )
    return self._long_term_context
```

### Step 3: injection_service 新增参数

**文件**: `memory/injection_service.py`

```python
def build_injection_context(
    memory_context=None,
    skills_prompt="",
    repo_path=".",
    *,
    session_context=None,
    session_notes_path=None,        # 新增
) -> str | None:
    parts = []
    # ... 现有 sections ...
    
    # ── Session notes ──
    if session_notes_path:
        notes_path = Path(session_notes_path)
        if notes_path.exists():
            notes = notes_path.read_text(encoding="utf-8").strip()
            if notes:
                parts.append(f"## Session Notes\n{notes}")
    
    return "\n\n".join(parts) if parts else None
```

### Step 4: 新增 session-memory agent definition

**文件**: `agent/session/models.py` — `_BUILTIN_AGENTS`

```python
"session-memory": AgentDefinition(
    name="session-memory",
    description="Updates session notes. Internal use only.",
    intent=TaskIntent.ANALYSIS,
    workspace_mode=WorkspaceMode.CURRENT,
    visibility=AgentVisibility.HIDDEN,
    # CC-aligned: only file_write tool, sandboxed to notes path
    tools=frozenset({"FileWrite", "file_write"}),
    disallowed_tools=frozenset({
        "Read", "Write", "Edit", "Bash", "Grep", "Glob",
        "Agent", "WebFetch", "WebSearch",
    }),
    max_turns=3,
    max_tokens=10_000,
    system_prompt=(
        "You are a session-memory subagent. Update the session notes file.\n\n"
        "Rules:\n"
        "- You may ONLY write the provided notes file path.\n"
        "- Preserve the exact template structure (all section headings).\n"
        "- Write detailed content with file paths, function names, commands,\n"
        "  errors, fixes, and user corrections when available.\n"
        "- Keep the file under 12,000 tokens.\n"
        "- Return the COMPLETE updated notes file content as your FINAL answer."
    ),
    permission_mode="default",
    background=True,  # always run in background
),
```

### Step 5: 新增 SessionMemoryForkRunner

**文件**: `memory/session_memory.py` — 新增类

```python
class SessionMemoryForkRunner:
    """Session-memory subagent via spawn_agent().
    
    CC-aligned: sandboxed to ONE tool (file_write) on ONE path (notes file).
    Runs in background — does NOT block the main agent loop.
    """
    
    def __init__(self, runtime, parent_session_id, definition, notes_path):
        self._runtime = runtime
        self._parent_session_id = parent_session_id
        self._definition = definition
        self._notes_path = notes_path
        self._running = False
        self._lock = threading.Lock()
    
    @property
    def running(self) -> bool:
        return self._running
    
    def fork(self, *, prompt, notes_path, current_notes):
        with self._lock:
            if self._running:
                return
            self._running = True
        
        from core.policy import PhasePolicy
        from agent.session import AgentSpawnRequest, ExecutionPlacement
        
        full_prompt = (
            f"Session notes path: {notes_path}\n\n"
            f"Current conversation context:\n{prompt}\n\n"
            f"<current_notes>\n{current_notes}\n</current_notes>\n\n"
            "Update the notes file to reflect the latest work."
        )
        
        request = AgentSpawnRequest.named(
            definition=self._definition,
            description="session-memory update",
            prompt=full_prompt,
            execution_placement=ExecutionPlacement.BACKGROUND,
        )
        
        try:
            self._runtime.spawn_agent(
                parent_session_id=self._parent_session_id,
                request=request,
                budget_tokens=5_000,
                parent_max_steps=3,
                cancellation_token=CancellationToken(),
                parent_policy=PhasePolicy(),
            )
        except Exception as exc:
            logger.debug("SessionMemory fork failed: %s", exc)
        finally:
            with self._lock:
                self._running = False
```

### Step 6: factory 新增参数并传递

**文件**: `agent/session/agent_factory.py`

create() 方法新增 `session_memory_tracker` 和 `session_notes_path` 参数，传给 ReActAgent。

### Step 7: SessionRuntime.run_session() 创建 tracker

**文件**: `agent/session/runtime.py`

```python
session_memory_tracker = None
session_notes_path = None
if self._root_agent_config.session_notes:
    notes_dir = Path(session.repo_path) / ".forge-agent" / "v2" / "sessions" / session.id
    session_notes_path = str(notes_dir / "session_notes.md")
    sm_def = _BUILTIN_AGENTS.get("session-memory")
    if sm_def is not None:
        runner = SessionMemoryForkRunner(
            runtime=self,
            parent_session_id=session.id,
            definition=sm_def,
            notes_path=notes_dir / "session_notes.md",
        )
        session_memory_tracker = SessionMemoryTracker(
            backend=self._backend,
            notes_path=notes_dir / "session_notes.md",
            session_title=f"Session {session.id[:8]}",
            runner=runner,
        )
```

### Step 8: ChatSession 接入

**文件**: `entry/chat.py`

ChatSession 的 `_build_agent_cfg()` 中开启 `session_notes=True`，并生成 `session_notes_path` 传给 runtime。

---

## 测试计划

### 回归
```bash
pytest tests/test_session_memory.py tests/test_cc_alignment_features.py -q
```

### 集成
1. 启动 Chat，验证 notes 目录被创建
2. 执行多轮对话，验证 notes 文件被写入
3. 重启 Chat，验证 notes 被注入 context
