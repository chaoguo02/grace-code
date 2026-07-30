# Multi-Agent 资源治理验收与整改记录

日期：2026-07-30

## 结论

附件中的 `57.5%` 审计结果不能直接作为当前代码状态。它把若干已经接通的链路判定为未实现，也把不同语义的控制结构误认为重复资源控制。

本轮按运行时调用链重新验收，并修复了仍然存在的真实缺口。当前 multi-agent token 预算只有一条控制路径：

`TaskContract -> ExecutionBudget.reserve_tokens() -> BudgetReservation.settle()`

`ResourceGovernor` 不再申请或裁决 `TOKEN_BUDGET`。为了让前端和持久化仍能看到预算生命周期，`ExecutionBudget` 会通过 governor 的 `publish_accounting_event()` 发布纯观测事件；该方法不会修改 governor 的 token reserved/consumed 账本。测试明确验证这两个值始终为零，因此展示链路不会重新形成第二预算控制器。

## 对原审计的纠正

### 已经实现、但原审计误报为缺失

- 后端已发送 `delegation_resource_queued/granted/released/cancelled/capacity_timeout/shutdown/rejected`，并通过 EventBus 推送 WebSocket。
- delegation task 已持久化 `resource_json`，刷新后由 snapshot 重建资源状态。
- `MultiAgentRunCard` 已显示资源压力、排队位置、等待时间以及 token granted/consumed。
- AgentBatch 和 DelegationScheduler 的生产路径已复用 `SessionRuntime._shared_executor`；局部 executor 仅用于没有 Runtime 的隔离测试兼容路径。
- Team task 的执行最终进入 `run_explicit_delegation -> spawn_agent -> ResourceGovernor`。Team `LeaseManager` 是任务所有权租约，不是 worker 容量控制器，不能删除或合并。
- `_spawn_lock/_spawn_reservations` 保护的是终身 spawn 数量和 session 创建 TOCTOU，不是并发 worker 容量；保留它不构成双重并发控制。
- failure policy 已用于 replay boundary 验证；本轮进一步补充了资源 admission 的细分类。

## 本轮完成的整改

### 1. 跨 root 公平排队

唯一排队决策点仍在 `ResourceGovernor._drain_queue_locked()`。

- 同一 root 内保持 FIFO。
- 多个 root 同时可运行时，在 root 之间轮转 grant。
- 被单 root 配额阻塞的队首不再阻塞其他 root。
- 取消和超时会扫描整个队列，不依赖其是否位于队首。
- 修复 `admit_wait()` 在“刚入队即被并发 grant”时误报 cancelled 的竞态。

### 2. MCP 工具统一治理

新增配置：

```yaml
resource_governance:
  tool:
    global_max: 8
    per_root_max: 4
```

治理入口位于所有工具共同经过的 `ToolExecutionPipeline`：

- 仅 externally-backed MCP 工具申请 `TOOL_SLOT`。
- 每次真实 retry attempt 都重新申请租约。
- `finally` 幂等释放。
- session 通过 resolver 归属到 root session。
- 容量失败返回结构化 metadata，而不是只能解析错误字符串。

### 3. Review worker 共享执行器

生产环境的 `ReviewService` 复用 `SessionRuntime._shared_executor`，不再为每个 review job 创建独立线程池。

没有 Runtime shared executor 的隔离测试环境仍保留局部兼容池，并由 ReviewService 自己关闭。测试会把私有池构造器替换为抛错函数，证明生产路径确实使用 shared executor。

### 4. 资源压力事件

新增 `resource_pressure_changed`：

- 包含 request/root/session/run/task 稳定身份。
- 包含 `resource_kind`、`old_pressure`、`pressure` 和时间戳。
- 通过 AgentService 的同一资源事件回调进入 WebSocket。
- 压力事件不会误写 delegation task 的资源生命周期字段。
- 前端 `WsMessage` 已包含该事件类型。

### 5. 单路 token 控制与完整展示链

预算 reservation 成功后发布 `delegation_resource_granted` 的 token 观测事实；结算后发布 `delegation_resource_reconciled`：

- granted 携带 reservation。
- reconciled 携带实际 tokens used。
- 持久化对 `requested/granted/consumed/refunded` 内层资源 map 做合并，worker 和 token 事件不会互相覆盖。
- 前端 reducer 将 reconciled 转换为 consumed/refunded。
- snapshot reload 与实时 WebSocket 使用同一数据模型。
- governor 不做 token admission，也不累计 token consumption。

### 6. 可操作的失败分类

新增稳定分类：

- `budget_exhausted`
- `capacity_timeout`
- `provider_throttled`
- `host_memory_pressure`
- `thread_capacity`
- `disk_capacity`
- `event_backpressure`
- `db_backpressure`

spawn admission 使用带 outcome/kind 的 `ResourceAdmissionError`。MCP 容量失败在 ToolResult metadata 中输出分类、资源类型和等待时间。

### 7. Worktree checkout 容量预估

创建 worktree 前通过 Git tracked file set 估计 checkout 体积，并预留 20% 文件系统/元数据余量。最终要求取以下最大值：

- 100 MB 安全底线；
- 配置的 minimum free；
- 预计 checkout 大小的 120%。

错误信息包含当前可用空间、所需空间和预计 checkout 大小。

### 8. Observe 容量建议

`ResourceMetricsCollector` 基于有界 snapshot history 输出每种资源的：

- observed peak；
- observed peak queue；
- suggested limit；
- sample count。

这使 observe 模式的数据可以直接用于容量参数调整，而不只是打印一次快照。

## 关键端到端链路

### 子代理预算

`TaskTool/AgentBatch`
→ parent `ExecutionBudget.reserve_tokens`
→ `spawn_agent`
→ child `TaskContract`
→ child execution/compaction/helper LLM usage
→ `BudgetReservation.settle(actual)`
→ token accounting event
→ AgentService persistence + WebSocket
→ frontend reducer
→ `MultiAgentRunCard`

### 子代理 worker

`spawn_agent`
→ `ResourceGovernor.admit_wait(WORKER_SLOT)`
→ queued/granted event
→ shared executor executes child
→ `finally lease.release`
→ released event
→ queue drain grants next root

### MCP

`ToolRegistry.execute_tool`
→ permission/capability/budget gate
→ `ToolExecutionPipeline._execute_once`
→ `ResourceGovernor.admit_wait(TOOL_SLOT)`
→ MCP call
→ `finally lease.release`

### Review

`ReviewService coordinator`
→ `SessionRuntime._shared_executor.submit`
→ `run_explicit_delegation`
→ normal spawn worker admission and ExecutionBudget path

## 验证

- Python compileall：通过。
- 资源治理、预算、失败分类、MCP 管线、Review、delegation persistence 聚焦测试：通过。
- Python 全量测试：通过。
- Web Vitest：16 files / 44 tests 通过。
- TypeScript + Vite production build：通过。
- `git diff --check`：通过。
- 新增 100 个并发 admission 请求压力测试，验证 active worker 始终不超过 2，结束后 reservation 和 queue 均归零。

## 保留项与原因

- `governor is None` 兼容分支只服务 isolated tests/embedded runtime；AgentService 生产构造始终注入 governor。删除它会破坏库级测试和嵌入式使用，但不会增强生产单路控制。
- Team `LeaseManager` 保留，因为它解决任务领取/过期恢复，不控制线程、token、provider 或 tool capacity。
- `_spawn_lock` 保留，因为它解决 session 创建竞态和终身 spawn 安全上限，不参与 renewable capacity。
- root Agent 不占用 subagent `WORKER_SLOT`。root 的 LLM 请求受共享 ProviderGovernor 治理；若让 root 长时间持有默认仅 2 个的 worker slot，会在等待自己的子代理时造成自阻塞或把默认子代理并发从 2 降为 1。若未来要统一 root CPU 执行容量，应新增独立 `ROOT_RUN_SLOT` 或可挂起的层级租约，不能复用当前 child worker lease。

## 仍建议单独立项

- 慢 WebSocket 当前通过有界队列对 producer 施加背压，内存有界且终态不丢，但极慢客户端会延长生产线程等待时间。若要完全隔离，需要全局 event dispatcher/spool，而不是再创建 per-session thread。
- 真实 provider 429、网络半开和 MCP 子进程卡死仍需带真实依赖的长期 soak test；单元与本地集成测试无法替代生产环境故障注入。
- 旧 `GRACE_*` 到 `resource_governance` 的覆盖映射已移除；生产容量只由
  `resource_governance`/`ResourceGovernor` 决定。spawn 深度和单 session
  终身数量仍是独立安全边界，由 `SubagentSafetyLimits` 单点解析。
