# Async 差距收敛计划——彻头彻尾 async，去兼容层

> 日期：2026-08-04
> 原则：不要兼容层，sync 路径（CLI）也替换为 async，连同深层 CC 对齐。
> 前置：Async 化 6 阶段（A-F）已完成，web 生产已走 aiterate。

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

```python
# application/coordinators/multi_agent_coordinator.py:119
# 当前 sync run()
outcome = asyncio.run(self._runtime.arun(execution))  # 或改 async 方法
```

**验证**：child 相关测试。

### Step 3（P0）：sync execute() 退役 + 删存量

`execute()` sync 主循环退役后：
- 删除 `_run_async_from_sync`（被 sync execute 用）
- 删除 `_execute_parallel_batch`（被 sync 并行用）
- 删除 `_start_cancel_watcher`（被 LocalRuntime sync exec 用，同步删）
- `AgentRuntime.run()` sync：保留 `RunCoordinator.execute` sync 兼容（若还有调用方），否则删

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
| `runtime_core/native_step_loop.py` | 3 | 删 `_run_async_from_sync`/`_execute_parallel_batch` |
| `core/process.py` | 3 | 删 `_start_cancel_watcher` |
| `runtime_core/conversation_state.py` | 4 | 加 snapshot/transition |
| `runtime_core/native_step_loop.py` | 4 | aiterate 用 snapshot |

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
