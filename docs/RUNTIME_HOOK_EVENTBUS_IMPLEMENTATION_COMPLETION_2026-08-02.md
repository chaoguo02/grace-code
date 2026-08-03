# Runtime / Hooks / EventBus 完整链路实施完成度

日期：2026-08-02  
状态：主链已落地；P0 验收通过；剩余项均为 P1/P2 边界收口

## 1. 本轮结论

本轮不再以“类和接口存在”为完成标准，而以生产调用链实际经过为准。以下链路已经闭合：

```text
HTTP/Runtime command
  -> SQLite BEGIN IMMEDIATE
  -> business state CAS
  -> event_outbox INSERT（同一事务）
  -> COMMIT
  -> OutboxRelay claim/lease/retry/dead-letter
  -> TraceProjection（receipt + trace 同一事务）
  -> EventBus.publish_live（只广播，不二次持久化）
  -> WebSocket subscriber
```

覆盖的生产事实：

- `run.submitted`
- `run.started`
- 所有 Run 终态：completed / partial / gave_up / blocked / failed / cancelled
- `delegation.completed`，包含 one-to-one、AgentBatch、scheduler、integration coordinator

旧的 `publish_raw`、`_persisted_event` 和 Run terminal 直接写 trace 旁路已删除。CAS 失败不再发布伪终态。

## 2. 代码落点

| 责任 | 实现 |
|---|---|
| Outbox schema、claim、lease、retry、DLQ | `server/services/event_outbox.py` |
| receipt + trace 原子投影 | `server/projections/trace_projection.py` |
| 版本校验、Trace→Live 顺序 | `server/projections/projection_runner.py` |
| DomainEvent→WS DTO | `server/ws/event_mapper.py` |
| Relay 生产装配与生命周期 | `server/services/agent_service.py`, `server/main.py` |
| Run 提交事务 | `server/services/run_submission.py` |
| Run start/final CAS + Outbox | `agent/session/session_store.py` |
| Runtime 唯一终态调用 | `agent/session/runtime.py` |
| Delegation terminal CAS + Outbox | `agent/session/session_store.py` |
| immutable composition contract | `agent/session/runtime.py::RuntimeDependencies` |

## 3. 已修复的原始阻断项

### P0-1：DDL 隐式提交破坏 UoW

`executescript()` 不再出现在业务事务中的 append 路径。Outbox schema 由 `install()` 在事务外预安装。故障发生在 outbox append 后、commit 前时，业务状态与事件会一起回滚。

### P0-2：Projection receipt 与 trace 分裂

receipt 和 trace INSERT 现在共享同一个 `BEGIN IMMEDIATE`。投影异常会抛回 Relay，只有投影成功后才 ACK；重复事件不会产生第二条 trace 或第二次 live broadcast。

### P0-3：状态成功但事件缺失 / CAS 失败仍通知

Run submit/start/final 与 Delegation final 全部改为 state + outbox 单事务。终态 CAS 失败返回 False，不写 outbox、不广播。

### P0-4：Relay 未装配

`AgentService` 创建真实 `OutboxStore + TraceProjection + ProjectionRunner + OutboxRelay`；server lifespan 启停该实例。SQLite claim/projection 运行于 worker thread，不阻塞 asyncio event loop。

### P0-5：取消和异常旁路

活跃取消只 push cancellation，由 Runtime finally 持有终态权威；无活跃 Runtime 的 orphan/queued run 才由 application service 使用同一原子 finalizer。ChatPipeline 和 Plan approval 异常同样进入该 finalizer。

## 4. Runtime / Hook / EventBus 边界现状

### Runtime

- 使用 frozen `RuntimeDependencies` 在 composition root 一次性注入依赖。
- server 层不再访问 `runtime._*` 私有状态。
- Runtime 只决定执行和提交事实；不再直接构造/发送 Run terminal WS DTO。
- 内部 multi-agent 工具改用公开的 `session_store`、`agent_registry`、`root_agent_config` 端口。

### Hooks

- STOP、POST_RESPONSE、SESSION_START 和工具 hooks 均经过 HookDispatcher。
- Hook 保持同步决策语义；未发现 legacy `_goal_stop_hook` 旁路。
- Hook 不承担 durable fact 的异步投递职责。

### EventBus

- `publish_typed` 继续服务非关键即时 UI 事件。
- `publish_live` 仅广播已持久化的 projection；不写 trace。
- Run/Delegation 关键生命周期不再由 EventBus 持久化。
- replay control 使用 live broadcast，不污染 trace。

## 5. Team 能力移除

运行时 `AgentTopology.TEAM`、`team_enabled`、`team_approved` 已删除。`PEER_TO_PEER` 请求会落到 primary-mediated fan-out/fan-in。旧 SQLite 的 `is_team` 列暂时保留，仅为数据库兼容，不代表运行时能力。

支持面收敛为：

- build
- plan
- multi-agent：one-to-one、fan-out/fan-in、chain、nested（受策略限制）

## 6. 机器验收

新增或加强的测试：

- `tests/test_outbox_lifecycle_chain.py`
- `tests/test_outbox_crash_recovery.py`
- `tests/test_runtime_architecture_gates.py`
- `tests/test_runtime_dependencies.py`
- Delegation terminal / AgentBatch / cancellation / trace regression tests

验收结果：

- Python 全量 pytest：通过
- `python -m compileall -q agent server hooks`：通过
- Web `npm run build`：通过
- `git diff --check`：通过
- 架构 grep gate：`publish_raw`、`_persisted_event`、Team runtime flags、server→runtime 私有穿透均为零

## 7. 当前成熟度与下一阶段

| 区域 | 本轮前 | 当前 |
|---|---:|---:|
| Durable lifecycle / Outbox | 15% | 90% |
| Runtime 边界 | 55% | 82% |
| Hooks 纯度 | 75% | 88% |
| EventBus / Projection | 50% | 88% |
| Team 移除与 multi-agent 收敛 | 60% | 95% |

下一轮只做三个 P1，不再扩功能：

1. 将 Outbox port 下沉到中立 application/core 层，消除 `agent.session.SessionStore -> server.services` 的反向依赖。
2. 给 Outbox 增加运维面：DLQ 查询/重放、pending age、attempts、relay health 和告警。
3. 明确普通 tool/thought 流的保留策略：哪些是 durable fact，哪些仅是有界 live telemetry；避免 EventBus trace 与 DomainEvent 两套长期并存。

本轮 P0 可以进入合并候选；上述 P1 不阻断当前生命周期链上线。
