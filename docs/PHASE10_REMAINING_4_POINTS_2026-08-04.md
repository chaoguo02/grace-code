# Phase 10 剩余点 — 实现方案（代码验证 + agent_service 审计版）

> 日期：2026-08-04
> 依赖：Batch A+B 已完成，点 1+2 已完成
> **v4 更新**：新增替代品审核——7 个替代品逐 CC 对照，1 个需修正（GRACE.md），其余正确
> **最终执行顺序**：6 步

---

## 1. Model Switch ✅

> **已完成** 2026-08-04

### 核实结果

`NativeBackend.invoke()`（[native_backend.py:219](runtime_core/native_backend.py#L219)）签名：
```python
def invoke(self, conversation, *, tool_choice=None, cancellation=None):
```
line 263 读 `self._model`——**没有** model 参数。原方案 `set_model()` 突变 backend 状态——错误。

`stream_iter()`（[native_backend.py:295](runtime_core/native_backend.py#L295)）相同——读 `self._model`，没有 model 参数。

### CC 行为

CC 用 `messages.create(model='sonnet')`——per-request 参数，不突变任何状态。CC 的 `NativeBackend` 在 `__init__` 绑定 model，但每次 `invoke()` 都读取该值——**不是 setter 模式**。

### 正确方案

给两个方法加 `model: str = ""` 参数。非空 → 用参数值；空 → 用 `self._model`（默认行为，与现有完全兼容）：

```python
# native_backend.py — invoke()
kwargs: dict[str, Any] = {
    "model": (model if model else self._model),  # ← 仅此一改
    "max_tokens": self._max_tokens,
    "messages": api_messages,
}

# stream_iter() 同理
kwargs["model"] = model if model else self._model
```

使用方（`switch_model()` 或 `AgentRuntime.run()`）调用时：
```python
backend.invoke(conv, model="haiku")
```

这是**零 mutation**、**向后兼容**（默认行为不变）的方案。与 CC 的 per-request model 参数对应。

| 文件 | 动作 | 行数 |
|---|---|---|
| `runtime_core/native_backend.py` | 修改：`invoke()` + `stream_iter()` 加 `model=""` 参数 | 4 行 |
| `entry/chat.py` `switch_model()` | 改为修改 `self._backend` + 构造 adapter 的 model 设置 | 2 行 |
| `tests/runtime_core/test_native_child.py` | BT-30：验证 child model override | +15 |

---

## 2. Stream 接口 ✅

> **已完成** 2026-08-04

### 核实结果

`NativeBackend.stream_iter()` **已完整实现且验证**：

- yield `StreamEvent(kind=TEXT_DELTA, text=chunk)` → 逐 token 文本
- yield `StreamEvent(kind=TOOL_USE, tool_call=MACToolCall(...))` → 工具调用
- yield `StreamEvent(kind=FINISH, text=..., finish_message=...)` → 完成
- yield `StreamEvent(kind=ERROR, text="...")` → 错误

`StreamEventKind` 枚举（[llm/base.py:114](llm/base.py#L114)）: `TEXT_DELTA`, `TOOL_USE`, `FINISH`, `ERROR`——4 个值全部存在。

**唯一的 gap**：`NativeStepLoop.execute()` 只调 `backend.invoke()`（同步），不调 `stream_iter()`。

### 正确方案

`NativeStepLoop.execute()` 加 `text_callback: Callable[[str], None] | None` 参数。存在时走 `stream_iter()`：

```python
def execute(self, context, *, text_callback=None):
    ...
    if text_callback and hasattr(self._backend, 'stream_iter'):
        model_action, usage = self._stream_model_call(
            pruned_conv, text_callback, context.cancellation,
        )
    else:
        model_action = self._backend.invoke(pruned_conv, ...)
```

`_stream_model_call()` 循环 `stream_iter()` → `TEXT_DELTA` 推给 callback → 收集 `TOOL_USE` → `FINISH` 组装 `ModelAction`。

流式输出的**非 stream 部分**（tool execution、conversation state、outcome）完全不变。

| 文件 | 动作 | 行数 |
|---|---|---|
| `runtime_core/native_step_loop.py` | 修改：`execute(text_callback)` + `_stream_model_call()` | +50 |
| `runtime_core/runtime.py` | 修改：`run(context, text_callback=None)` 透传 | +3 |
| `entry/chat.py` `_run_native_turn()` | 修改：传 `text_callback=self._make_stream_callback()` | +2 |
| `tests/runtime_core/test_native_child.py` | BT-31：stream callback 收到 token | +20 |

---

## 3. CLI Wiring（entry/cli.py）

### 核实结果

`ApplicationComponents`（[application_components.py](composition/application_components.py#L36)）有 `runtime: AgentRuntime` + `runtime_ports: RuntimePorts`——**但没有 `ConversationStore`**。

CLI 需要 `ConversationStore` 做跨轮 history 注入。选项：

**A）从 `SqliteStorageBackend` 独立构造**（Phase 0a Web 路径的做法）
**B）在 `ApplicationComponents` 加 `conversation_store` 字段**

选 A——不改 `ApplicationComponents` 结构。CLI 已经构造了 `SqliteStorageBackend`（`entry/cli.py` 中有 `db_path`），直接用它。

### 正确方案

```python
# entry/cli.py — _chat_loop()
from app.storage.sqlite import SqliteStorageBackend

db_path = os.path.join(str(repo_path), ".grace", "grace.db")
storage = SqliteStorageBackend(db_path)

# 构造 Native agent runtime
from composition.runtime_composition import assemble
_components = assemble(db_path, llm_backend=backend, tool_registry=registry)

session = ChatSession(
    ...
    # Phase 0b: native path
    agent_runtime=_components.runtime,
    conversation_store=storage,
)
```

`ChatSession` 中用的是 `_conversation_store.list_messages()`——`SqliteStorageBackend` 已有 `list_messages(session_id, limit)` 方法。

| 文件 | 动作 | 行数 |
|---|---|---|
| `entry/cli.py` | 修改：`ChatSession()` 加 `agent_runtime=` + `conversation_store=` | +6 |
| `tests/runtime_core/test_native_child.py` | BT-32：CLI native turn 端到端 | +20 |

---

## 4. SessionRuntime 删除

### 核实结果

`SessionRuntime` 当前引用（排除 `_native_mode` guard 和 build/）：

```
entry/chat.py:146     → SessionRuntime(...)   [仅 _native_mode=False]
entry/modes/v2_runner.py:521 → SessionRuntime(...)   [v2 mode runner]
server/services/agent_service.py:509 → SessionRuntime(...)  [web service]
```

另外 `RuntimeDependencies` (agent/session/run_context.py) 仅被 SessionRuntime 使用。

### 正确方案（4a + 4b，4c 留后续）

**本次**：标记废弃 `SessionRuntime` + 移除 `agent_service.py` 中不需要的构造（Phase 0a 后不调 `run_session()` 但仍在 init 中创建 `SessionRuntime`，只是 callback/review_service 依赖它）。

**`v2_runner.py` 不删**——它是独立的 plan/build CLI 执行路径，不在本次范围。

| 文件 | 动作 |
|---|---|
| `agent/session/runtime.py` | 模块 docstring 加 `DEPRECATED` |
| `server/services/agent_service.py` | 保留——SessionRuntime 仍在 callback/hook 中使用 |
| `entry/modes/v2_runner.py` | 不动——不在本次范围 |

---

---

## 5. agent_service.py 死代码清理

### 背景

Phase 0a 将 Web 执行路径切到了 `ChatPipeline._execute_native()`，但 `AgentService.__init__` 仍然构造 `SessionRuntime`（line 509）并持有大量**已被替代**或**纯死代码**的方法。审计发现了以下清单。

### 高置信删除（本轮立即执行）

**5a. `chat()` (line 885-979)**

判断依据：
- 全仓搜索无真实调用——只有 docstring 示例
- 生产路径已走 `run_chat_async()`（line 992）+ `ChatPipeline._execute_native()`
- 内部实现的 `uuid`、`AggregateVersion` 在当前模块未导入——**如果被调用会运行时崩溃**
- 逻辑重复于 `ChatPipeline.execute()`

处置：删除 `chat()` 方法。

**5b. `_memory_maintenance_loop()` (line 1312) + `_do_memory_maintenance()` (line 1323)**

判断依据：
- `__init__` 当前启动的是 `jobs/memory_maintenance.py:16 MemoryMaintenanceJob`
- `_memory_maintenance_stop` / `_memory_maintenance_thread` 只初始化（`None`），从未使用
- 老 loop 依赖 `_memory_maintenance_stop.wait(...)` → `_memory_maintenance_stop` 当前是 `None`，**被误调用会 AttributeError**

处置：删除两个方法。

**5c. `_inject_session_context()` (line 1357)**

判断依据：
- 全仓无调用点
- 新路径在 `chat_pipeline.py:306 inject_session_context()`
- 新路径调用 `session_service.claim_session_context()` → 不直接写 session_messages
- 旧 helper 直接写 session_messages——与 pipeline 设计冲突

处置：删除方法。

### 中置信清理（本轮清理，防止未来误用）

**5d. `_build_recovery_context()` (line 1163)**

判断依据：
- 仍被 `compact_session_async()` 调用（line 1254）——**不是完全死代码**
- 但已有重复实现：`jobs/session_context.py:14 build_recovery_context()`

处置：将 `compact_session_async()` 改为调用 `jobs.session_context.build_recovery_context`，删除 AgentService 中的 static copy。

### 保留但标注迁移（本轮不动，仅标注）

**5e. `_load_permission_rules()` (line 678) + `_maybe_reload_rules()` (line 715)**

判断依据：
- 被 `run_chat_async()` 调用（line 1069 传入 `ChatPipelinePorts.loaded_rules`）
- `server/services/safety_service.py` / `architecture_service.py` 读取 `_loaded_rules`
- **不是死代码**，但属于 legacy permission ownership

处置：保留不动。标注为"应迁移到 native permission gate / registry composition"。

### 文件范围（点 5）

| 文件 | 动作 | 删行 |
|---|---|---|
| `server/services/agent_service.py` | 删除 `chat()` 方法 | ~95 |
| `server/services/agent_service.py` | 删除 `_memory_maintenance_loop()` + `_do_memory_maintenance()` | ~50 |
| `server/services/agent_service.py` | 删除 `_inject_session_context()` | ~15 |
| `server/services/agent_service.py` | 删除 `_build_recovery_context()` + 改写 `compact_session_async()` | ~50 |

### 验证

```bash
# 确认无引用
rg -n "\.chat\(session_id" server/ --glob="*.py"  # 零命中
rg -n "_memory_maintenance_loop\|_do_memory_maintenance\|_inject_session_context" server/ --glob="*.py"  # 零命中

# 全量回归
pytest tests/ --ignore=tests/test_smoke_e2e.py -q
```

---

## 6. 替代品审核——逐 CC 对照

### R1: `ChatPipeline._execute_native()` 替代 `chat()`

| 维度 | 评估 |
|---|---|
| CC 对齐 | ✅ 一条执行路径（Web → pipeline → coordinator → AgentRuntime） |
| 正确性 | ⚠️ 有 2 个实现 gap |
| Gap 1 | `max_steps=25` 硬编码。CC 用 agent definition 的 `maxTurns`。Phase 6 在 NativeAgentTool 中已读 `getattr(definition, "max_turns", 25)`——这里没跟上 |
| Gap 2 | `CapabilitySnapshot` 从 `backend.tools` 读，但 NativeBackend 暴露 `tool_schemas` property（Phase 2 新增）——若 `backend.tools` 为 None，返回空 CapabilitySnapshot，生产中未触发错误只因 `invoke()` 不依赖它 |

**处置**：保留替代品。Gap 1+2 在点 3（CLI Wiring）中顺手修。

### R2: `MemoryMaintenanceJob` 替代 `_memory_maintenance_loop/do`

| 维度 | 评估 |
|---|---|
| CC 对齐 | N/A——CC 没有周期 memory 维护（事件驱动） |
| 正确性 | ✅ 独立类，双模式 thread/async，由 `agent_service.py` 显式 `start_async()` |

**处置**：旧 loop 安全删除。替代品已就绪且更优。

### R3: `ChatPipeline.inject_session_context()` 替代 `_inject_session_context()`

| 维度 | 评估 |
|---|---|
| CC 对齐 | ✅ 概念对齐——project rules 注入 conversation。CC 用 CLAUDE.md `<system-reminder>` 每轮注入 |
| 正确性 | ✅ `claim_session_context()` 走内存通道，不持久化 runtime context，比旧的直接写 session_messages 更正确 |

**处置**：旧 helper 安全删除。

### R4: `jobs/session_context.build_recovery_context()` 替代 `_build_recovery_context()`

| 维度 | 评估 |
|---|---|
| CC 对齐 | ❌ CC 无此概念——compaction 后通过 JSONL transcript + CLAUDE.md system-reminder 自然恢复 |
| 正确性 | ⚠️ 是 Grace Code 扩展（web SQLite 存储的 compaction recovery）——但用了 `CLAUDE.md` 而非 Phase 3 决定的 `GRACE.md` |

**处置**：合并两个副本到 `jobs/session_context.py`，**改 `CLAUDE.md` → `GRACE.md`**（点 4c）。删除 `AgentService` 中的 static copy。

### R5: `assemble()` `_perm_rules` + `_RealHooks` 替代 `_load_permission_rules()`

| 维度 | 评估 |
|---|---|
| CC 对齐 | ✅ 完全对齐——settings.json 在启动时加载一次，PreToolUse hook 评估每个工具调用 |
| 正确性 | ✅ PermissionPipeline 注入 _RealHooks.check()——调用链路正确 |
| 问题 | `AgentService._loaded_rules` 仍独立加载一份供 read-side 服务使用——**数据源不一致风险** |

**处置**：保留不动，标注迁移。Read-side 服务应统一读 PermissionPipeline。

### R6: `cancellation_coordinator` 替代 `cancel_session()` alias

| 维度 | 评估 |
|---|---|
| CC 对齐 | ✅ 核心机制——CancellationHandle.cancel() → StepLoop 每轮检查 |
| 正确性 | ✅ DB-level CAS（web 持久化扩展）正确 |

**处置**：保留不动，标注简化。Routes 可直接调 coordinator 去 alias。

### R7: `RuntimeExecution` 替代 `AgentConfig`

| 维度 | 评估 |
|---|---|
| CC 对齐 | ✅ per-request 分散参数的聚合——等价于 CC 的 callModel 参数 |
| 正确性 | ✅ frozen dataclass, session_id/run_id/cancellation/conversation/capabilities/max_steps/budget_tokens——覆盖 CC 的 per-request 参数空间 |

**处置**：✅ 替代品正确。AgentConfig 随 ReActAgent 废弃。

---

## 执行计划（最终版）

```
✅ 点 1   Model Switch (已完成)
✅ 点 2   Stream 接口  (已完成)
⬜ 点 3   CLI Wiring + 修 R1 gap
         - entry/cli.py: ChatSession() 加 agent_runtime= + conversation_store=
         - chat_pipeline.py: _execute_native() max_steps 从 agent_def 读
⬜ 点 4   agent_service 死代码清理
         - 4a: 删除 chat() (~95 行)
         - 4b: 删除 _memory_maintenance_loop/do (~50 行)
         - 4c: 删除 _inject_session_context() (~15 行)
         - 4d: 迁移 _build_recovery_context() → jobs/ 版 + 修 GRACE.md (~50 行)
⬜ 点 5   SessionRuntime 标记废弃 (1 行 docstring)
⬜ 点 6   全量回归 + 对照计划最终反思
```

### 替代品审核结论

7 个替代品中：5 个 CC-aligned 或概念对齐，1 个 Grace Code 基础设施（MemoryMaintenanceJob），1 个需修正（GRACE.md 改名）。**零个需要重写。** 旧代码安全删除。
