# Runtime / Hook / EventBus 全链路差距分析

> 日期：2026-08-04
> 基线：H0-H8 完成（运行就绪 92/100）
> 方法：模拟完整请求流转，逐节点验证代码行为
> 结果：发现 10 个 P0、3 个 P1 差距

---

## 0. 总览

本文模拟一个 Chat 请求从 `POST /api/sessions/{id}/messages` 到 `RuntimeOutcome` 落库的全链路，
追踪每一步实际执行的代码，找出对象图已装配但**数据流未接通**的断点。

```text
curl POST /api/sessions/s1/messages
  → run_server.py (Entry)
    → create_app(native_components=...)    ← [GAP 1] native_components 被丢弃
  → sessions.py router
    → submit_run_turn(storage, ...)        ← [GAP 2] coordinator 未注入
      → RunCoordinator.submit()           ← 仅写 run.submitted.v1
    → ChatPipeline.execute()
      → SessionRuntime.run_session()       ← [GAP 3] 旧 Runtime，未切到 native
        → legacy hook dispatcher           ← [GAP 5] HookDispatcher 被绕过
        → legacy event_bus.publish_typed()  ← [GAP 6-7] 旧 EventBus，非 Outbox
  → OutboxRelay (单独线程)
    → 仅收到 run.submitted.v1             ← [GAP 7] 生命周期事件缺失
```

**根因：两个"丢弃点"阻断了整个 Native 链路**

1. `create_app()` 收到 `native_components` 但不传递给路由
2. Web 执行路径未从 `SessionRuntime` 切换到 `AgentRuntime`

只要在 `create_app()` 中注入 `native_components` → `app.state`，并在 `ChatPipeline` 中增加 `if native → coordinator.execute()` 分支，整条链路将激活。

---

# 链路 1：入口点

## 1.1 代码证据

**文件**：`run_server.py:36-45`

```python
components = assemble(db_path)                 # 完整的 Native 对象图
lifecycle = ApplicationLifecycle(components)
lifecycle.start()                              # Relay 启动，lease 获取

app = create_app(native_components=components) # ← 传递了 native_components
```

**文件**：`server/main.py:142-144`

```python
def create_app(service: AgentService | None = None,
                native_components=None) -> FastAPI:
```

## 1.2 实际行为

`create_app()` 接收了 `native_components` 参数，但在函数体 **185 行之后的所有代码中从未引用它**。只存储了 `app.state.service = service`——这是旧 `AgentService`。

没有 `if native_components:` 分支。没有向路由注入 `run_coordinator` 的代码。参数被**静默丢弃**。

## 1.3 预期行为

`native_components` 必须向下传递：

```python
app.state.native_components = native_components
# 路由通过 get_service() 获取后注入到 submit_run_turn(coordinator=...)
```

## 1.4 差距等级

**P0**——入口点丢弃了整个 Native 对象图，导致下游全部使用旧路径。

## 1.5 修复路线

| 文件 | 行 | 动作 |
|---|---|---|
| `server/main.py` | 185 | 增加 `app.state.native_components = native_components` |
| `server/main.py` | 193-210 | 改造 `get_service()` 同时暴露 `native_components` |
| `server/routers/sessions.py` | 613 | 路由中使用 `native_components.run_coordinator` |

---

# 链路 2：Run 提交

## 2.1 代码证据

**文件**：`server/services/run_submission.py:42-84`

```python
def submit_run_turn(storage, *, session_id, prompt,
                    idempotency_key="", coordinator=None):
    if coordinator is not None:
        # Path A — 注入路径
        result = coordinator.submit(cmd)
    # Path B — fallback, 每次都走到这里
    rt = AgentRuntime(RuntimePorts(    # 全部 stub ports
        llm=_stub_llm(), tools=_stub_tools(), ...
    ))
```

**文件**：`server/routers/sessions.py:613`

```python
submitted = submit_run_turn(
    _storage, session_id=session_id, prompt=body.prompt,
    idempotency_key=_idem_key,
    # ← coordinator= 未传递
)
```

## 2.2 实际行为

路由调用 `submit_run_turn()` 时**未传递 `coordinator=` 参数**。Path B 每次都执行，在 fallback 中创建一个**全部 stub 的 RuntimePorts** 的 AgentRuntime——尽管 `submit()` 不需要 runtime，但 coordinator 携带了一个退化 runtime 引用。

## 2.3 预期行为

```
coordinator = request.app.state.native_components.run_coordinator
submit_run_turn(storage, ..., coordinator=coordinator)
```

## 2.4 差距等级

**P0**——每次请求都重新创建带 stub 的 coordinator。

## 2.5 修复路线

| 文件 | 行 | 动作 |
|---|---|---|
| `server/main.py` | 185 | `app.state.native_components = native_components` |
| `server/routers/sessions.py` | 613 | 注入 `coordinator` 参数 |

---

# 链路 3：Run 执行

## 3.1 代码证据

**文件**：`server/services/agent_service.py:970` (ChatPipeline 构造)

ChatPipeline 执行路径（line 410-475）调用 `self._runtime.run_session()`，
这个 `_runtime` 是 `SessionRuntime`（`agent/session/runtime.py:1169`）。

**文件**：`application/coordinators/run_coordinator.py:124-128`

```python
def execute(self, cmd: ExecuteRun) -> RuntimeOutcome:
    context = RuntimeExecution(
        session_id=cmd.session_id, run_id=cmd.run_id,
    )                                # ← 空 conversation, 空 capabilities
    return self._runtime.run(context)
```

## 3.2 实际行为

`RunCoordinator.execute()` **从未被 web 流程调用**。Web 执行始终走旧 `SessionRuntime.run_session()`——这是一个完全独立的旧 Runtime 实现，不经过 `step_loop`、不调用 `HookDispatcher`、不写入 Outbox。

即使被调用，`execute()` 创建的 `RuntimeExecution` 有空的 `conversation`（零消息）和空的 `capabilities`（零工具 schema），LLM 将收不到任何上下文。

## 3.3 预期行为

`ChatPipeline` 或 `agent_service.py` 在检测到 `_native_components` 时，应改为调用：

```python
context = RuntimeExecution(
    session_id=..., run_id=...,
    conversation=ConversationSnapshot(messages=...),
    capabilities=CapabilitySnapshot(tool_schemas=...),
)
outcome = native_components.run_coordinator.execute(ExecuteRun(...))
native_components.run_coordinator.finalize(FinalizeRun(...))
```

## 3.4 差距等级

**P0**——Web 执行路径完全绕过 Native Runtime，导致整个 step_loop、HookDispatcher、Outbox relay 链对 web 请求无效。

## 3.5 修复路线

| 文件 | 行 | 动作 |
|---|---|---|
| `server/services/agent_service.py` | ~970 | 增加 `if _native_components → coordinator.execute()` 分支 |
| `application/coordinators/run_coordinator.py` | 126-127 | 填充 `conversation` 和 `capabilities` |

---

# 链路 4：Runtime 执行循环

## 4.1 代码证据

**文件**：`runtime_core/step_loop.py:84-208`

step_loop 的 `execute()` 已完整实现：
- 模型调用（line 110）→ `self._ports.llm.invoke(conv_json)`
- Token 提取（lines 119-126）→ `model_action.usage`
- 工具处理（lines 151-177）→ PreToolUse→execute→PostToolUse
- Evidence 采集（line 156）→ `tool_evidences.append(tr.evidence)`
- 并行调度（line 154-159）→ `len(calls) > 1` 时走 `_process_tool_calls_parallel`

**文件**：`composition/runtime_composition.py:271-292`

每个端口的实际实现：

| 端口 | 代码 | 行为 |
|---|---|---|
| `llm` | `_RealLLM(backend=None)` | 返回 `AssistantText("H1 fake response")` |
| `tools` | `_RealTools(tool_lookup=None)` | 返回 `ToolSuccess("H2 fake output")` |
| `live_events` | `publish(): pass` | 什么都不做 |
| `token_usage` | `record(): pass` | 什么都不做 |
| `cancellation` | `cancelled → False` | 总是未取消 |

## 4.2 实际行为

即使 step_loop 被调用（例如通过 Coordinator 或直接测试），**5 个端口中的 4 个是空操作**。只有 `clock` 端口工作正常。

## 4.3 预期行为

| 端口 | 预期实现 |
|---|---|
| `llm` | `_RealLLM(backend=real_backend)` — 委托到 Anthropic/OpenAI |
| `tools` | `_RealTools(tool_lookup=real_tool_registry)` — 委托到 BaseTool.run() |
| `live_events` | `publish()` → `self._bus.async_publish(message)` 或 `self._bus.publish(message)` |
| `token_usage` | `record()` → 通过 UoW 写入 Outbox 或直接写 SQLite |
| `cancellation` | 通过 `CancellationHandle` 已正确工作（step_loop line 94 直接读 handle） |

## 4.4 差距等级

**P0**——5 个端口中有 4 个是 no-op。LLM 返回假响应，工具不执行，live events 丢失，token 未记录。

## 4.5 修复路线

| 文件 | 行 | 动作 |
|---|---|---|
| `composition/runtime_composition.py` | 289 | `_RealLLM(backend=llm_backend)` |
| `composition/runtime_composition.py` | 290 | `_RealTools(tool_lookup=tool_registry_lookup)` |
| `composition/runtime_composition.py` | 222 | `publish()` → `self._bus.publish(...)` |
| `composition/runtime_composition.py` | 229 | `record()` → 写入 outbox/sqlite |

---

# 链路 5：Hook Gate

## 5.1 代码证据

**文件**：`composition/runtime_composition.py:245-256`

```python
class _RealHooks:
    def __init__(self, dispatcher):
        self._dispatcher = dispatcher
    def check(self, event_type, hook_input, tool_name=""):
        result = self._dispatcher.dispatch(event_type, hook_input, tool_name=tool_name)
        return HookGateResult(allowed=not result.blocked, ...)
```

`_RealHooks` 正确包装了 `HookDispatcher`。

**文件**：`composition/runtime_composition.py:200-201`

```python
hook_registry = HookRegistry()
hook_dispatcher = HookDispatcher(hook_registry)
# ← 没有 register() 调用
```

## 5.2 实际行为

HookDispatcher → StepLoop 的**接线正确**：StepLoop._process_tool_calls() line 265 调用 `self._ports.hooks.check(...)` → `_RealHooks.check()` → `dispatcher.dispatch()`。

但有两个问题：

1. **Hook 注册表为空**：`assemble()` 创建 `HookRegistry()` 后没有注册任何 hook。`get_hooks()` 始终返回空列表——dispatch 循环体(line 92) 永远不会执行。所有工具调用都跳过 hook 检查（直接到 `HookGateResult(allowed=True)` 的回退路径？不，看 StepLoop line 265-272：如果 `gate_result is None` 或 exception，才回退到 `allowed=False`。再看 `_RealHooks.check()`——dispatch 返回 `DispatchResult(results=[])` 即 `blocked=False`，所以 gate_result 是 `HookGateResult(allowed=True, reason="", updated_input=None)`——实际上 hooks 被有效跳过了）。

2. **Web 流程中完全绕过**：如链路 3 所述，web 执行走的是旧 `SessionRuntime`，不使用 `_RealHooks`。

3. **`dispatch_async()` 未使用**：sync `dispatch()` 是唯一调用路径，hook 串行执行。

## 5.3 预期行为

- 启动时从配置加载 hook 并注册到 `HookRegistry`
- Web 执行切换到 `StepLoop` 从而激活 `HookDispatcher`
- 多 hook 场景使用 `dispatch_async()` 并行执行

## 5.4 差距等级

**P1**——接线正确但因 hook registry 为空 + web 绕过而无效。

## 5.5 修复路线

| 文件 | 行 | 动作 |
|---|---|---|
| `composition/runtime_composition.py` | 201 | 加载并注册 hook（从 settings.json 或 defaults） |
| `server/services/agent_service.py` | ~970 | 切到 native 执行路径 |
| `runtime_core/step_loop.py` | 154 | ToolCallBatch 中考虑用 `dispatch_async` 做并行 hook |

---

# 链路 6：EventBus

## 6.1 代码证据

**文件**：`composition/runtime_composition.py:217-222`

```python
class _RealLiveEvents:
    def __init__(self, bus):
        self._bus = bus
    def publish(self, event_type, payload):
        pass  # ← 什么都不做
```

**文件**：`eventing/scoped_bus.py:159-181`（sync `publish()` 已实现）
**文件**：`eventing/scoped_bus.py:195-230`（async `async_publish()` 已实现）

## 6.2 实际行为

`ScopedEventBus` 对象存在于 `ApplicationComponents` 中，`_RealLiveEvents` 持有引用。但 `publish()` 是 `pass`——从不调用 `self._bus.publish()` 或 `self._bus.async_publish()`。

此外，没有代码调用 `bus.subscribe()`——EventBus 有**零订阅者**。

async delivery infrastructure（`BoundedChannel`、`asyncio.Semaphore` backpressure、`shutdown_async()` drain）完全是**死代码**。

## 6.3 预期行为

```python
def publish(self, event_type, payload):
    # 创建 ScopedMessage 并发布到 EventBus
    scope = ...  # 需要从上下文获取 session scope
    self._bus.publish(message)
```

以及订阅方：

```python
# WsGateway 注册为 subscriber
bus.subscribe("tool.executed.v1", ws_gateway.on_event, "ws", scope=...)
```

## 6.4 差距等级

**P0**——整个 EventBus 是死的。所有 live event（tool 进度、run heartbeat、hook warning）被静默丢弃。

## 6.5 修复路线

| 文件 | 行 | 动作 |
|---|---|---|
| `composition/runtime_composition.py` | 222 | `publish()` → `self._bus.publish(message)` |
| `composition/runtime_composition.py` | ~200 | 注册 WebSocket subscriber 到 bus |
| `runtime_core/step_loop.py` | 166-171 | tool live event 走 `_RealLiveEvents.publish()` |

---

# 链路 7：Outbox / Projection

## 7.1 代码证据

**文件**：`composition/runtime_composition.py:319-326`

```python
relay = OutboxRelay(outbox, _deliver, lease=lease)
```

Relay 正确启动（`ApplicationLifecycle.start()` line 89-90）。

## 7.2 实际行为

Outbox relay 轮询、claim、投递、ACK/retry/DLQ 全部正常工作。

**但 relay 只能投递 `run.submitted.v1` 事件。**

原因：
- `RunCoordinator.submit()` 写入 `run.submitted.v1` 到 outbox（在 `_submit_in_tx()` 的 `tx.append_fact(envelope)` line 121）
- `RunCoordinator.execute()` 和 `finalize()` 从不在 web 流程中调用
- Web 执行通过 `SessionRuntime.run_session()` → 旧 `event_bus.publish_typed()` → 旧 trace 系统，不经过 Outbox

因此 relay-projection 链在 web 请求中**每个 run 只投递 1 个事件**。Trace、Stats、Audit 表几乎为空。

## 7.3 预期行为

切换到 native coordinator 执行路径后，`execute()` 和 `finalize()` 将自动产生 `run.started.v1`、`run.completed.v1` 等事件通过同一 Outbox → Relay → Projection → Trace/Stats/Audit。

## 7.4 差距等级

**P0**——projection 只收到 submitted 事件，缺少全部生命周期事件。

## 7.5 修复路线

同链路 3——切换到 native coordinator 执行。

---

# 链路 8：Composition 端口状态总览

## 8.1 端口状态矩阵

| # | 端口 | 行 | 当前 | 级别 | 修复 |
|---|---|---|---|---|---|
| 1 | `llm` | 289 | `_RealLLM(backend=None)` — 假响应 | P0 | 传入真实 Anthropic/OpenAI backend |
| 2 | `tools` | 290 | `_RealTools(tool_lookup=None)` — 假输出 | P0 | 传入真实 `BaseTool` registry lookup |
| 3 | `hooks` | 245 | `_RealHooks(hook_dispatcher)` — 已接线但 registry 为空 | P1 | 加载 hook 配置 |
| 4 | `live_events` | 222 | `publish(): pass` — 完全不起作用 | P0 | 调用 `self._bus.publish()` |
| 5 | `clock` | 210 | `_RealClock()` — `time.monotonic()` | ✅ OK | — |
| 6 | `token_usage` | 229 | `record(): pass` — 不记录 | P0 | 通过 UoW 写入 Outbox |
| 7 | `cancellation` | 231 | `_RealCancellation(None)` — 总是 False | P1 | step_loop 直接用 Handle，port 冗余可删除 |

## 8.2 汇总

| 状态 | 计数 | 端口 |
|---|---|---|
| ✅ OK | 1 | clock |
| P0 — 死/假 | 4 | llm, tools, live_events, token_usage |
| P1 — 接线正确但无效 | 2 | hooks (无注册), cancellation (冗余) |

---

# 9. 综合差距矩阵

| 链路 | P0 | P1 | 描述 |
|---|---|---|---|
| 1. Entry | 1 | 0 | `create_app()` 丢弃 native_components |
| 2. Submit | 1 | 0 | coordinator 从未注入路由 |
| 3. Execute | 1 | 0 | Web 走旧 SessionRuntime，Coordinator.execute() 死代码 |
| 4. Runtime | 4 | 0 | LLM/Tools/LiveEvents/TokenUsage 全部 no-op |
| 5. Hook | 0 | 3 | Registry 空 + dispatch_async 死代码 + web 绕过 |
| 6. EventBus | 2 | 0 | publish() no-op + 零订阅者 |
| 7. Outbox | 1 | 0 | 只有 submitted 事件流过 |
| 8. Compose | 4 | 2 | 5/7 端口死/假 |

**总计：P0 × 10, P1 × 3**

---

# 10. 预期解决路线

## 阶段 I：激活 Native 执行路径（3 个文件，1 个 P0 根因）

这是**关键开关**——修复后，其余差距从"死代码"变为"可测试的活代码"。

| 文件 | 动作 | 消灭的 P0 |
|---|---|---|
| `server/main.py:185` | `app.state.native_components = native_components` | 链路 1, 2 |
| `server/routers/sessions.py:613` | `submit_run_turn(..., coordinator=...)` | 链路 2 |
| `server/services/agent_service.py:~970` | `if native → coordinator.execute()` | 链路 3, 7 |

## 阶段 II：激活端口（4 个文件，4 个 P0）

| 文件 | 动作 | 消灭的 P0 |
|---|---|---|
| `composition/runtime_composition.py:289` | `_RealLLM(backend=real_backend)` | 链路 4 |
| `composition/runtime_composition.py:290` | `_RealTools(tool_lookup=registry_lookup)` | 链路 4 |
| `composition/runtime_composition.py:222` | `publish()` → `self._bus.publish()` | 链路 4, 6 |
| `composition/runtime_composition.py:229` | `record()` → outbox write | 链路 4 |

## 阶段 III：激活 Hook + EventBus（3 个文件，3 个 P1）

| 文件 | 动作 | 消灭的 P1 |
|---|---|---|
| `composition/runtime_composition.py:201` | 注册 hook（从 settings.json/defaults） | 链路 5 |
| `runtime_core/step_loop.py:154` | `dispatch_async` 用于并行 hook | 链路 5 |
| `composition/runtime_composition.py:~200` | 注册 WebSocket subscriber | 链路 6 |

---

> **优先级**：阶段 I > 阶段 II > 阶段 III
> 
> 阶段 I 是"开关"——完成后整个 Native 对象图将对 web 请求激活。
> 阶段 II 是"通流"——端口接入真实后端后，Runtime 产生真实输出。
> 阶段 III 是"完形"——Hook 和 EventBus 从死代码变为活系统。
