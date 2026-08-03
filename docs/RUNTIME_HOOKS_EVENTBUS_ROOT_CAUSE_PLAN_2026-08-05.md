# Runtime / Hook / EventBus 根因分析与 CC-Native 根治方案

> 文档版本：1.0.0
> 创建日期：2026-08-05
> 基线代码：H0-H8 完成（运行就绪 92/100）
> 前置分析：`RUNTIME_HOOKS_EVENTBUS_CHAIN_GAP_ANALYSIS_2026-08-04.md`（10 P0 + 3 P1）
> 核心原则：**不补丁、不对齐旧 API、从 CC 架构倒推设计**

---

# 0. 方法论

## 0.1 本文与差距分析的关系

`CHAIN_GAP_ANALYSIS` 回答了"哪里断了"—— 逐链路追踪，发现两个丢弃点阻断了整个 Native 对象图。

本文回答"为什么断了，怎么从根上修"——对 8 个链路的每个 P0/P1，追溯其产生的原始设计决策，对照 Claude Code 官方架构找到正确的设计方向，给出不引入新兼容层的根治方案。

## 0.2 根因分析模板

每个差距按以下结构分析：

```text
症状：代码表现出的行为
直接原因：哪一行代码导致了症状
原始决策：当初为什么这样写（设计时的合理假设）
CC 对齐：Claude Code 官方文档/架构对这个点的规定
根治方案：基于 CC 对齐的修改方向
不可行方案（及原因）：看起来能修但不能用的方案
```

## 0.3 CC 官方事实基线

以下事实来自 [Claude Code Hooks Reference](https://code.claude.com/docs/en/hooks) 和 [Cline Hub-Spoke Architecture](https://docs.cline.bot/sdk/architecture/hub-spoke)，是本文所有设计的"为什么"：

| # | CC 事实 | 对代码的要求 |
|---|---|---|
| F1 | Hook 嵌入 agent loop，是同步拦截点，不是 sidecar | HookDispatcher 必须在 step_loop 的每个生命周期点被 awaited |
| F2 | 所有匹配 hook 并行执行，去重 | `dispatch_async()` + TaskGroup 必须是默认路径，不是 "could use" |
| F3 | `PreToolUse.permissionDecision` 支持 allow/deny/ask/defer，优先级固定 | StepLoop 必须在 tool 执行前调用 HookDispatcher 并遵守决策优先级 |
| F4 | `updatedInput` 替换 tool arguments | StepLoop 必须用 gate_result.updated_input 覆盖 tc.params |
| F5 | Exit code 2 = 阻断；exit 0 stdout = 解析为决策；其他 = 非阻断错误 | ProcessRunner 已实现 (G13)，但未被 web 流程使用 |
| F6 | Hook 配置从 settings.json 多层合并加载 | Composition Root 必须在 assemble() 时加载 hook 配置 |
| F7 | `additionalContext` 注入 system reminder | DispatchResult.additional_context 必须被 step_loop 读取并追加到下一轮 messages |
| F8 | Cline: Hub 协调（Coordinator）、Spoke 执行（Runtime）、Client 呈现（Gateway）分离，Session 独立于 Client 生命周期 | Runtime 不 import Server；Gateway 不驱动业务状态；Coordinator 是唯一流程 Authority |
| F9 | CloudEvents: `source + id` 唯一标识，至少一次投递 | Outbox relay 已实现(G9/G10)，但只有 submit 产生事件 |
| F10 | state + fact 同事务提交（Transactional Outbox） | Coordinator.finalize() 必须在同一 UoW 中写入 terminal state + terminal fact |

---

# 1. 差距 1：`create_app()` 丢弃 `native_components`

## 1.1 症状

`run_server.py:45` 调用 `create_app(native_components=components)`，但 `create_app()` 函数体从未引用该参数。整个 Native 对象图在入口点被丢弃。

## 1.2 直接原因

`server/main.py:142-144`。函数签名接受 `native_components`，但函数体只使用 `service` 旧对象。

## 1.3 原始决策

G29 改造 `create_app` 时，为了**向后兼容旧 AgentService 路径**，将 `native_components` 设为可选参数。当时的假设是"先让旧路径继续工作，后续阶段再切换"。这个假设在 G29 时是合理的——G29 的目标是"单 Native Startup"，不是"全 Native 执行"。

但 G29 之后经过了 G30-G44（删除阶段）和 H0-H8（端到端打通），"后续阶段"已经到来。向后兼容的理由不复存在。

## 1.4 CC 对齐

**F8**：Cline 架构中，Client（Gateway/Web）通过 Hub（Coordinator）与 Spoke（Runtime）通信。Web 路由应该通过 Coordinator 访问 Runtime，而不是直接持有 SessionRuntime 引用。

CC 的 hook 配置通过 `settings.json` 加载，配置由 `claude` CLI 进程在启动时解析并注入到 hook 引擎。Grace Code 的等价物是：`assemble()` 返回完整的 `ApplicationComponents`，Web 层通过 `app.state` 接收并注入到路由。

## 1.5 根治方案

**删除 `native_components` 可选参数，改为 `create_app(components: ApplicationComponents)` 必选。**

具体变化：

1. `create_app` 签名从 `(service=..., native_components=...)` 改为 `(components: ApplicationComponents)`。
2. `app.state.components = components` —— 替代 `app.state.service`。
3. 所有路由的 `get_service()` dependency 改为 `get_components()`，返回 `ApplicationComponents`。
4. `run_server.py` 不再创建旧 `AgentService`，直接 `create_app(components)`。

**这是 P0 中的 P0**——修复后，链路 2、3、7 的根因自动解除。

## 1.6 不可行方案

- ❌ 保存 `native_components` 到 `app.state` 但保留 `service` 参数：双对象图共存导致路由需要判断用哪个——引入新分支而非消除分支。
- ❌ 通过 `service._native_components` 传递：私有属性访问，脆弱。

---

# 2. 差距 2：Coordinator 未注入路由

## 2.1 症状

`submit_run_turn(storage, ...)` 在路由中被调用时不带 `coordinator=` 参数。每次 fallback 到创建带 stub 的 coordinator。

## 2.2 直接原因

`server/routers/sessions.py:613`。`submit_run_turn()` 的 `coordinator` 参数是后来加的（G31），但调用方从未更新。

## 2.3 原始决策

`submit_run_turn` 的设计是一个"双模式"函数：有 coordinator → 走 Native，无 coordinator → fallback 自建。这个设计是 G31 "Production Run Cutover" 的过渡策略——允许旧路由逐步迁移。

但 G31 之后经过了 G37（删除旧 EventBus 启动路径），过渡期已结束。

## 2.4 CC 对齐

**F8**：Coordinator 是唯一流程 Authority。路由不应直接操作 DB（`_storage`），而应通过 Coordinator 发出 Command。

## 2.5 根治方案

**消除 `submit_run_turn` 的双模式，删除 fallback 路径。**

具体变化：

1. `submit_run_turn` 的 `coordinator` 参数从可选改为必选——去掉 fallback 分支（Path B，lines 56-84）。
2. 路由从 `app.state.components.run_coordinator` 获取 coordinator。
3. 删除 `submit_run_turn` 中创建 stub RuntimePorts 的代码（`_stub_llm()` 等 7 个 stub 工厂函数）。

## 2.6 不可行方案

- ❌ 在路由中判断 `if hasattr(service, '_native_components')`：脆弱，依赖私有属性。
- ❌ 保留 fallback 但加 deprecation warning：产生噪音，不解决根本问题。

---

# 差距 3：Web 执行走旧 SessionRuntime

## 3.1 症状

ChatPipeline 调用 `self._runtime.run_session()` → `SessionRuntime.run_session()`。`RunCoordinator.execute()` 在 web 流程中是死代码。

## 3.2 直接原因

`server/services/agent_service.py:~970`。ChatPipeline 构造时接收的 `runtime` 是 `SessionRuntime` 实例，不是 `AgentRuntime`。

## 3.3 原始决策

`ChatPipeline` 是整个代码库中最古老的类之一——它在 `SessionRuntime` 出现之前就已存在，后来才接入 `SessionRuntime`。当 Native Runtime（`AgentRuntime` + `StepLoop`）出现时（G15-G20），`ChatPipeline` 从未被更新——因为当时 Native Runtime 还没有真实后端（H0-H8 才解决）。

## 3.4 CC 对齐

**F1**：Hook 嵌入 agent loop 是同步拦截点。CC 的执行循环是 `model → action → PreToolUse gate → tool execution → PostToolUse → next model call`。StepLoop 的 `execute()` 实现了这个完全相同的循环。

**F8**：Spoke（Runtime）执行模型循环，不直接通知 Client。Gateway 只做 transport。

## 3.5 根治方案

**ChatPipeline 在检测到 `native_components` 时，使用 `RunCoordinator.execute()` 替代 `SessionRuntime.run_session()`。**

具体变化：

1. ChatPipeline 构造时增加 `coordinator` 参数（可选，来自 `native_components`）。
2. 在执行路径中增加分支：`if self._coordinator → coordinator.execute(cmd)` else `self._runtime.run_session()`。
3. 构建 `RuntimeExecution` 时从 `ChatPipeline` 上下文中填充真实的 `conversation` 和 `capabilities`。
4. 执行完成后调用 `coordinator.finalize(outcome)` 将 terminal state + fact 写入 UoW/Outbox。

## 3.6 不可行方案

- ❌ 删除 `SessionRuntime`：旧代码中仍然有测试和离线工具依赖它。保留为 legacy path，但 web 执行不经过它。
- ❌ 在 `agent_service.py` 中拦截 `run_session` 调用并重定向：在调用方打补丁而非修复被调用方。

---

# 差距 4：Runtime 端口 4/5 是 no-op

## 4.1 症状

`_RealLLM` 返回假文本，`_RealTools` 返回假输出，`_RealLiveEvents.publish()` 是 `pass`，`_RealTokenUsage.record()` 是 `pass`。

## 4.2 直接原因

`composition/runtime_composition.py:289-292`。每一个端口在 `assemble()` 中都是 `_Real*(None)` 或 `_Real*(pass)`。

## 4.3 原始决策

G28（Typed Composition Root）的设计原则是：**先让类型正确，再让行为正确**。`assemble()` 要产生一个类型完整的 `ApplicationComponents`，其中每个字段都是正确的类型。至于这些类型的实例是真实实现还是 stub，在 G28 时属于"后续阶段"的工作。

H0-H8 完成了 stub 消灭的准备工作（H1 做了 `_invoke_via_backend` 适配器，H2 做了 `_execute_via_registry` 适配器），但**从未在 `assemble()` 中实际注入真实依赖**。`assemble()` 仍然传 `backend=None` 和 `tool_lookup=None`。

## 4.4 CC 对齐

**F1-F5**：CC 的 Runtime 在启动时就知道所有 hook、工具和模型配置。它不是"运行时动态发现"——配置在进程启动时全部加载完毕。Grace Code 的等价物是：`assemble()` 在进程启动时必须完成全部依赖注入。

## 4.5 根治方案

**`assemble()` 接受真实依赖参数，在进程启动时一次性注入。**

具体变化：

1. `assemble(db_path, *, llm_backend=None, tool_registry=None)` —— 接受可选的真实后端。
2. 在 `run_server.py` 中，创建真实 LLM backend（从 `agent.llm` 现有的 provider 配置）和 tool registry（从 `BaseTool` 子类扫描），传入 `assemble()`。
3. 测试中继续使用 `assemble(db_path)`（不传参数 → fake 模式），但 fake 模式不再是默认——没有真实后端时，`assemble()` 应该直接报错或返回明确标记为 "degraded" 的对象图。
4. `_RealLiveEvents.publish()` → 调用 `self._bus.publish(message)`。
5. `_RealTokenUsage.record()` → 通过 UoW 写入 Outbox（创建 `token.usage.v1` fact）。

## 4.6 不可行方案

- ❌ 在 `run_server.py` 中 mutate `components.runtime_ports.llm = real_llm`：破坏 frozen dataclass 的不变性。
- ❌ 全局 singleton backend：与 Composition Root 的单次构造原则冲突。

---

# 差距 5：Hook Registry 为空 + Dispatcher 被绕过

## 5.1 症状

`HookRegistry` 在 `assemble()` 中创建后没有 `register()` 任何 hook。`get_hooks()` 始终返回空列表。Web 流程也不经过 HookDispatcher。

## 5.2 直接原因

`composition/runtime_composition.py:200-201`。创建了 `HookRegistry` 和 `HookDispatcher`，但没有加载 hook 配置。

## 5.3 原始决策

Hook 配置加载最初在 `hooks/registry.py` 中实现（`load_from_settings()`），后来在 `entry/bootstrap/hook_bootstrap.py` 中调用。当 `hook_core/registry.py`（新的 `HookRegistry`）出现时（G12），配置加载从未被迁移。

这是一个**功能迁移遗漏**——新 Registry 类已实现，但旧配置加载代码还在用旧 Registry。

## 5.4 CC 对齐

**F6**：CC 从 `settings.json` 多层合并加载 hook 配置（`~/.claude/settings.json`、`.claude/settings.json`、`.claude/settings.local.json`、managed policy、plugin hooks.json、skill frontmatter）。hook 配置包括 matcher、command/args、timeout。

**F2**：所有匹配 hook 并行执行。`dispatch_async()` 必须被使用。

## 5.5 根治方案

**在 `assemble()` 时加载 hook 配置到 `HookRegistry`，并切换 `dispatch()` → `dispatch_async()` 为默认。**

具体变化：

1. 在 `assemble()` 中，`hook_registry = HookRegistry()` 之后，调用 `_load_hooks_from_settings(hook_registry, repo_path)`。
2. `_load_hooks_from_settings` 读取 `.grace/settings.json` 的 `hooks` 字段，按 CC 的 matcher → hooks 结构解析，对每个 command hook 创建 `HookCommand(argv=...)` 并 register 到 `HookRegistry`。
3. `_RealHooks.check()` 改为调用 `dispatch_async()` 而非 `dispatch()`（或在 sync `check()` 中保持 sync dispatch 但标记为过渡）。
4. StepLoop 中对多 hook 场景使用 `dispatch_async` 的并行能力。

## 5.6 不可行方案

- ❌ 继续使用旧 `hooks/registry.py` 加载配置：旧 Registry 与新 HookDispatcher 类型不兼容。
- ❌ 在 `dispatch()` 中自动 fallback 到 `dispatch_async()`：增加无意义的分支。

---

# 差距 6：EventBus 完全是死代码

## 6.1 症状

`ScopedEventBus` 对象存在但 `publish()` / `async_publish()` / `subscribe()` 都从未被调用。`_RealLiveEvents.publish()` 是 `pass`。

## 6.2 直接原因

`composition/runtime_composition.py:222`。`publish()` 方法体是 `pass`。

## 6.3 原始决策

ScopedEventBus 的 G5-G7 重建完成后，它被正确地创建并放入 `ApplicationComponents`。但**谁调用它**这个问题从未被解决。设计文档（原 38→90+ 规划）说 `Runtime → non-authoritative live fact → ScopedEventBus`，但没有明确谁来桥接 `RuntimePorts.live_events` 和 `ScopedEventBus.publish()`。

G28 组装时把 `bus` 传给了 `_RealLiveEvents(bus)`，但 `publish()` 的实现被留空。

## 6.4 CC 对齐

CC 没有公开的"EventBus"概念——CC 的内部架构未公开。但根据 **F8**（Hub-Spoke-Client 分离）和 **F1**（hook 嵌入 agent loop），可以推导：

- Tool progress、model delta、hook warning 这些 live event 是 non-authoritative 的——丢失后可以从 Trace projection 恢复。
- 它们应该通过 exact-scope 的 publish/subscribe 到达 Gateway（WebSocket），而不是通过 durable Outbox。
- Live event 的订阅者（WebSocket gateway）在连接建立时注册，断开时注销。

Grace Code 的 EventBus 设计（G5-G7）已经满足这些要求——只是没有人调用它。

## 6.5 根治方案

**`_RealLiveEvents.publish()` 调用 `self._bus.publish(message)`。在 `assemble()` 中注册 WebSocket gateway 为 subscriber。**

具体变化：

1. `_RealLiveEvents.publish()` 实现：
```python
def publish(self, event_type, payload):
    from eventing.publisher import ScopedMessage
    # payload is FrozenJsonObject; wrap in minimal ScopedMessage
    # scope comes from the execution context — need to pass it
    ...
```

2. 在 `assemble()` 中注册 subscriber：
```python
bus.subscribe("tool.executed.v1", ws_gateway.on_event, "ws", scope=...)
bus.subscribe("run.completed.v1", ws_gateway.on_event, "ws", scope=...)
```

3. 订阅的生命周期与 WebSocket 连接绑定——Gateway 的 `subscribe()`/`unsubscribe()` 方法已实现。

## 6.6 不可行方案

- ❌ 让 LiveEvent 走 Outbox：Live Event 是非权威的、可丢失的——与 Outbox 的至少一次投递语义冲突。
- ❌ 让 Gateway 直接 import EventBus：Gateway 是 transport adapter，不应该直接操作 EventBus——应该通过 `LiveEventPort` 接口。

---

# 差距 7：Outbox 只收到 submitted 事件

## 7.1 症状

每个 run 只有 `run.submitted.v1` 进入 Outbox → Relay → Projection。`run.started.v1`、`run.completed.v1` 等从未产生。

## 7.2 直接原因

`RunCoordinator.execute()` 和 `finalize()` 在 web 流程中是死代码（差距 3）。`SessionRuntime.run_session()` 不通过 Coordinator/UoW 写入 Outbox。

## 7.3 原始决策

同差距 3。这是差距 3 的直接后果——不需要独立的根因分析。

## 7.4 CC 对齐

**F9 + F10**：CC 没有公开的 Outbox 实现，但 CloudEvents + Transactional Outbox 模式要求 state 和 fact 在同一事务提交。Grace Code 的 `RunCoordinator.submit()` 和 `finalize()` 已经实现了这一点——只是没被调用。

## 7.5 根治方案

同差距 3——切换到 Native Coordinator 执行路径后，`execute()` 和 `finalize()` 将自动产生完整的事件流。

---

# 差距 8：5/7 端口在 Composition 中处于死/假状态

## 8.1 症状

LLM、Tools、LiveEvents、TokenUsage、Cancellation 五个端口在 `assemble()` 中都是 no-op 实现。

## 8.2 直接原因

同差距 4。这是差距 4 在 composition 层面的反映。

## 8.3 原始决策

同差距 4——G28 的类型优先策略。

## 8.4 CC 对齐

**F1-F5**：CC 的 Runtime 启动时已知所有配置。`assemble()` = CC 进程启动。

## 8.5 根治方案

同差距 4——`assemble()` 接受真实依赖参数。

---

# 9. 三阶段根治路线图

## 阶段 A：消除 Service Locator，接入 Component Injection（2 P0）

**做什么**：删除 `create_app` 对 `AgentService` 的依赖，改为接收 `ApplicationComponents`。路由通过 `app.state.components` 获取 Coordinator。

**修哪个根因**：差距 1 的原始决策——向后兼容旧 AgentService 路径。G29-G44 + H0-H8 已完成后，兼容不再需要。

| # | 文件 | 动作 |
|---|---|---|
| A1 | `server/main.py` | `create_app(components: ApplicationComponents)` — 删除 `service` 参数 |
| A2 | `run_server.py` | 删除旧 `AgentService` 创建，直接 `create_app(components)` |
| A3 | `server/routers/sessions.py` | 路由从 `app.state.components` 获取 coordinator |

## 阶段 B：激活 Coordinator 执行路径（3 P0）

**做什么**：消除 `submit_run_turn` 的双模式，ChatPipeline 增加 native 分支。

**修哪个根因**：差距 2、3、7 的原始决策——双模式过渡策略和 ChatPipeline 从未更新。

| # | 文件 | 动作 |
|---|---|---|
| B1 | `server/services/run_submission.py` | `coordinator` 必选；删除 fallback + stub 工厂 |
| B2 | `server/services/chat_pipeline.py` | 增加 `coordinator.execute()` 分支 |
| B3 | `application/coordinators/run_coordinator.py` | `execute()` 填充 conversation + capabilities |

## 阶段 C：注入真实依赖到 Composition Root（5 P0 + 3 P1）

**做什么**：`assemble()` 接受真实 LLM backend、tool registry、hook 配置、event bus subscriber。

**修哪个根因**：差距 4、5、6、8 的原始决策——G28 的类型优先策略。

| # | 文件 | 动作 |
|---|---|---|
| C1 | `composition/runtime_composition.py` | `assemble(db_path, *, llm_backend, tool_registry, hook_settings)` |
| C2 | `composition/runtime_composition.py` | `_RealLiveEvents.publish()` → `self._bus.publish()` |
| C3 | `composition/runtime_composition.py` | `_RealTokenUsage.record()` → Outbox write |
| C4 | `composition/runtime_composition.py` | 加载 hook 配置到 `HookRegistry` |
| C5 | `composition/runtime_composition.py` | 注册 WebSocket subscriber 到 EventBus |

---

## 9.1 阶段依赖

```text
阶段 A (消除 Service Locator)
  └→ 阶段 B (激活 Coordinator)
       └→ 阶段 C (注入真实依赖)
```

阶段 A 必须先做——没有它，`ApplicationComponents` 无法到达路由和 ChatPipeline。阶段 B 是执行切换——完成后 web 请求流经完整 Native 链路。阶段 C 是端口激活——完成后整个系统产生真实输出。

## 9.2 不可违背的纪律

1. **禁止保留旧 AgentService 作为主路径**：阶段 A 完成后，`create_app` 不再接受 `AgentService` 参数。旧 AgentService 可保留为内部实现细节，但路由不得直接依赖它。
2. **禁止在 `submit_run_turn` 中保留 fallback**：阶段 B 完成后，`submit_run_turn` 必须有 `coordinator` 参数且为必选。
3. **禁止在 `assemble()` 中保留 `backend=None` 的默认行为**：阶段 C 完成后，`assemble()` 如果没有真实后端必须明确报错或返回 `degraded=True` 标记。
4. **每阶段 ≤ 3 文件修改**：如原规划书。
5. **所有端口修改必须有对应的 fake adapter 测试**。

---

> **文档结束。执行路线：阶段 A → B → C。共 11 个文件修改，3 个阶段，消灭全部 10 个 P0 + 3 个 P1。**
