# R3/R4 完成度复审与下一阶段指导

> 审计日期：2026-08-02  
> 审计基准：`R3_R4_RUNTIME_EVENTBUS_EXECUTION_PLAN.md`  
> 审计方式：代码定位、生产链路追踪、静态门禁、全量测试、最小故障复现。

## 1. Executive Summary

当前实现不能判定为 R3/R4 完成。

| 评价对象 | 完成度 | 结论 |
|---|---:|---|
| R3 代码骨架 | 55% | DomainEvent、Outbox、UoW、Relay、Projection、Mapper 类已创建 |
| R3 生产链路 | 15% | Relay 未实例化，权威提交路径未迁移，Projection 存在确定性丢事件缺陷 |
| R3 验收测试 | 30% | 测试全绿，但未覆盖真实 transaction/relay/projection 组合，存在假绿 |
| R4 依赖反转 | 10% | 私有字段赋值改成 public setter，不是 Ports/IoC；无 RuntimeDependencies/Coordinator |
| Build/Plan/Multi-Agent 回归基线 | 100% | 全量 pytest 及 Web build 通过 |

建议将当前里程碑重新标记为：

> **R3 Infrastructure Skeleton Complete / Production Integration NOT Complete**

下一阶段必须执行 `R3-Recovery`，暂停继续开发 R4。

## 2. 验证结果

### 2.1 通过项

- 全量 `pytest -q`：通过。
- `npm run build`：TypeScript 与 Vite production build 通过。
- `python -m compileall`：通过。
- `git diff --check`：通过。
- Outbox/UoW 现有专项测试 21 项通过。

### 2.2 最小复现结果

#### P0：UoW 原子性被破坏

复现场景：business state 写入后调用 `OutboxStore.append_event()`，随后在 commit 前抛错。

实际结果：

```text
UOW_ATOMICITY_REPRO business_state=1 outbox=0
```

正确结果应为：

```text
business_state=0 outbox=0
```

代码原因：

- `server/services/session_uow.py:38` 已经执行 `BEGIN IMMEDIATE`。
- `server/services/event_outbox.py:95` 的 `append()` 和 `:117` 的 `append_event()` 再调用 `ensure_tables()`。
- `ensure_tables()` 使用 `sqlite3.Connection.executescript()`；该 API 会在执行脚本前提交现有 transaction。

因此状态写入可能提前 commit，后续 rollback 只能回滚 Outbox insert。

#### P0：Trace 首次投影被永久跳过

实际结果：

```text
TRACE_PROJECTION_REPRO result=False traces=0 receipts=1
```

代码原因：

- `event_outbox.py:217` 的 `record_projection()` 首次 insert 返回 `True`。
- `trace_projection.py:30` 将 `True` 解释为“Already projected”，立即 return。
- Receipt 已提交，Trace 为零；后续重试也会因 Receipt 已存在而跳过。

这是确定性事件丢失，不是边界概率问题。

## 3. 分阶段完成度

### R3.0 DomainEvent v2 — 60%

已完成：

- `server/domain_events.py` 提供稳定 envelope。
- 有 event_id、event_type、event_version、session/aggregate 信息。
- JSON round-trip 测试存在。

未完成：

- `payload: dict` 未约束键值类型，也没有 JSON-safe runtime validation。
- Concrete event 仍是工厂函数返回通用 DomainEvent，不是可穷举 sum type。
- Relay 未校验 `event_version`，未知版本不会进入 dead-letter。
- aggregate_version 多数固定为 1，尚未提供真实单调版本来源。

### R3.1 Outbox Repository — 40%

已完成：

- Schema、claim、ack、reschedule、dead-letter、receipt API 已创建。
- 双 worker 顺序 claim 基础测试存在。

阻断问题：

- `ensure_tables()` 在 append 时执行，破坏外层 transaction。
- `INSERT OR IGNORE` 没有校验“相同 event_id、不同 payload”冲突。
- `release_expired_claims(now=...)` 忽略 `now` 参数，并固定使用 60 秒。
- Claim 的 `lease_s` 没有存入记录，独立 release 无法遵循真实 lease。
- Claim 顺序只按 occurred_at，未保证 aggregate_version 顺序。
- OutboxRecord 非 frozen，字段 status 为自由字符串。

### R3.2 Session UoW — 20%

已完成：

- `SessionUnitOfWork` 类和演示测试存在。

未完成：

- 原子性已经被最小复现证明失效。
- `run_submission.py` 仍直接访问 `storage._store._connect()`。
- `submit_run_turn()` 没有写 `RunSubmitted` Outbox。
- UoW 没有 typed transaction/repository ports，只暴露 raw sqlite connection。
- 生产路径没有使用 SessionUnitOfWork。

### R3.3 Terminal Run 迁移 — 15%

已完成：

- `SessionStore.transactional_finalize_run_v2()` 已存在。

未完成：

- 生产代码没有调用该方法。
- `SessionRuntime._finalize_run()` 仍调用 `update_run()`，再直接调用 `_publish_run_terminal` callback。
- CAS 失败仍“ALWAYS”发布 terminal，可能产生无权威状态支持的假终态。
- 旧 `transactional_finalize_run()` 仍直接写 Trace。
- ChatPipeline 异常路径仍是先 WS、后 update_run 的双写。
- v2 event aggregate_version 固定为 1。

### R3.4 Relay / Projection / WS Mapper — 10%

已完成：

- `OutboxRelay`、`TraceProjection`、`map_domain_to_ws()`、`LiveEventSink` 文件存在。

阻断问题：

- `AgentService._outbox_relay` 只初始化为 None，没有任何赋值；生产 Relay 从未启动。
- `server/main.py` 只会尝试启动非 None relay，因此当前代码是 no-op。
- Relay 将 `record.payload` 传给 deliver callback，TraceProjection/Mapper 却要求完整 record，接口不兼容。
- Trace receipt 和 trace insert 不在同一 transaction。
- TraceProjection 捕获异常并返回 False；若 Relay callback 不抛错，Relay 会错误 ACK。
- 首次投影逻辑反转，会稳定丢失全部首次 Trace。
- LiveEventSink 未接 EventBus/WebSocket subscriber，没有生产消费者。
- Relay 同步执行 SQLite 和 callback，会阻塞 asyncio event loop。
- Stop 返回 delivered 数量，AgentService 将它命名为 remaining，关闭语义错误。

### R3.5 Raw 与双语义清理 — 20%

残留包括：

- `server/services/replay_service.py:279` 仍调用 `publish_raw()`。
- `event_bus.py` 仍公开 `publish()`、`publish_raw()`、`publish_typed()`。
- `_persisted_event` 仍在 Batch、Scheduler、Integration、Spawn 和 MultiAgentService 中传播。
- EventBus 仍同时持久化 Trace、缓存和 WebSocket 广播。
- `publish_typed()` 仍混合处理 Durable fact 与 Ws DTO。

### R3.6 Crash Recovery — 25%

测试名称覆盖了 crash、lease、poison、receipt，但没有验证真实生产链路。

其中 poison test 存在错误：第一次 `reschedule()` 后 claimed_by 已清空，后续 reschedule/dead_letter 实际不会更新；测试只验证事件当前没有被立即 claim，没有验证 status 真正为 dead_letter。

缺失测试：

- Projection commit 后、Outbox ACK 前崩溃。
- Receipt 与 Projection 的原子性。
- 真实 Relay + Trace + WS Mapper 组合。
- Unknown event version dead-letter。
- 慢 WS 不阻塞 durable ACK。
- aggregate 内有序投递。
- shutdown 后 claim lease 可恢复。

### R4 Runtime Ports — 10%

当前提交将私有字段写入改成以下 setter：

- `set_web_mode()`
- `set_stats_recorder()`
- `set_publish_run_terminal()`
- `set_evidence_event_callback()`
- `set_memory_event_callback()`
- `update_run()` facade

这不符合设计目标。它只是把可变 Service Locator 从 private 改成 public。

仍然缺失：

- frozen `RuntimeDependencies`。
- RunEventPort、StatsPort、EvidencePort、MemoryEventPort、RunStorePort。
- Application Coordinator。
- 构造期一次性依赖注入。
- 删除 callback setter。
- `agent/` 到 `server/` 的反向依赖清理。

此外仍有实际私有穿透：

- `server/routers/sessions.py` 三处访问 `runtime._store`。
- AgentBatch、TaskTool、Scheduler、Subagent 多处访问 Runtime 私有 store/config/registry。
- `agent/session/session_store.py` 反向 import `server.services.event_outbox`。

## 4. 下一阶段：R3-Recovery

R3-Recovery 分为四个强制顺序批次。每个批次必须独立全绿后才能继续。

### Batch A — 修复事务与 Projection 基础

优先级：P0。预估 2 人日。

#### A1. Schema migration 与 append 分离

- 将 Outbox schema 纳入 SessionStore/SchemaMigrator 启动 migration。
- 删除 `append()`、`append_event()` 内的 `ensure_tables()`。
- Repository append 只使用调用方 connection，不 commit、不执行 DDL。
- 增加不同 payload 的 event_id 冲突检测。

验收：

```text
business_state=0 outbox=0
```

#### A2. Projection 与 Receipt 原子提交

在同一个 transaction 中：

1. `INSERT OR IGNORE projection_receipt`。
2. 若 rowcount == 0，判定已处理并返回。
3. 写入 Trace Projection。
4. commit。

Projection 失败必须 rollback 并向 Relay 抛异常，禁止吞掉异常返回 False。

验收：

- 首次投递：1 receipt + 1 trace。
- 重复投递：仍为 1 receipt + 1 trace。
- Trace insert 失败：receipt 为 0。

#### A3. 修正测试假绿

- UoW rollback test 同时查询 business state 和 outbox。
- Poison test 每次重新 claim，再 reschedule；最后查询 status=dead_letter。
- 新增 TraceProjection 首次/重复/失败三项测试。

### Batch B — 打通真实生产 Relay

优先级：P0。依赖 Batch A。预估 2 人日。

- Relay callback 接收 `OutboxRecord`，禁止只传 payload。
- 新增 ProjectionRunner：先做所有 durable projection，再做 best-effort WS mapping。
- Durable projection 任一失败，不得 mark_delivered。
- WebSocket 无订阅者不影响 ACK。
- AgentService 构造真实 OutboxStore、TraceProjection、Mapper、Relay。
- Lifespan start/stop 使用明确的 Relay port，不用 getattr(None) no-op。
- SQLite claim/project 放入 worker thread，避免阻塞 event loop。
- 修正 stop 返回值为 `undelivered_count` 或 typed ShutdownResult。

验收：启动真实 FastAPI lifespan 后，提交一条 Outbox event，最终得到 delivered + trace；重启可补投。

### Batch C — 迁移权威 Command 路径

优先级：P0。依赖 Batch B。预估 2.5 人日。

按顺序迁移：

1. `submit_run_turn()`：Run + Message + RunSubmitted。
2. Runtime terminal：Run CAS + RunCompleted/Cancelled/Failed。
3. ChatPipeline exception：RunFailed command。
4. Child/Batch/Delegation terminal。

规则：

- CAS 成功才产生 terminal fact。
- CAS 失败不得发布第二个 terminal。
- Runtime 不再直接构造 WsRunTerminal。
- 删除未使用的 v2/legacy 双实现，保留一个 canonical 方法。

### Batch D — 清除旁路

优先级：P1。依赖 Batch C。预估 1.5 人日。

删除：

- `publish_raw()`。
- `_persisted_event`。
- `skip_persist`。
- EventBus 的 durable trace persistence。
- Runtime terminal callback setter。

建立门禁：

```powershell
rg -n "publish_raw|_persisted_event|skip_persist" agent server
rg -n "transactional_finalize_run_v2|transactional_finalize_run\(" agent server
```

第一项必须为零；第二项必须只剩一个 canonical terminal transaction API。

## 5. R4 的正确启动条件

只有以下条件全部满足后才允许恢复 R4：

- Batch A-D 全部完成。
- Relay 真实启动并处理生产事件。
- 全部 terminal path 已事务化。
- Raw/persisted-event 旁路为零。
- Crash recovery 组合测试通过。

R4 第一阶段必须创建真实 Ports，而不是继续增加 setter：

```python
@dataclass(frozen=True)
class RuntimeDependencies:
    run_events: RunEventPort
    stats: StatsPort
    evidence: EvidencePort
    memory_events: MemoryEventPort
    run_store: RunStorePort
    web_mode: bool
```

Runtime 构造完成后禁止替换依赖。Application Coordinator 持有 UoW 和 command handler；Router 只能调用 Application Service，不得访问 Runtime Store。

## 6. 下一轮验收门禁

下一轮实现结束时至少执行：

```powershell
python -m compileall -q agent server hooks entry core llm hitl
pytest -q tests/test_outbox_repository.py
pytest -q tests/test_session_uow.py
pytest -q tests/test_trace_projection.py
pytest -q tests/test_outbox_relay_integration.py
pytest -q tests/test_terminal_outbox_atomicity.py
pytest -q tests/test_outbox_crash_recovery.py
pytest -q
cd web; npm run build
```

静态门禁：

```powershell
rg -n "publish_raw|_persisted_event|skip_persist" agent server
rg -n "runtime\._|_runtime\._" server
rg -n "from server" agent
```

最终目标分别为零。ApprovalBroker 等确需跨层的能力也应改成 agent-owned Port，而不是作为永久例外。

## 7. 最终指导

不要基于当前全量测试绿色继续宣告完成，也不要先做 R4 Coordinator。当前最有价值的下一步是 Batch A：先让 transaction 和 projection 语义真实成立，并把两个最小复现场景转成永久回归测试。

完成 Batch A 后重新审计，再决定是否进入 Relay 生产接线。
