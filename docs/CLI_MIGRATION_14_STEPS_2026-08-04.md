# CLI 迁移：14 步从 SessionRuntime → AgentRuntime

> 文档版本：1.0.0  
> 创建日期：2026-08-04  
> 当前基线：Phase 0a+1~9 已完成，Web 已单路径，CLI 仍用 SessionRuntime  
> 目标：CLI `ChatSession` 不再依赖 `SessionRuntime`，全项目单一路径

---

## 0. 当前状态

`entry/chat.py` 的 `ChatSession` 依赖 `SessionRuntime` 的 9 个直接调用点：

```
__init__     → SessionRuntime(...)          # 构造
line 150     → create_root_session()        # 创建 root session
line 285     → _store.list_messages()       # 读取跨轮 history
line 315     → run_session()                # 核心执行
line 370     → _backend = ...               # model 切换时注入
line 398     → record_skill_activation()    # skill 激活记录
line 433     → spawn_agent()                # fork agent spawn
line 451     → compact()                    # 主动压缩
```

每个调用点都列在这份文档中，标注了替换方案。

---

## Step 1：`run_session()` → `AgentRuntime.run()`

**当前代码**（[chat.py:315](entry/chat.py#L315)）：
```python
result = self._runtime.run_session(
    self._root_session_id,
    agent_name=self._agent_name,
    task_description=user_input,
    intent=intent,
)
```

**替换后**：
```python
from runtime_core.execution import ConversationSnapshot, RuntimeExecution
from core.eventing.identifiers import SessionId, RunId

# 从 ConversationStore 重建跨轮 messages
conv_msgs = self._conversation_store.rebuild_messages(self._root_session_id)
conv_msgs.append({"role": "user", "content": user_input})
conv = ConversationSnapshot(messages=tuple(conv_msgs))

ctx = RuntimeExecution(
    session_id=SessionId(self._root_session_id),
    run_id=RunId(str(uuid.uuid4())),
    max_steps=getattr(definition, "max_turns", 25),
    budget_tokens=200_000,
    conversation=conv,
)

outcome = self._agent_runtime.run(ctx)
```

**依赖**：Step 11（ConversationStore 需先就绪）。

**风险**：`run_session()` 内部做了 skill modifier / effort / thinking 的 pop 处理——这些在 Step 4/5/6 中已显式处理，不会遗漏。

---

## Step 2：`release_session()` → 删除

**当前代码**：`ChatSession` 不显式调 `release_session()`。它在 Web 路径的 `agent_service.py` 中使用。

**替换**：不迁移。CLI 没有并发 session 场景——REPL 是单线程的，用户一次只发一个请求。

**风险**：零。

---

## Step 3：`try_acquire_session()` → 删除

**当前代码**：`ChatSession` 不显式调 `try_acquire_session()`。它在 Web 路径 `run_chat_async()` 中使用，防止同一 session 的并发 HTTP 请求。

**替换**：不迁移。CLI REPL 天然串行。

**风险**：零。

---

## Step 4：`pop_pending_effort()` → `RuntimeExecution.effort` 字段

**当前代码**（`chat_pipeline.py:428-429`，Web 路径中还在 pop）：
```python
effort_override = self._runtime.pop_pending_effort(request.session_id) or ""
```

**CC 行为**：effort 是 per-request 参数——`callModel({effort: 'high'})`。不是 pop 模式。

**替换**：给 `RuntimeExecution` 加字段：
```python
@dataclass(frozen=True, slots=True)
class RuntimeExecution:
    ...
    effort: str = ""  # Phase 0b: "low" | "medium" | "high" | ""
```

`NativeBackend.invoke()` 在构造 API 请求时检查 `cancellation._effort` 或通过参数传入。当前阶段不强依赖 effort（Claude API 的推理 effort 是 `xhigh` / `max` 等，不常用）。

**风险**：低。effort 功能在 CLI 中几乎不触发。

---

## Step 5：`pop_pending_skill_modifier()` → 删除

**当前代码**（`chat_pipeline.py:431-439`）：
```python
skill_modifier = pop_skill_modifier(request.session_id) if callable(...) else None
```

**CC 行为**：Skills 是 tool-invoked。模型调用 `Skill` 工具 → `SKILL.md` 作为 `tool_result` 注入。没有 pop 模式。

**替换**：删除 `pop_pending_skill_modifier()` 调用。Grace Code 已有的 `Skill` 工具（在 `ToolRegistry` 中注册）会通过 `NativeStepLoop` 的 `tool_execute` 自动处理——模型调用时加载 `SKILL.md`。

**风险**：零。Skills 已走 tool-invoke 通道。

---

## Step 6：`pop_pending_thinking()` → 删除

**当前代码**（`chat_pipeline.py:441`）：
```python
self._runtime.pop_pending_thinking(request.session_id)
```

**CC 行为**：thinking 是 per-request 参数。不是 pop 模式。

**替换**：删除。与 Step 4 相同的处理——必要时给 `RuntimeExecution` 加字段，当前阶段不需要。

**风险**：零。

---

## Step 7：`set_web_confirm_callback()` → 删除（CLI 不需要）

**当前代码**：CLI 路径不调 `set_web_confirm_callback`。它在 Web 路径中用于异步确认弹窗。

**CC 行为**：CC 的权限确认通过 `control_request` / `control_response` 协议。CLI 中用户看到确认提示后手动输入。

**替换**：CLI 当前用 `terminal_confirm`（同步 `input()` 函数）处理危险操作的确认。`_RealHooks.check()` 返回 `HookGateResult(allowed=True/False)`，如果 `False`，CLI 可以同步问用户。不需要异步 callback 机制。

**风险**：零。

---

## Step 8：流式输出 → 🔶 标为后续 Phase，迁移时不阻塞

**当前代码**：`ChatSession` 的流式渲染通过 `SessionRuntime` 的内置机制——`_renderer.on_tool_call()`、`_renderer.on_text_delta()` 等回调在 `run_session()` 内部触发。

**CC 行为**：
1. API SSE stream → SDK 解析 `content_block_delta` / `text_delta` 事件
2. `Stream<T>` 类做 producer-consumer 管道
3. React + Ink 渲染器做终端输出

**当前 gap**：`NativeStepLoop` 只调 `backend.invoke()`（同步），没有 `invoke(stream=True)` 接口。`NativeBackendAdapter` 有 `stream()` 方法签名但未接线。

**临时方案（迁移不做阻塞）**：`ChatSession._run_turn()` 调用 `AgentRuntime.run()` 前，将现有的 `InlineRenderer` 绑定到 `NativeStepLoop` 的工具执行回调上：
```python
# 近似——NativeStepLoop 的 _process_tool_calls 有 renderer hook 点
loop = NativeStepLoop(ports, scheduler=scheduler)
# 暂时不处理流式文本——只渲染工具调用
# 文本在 run() 完成后一次性渲染
outcome = runtime.run(ctx)
self._renderer.on_response(outcome.summary)
```

**后续 Phase**：`NativeStepLoop.execute(stream_callback=...)` 参数支持逐 token 产出。不在 Phase 10 范围内。

**风险**：CLI 用户体验降级——从"流式逐 token 输出"变成"完成后再显示"。但功能完整——Agent 执行不受影响。

---

## Step 9：`switch_mode()` → 已就绪

**当前代码**（[chat.py:354-361](entry/chat.py#L354)）：
```python
def switch_mode(self, agent_name: str) -> None:
    from agent.session.models import _BUILTIN_AGENTS
    if agent_name not in _BUILTIN_AGENTS:
        raise ValueError(...)
    self._agent_name = agent_name
    self._renderer.mode = agent_name
```

**CC 行为**：CC 的模式切换（`/plan`）只在 REPL 层改 mode 值。不涉及 runtime。

**替换**：不需要改。`switch_mode()` 只改了 `self._agent_name` + `self._renderer.mode`——完全独立于 `SessionRuntime`。唯一依赖是 `_BUILTIN_AGENTS`（来自 `models.py`），这不属于旧路径。

**风险**：零。

---

## Step 10：`switch_model()` → 🔶 标为 gap

**当前代码**（[chat.py:363-370](entry/chat.py#L363)）：
```python
def switch_model(self, model, provider=None, api_key=None, base_url=None) -> None:
    self._backend, self._model, self._provider = rebuild_backend_for_model(...)
    self._runtime._backend = self._backend
```

**CC 行为**：CC 的 model 切换：REPL 收到 `/model sonnet` → 下一次 `query()` 用新 model 调 API。**不重建 backend——只改 per-request 参数**。

**当前 gap**：`NativeBackend` 的 model 绑定在 `__init__`。不支持 per-invoke model override。`AgentRuntime.run()` 已经传入 `RuntimeExecution`——可以在这里加 model 字段。

**临时方案（迁移不做阻塞）**：`switch_model()` 改为重建 `AgentRuntime`：
```python
def switch_model(self, model, provider=None, api_key=None, base_url=None) -> None:
    self._backend, self._model, self._provider = rebuild_backend_for_model(...)
    # 重建 NativeBackend + RuntimePorts（复用 assemble 逻辑）
    self._agent_runtime = _build_runtime_for_model(self._backend, ...)
```

**后续 Phase**：`NativeBackend` 接受 per-invoke model override 或 `RuntimeExecution.model` 字段生效。

**风险**：中。重建 RuntimePorts 是 expensive 操作，但 model 切换极其罕见（整个 session 可能只发生一两次）。

---

## Step 11：`session_store` → `ConversationStore`

**当前代码**（[chat.py:285](entry/chat.py#L285)）：
```python
msgs = self._runtime._store.list_messages(self._root_session_id)
self._shared_history._messages.clear()
for m in msgs:
    self._shared_history.add(m)
```

**CC 行为**：CC 从 JSONL transcript 文件恢复跨轮 history。消息以 `{"role": "user"|"assistant"|"tool", "content": ...}` dict 格式读出，注入下一次 `query()` 调用。

**替换**：`ConversationStore`（`runtime_core/conversation_store.py`）已存在。Phase 0a 的 Web 路径已用 `self._storage.list_messages(session_id, limit=50)` → `ConversationSnapshot`。

CLI 迁移：
```python
# 替换 _sync_shared_history()
def _sync_shared_history(self) -> None:
    msgs = self._storage.list_messages(self._root_session_id, limit=200)
    self._shared_history._messages.clear()
    for m in msgs:
        self._shared_history.add(LLMMessage(role=m.role, content=m.content))
```

或完全废弃 `_shared_history`（`ConversationHistory` 类），改用在 `_run_turn()` 内部直接构建 `ConversationSnapshot`（Step 1 的代码已包含）。

**风险**：低。`ConversationStore` 已存在且被 Phase 0a 验证。

---

## Step 12：`agent_registry` → `load_agent_definitions()`

**当前代码**（[chat.py:120](entry/chat.py#L120)）：
```python
self._agent_registry = AgentRegistryV2(project_dir=repo_path)
```

**CC 行为**：CC 的 agent definition 预加载在 `activeAgents` Map（`.claude/agents/*.md` 文件集体解析）。查询是 map lookup。

**替换**：
```python
from agent.session.agent_definition import load_agent_definitions

self._agent_definitions = load_agent_definitions(project_dir=repo_path)
```

`switch_mode()` 中检查 agent name 是否合法的逻辑改为：
```python
if agent_name not in self._agent_definitions:
    raise ValueError(...)
```

**风险**：低。`load_agent_definitions()` 是纯函数，Phase 2 已在 `NativeAgentTool.__init__` 和 `assemble()` 中使用。

---

## Step 13：`hook_dispatcher` → `_run_turn()` 入口手动调

**当前代码**：`SessionRuntime.run_session()` 在入口触发 `SESSION_START` hook。

**CC 行为**：CC 的 hooks 在 REPL 入口层执行——`SessionStart` hook 的 shell 命令在 agent 启动前运行，`additionalContext` 注入 system prompt。

**替换**：在 `ChatSession._run_turn()` 的**最开头**加：
```python
def _run_turn(self, user_input: str) -> bool:
    # 触发 SESSION_START hook（仅首次）
    if self._hook_dispatcher and not self._hooks_started:
        from hooks.events import HookContext, HookEvent
        self._hook_dispatcher.dispatch(
            HookEvent.SESSION_START,
            HookContext(session_id=self._root_session_id, user_input=user_input),
        )
        self._hooks_started = True
    ...
```

**风险**：低。hook_dispatcher 本身不依赖 SessionRuntime——只依赖 `HookRegistry`。

---

## Step 14：`compact()` → `ContextBudgetManager`

**当前代码**（[chat.py:450-453](entry/chat.py#L450)）：
```python
def compact(self, focus: str = "") -> str:
    msg = self._runtime.compact(focus=focus)
    self._sync_shared_history()
    return msg
```

**CC 行为**：CC 的 compaction 自动触发——context window 接近上限时 old messages 被 summary 替换。`/compact` 命令是 CLI 快捷方式。

**替换**：自动压缩已在 NativeStepLoop 中工作（Phase 7 的 `_budget.ensure_budget(conv)`）。主动压缩可以在 `ChatSession` 层面实现：
```python
def compact(self, focus: str = "") -> str:
    # 用 ContextBudgetManager 的 truncation 截断 history
    result = self._budget_manager.ensure_budget(self._shared_history)
    self._sync_shared_history()
    return f"Compacted: {result.messages_trimmed} messages trimmed"
```

或完全不实现（标记为后续 Phase）——因为 `ContextBudgetManager.ensure_budget()` 在每个 turn 自动执行截断。

**风险**：低。自动截断已就绪。主动压缩是锦上添花。

---

## 汇总

| # | 方法 | 处置 | 难度 |
|---|---|---|---|
| 1 | `run_session()` | `AgentRuntime.run()` 替代 | 中 |
| 2 | `release_session()` | 删除 | 零 |
| 3 | `try_acquire_session()` | 删除 | 零 |
| 4 | `pop_pending_effort()` | `RuntimeExecution.effort` 字段 | 低 |
| 5 | `pop_pending_skill_modifier()` | 删除（Skills 走 tool-invoke） | 零 |
| 6 | `pop_pending_thinking()` | 删除 | 零 |
| 7 | `set_web_confirm_callback()` | 删除（CLI 用同步 input） | 零 |
| 8 | `set_stream_callback()` | 🔶 后续 Phase——迁移时降级为一次性输出 | 高 |
| 9 | `switch_mode()` | 已就绪 | 零 |
| 10 | `switch_model()` | 🔶 gap——迁移时重建 AgentRuntime | 中 |
| 11 | `session_store` | `ConversationStore` 替代 | 低 |
| 12 | `agent_registry` | `load_agent_definitions()` | 低 |
| 13 | `hook_dispatcher` | `_run_turn()` 入口手动调 | 低 |
| 14 | `compact()` | `ContextBudgetManager` 替代 | 低 |

**完成 14 步后的 ChatSession 不再持有 `self._runtime`（SessionRuntime 引用）**。届时整个项目只有 4 个 import 通路还引用 SessionRuntime——全部可以一并删除（Wave 3+4）。

---

## 实现依赖顺序

```
Step 11 (ConversationStore) ← 先做，Step 1 依赖
Step 12 (agent_definitions) ← 独立
Step 13 (hook_dispatcher)  ← 独立
Step 9  (switch_mode)      ← 已就绪，只验证
Step 4+5+6 (pop_*)         ← 独立，可批量
Step 2+3+7 (删除)           ← 独立，可批量
Step 1  (run_session)      ← 依赖 Step 11+12
Step 8  (stream)           ← 后续 Phase
Step 10 (switch_model)     ← gap
Step 14 (compact)          ← 独立
```

**执行批次**：
```bash
# Batch A: 独立低风险（可并行）
Steps 11, 12, 13, 9, 4, 5, 6, 2, 3, 7, 14

# Batch B: 核心执行替换（依赖 Batch A 的 Step 11+12）
Step 1

# Batch C: 后续 Phase（不在此文档内）
Steps 8, 10
```
