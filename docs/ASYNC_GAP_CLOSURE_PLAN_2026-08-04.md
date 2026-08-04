# Async 差距收敛计划——彻头彻尾 async，去兼容层

> 日期：2026-08-04（更新：2026-08-05）
> 原则：不要兼容层，sync 路径（CLI）也替换为 async，连同深层 CC 对齐。
> 前置：Async 化 6 阶段（A-F）已完成，web 生产已走 aiterate。
> **状态：Step 1-4 ✅ 完成 | Step 5 ⏸ 推迟**

---

## 一、差距清单 + 目标

### 差距 1（P0）：CLI 路径仍走 sync `run()`

**现状**：
```
CLI: run_round (sync) → _run_native_turn (sync) → AgentRuntime.run() (sync execute)
```

**目标**：
```
CLI: run_round (sync 主循环, input 阻塞) → _run_native_turn → asyncio.run(agent_runtime.arun(ctx))
```

CLI 主循环 `while True: input()` 是同步交互——**不需 async 化主循环**。只需 `_run_native_turn` 切到 `arun()`（async），CLI 无 running loop，`asyncio.run` 安全。这样 CLI 也走 aiterate。

**文件**：`entry/chat.py` `_run_native_turn`

### 差距 2（P1）：sync 工具 `BaseTool.aexecute()` to_thread 兜底

**现状**：`BaseTool.aexecute()` 默认 `asyncio.to_thread(self.execute)`。sync 工具（file/git/evidence/memory 等）用此兜底。

**目标**：逐个工具真 async。但 file I/O / git 子进程本质阻塞——CC 也用线程池（`StreamingToolExecutor` 里 sync 工具 to_thread）。**判断**：真 async 只对 Bash（已做）和 MCP（已做）有意义；file/git 用 to_thread 是 CC 也接受的（async 包装 sync I/O）。**保留 to_thread 兜底，但明确它是"async 包装 sync 工具"，不是背道而驰。**

### 差距 3（P1）：`state = next` 不可变 vs `_state` 可变

**现状**：`ConversationState` 可变累积（`add_user_message`/`add_assistant_message` 原地改 `self._messages`）。

**目标**：加不可变快照 + transition 语义（CC `state = next`）。具体：
- `ConversationState.snapshot()` → 不可变 frozen view
- `aiterate` 每轮用快照（对齐 CC 每迭代重建 State）
- 支持 `transition` 记录（为何继续——tool_use/max_steps/cancel）

**文件**：`runtime_core/conversation_state.py` + `native_step_loop.py` aiterate

### 差距 4（P2）：StreamingToolExecutor（流中执行）

**现状**：aiterate await 工具在模型流结束后（`astream_iter` FINISH 后）。

**目标**：流式时边收 tool_use 边执行（CC StreamingToolExecutor）。但这是性能优化，非正确性。**推迟**——功能正确，优化后续。

### 差距 5（P0）：sync `execute()` 主循环退役

**现状**：`execute()` sync 主循环 + `_run_async_from_sync` + `_execute_parallel_batch` + `_start_cancel_watcher`。被 CLI（差距 1 修后）和 child（multi_agent_coordinator）用。

**目标**：
- 差距 1 修后，CLI 不再用 sync `run()`
- child 路径（multi_agent_coordinator）切到 `arun()`（async）
- 之后 sync `execute()` 只有 `RunCoordinator.execute` sync 兼容 + 测试用
- **删除**：`_run_async_from_sync`、`_execute_parallel_batch`（被 sync execute 用，删 execute 时一并删）

---

## 一-B、CC 架构对照 —— 逐 gap 比对

> 方法论：每个 gap 先看 CC 怎么做（源码分析 + 社区深度拆解），再看我们怎么做，
> 以架构师视角评判差距的真实性和修正方向。

### CC 核心架构速览

CC 的核心是一个 **async generator `queryLoop()`**（`src/query.ts`，~1400 行），一个 `while(true)` 无限循环。关键设计决策：

```
queryLoop (async generator)
  └─ while(true):                    ← 状态驱动循环，非递归
       ├─ 1. context compression     ← 4 级渐进压缩 (snip→micro→collapse→auto-compact)
       ├─ 2. for await (callModel)   ← async generator 流式消费
       ├─ 3. decision: tool_use?     ← stop_reason + tool_use block 双重检测
       ├─ 4. runTools / StreamingToolExecutor  ← 读并行/写串行 + 流中预执行
       └─ 5. state = next            ← 不可变状态更新 + transition 记录
```

**五大架构支柱**：

| 支柱 | CC 实现 | 说明 |
|---|---|---|
| **Async Generator Loop** | `async function* queryLoop()` | 单一 async generator，所有接口（CLI/Web/SDK）共用 |
| **Immutable State** | `state = next` (新对象，非原地改) | `State` type 含 `messages`、`transition`、`turnCount` 等 |
| **Transition 追踪** | `transition: Continue \| undefined` | 记录每次 while 循环 continue 的原因（~9 个 continue 点） |
| **StreamingToolExecutor** | 流中边收 tool_use 边执行 | ~40% 加速，读工具并行(≤10)、写工具串行 |
| **sync 工具 to_thread** | `StreamingToolExecutor` 内 sync 工具走线程池 | 文件 I/O、git 子进程本质阻塞，to_thread 是正确抽象 |

---

### Gap 1（P0）：CLI 路径 → `asyncio.run(arun())`

#### CC 怎么做

CC 的 CLI 入口最终调用同一个 `queryLoop()` async generator。Node.js 的异步模型下，
整个进程从 `main()` 开始就是 async 的，**没有 sync wrapper / async 内部的分裂**。
CLI、WebSocket、IDE plugin 三条路径共用一个 `queryLoop`。

```
CC CLI:   main() → queryLoop()        (全程 async，Node.js 原生)
CC Web:   WS handler → queryLoop()    (同一个 queryLoop)
CC SDK:   ToolRunner.run() → queryLoop()  (同一个 queryLoop)
```

#### 我们怎么做

```
我们的 CLI:   chat.py::_run_native_turn → AgentRuntime.run() → NativeStepLoop.execute()  (sync)
我们的 Web:   chat_pipeline.py → asyncio.run(coord.aexecute()) → AgentRuntime.arun() → aiterate()  (async ✅)
```

Web 路径（[chat_pipeline.py:538](server/services/chat_pipeline.py#L538)）已经走 `asyncio.run(coord.aexecute())` → `AgentRuntime.arun()` → `aiterate()`。
CLI 路径（[entry/chat.py:421](entry/chat.py#L421)）还在走 `AgentRuntime.run()` → `NativeStepLoop.execute()`。

#### 架构评判

**差距真实存在。** CLI 和 Web 走的是两条不同的主循环——`execute()` sync vs `aiterate()` async。
这不是"CLI 不需要 async"的问题，而是**两个 while 循环在维护两份几乎相同的逻辑**（模型调用、工具执行、
状态管理、取消检查全重复了一遍）。

**修正方向**：CLI 主循环 (`while True: input()`) 保持同步没问题。`_run_native_turn` 内部切到
`asyncio.run(self._agent_runtime.arun(ctx))` 即可。改完后 CLI 也走 `aiterate()`，与 CC 对齐：
**一条主循环，所有入口共用**。

> **CC 对齐度**：低 → 高（一行改动）

---

### Gap 2（P1）：sync 工具 `BaseTool.aexecute()` → `to_thread`

#### CC 怎么做

CC 的 `StreamingToolExecutor` 处理所有工具执行。对于文件读写、git 命令等本质阻塞的工具，
CC 也是用线程池（`worker_threads` / `uv_work`）执行——Node.js 的 `fs.readFileSync` 同理，
在 async 上下文中必须 `to_thread` 否则阻塞事件循环。

CC 的关键区分不是 "async vs sync 工具"，而是 **read_only/concurrency_safe vs 需串行**：
- 读工具（Read, Glob, Grep）→ 并行（cap 10）
- 写工具（Write, Edit, Bash）→ 串行

CC 的 `partitionToolCalls` 就是这个逻辑。

#### 我们怎么做

```python
# core/base.py:493
async def aexecute(self, params: dict[str, Any]) -> ToolResult:
    import asyncio
    return await asyncio.to_thread(self.execute, params)  # 兜底
```

- Bash 工具：已 override 为真 async（`aexec`）
- MCP 工具：已 override 为真 async（`bridge.call_tool`）
- File/Git/Evidence 等：走 `to_thread` 兜底

我们的 `_atool_calls()`（[native_step_loop.py:700](runtime_core/native_step_loop.py#L700)）已有 CC `partitionToolCalls` 的分区逻辑：
读/并发安全的工具并行（`asyncio.gather`，cap 10），其余串行。

#### 架构评判

**这不是差距——我们的实现已经和 CC 对齐。** CC 也用线程池处理 sync I/O。
Python 的 `asyncio.to_thread` 等价于 Node.js 的 worker thread pool。
文档中的关键决策（"sync 工具 to_thread 保留——CC 也用"）是对的。

唯一可做的是逐个工具 override 为真 async（如 `aiofiles` 替换 `open()`），
但这是性能优化，不改变架构正确性。

> **CC 对齐度**：高（已对齐）

---

### Gap 3（P1）：state 不可变 + transition

#### CC 怎么做

CC 的 `State` 类型是**不可变快照**——每次 while 循环末尾创建新的 `next` 对象：

```typescript
// CC: 每个 continue 点创建新 State
const next: State = {
  messages: [...messagesForQuery, ...assistantMessages, ...toolResults],
  toolUseContext: toolUseContextWithQueryTracking,
  autoCompactTracking: tracking,
  turnCount: nextTurnCount,
  transition: { reason: 'tool_use' },   // ← 记录为何继续
}
state = next  // 赋值，非原地修改
```

`transition` 字段是所有 continue 点都必须写入的，它记录了循环继续的原因。
CC 有 ~9 个 continue 点和对应的 transition reason：
- `'tool_use'` — 有工具待执行
- `'max_output_tokens'` — token 超限 recovery
- `'reactive_compact'` — 413 context 超限后压缩重试
- `'stop_hook'` — stop hook 阻断
- `'token_budget'` — budget 内继续
- 等等

这个设计的关键价值：
1. **可追踪**：每个状态转换有原因，调试时一眼看出是哪个路径
2. **防无限循环**：transition reason 被后续迭代消费（如 `hasAttemptedReactiveCompact` 防止 413 recovery loop）
3. **可恢复**：crash 后从 state + transition 恢复，知道上次退出的原因

#### 我们怎么做

```python
# 我们当前的 ConversationState
@dataclass
class ConversationState:
    _messages: list[NativeMessage] = field(default_factory=list)  # ← 可变 list
    _pending_tool_uses: dict[str, ToolUseBlock] = field(default_factory=dict)

    def add_user_message(self, text: str) -> None:
        self._messages.append(...)  # 原地修改

    def add_assistant_message(self, action: ModelAction) -> None:
        self._messages.append(...)  # 原地修改

    def add_tool_result(self, outcome, tool_call) -> None:
        self._messages.append(...)  # 原地修改
```

- `messages` property 返回 `tuple(self._messages)` — 有不可变视图，但是事后快照
- **没有 `snapshot()` 方法** — CC 是在 continue 前创建新 State，我们是在查询时创建 tuple
- **没有 `transition` 属性** — `aiterate` 的 `continue` 没有记录原因
- **没有 transition 消费** — 无法做 recovery loop 防止

关键差异：CC 是**构建阶段不可变**（state = next，新对象），我们是**读取阶段不可变**（property 返回 tuple）。
CC 的 state = next 保证了每个迭代的状态是隔离的——如果迭代 N+1 改了 state，迭代 N 的数据不受影响。
我们的 `add_*` 方法原地修改同一个 `_messages` list——在多处并发读写时是不安全的（虽然目前没有这个场景）。

#### 架构评判

**差距真实存在，但严重程度低于文档所述。** 当前 `ConversationState` 已经通过 property 提供不可变视图，
且 Python 的 GIL + 单线程 event loop 下原地修改不会产生并发 bug。但缺少 `transition` 意味着：

1. **无法做 recovery loop 防护**：CC 用 transition reason 防止无限 recovery（如 `hasAttemptedReactiveCompact`），我们没有这个机制
2. **状态转换不可追踪**：aiterate 的每个 continue 没有留下"为什么继续"的记录

**修正方向**：最小改动——
- `ConversationState` 加 `transition` 属性（str，记录上次状态转换原因）
- `aiterate` 的每个 `continue` 前写 `self._state.transition = 'tool_use'` / `'error_retry'`
- `snapshot()` 方法返回带 transition 的 frozen view（用于 `arun` 的 event_handler 消费）
- 暂不需要把 `_messages: list` 改为 `tuple` 每轮重建——Python 内存模型下收益不大，改动成本高

> **CC 对齐度**：中（有不可变视图，缺 transition 追踪和 recovery loop 防护）

---

### Gap 4（P2）：StreamingToolExecutor

#### CC 怎么做

CC 的核心性能优化：模型还在流式生成 tool_use block 时，`StreamingToolExecutor` 就开始执行工具。
不等到整个 response 接收完——工具执行与模型生成**并行**。这是 CC 的 ~40% 速度来源。

```
Traditional:  [── LLM stream (5 tool_use blocks, 30s) ──] → [── run 5 tools (15s) ──]  = 45s
CC:           [── LLM stream ──]
                  ├ T1 arrives → execute T1 (in background)
                  ├ T2 arrives → execute T2 (in background)
                  └ ...           = ~18s  (40% faster)
```

#### 我们怎么做

我们的 `aiterate` 是**流结束后再执行工具**：

```python
# native_step_loop.py aiterate — astream_iter 完全消费后才执行工具
async for event in _stream:       # ← 等 stream 结束
    if event.kind == TOOL_USE:
        tool_uses.append(...)     # 收集
# stream 结束后才 await 工具
tool_results = await self._atool_calls(calls, context)
```

没有 StreamingToolExecutor 等价物。

#### 架构评判

**功能正确，性能可优化。文档推迟决定合理。** 我们的 `_atool_calls` 已经有并行/串行分区（`asyncio.gather`），
只是没有在流中启动。实现 StreamingToolExecutor 等价物需要：
- `astream_iter` 中收到 TOOL_USE 事件时 `asyncio.create_task(_atool_one(...))` 后台启动
- 流结束后 gather 所有 pending tasks
- 这是纯并发优化，零正确性影响

> **CC 对齐度**：低（功能正确，优化后续）——**推迟，见 Section 二 Step 5**

---

### Gap 5（P0）：sync `execute()` 退役

#### CC 怎么做

CC 只有一条主循环——`queryLoop()`。没有 sync 版本。没有 `_run_async_from_sync` 这种桥接层。
没有 `_start_cancel_watcher` 这种独立线程（取消通过 `AbortController.signal` 协作式检查）。

#### 我们怎么做

当前**两条主循环共存**：

| | sync `execute()` | async `aiterate()` |
|---|---|---|
| 调用方 | CLI, child, shadow_comparator, 测试 | Web (chat_pipeline) |
| 模型调用 | `_backend.invoke()` / `_stream_model_call()` | `_backend.ainvoke()` / `astream_iter()` |
| 工具执行 | `_process_tool_calls()` (sync) + `_run_async_from_sync(_process_tool_calls_parallel())` | `await _atool_calls()` (真 async) |
| 取消机制 | `_start_cancel_watcher` (独立线程 poll cancel_token) | `context.cancellation.cancelled` (协作式检查) |

这是最大的架构债务——两份逻辑，五种辅助函数（`_run_async_from_sync`、`_execute_parallel_batch`、
`_stream_model_call`、`_process_tool_calls`、`_process_tool_calls_parallel`），
维护成本 double，且两条路径的行为可能悄悄漂移。

#### 架构评判

**差距真实且严重。这是整个计划的核心。** 当前 sync `execute()` 有 **5 个生产调用方 + 13 个测试调用点**。
退役后 `NativeStepLoop` 只保留 `aiterate()` 一条主循环，对齐 CC 的 `queryLoop()`。

但文档遗漏了一个调用方：[validation/shadow_comparator.py](validation/shadow_comparator.py#L111) 直接调 `loop.execute()`，
需要在 Step 3 中一并处理。

退役的正确方式不是"直接删 `execute()`"，而是：
1. 先切所有调用方到 `arun()` / `asyncio.run(arun())`
2. 确认零外部调用方
3. 删除 `execute()` + `_run_async_from_sync` + `_execute_parallel_batch` + `_start_cancel_watcher`
4. `_stream_model_call` + `_process_tool_calls` 仅在 `execute()` 内使用 → 随 `execute()` 一起删

> **CC 对齐度**：低 → 高（核心差距，Step 1-3 解决）

---

### 对照总表

| Gap | CC | 我们 | 对齐度 | 动作 |
|---|---|---|---|---|
| **G1** CLI sync | 全路径 async，单一 queryLoop | CLI 走 sync execute()，Web 走 aiterate() | 🟡 低 | 1 行切 arun |
| **G2** to_thread | 线程池执行 sync I/O（read_only 并行/写串行） | to_thread 兜底 + _atool_calls 分区 | 🟢 高 | 保持现状 |
| **G3** state=next | 不可变 State 对象 + transition 追踪 ~9 个 continue 点 | 可变 _messages + property 只读视图，无 transition | 🟡 中 | 加 transition + snapshot |
| **G4** StreamingToolExecutor | 流中预执行，~40% 加速 | 流后执行 | 🔴 低 | 推迟（Step 5） |
| **G5** sync 退役 | 无 sync loop，单一 queryLoop | execute() + aiterate() 双轨 | 🔴 低 | Step 1-3 核心目标 |

---

## 二、实现步骤

### Step 1（P0）：CLI 切到 async arun

```python
# entry/chat.py _run_native_turn
import asyncio
outcome = asyncio.run(self._agent_runtime.arun(ctx))
# 不再用 sync run()
```

**验证**：CLI 测试（如果有）或手动确认。

### Step 2（P0）：child 路径切到 arun

当前 sync `run()` 调用点（全部切 `asyncio.run(arun())` 或改为 async 方法）：

| 文件 | 行号 | 当前调用 | 改法 |
|---|---|---|---|
| `application/coordinators/multi_agent_coordinator.py` | 119 | `self._runtime.run(execution)` | `asyncio.run(self._runtime.arun(execution))` |
| `runtime_core/native_child_runner.py` | 232 | `runtime.run(ctx)` (在 `run_native_child()` 内) | `asyncio.run(runtime.arun(ctx))` |
| `runtime_core/native_child_runner.py` | 299 | `run_native_child(...)` (在 `run_native_child_background()` 内) | `asyncio.run(run_native_child_async(...))` |

> `native_child_runner.py` 是文档最初遗漏的——`run_native_child()` 和 `run_native_child_background()` 都直接调 `AgentRuntime.run()`。

**验证**：child 相关测试（`tests/runtime_core/test_native_child.py`）。

### Step 3（P0）：sync execute() 退役 + 删存量

**前置：所有调用方已切 `arun`**（Step 1-2 完成）。

退役前需确认的调用方清单：

| 调用方 | 文件 | 处理 |
|---|---|---|
| CLI `_run_native_turn` | `entry/chat.py:421` | Step 1 已切 |
| Child coordinator | `multi_agent_coordinator.py:119` | Step 2 已切 |
| Child runner `run_native_child()` | `native_child_runner.py:232` | Step 2 已切 |
| Child runner `run_native_child_background()` | `native_child_runner.py:299` | Step 2 已切 |
| RunCoordinator.execute (sync) | `run_coordinator.py:145` | 切为 `asyncio.run(arun())`，或保留为 sync 兼容（web 已走 aexecute） |
| **Shadow comparator** ⚠️ 文档遗漏 | `validation/shadow_comparator.py:111` | 切为 `asyncio.run(loop.aiterate(...))` |
| 测试文件 (~13 处) | `test_native_step_loop.py` 等 | 改为调 `asyncio.run(loop.aiterate(...))` |

确认零外部调用方后，删除：
- **`NativeStepLoop.execute()`**（~230 行，[native_step_loop.py:138](runtime_core/native_step_loop.py#L138)）— sync 主循环
- **`_run_async_from_sync`**（[native_step_loop.py:57](runtime_core/native_step_loop.py#L57)）— sync→async 桥接
- **`_execute_parallel_batch`**（[native_step_loop.py:835](runtime_core/native_step_loop.py#L835)）— sync 并行工具执行
- **`_stream_model_call`** — sync 流调用（`execute()` 专用，aiterate 用 `astream_iter`）
- **`_process_tool_calls`** — sync 串行工具（`execute()` 专用，aiterate 用 `_atool_one`）
- **`_process_tool_calls_parallel`** — sync 并行工具（`execute()` 专用，aiterate 用 `_atool_calls`）
- **`_start_cancel_watcher`**（[core/process.py:55](core/process.py#L55)）— sync 取消监视线程
- **`AgentRuntime.run()`** sync — 若 `RunCoordinator.execute` 保留 sync 兼容则保留包装；否则删

**验证**：全量回归。

### Step 4（P1）：state 不可变 + transition

```python
# conversation_state.py
@dataclass(frozen=True)
class ConversationSnapshot2:  # 或加方法
    messages: tuple[NativeMessage, ...]

class ConversationState:
    def snapshot(self) -> tuple[NativeMessage, ...]:
        """不可变视图 — CC state.messages."""
        return tuple(self._messages)
    @property
    def transition(self) -> str:
        """为何继续 — CC State.transition."""
        return self._transition

# aiterate: 每轮 state.snapshot() 传给 model（对齐 CC 每迭代重建 State）
```

**验证**：aiterate 事件流测试。

### Step 5（P2）：StreamingToolExecutor（可选，推迟）

记录为后续优化，不阻塞。

---

## 三、文件范围

| 文件 | Step | 动作 |
|---|---|---|
| `entry/chat.py` | 1 | `_run_native_turn` 切 `arun` |
| `application/coordinators/multi_agent_coordinator.py` | 2 | child 切 `arun` |
| `runtime_core/native_child_runner.py` | 2 | `run_native_child` / `run_native_child_background` 切 `arun` |
| `validation/shadow_comparator.py` | 3 | 切 `aiterate`（sync `execute()` 调用方） |
| `application/coordinators/run_coordinator.py` | 3 | `execute()` sync 兼容：切或保留 |
| `runtime_core/native_step_loop.py` | 3 | 删 `execute()`/`_run_async_from_sync`/`_execute_parallel_batch`/`_stream_model_call`/`_process_tool_calls`/`_process_tool_calls_parallel` |
| `core/process.py` | 3 | 删 `_start_cancel_watcher` |
| `runtime_core/conversation_state.py` | 4 | 加 snapshot/transition |
| `runtime_core/native_step_loop.py` | 4 | aiterate 用 snapshot + transition |

---

## 四、验证

```bash
# 每步
python -m pytest <相关测试> -q

# 全量
python -m pytest tests/ -q --ignore=tests/test_smoke_e2e.py
```

---

## 五、关键决策

1. **CLI 主循环不 async 化**——`input()` 同步阻塞是 CLI 本质，`_run_native_turn` 用 `asyncio.run` 驱动 arun 即可
2. **sync 工具 to_thread 保留**——CC 也用（async 包装 sync I/O），不是背道而驰
3. **sync execute() 退役**——差距 1/2 修后，web/CLI/child 全 async，sync 只留兼容
4. **state 不可变 + transition**——对齐 CC state=next，支持恢复路径
5. **StreamingToolExecutor 推迟**——性能优化非正确性
