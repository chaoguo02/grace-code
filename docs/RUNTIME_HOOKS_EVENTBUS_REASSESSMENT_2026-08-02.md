# Runtime / Hooks / EventBus P1–P28 第二次复审报告

> 文档版本：1.0.0  
> 复审日期：2026-08-02  
> 对比基线：`RUNTIME_HOOKS_EVENTBUS_P1_P28_COMPLETION_AUDIT_2026-08-02.md`  
> 审查对象：基线审计之后的未提交工作区修改  
> 最终结论：**有实质进展，但仍为 REJECTED；不得开启 NATIVE 生产模式**

---

# 1. Executive Summary

## 1.1 新结论

本轮不是纯文档修饰，确实关闭了上次审计中的部分真实缺陷：

1. Native run submission 不再使用空 `append_fact()`，已经能在一个 SQLite 事务内写 Run、User Message 和 typed Outbox fact。
2. SchemaRegistry 增加 typed decode，canonical JSON 测试已经从“只做 json.loads”提升为对象重建验证。
3. ScopedEventBus 增加 Session/Task scope 订阅过滤和 close 时的 subscription 清理。
4. Native pipeline 已接通 typed decode → ScopedEventBus → Trace/Stats/WS projection，并增加两组 integration tests。
5. Hook contracts、matcher、registry 和 dispatcher 的事件覆盖面明显扩大。

但整体仍未达到 CC-Native 完成条件，且新增了一项严重迁移风险：旧 Outbox Relay 与 Native Relay 可能同时运行并竞争同一张 outbox 表；Native delivery callback 吞掉异常后正常返回，Relay 随后会把实际未投影的事件标记为 delivered。

复审评分：

- 上次：**27 / 100**。
- 本次：**38 / 100**。
- 生产就绪阈值：至少 85/100，且所有 P0、AC-1、AC-11～21、AC-24～26、AC-36～42、AC-54～60 必须通过。
- 当前阶段判定：约 **2/28 PASS、14/28 PARTIAL、12/28 FAIL/NOT IMPLEMENTED**。

因此准确表述应为：

> Native submission、typed decode 和基础 projection pipeline 已从占位骨架提升为可测试实现；Runtime 核心、Hook 原生切换、EventBus 精确作用域语义、单一生产入口和旧代码删除仍未完成。

## 1.2 本轮验证结果

| 检查 | 结果 | 解释 |
|---|---|---|
| 修改相关专项测试 | PASS | application/eventing/hook/integration/architecture gate 全绿 |
| 全量 pytest | PASS | 约 272.7 秒，无 test failure |
| compileall | PASS | 新架构目录语法正确 |
| mypy strict | FAIL | 151 errors / 30 files；上次为 71 errors / 24 files |
| architecture CI | FAIL | `.github/workflows/architecture.yml` 仍不存在 |
| perf/fault/leak | FAIL | `tests/benchmarks`、`tests/faults` 仍不存在 |
| 旧代码删除 | FAIL | 旧 EventBus、HookContext、HookDispatcher、SessionRuntime 全部仍可 import |

全量 pytest 通过是积极信号，但不能替代 Native 验收：默认模式仍是 LEGACY，绝大多数历史测试仍运行旧路径。

---

# 2. 上次问题的关闭情况

## 2.1 已实质修复

### A. Native submission 的空事务

上次：

```python
def append_fact(self, envelope) -> None:
    pass
```

本次实现已经加入：

- `increment_generation()`；
- `create_run()`；
- `insert_message()`；
- `SqliteOutboxStore.append(conn, envelope)`；
- 同一 connection 上的 BEGIN IMMEDIATE / commit / rollback；
- 返回真实 payload 中的 turn_id/turn_index。

评价：**从 FAIL 提升为 PARTIAL/PASS 边界**。基础原子提交成立，但 idempotency/active-run pre-check 仍在事务外，存在检查与写入之间的竞争窗口；最终仍需依赖数据库唯一约束和明确错误映射证明并发语义。

### B. Schema typed decode

SchemaRegistry 现在可以从 canonical JSON 重建 EventEnvelope 和具体 payload，并增加 round-trip 测试。

评价：**AC-3 基本关闭**。但 AC-1/2/5 仍失败：registry 和 hook inputs 继续使用 `Any`、裸 dict、mutable list/dict。

### C. Session/Task cross-scope filtering

`ScopedEventBus.subscribe()` 现在接收 ScopeToken，publish 会调用 `_scope_matches()`；测试已把 Session B 的错误断言由 1 修正为 0，并新增 Task A/Task B 隔离。

评价：**修复了最直接的跨 Session 泄漏**，但没有满足原 V2.0 的完整 exact-scope 语义，详见 P0-2。

### D. 基础 Native delivery integration

新增测试覆盖：

```text
EventEnvelope
  -> canonical JSON
  -> SchemaRegistry.decode
  -> ScopedEventBus.publish
  -> Trace / Stats / WsGateway
```

评价：从 composition 0% coverage 提升为基础路径可执行。但失败路径和生产双 Relay 场景未覆盖。

## 2.2 尚未关闭

- Runtime Core 仍为固定 `{"content": "ok"}` 的 skeleton。
- callable Hook timeout 仍是 handler 返回后再计算耗时，无法在 deadline 主动返回。
- 旧 HookContext/HookDispatcher/registry 仍在生产路径。
- 旧 EventBus 仍由 `server/main.py` 和 `run_server.py` 实例化。
- `GRACE_RUNTIME_MODE` 默认仍为 LEGACY。
- P20–P24 规划中的 MultiAgentCoordinator、ContextAssembler、ConversationService、WorkspaceLeaseService、EvidenceCollector 等目标模块仍不存在。
- P25–P28 的删除、性能、故障和泄漏验收仍未执行。

---

# 3. 当前 P0 阻断

## P0-1：Native delivery 吞异常后会产生 false ACK

### 代码链路

`composition/runtime_composition.py` 的 `_deliver(record)`：

```python
try:
    envelope = registry.decode(record.payload_json)
    bus.publish(envelope)
except Exception:
    logger.warning(...)
```

`infrastructure/outbox/relay.py` 的行为是：

```python
self._deliver(record)
self._store.mark_delivered(record.event_id, self._worker_id)
```

因为 `_deliver()` 捕获异常后不再抛出，Relay 会认为投递成功并执行 `mark_delivered()`。

### 复现场景

1. Outbox 中存在旧 DomainEvent JSON，SchemaRegistry 无法按新 envelope 解码。
2. Native Relay claim 该记录。
3. `_deliver()` 记录 warning 并正常返回。
4. Relay 把记录标记 delivered。
5. Trace/Stats/WS 均未收到事件，且记录不会重试/DLQ。

### 判定

**Critical，禁止生产启用。**

### 正确修复

- Delivery callback 不得吞异常；必须把 typed DeliveryFailure 抛回 Relay。
- Projection 的失败必须聚合为明确 delivery result；只要 required projection 失败，不能 ack。
- best-effort WS 与 durable Trace 的策略必须区分；不能由一个 catch-all 决定全部成功。
- 增加测试：decode failure、Trace failure、Stats failure、WS failure、poison+good、retry→DLQ。

## P0-2：Scope 语义被改成隐式 Global bubbling

V2.0 的 AC-11 明确要求：Task A event 只被 Task A subscriber 收到，Session、Task B、其他 Session、Global 均收不到。

当前 `_scope_matches()` 定义：

```python
if sub_scope is None:
    return True
if sub_scope.kind == ScopeKind.GLOBAL:
    return True
```

测试又新增 `test_global_subscriber_receives_all()`，把 Global 接收所有子 scope 事件固定成正式行为。

这不是 exact-scope routing，而是隐式向 Global 广播。它与原设计“无 implicit bubbling；跨层只能由 Coordinator 新建 parent fact”直接冲突。

此外仍有以下缺陷：

- `_scope_matches()` 不比较 global_id；
- 不比较 generation；
- `ScopeTree.ensure_session()` 升级 generation 时复用已 closed node，closed 状态没有重置；
- 无 scope 的订阅被定义为 catch-all，形成逃逸通道；
- EventBus 仍直接 import `application.events.envelope`，违反基础设施不依赖业务 schema 的方向。

### 正确修复

- 删除 `scope=None` catch-all production subscription。
- GLOBAL subscriber 只接收 GLOBAL event。
- SESSION subscriber 只接收完全相同的 Session ScopeToken identity。
- TASK subscriber 只接收完全相同的 Task ScopeToken identity。
- Projection 若需要全局聚合，应由 composition 显式为每个 scope 注册，或消费 durable outbox stream；不能破坏 in-process scope isolation。
- generation 切换必须创建新 node/token，旧 node 永久 closed。

## P0-3：旧、新 Relay 可能竞争同一 Outbox

`run_server.py` 启动流程仍然：

1. 创建旧 `server.services.event_bus.EventBus`；
2. 创建 AgentService，旧 OutboxRelay 仍由旧 composition 管理；
3. 当 `GRACE_RUNTIME_MODE=NATIVE` 时，再调用 `start_native_pipeline()` 启动新 Relay。

两套 Relay 可能 claim 同一 `event_outbox` 表的记录。旧、新 payload schema 也不完全相同。结合 P0-1 的 false ACK，这会造成不可预测的投影缺口。

### 正确修复

- 一个进程只能有一个 authoritative relay owner。
- Native 模式必须从 composition root 选择完整对象图，不能在旧对象图启动后追加第二套 pipeline。
- legacy/shadow/native 三种 mode 的启动必须互斥。
- 加入进程级 owner/lease 测试和“两个 relay 同库启动必须失败”测试。

## P0-4：HookBridge 违反 From-Scratch 和零兼容层约束

新增 `hook_core/bridge.py` 明确自称：

> “旧 HookDispatcher 接口兼容层”

它继续 import 旧 `hooks.dispatcher`、`hooks.events.HookContext` 和旧 registry，把旧 callback 包装后注入新 dispatcher。这违反规划书：

- 禁止兼容层；
- Hook 不得继续使用 generic HookContext；
- P26 后旧 Hook API 必须无法 import；
- 零隐式依赖。

同时 callable Hook timeout 仍未真正实现：`handler(hook_input)` 同步返回后才检查耗时。一个阻塞 callable 仍会阻塞整个 Runtime。

### 正确修复

- 不继续完善 HookBridge；将其视为临时迁移产物并排入删除。
- 逐调用点改为 typed Hook input/decision，不通过旧 HookContext 转换。
- external command hook 使用 argv、`shell=False`、ProcessRegistry/CancellationHandle；当前 `shell=True` 也违反 Shell 安全设计。
- callable Hook 必须运行在可受控 deadline 的执行边界；无法 kill 的 in-process callable 不应被当成强超时安全边界。

## P0-5：Runtime 仍未实现 Agent 执行语义

`runtime_core/step_loop.py` 仍然：

- 不调用 `LLMPort.invoke()`；
- 硬编码 assistant response；
- 没有 HookDispatcher port；
- cancellation 永不改变；
- tokens 永远为 0；
- 没有模型流、tool schema、tool result、context budget 或 terminal reason 的真实映射。

因此 P13、P19、P27 仍不能通过。Native run submission 现在可以正确“创建 Run”，但不能证明新 Runtime 能执行该 Run。

---

# 4. 类型、打包和 CI 复审

## 4.1 Strict typing 退化

命令：

```powershell
python -m mypy --strict --explicit-package-bases `
  application runtime_core hook_core eventing listeners infrastructure composition
```

结果：

- 上次：71 errors / 24 files。
- 本次：151 errors / 30 files。

新增错误主要来自：

- HookBridge 把旧 hooks/core 包带入依赖图；
- 裸 `dict`、`list`、Callable、EventEnvelope 缺失类型参数；
- decision union 字段不一致；
- `PostToolUseDecision` 调用参数与定义不一致；
- composition 把返回 DeliveryReceipt 的 handler 注入声明返回 None 的 subscriber；
- `RunCoordinator(runtime, None)` 与构造器契约冲突。

结论：AC-1、AC-2、AC-23、AC-60 仍失败。

## 4.2 CI gate 未落地

`.github/workflows/architecture.yml` 不存在；现有 workflow 没有执行：

- mypy strict；
- import-linter；
- architecture import contract；
- Any/dict/static boundary gate；
- fault/benchmark/leak tests。

因此任何人仍可提交旧依赖、兼容层或 catch-all EventBus，而 CI 不会阻断。

---

# 5. 更新后的阶段判定

| 阶段 | 上次 | 本次 | 变化 |
|---|---|---|---|
| P2 Schema | PARTIAL | PARTIAL+ | typed decode 已实现；Any/mutable payload 未清 |
| P3 Registry | PARTIAL | PARTIAL+ | decode 能力增强；strict typing 仍失败 |
| P5 Scope | FAIL | PARTIAL | Session/Task filter 修复；Global bubbling/generation 仍错 |
| P9–P11 Hooks | FAIL | PARTIAL | typed 事件覆盖增强；Bridge/假 timeout/shell=True 阻断 |
| P14 Coordinator | FAIL | PARTIAL+ | Run+Message+Fact 同事务已有基础实现；CAS/finalize/并发仍缺 |
| P18 Composition | FAIL | PARTIAL | 基础 pipeline 已接线；错误 ack/双 Relay 阻断 |
| P19 Run cutover | FAIL | PARTIAL | submission 可工作；默认旧链路，Runtime execution 未切 |
| P20–P24 | NOT/PARTIAL | 无实质变化 | 目标模块仍缺失 |
| P25–P28 | NOT IMPLEMENTED | NOT IMPLEMENTED | 旧代码、flags、perf/fault/leak 均未清 |

---

# 6. 下一轮执行顺序

不要继续扩展 HookBridge，也不要直接删除旧 Runtime。下一轮只按以下顺序推进：

## N0 — 先封住生产数据风险

目标：在修好前确保 Native 不可能与旧 Relay 同时启动。

验收：

- 同一 composition root 只能选一个 relay owner；
- delivery exception 必须导致 reschedule/DLQ，绝不能 mark_delivered；
- 新增 decode/projection failure integration tests；
- 禁止通过 catch Exception 后 return 让测试变绿。

## N1 — 恢复原始 exact-scope 契约

目标：删除 Global bubbling 和无 scope catch-all。

验收：

- Task event 只到同 Task；
- Session/Global 均收不到 Task event；
- global_id、session_id、task_id、generation 全部参与 identity；
- 1000 child close 后 subscription/node/task retained=0。

## N2 — Hook Native 化，不做 Bridge

目标：生产调用点直接使用 hook_core typed inputs/decisions。

验收：

- `hook_core/bridge.py` 删除；
- 旧 HookContext/dispatcher 无生产 import；
- command hook `shell=False`；
- callable/command deadline、fail-open/closed 有真实故障测试；
- strict mypy 的 hook_core 错误为 0。

## N3 — 实现真实 Runtime loop

目标：fake LLM/Tool/Hook/Cancellation 的确定性执行链先完整成立，再接真实 adapter。

验收：

```text
ContextSnapshot
  -> LLMPort.invoke
  -> typed model action
  -> PreToolUse Hook
  -> ToolPort.execute
  -> PostToolUse Hook
  -> next turn / terminal RuntimeOutcome
```

不得存在硬编码 response、恒 false cancellation 或恒 0 token。

## N4 — 完成 Coordinator/CAS/Outbox fault matrix

目标：补齐 terminal state、cancel_requested、CAS、crash recovery、content conflict。

验收：AC-36～42 全通过。

## N5 — 单入口 Native cutover

目标：composition 在进程启动前选择唯一对象图；不追加第二套 pipeline。

验收：

- Native 生产 E2E 执行真实 Run；
- 默认不再是 LEGACY；
- shadow 不执行真实 tool、不写权威表；
- digest 一致率报告满足 99.5%。

## N6 — P20–P24 业务提取

完成 Multi-agent、Context、Conversation、Workspace、Evidence、Memory、Resource、Approval 的 Coordinator/Service 提取。

## N7 — P25–P28 删除与最终门禁

只有 N0–N6 全通过后执行：

- 删除旧 EventBus；
- 删除旧 Hooks/HookContext；
- 删除旧 SessionRuntime 与 flags；
- 添加 architecture CI；
- 添加 benchmark/fault/leak tests；
- strict mypy/import-linter/pytest/Web build 全部通过。

---

# 7. 当前可发布决策

| 决策 | 结论 |
|---|---|
| 合并本轮作为中间开发分支 | 可以，但必须标注 incomplete，并先修 P0-1/P0-3 |
| 开启 `GRACE_RUNTIME_MODE=NATIVE` | **禁止** |
| 宣称 P1–P28 完成 | **禁止** |
| 删除旧 Runtime | **暂时禁止**，新 Runtime 尚不能执行真实 Agent run |
| 继续完善 HookBridge | **禁止**，应迁移调用点并删除 Bridge |
| 下一步首要任务 | N0：单 Relay owner + delivery failure 正确重试/DLQ |

最终判断：本轮把实现从“不可运行骨架”推进到了“部分 Native 基础设施可执行”，但尚未跨过生产架构切换线。

