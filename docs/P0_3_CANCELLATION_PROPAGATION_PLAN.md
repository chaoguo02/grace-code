# P0 #3: 端到端可取消执行链 — 实施计划

> 状态：计划阶段 | 日期：2026-08-01
> 来源：跨模块安全审计 — Tools + Session 模块 P0 发现
> 预计工作量：4–7 人日（核心取消链）+ 2–4 人日（run/session 状态机）
> 前置依赖：与 P0 #1 的解耦关系见交叉依赖文档

---

## 1. 问题陈述

### 1.1 根因

当前取消链存在**三层断裂**：

```
User cancels
  → agent_service.cancel_run()
    → runtime.cancel_session() → token.cancel()          [✅ token 已 signal]
    → storage CAS update                                [✅ DB 已写]

token.cancel() 仅设置 threading.Event
  → iter_with_timeout 每 0.1s 轮询                       [✅ LLM 流中断]
  → Agent 主循环 tool batch 后检查                        [✅ 仅 batch 之间]

  → StreamingExecutor._execute_one()                     [❌ 工作线程从不检查 token]
    → ToolExecutionPipeline.execute()                    [❌ 从不接收 token]
      → ResourceGovernor.admit_wait()                    [❌ cancel_token 字段存在但未填充]
        → tool.execute()                                 [❌ 无 token，无中断机制]
          → Runtime.execute()                            [❌ 阻塞在 communicate()]
            → subprocess.Popen.communicate()             [❌ 进程继续运行直到完成/超时]
```

**结果**：用户看到 UI 显示"已取消"，但 shell 进程继续执行并产生副作用。三个关键证据：

1. `StreamingExecutor._cancel_executing()`（[streaming_executor.py:445](D:/StudyProjects/ProjectBench/grace-code/core/streaming_executor.py:445)）仅改变 Python 枚举值，不调用 `future.cancel()` 或 `kill_process_tree()`
2. `ResourceGovernor.admit_wait()`（[resource_governor.py:419](D:/StudyProjects/ProjectBench/grace-code/core/resource_governor.py:419)）已实现 `cancel_token` 检查，但 `ToolExecutionPipeline._execute_once()`（[tool_execution.py:228](D:/StudyProjects/ProjectBench/grace-code/core/tool_execution.py:228)）从不填充该字段
3. `LocalRuntime._current_proc`（[process.py:461](D:/StudyProjects/ProjectBench/grace-code/core/process.py:461)）保存了 `subprocess.Popen` 引用，仅用于事后清理，不用于外部取消

**次要问题**：`cancel_run()`（[agent_service.py:1367](D:/StudyProjects/ProjectBench/grace-code/server/services/agent_service.py:1367)）的 CAS 更新结果被忽略；run 和 session 是独立状态机，无事务协调。

### 1.2 受影响文件

| 文件 | 当前行为 | 需要变更 |
|------|---------|---------|
| `core/streaming_executor.py` | 状态标记式取消 | 接入 CancellationToken；active process kill |
| `core/tool_execution.py` | 无 token 转发 | 接收并转发 CancellationToken |
| `core/resource_governor.py` | `cancel_token` 存在但未用 | 管线填充 cancel_token |
| `core/process.py` | `_current_proc` 仅用于清理 | 新增 `cancel()` — 调用 kill_process_tree |
| `tools/shell_tool.py` | 参数重新拼接 | 原生 argv 执行（次要修复） |
| `agent/session/runtime.py` | token 注册/查找 | 传递 token 到 StreamingExecutor |
| `server/services/agent_service.py` | CAS 结果忽略 | 检查 CAS；事务性 session+run 更新 |
| `agent/session/run_context.py` | CancellationToken 定义 | 增强 state machine（取消中/已取消） |

---

## 2. 目标架构

```
User cancels
  → agent_service.cancel_run()
    ├─ 1. CAS update run status → cancel_requested (NEW intermediate state)
    ├─ 2. CAS update session status → cancelling (SAME TRANSACTION)
    └─ 3. runtime.cancel_session() → token.cancel()

token.cancel() → threading.Event.set()
  → iter_with_timeout 每 0.1s 轮询 → InterruptedError → LLM 流中断    [✅ 保持]
  → Agent 主循环 tool batch 后检查 → RunStatus.CANCELLED               [✅ 保持]
  → StreamingExecutor._monitor_cancel()                                [🆕 后台监视线程]
    → ResourceGovernor 立即释放等待中的 admission
    → _execute_one() → tool.execute(cancel_token=token)
      → Runtime 后台监视线程每 0.1s 检查 token → kill_process_tree()
      → Popen 被 SIGTERM → wait(5s) → SIGKILL                          [🆕 三级升级]

终态更新：
  → StreamingExecutor CAS: cancelled → completed 被拒绝
  → Run 终态: cancel_requested → cancelled (由执行线程确认)
  → Session: 仅当 run.cancelled 确认后 → CANCELLED
```

### 2.1 新状态机

```
Run:
  queued → running → cancel_requested → cancelled  [取消链路]
  queued → running → completed                      [正常链路]
  cancel_requested → completed (拒绝)               [CAS 保护]

Session:
  IDLE → RUNNING → CANCELLING → CANCELLED           [取消链路]
  IDLE → RUNNING → COMPLETED                        [正常链路]
```

---

## 3. 实施步骤

### Step 1: CancellationToken 传播基础设施 (1–1.5 人日)

**3.1.1** 扩展 `StreamingToolExecutor` 接收 `CancellationToken`：

**文件**: `core/streaming_executor.py`

```python
class StreamingToolExecutor:
    def __init__(self, ...):
        ...
        self._cancellation_token: CancellationToken | None = None
        self._active_futures: dict[str, concurrent.futures.Future] = {}
        self._active_processes: dict[str, subprocess.Popen] = {}

    def set_cancellation_token(self, token: CancellationToken) -> None:
        self._cancellation_token = token

    def register_active_process(self, invocation_id: str, proc: subprocess.Popen) -> None:
        """Allow Runtime to register a running subprocess for external kill."""
        self._active_processes[invocation_id] = proc

    def unregister_active_process(self, invocation_id: str) -> None:
        self._active_processes.pop(invocation_id, None)
```

**3.1.2** `_execute_one()` 开始前和执行后检查 token：

```python
def _execute_one(self, tc: TrackedCall) -> None:
    # Check BEFORE execution
    if self._cancellation_token and self._cancellation_token.is_cancelled:
        tc.status = TrackedStatus.CANCELLED
        tc.error = self._cancellation_token.detail or "Cancelled"
        self._wake.set()
        return

    try:
        result = self._registry.execute_tool(...)
        # Check AFTER execution (for cooperative cancellation)
        if self._cancellation_token and self._cancellation_token.is_cancelled:
            tc.status = TrackedStatus.CANCELLED
            ...
    except Exception as e:
        ...
```

**3.1.3** `_cancel_executing()` 改为主动终止：

```python
def _cancel_executing(self, reason: str) -> None:
    with self._lock:
        for t in self._tracked:
            if t.status == TrackedStatus.EXECUTING:
                t.status = TrackedStatus.CANCELLED
                t.error = reason
                # NEW: kill associated subprocess
                proc = self._active_processes.get(t.id)
                if proc is not None:
                    _kill_process_tree(proc)
                # NEW: try to cancel the future
                future = self._active_futures.get(t.id)
                if future is not None:
                    future.cancel()
        self._wake.set()
```

**3.1.4** 将 token 从 Agent 主循环传送到 StreamingExecutor：

**文件**: `agent/loop/turns.py`

```python
async def execute_action(self, action: Action, ...) -> ...:
    executor = StreamingToolExecutor(self._registry, ...)
    executor.set_cancellation_token(self._cancellation_token)  # NEW
    dispatch_result = executor.dispatch(tool_calls)
    return executor.collect()
```

### Step 2: 工具执行管线接入 CancellationToken (1–1.5 人日)

**文件**: `core/tool_execution.py`, `core/base.py`

**3.2.1** `ToolExecutionPipeline.execute()` 接收并转发 token：

```python
class ToolExecutionPipeline:
    def execute(
        self,
        tool: BaseTool,
        params: dict,
        *,
        invocation_id: str = "",
        cancellation_token: CancellationToken | None = None,  # NEW
    ) -> ToolResult:
        ...
```

**3.2.2** `_execute_once()` 中填充 `ResourceRequest.cancel_token`：

```python
def _execute_once(self, tool, params, *, cancellation_token=None):
    admission = self._resource_governor.admit_wait(ResourceRequest(
        request_id=f"{invocation_id}:attempt-{attempt}",
        ...
        cancel_token=cancellation_token,  # NEW — was always None
    ))
```

**3.2.3** `ResourceGovernor.admit_wait()` 中的取消检查（已实现，确保调用链到达）：

```python
# resource_governor.py:419 — existing code, now actually triggered
if request.cancel_token is not None and request.cancel_token.is_cancelled:
    raise ResourceAdmissionCancelled(...)
```

**3.2.4** `ToolRegistry.execute_tool()` 转发 token：

```python
# core/base.py:775
def execute_tool(
    self,
    name: str,
    params: dict,
    *,
    invocation_id: str = "",
    cancellation_token: CancellationToken | None = None,  # NEW
) -> ToolResult:
    pipeline = ToolExecutionPipeline(...)
    return pipeline.execute(
        tool, params,
        invocation_id=invocation_id,
        cancellation_token=cancellation_token,
    )
```

### Step 3: Subprocess 中断 (1–2 人日)

**3.3.1** `LocalRuntime` 中实现基于取消的后台进程终止：

**文件**: `core/process.py`

```python
class LocalRuntime:
    def __init__(self, ...):
        ...
        self._cancel_token: CancellationToken | None = None
        self._cancel_watcher: threading.Thread | None = None

    def execute(
        self,
        cmd: list[str],  # NEW: prefer list for argv
        *,
        timeout: float | None = None,
        cancel_token: CancellationToken | None = None,  # NEW
        shell_mode: bool = False,  # NEW: explicit opt-in
    ) -> ProcessResult:
        self._cancel_token = cancel_token

        if cancel_token is not None:
            # Start background watcher thread
            self._cancel_watcher = threading.Thread(
                target=self._watch_cancel,
                daemon=True,
            )
            self._cancel_watcher.start()

        try:
            proc = subprocess.Popen(...)
            self._current_proc = proc
            # Register with executor for direct kill capability
            if self._executor is not None:
                self._executor.register_active_process(invocation_id, proc)

            stdout, stderr = proc.communicate(timeout=timeout)
            ...
        finally:
            if self._cancel_watcher is not None:
                self._cancel_watcher.join(timeout=1.0)
            self._current_proc = None
            if self._executor is not None:
                self._executor.unregister_active_process(invocation_id)

    def _watch_cancel(self) -> None:
        """Poll cancellation token every 0.1s. On cancel, kill the process tree."""
        while self._current_proc is not None:
            if self._cancel_token is not None and self._cancel_token.is_cancelled:
                _kill_process_tree(self._current_proc)
                return
            time.sleep(0.1)
```

**3.3.2** Shell 参数边界修复：

**文件**: `tools/shell_tool.py`

```python
# 当前 (line 249): 将 command + args 重新拼接为字符串
# 修复后:
def execute(self, params: dict, *, cancel_token=None) -> ToolResult:
    command = params.get("command", "")
    args = params.get("args", [])
    use_shell = params.get("use_shell", False)  # NEW: explicit opt-in

    if use_shell:
        # Shell mode: high risk, requires explicit user approval
        full_command = " ".join([command] + args)
        result = self._runtime.execute(
            ["powershell.exe", "-Command", full_command],
            shell_mode=True,
            cancel_token=cancel_token,
            ...
        )
    else:
        # Preferred: direct argv execution, no shell interpretation
        argv = [command] + list(args)
        result = self._runtime.execute(
            argv,
            shell_mode=False,
            cancel_token=cancel_token,
            ...
        )
```

对于 workspace guard：`root` 缺失时 `fail closed`（拒绝执行），而非退化为无工作区限制。

### Step 4: run/session 取消状态机事务化 (1–2 人日)

**3.4.1** 修复 `cancel_run()` 的 CAS 忽略：

**文件**: `server/services/agent_service.py`

```python
async def cancel_run(self, session_id: str, detail: str = "") -> bool:
    session = self._store.get_session(session_id)
    active_run = self._get_active_run(session_id)

    # Step 1: CAS update run status — CHECK the result
    run_updated = self._storage.update_run(
        active_run["id"],
        status="cancelled",
        expect_status="running",   # only if currently running
        detail=detail,
    )
    if not run_updated:
        # Run already completed/failed/cancelled — don't touch session
        logger.warning(
            "Cancel CAS failed: run %s is no longer running",
            active_run["id"],
        )
        return False

    # Step 2: Cancel session ONLY IF run CAS succeeded
    self._storage.update_status(session_id, SessionStatus.CANCELLED, detail=detail)

    # Step 3: Signal the CancellationToken (memory-side)
    cancelled = self._runtime.cancel_session(session_id, detail=detail)

    return cancelled
```

**3.4.2** 引入 `cancel_requested` 中间态，防止 `cancelled → completed` 状态覆盖：

**文件**: `core/streaming_executor.py`

```python
def _execute_one(self, tc: TrackedCall) -> None:
    ...
    try:
        result = self._registry.execute_tool(...)
        # CAS: only mark completed if not already cancelled
        with self._lock:
            if tc.status != TrackedStatus.CANCELLED:
                tc.status = TrackedStatus.COMPLETED
                tc.result = result
    except Exception as e:
        with self._lock:
            # Error can override pending, but NOT cancelled
            if tc.status != TrackedStatus.CANCELLED:
                tc.status = TrackedStatus.ERROR
                tc.error = str(e)
    ...
```

### Step 5: 通用工具执行超时 + 回归测试 (1–1.5 人日)

**3.5.1** `BaseTool` 增加 `execution_timeout`：

```python
# core/base.py
class BaseTool:
    execution_timeout: float | None = None  # seconds

class ToolDefinition:
    execution_timeout: float | None = None
```

**3.5.2** `ToolExecutionPipeline._execute_once()` 中统一应用超时：

```python
def _execute_once(self, tool, params, *, cancellation_token=None):
    timeout = getattr(tool, 'execution_timeout', None) or DEFAULT_TOOL_TIMEOUT
    admission = self._resource_governor.admit_wait(ResourceRequest(
        ...
        timeout_s=timeout,
        cancel_token=cancellation_token,
    ))
    ...
```

**3.5.3** 超时处理器：超时时向 `CancellationToken` 发 cancel，将副作用标记为 `UNKNOWN`（而非 `FAILED`）。

**3.5.4** 新增测试：

| 测试 | 覆盖 |
|------|------|
| `test_cancel_during_shell_execution` | 取消信号 → `kill_process_tree()` 被调用 |
| `test_cancel_before_execution_skips_tool` | token 已发信号 → 工具永不执行 |
| `test_cancel_during_resource_wait` | `admit_wait()` 中取消 → `ResourceAdmissionCancelled` |
| `test_cas_prevents_cancelled_to_completed` | 取消后的完成更新被 CAS 拒绝 |
| `test_cancel_run_cas_failure_leaves_session` | 已完成 run 的取消 → session 不变 |
| `test_tool_timeout_marks_side_effect_unknown` | 超时 → 副作用标记为 UNKNOWN，进程树被 kill |
| `test_shell_argv_mode_no_injection` | 含 `;` 参数在 argv 模式下不被解释为命令 |

---

## 4. 风险与回滚

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| `_watch_cancel` 后台线程与 `communicate()` 竞争导致资源泄漏 | 中 | 中 | 使用 `proc.wait(timeout=5)` 确认 SIGTERM 后进程终止；fail-safe finally 块总是关闭句柄 |
| Tokencancellation 传播到所有工具带来破坏性变更 | 中 | 高 | `cancellation_token` 参数默认 `None`（向后兼容）；按 ShellTool → MCP → 其他工具的顺序逐步接入 |
| 调试/日志中 kill_process_tree 的误触发 | 低 | 低 | 仅当 `token.is_cancelled` 为 True 时触发；需要同时满足: token 已 cancel + 进程仍存活 |
| 平台差异（Windows taskkill vs POSIX SIGTERM） | 低 | 中 | `kill_process_tree()` 已有跨平台实现（`taskkill /F /T` vs `os.killpg`）— 沿用现有代码 |

**回滚方式**: `StreamingToolExecutor.set_cancellation_token()` 的参数为 `Optional`；不传 token 时所有行为与旧代码完全一致。feature flag `CANCELLATION_PROPAGATION_ENABLED` 控制是否向下传递 token。

---

## 5. 验证清单

- [ ] Shell 工具执行中取消 → 子进程在 5s 内终止
- [ ] MCP 工具执行中取消 → MCP session 收到关闭指令
- [ ] 排队等待 ResourceGovernor 的工具在取消时被立即释放
- [ ] 取消后的完成状态更新被 CAS 拒绝
- [ ] 已完成 run 的 cancel_run() → CAS 失败 → session 状态不变
- [ ] `;`、`$()` 在 argv 模式下被作为字面参数传递，不被 shell 解释
- [ ] 超时工具 → 副作用标记为 UNKNOWN（非 FAILED）
- [ ] 未传 cancellation_token 时 → 所有行为与旧代码完全一致（向后兼容）
- [ ] 68 项现有测试保持通过

---

## 6. 文件变更汇总

| 文件 | 变更类型 | 行数估计 |
|------|---------|---------|
| `core/streaming_executor.py` | 接收 token；`_cancel_executing()` 主动 kill；终态 CAS | +80/-20 |
| `core/tool_execution.py` | 接收并转发 token；填充 cancel_token；通用超时 | +40/-10 |
| `core/resource_governor.py` | (已有 cancel_token 检查，确认管线到达) | +10/-5 |
| `core/process.py` | `_watch_cancel()` 监视线程；`cancel()` 方法 | +60/-5 |
| `core/base.py` | `ToolRegistry.execute_tool()` 接收 token；`execution_timeout` | +25/-5 |
| `tools/shell_tool.py` | argv vs shell mode；workspace root fail-closed | +50/-20 |
| `agent/loop/turns.py` | 将 token 传入 StreamingExecutor | +15/-5 |
| `agent/session/runtime.py` | 将 token 传入 executor | +10/-2 |
| `server/services/agent_service.py` | CAS 检查；事务性 run+session 更新 | +30/-15 |
| `tests/test_cancellation.py` | 新增 7 个测试 | +300 |
| **总计** | | **~620/+87** |
