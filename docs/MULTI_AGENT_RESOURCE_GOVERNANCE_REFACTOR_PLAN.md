# Multi-Agent 统一资源治理改造设计文档

> **状态**: Phase 0 实施中  
> **基线日期**: 2026-07-29  
> **SSOT**: 本文档是资源治理改造的唯一设计真相来源  
> **关联文档**: [`multi-agent.md`](./multi-agent.md) — 改造计划摘要；[`MULTI_AGENT_MODE_IMPLEMENTATION_PLAN.md`](./MULTI_AGENT_MODE_IMPLEMENTATION_PLAN.md) — Multi-Agent 模式实现计划

## 1. 摘要

本文档定义 Multi-Agent 统一资源治理（Resource Governance）的目标架构、接口契约、配置迁移、状态机、持久化、API/事件/UI、测试和发布方案。

**核心策略**: 接口全服务化、首期只接入 Multi-Agent。

1. 首期统一治理所有 Multi-Agent worker。
2. 随后治理 LLM、工具、事件、数据库和工作树。
3. 最后接入普通根 Agent 与 Agent Team。
4. 全程保持旧配置兼容，按 observe → soft-enforce → enforce 分阶段上线。

**不变量**: 资源不足时不改变任务拓扑——进入有界公平队列，不自动降级。队列全局最多 64 项，单项最多等待 120 秒，超时返回 `capacity_timeout`。

---

## 2. 现状审计与根因树

### 2.1 问题清单

| # | 问题 | 确认状态 | 影响模块 |
|---|------|---------|---------|
| 1 | 子代理预算非原子分配，可能并发超卖 | 已确认 | `runtime_spawn.py`, `TaskContract` |
| 2 | YAML、环境变量、运行时默认值多源分叉 | 已确认 | `config/schema.py`, `multi_agent_config.py`, `runtime_spawn.py` |
| 3 | 每 wave、每 agent、每 LLM、每工具池的线程乘法 | 已确认 | `delegation_scheduler.py`, `llm/invoker.py`, `streaming_executor.py` |
| 4 | 超时后 daemon thread 和底层连接无法保证退出 | 已确认 | `llm/invoker.py:_call_with_timeout`, `llm/openai_backend.py:stream_iter` |
| 5 | 取消只传播信号，不能终止阻塞 provider/tool | 已确认 | `CancellationToken`, `llm/invoker.py` |
| 6 | 子代理重复加载系统提示、工具 schema 和项目上下文 | 已确认 | `subagent.py:run_child_agent` |
| 7 | 子代理禁用 history compaction | 待压测 | `context/` |
| 8 | provider 侧缺少共享 RPM、TPM、并发和 429 协调 | 已确认 | `llm/openai_backend.py` |
| 9 | EventBus 使用无界队列，高频 delta 缺少合并和背压 | 已确认 | `server/services/event_bus.py:asyncio.Queue()` |
| 10 | SQLite 并发写、trace/event 放大和 WAL 压力 | 待压测 | `app/storage/sqlite.py`, `session_store.py` |
| 11 | modified/unknown worktree 长期保留，缺少容量预检和 orphan 清理 | 已确认 | `worktree_manager.py`, `worktree_service.py` |
| 12 | `RESOURCE_EXHAUSTED` 混合逻辑预算、主机资源和 provider 容量 | 已确认 | `observability/failure_policy.py` |
| 13 | 关闭流程只清理引用，没有完整 stop/cancel/drain/close/join | 已确认 | `SessionRuntime.dispose()`, `AgentService.shutdown()` |
| 14 | 测试只覆盖功能边界，没有覆盖资源稳定性 | 已确认 | `tests/` |

### 2.2 根因树

```
资源治理缺失
├── 无统一容量模型
│   ├── 并发限制散落: _spawn_lock / GRACE_MAX_CONCURRENT_SUBAGENTS / MultiAgentFeatureConfig
│   ├── 无 token 预算原子预留 → 并发超卖
│   └── 无 provider 级 RPM/TPM/429 协调
├── 无统一生命周期
│   ├── daemon thread 遗弃模式 (llm/invoker.py, openai_backend.py)
│   ├── 取消不能终止阻塞 I/O
│   └── 关闭流程不完整 (drain/cancel/close/join)
├── 无背压机制
│   ├── EventBus 无界队列
│   ├── trace event 无批量写入
│   └── 慢 WebSocket 不反压 agent 线程
├── 无容量预检
│   ├── worktree 无配额/磁盘检查
│   └── 子 agent 上下文重复加载
└── 多源配置分叉
    ├── YAML (default.yaml) vs env var (GRACE_*) vs 默认值
    └── 无弃用迁移路径
```

---

## 3. 目标架构

```
                        ┌──────────────────────────┐
                        │    ResourceGovernor       │
                        │  (core/resource_governor) │
                        │                          │
                        │  admit() / release()      │
                        │  snapshot() / shutdown()  │
                        └─────┬────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   ┌────▼────┐          ┌────▼────┐          ┌─────▼─────┐
   │ Worker  │          │  Token  │          │ Provider  │
   │ Slots   │          │ Budget  │          │ RPM/TPM   │
   │ (2/2)   │          │ Reserve │          │ Semaphore │
   └─────────┘          └─────────┘          └───────────┘
        │                     │                     │
   ┌────▼────┐          ┌────▼────┐          ┌─────▼─────┐
   │ Event   │          │Worktree │          │    DB     │
   │ Capacity│          │  Slots  │          │  Writer   │
   └─────────┘          └─────────┘          └───────────┘
```

**原则**:
- 所有资源先申请（admit）、后执行、finally 释放（release）
- ResourceGovernor 是纯服务接口——不知道 agent/session 细节
- 首期（Phase 1）只 Multi-Agent worker 和 token 走 governor；其余资源在 observe 模式追踪

---

## 4. 核心接口契约

### 4.1 ResourceKind

```python
class ResourceKind(str, Enum):
    WORKER_SLOT = "worker_slot"         # 并发 worker 槽位
    LLM_SLOT = "llm_slot"               # LLM 并发调用槽位
    PROVIDER_RPM = "provider_rpm"       # provider 每分钟请求数
    PROVIDER_TPM = "provider_tpm"       # provider 每分钟 token 数
    TOOL_SLOT = "tool_slot"             # 工具执行槽位
    TOKEN_BUDGET = "token_budget"       # token 预算
    EVENT_CAPACITY = "event_capacity"   # 事件队列容量
    WORKTREE_SLOT = "worktree_slot"     # 工作树槽位
    DISK_BYTES = "disk_bytes"           # 磁盘配额
    DB_WRITE_CAPACITY = "db_write_capacity"  # 数据库写容量
```

### 4.2 ResourceRequest

```python
@dataclass(frozen=True)
class ResourceRequest:
    root_id: str                        # 根会话 ID（用于公平排队）
    session_id: str                     # 请求所属 session
    run_id: str                         # 请求所属 run
    task_id: str                        # 请求所属 task
    resources: dict[ResourceKind, float]  # 资源申请向量
    priority: int = 0                   # 优先级
    deadline_seconds: float = 120.0     # 排队截止时间
    cancel_token: object = None         # 取消令牌引用
```

### 4.3 ResourceLease

```python
@dataclass(frozen=True)
class ResourceLease:
    request: ResourceRequest
    granted: dict[ResourceKind, float]  # 实际获批量
    lease_id: str                       # 唯一租约 ID
    released: bool = False              # 是否已释放（幂等保护）
```

### 4.4 AdmissionResult

```python
class AdmissionResult(str, Enum):
    GRANTED = "granted"                 # 立即获批
    QUEUED = "queued"                   # 进入等待队列
    CANCELLED = "cancelled"             # 被取消
    CAPACITY_TIMEOUT = "capacity_timeout"  # 排队超时
    IMPOSSIBLE = "impossible"           # 所需资源超出总容量
    SHUTDOWN = "shutdown"               # 已关闭
```

### 4.5 ResourceSnapshot

```python
@dataclass(frozen=True)
class ResourceSnapshot:
    kind: ResourceKind
    capacity: float                     # 总容量
    reserved: float                     # 已预留
    consumed: float                     # 已消费
    queued_count: int                   # 排队数
    wait_time_ms_avg: float             # 平均等待时间
    pressure: ResourcePressure          # 压力等级
    reject_reason: str = ""             # 拒绝原因
```

### 4.6 ResourcePressure

```python
class ResourcePressure(str, Enum):
    NORMAL = "normal"                   # 正常
    WARNING = "warning"                 # 预警 (>70%)
    CRITICAL = "critical"               # 临界 (>90%)
    DRAINING = "draining"               # 排空中 (shutdown)
```

### 4.7 不变量

1. **先申请后执行**: 所有资源必须通过 `admit()` → `release()` 路径。
2. **Token 原子预留**: 启动前预留，完成后按 provider 实际 usage 结算并退款。
3. **超出预留必须二次申请**: 不允许出现负余额。
4. **容量守恒**: `available + reserved + consumed = configured_limit`。
5. **取消立即移除**: 取消排队请求必须立即从队列移除。
6. **租约释放幂等**: 多次调用 `release()` 无副作用。
7. **Shutdown 排空**: `shutdown()` 后拒绝新申请，等待现有租约全部释放。
8. **跨 root FIFO 公平排队**: 限制单 root 连续获批，防止单会话垄断。

---

## 5. 配置迁移表

### 5.1 新配置入口 (YAML)

```yaml
resource_governance:
  mode: enforce              # observe | soft_enforce | enforce
  
  worker:
    global_max: 2            # 全服务并发 worker
    per_root_max: 2           # 单 root 并发 worker
  
  queue:
    max_size: 64              # 全局队列容量
    timeout_seconds: 120.0    # 单项最大等待时间
  
  token:
    reservation_enabled: true
    overcommit_ratio: 1.0
  
  provider:
    rate_limit_enabled: false
    rpm: 0
    tpm: 0
    max_concurrent: 0
  
  event:
    queue_max_size: 4096      # 每个 session 的有界 WebSocket 事件队列
  
  worktree:
    global_max: 10
    per_root_max: 3
    disk_limit_mb: 0
  
  shutdown:
    drain_timeout_seconds: 30.0
    force_kill_seconds: 5.0
```

### 5.2 环境变量边界

生产 worker 容量只读取 `resource_governance.worker.*`，不再把旧环境变量
映射到 governor。`GRACE_MAX_CONCURRENT_SUBAGENTS` 仅用于没有 enforcing
governor 的隔离或 observe-mode runtime。

以下变量不表达可续租容量，继续由各自的单一配置对象读取：

| 变量 | 所有者 | 语义 |
|------|--------|------|
| `GRACE_MAX_SUBAGENTS_PER_SESSION` | `SubagentSafetyLimits` | 单 session 终身 spawn 安全上限 |
| `GRACE_MAX_SUBAGENT_SPAWN_DEPTH` | `SubagentSafetyLimits` | spawn 深度安全上限 |
| `GRACE_MAX_MULTI_AGENT_TASKS` | `MultiAgentFeatureConfig` | 单次 batch 任务上限 |
| `GRACE_MULTI_AGENT_MODE_ENABLED` | `MultiAgentFeatureConfig` | 功能开关 |
| `GRACE_MAX_FANOUT_PER_TURN` | `MultiAgentFeatureConfig` | 单轮 DAG fanout 上限 |

---

## 6. 状态机

### 6.1 Worker Lifecycle

```
                admit()
  IDLE ─────────────────────► QUEUED
   ▲                            │
   │                            │ granted
   │                            ▼
   │ release()              RUNNING
   │                            │
   │                            │ completing
   │                            ▼
   └────────────────────── COMPLETING
```

### 6.2 Queue Lifecycle

```
  enqueue ──► [FIFO Queue, max 64]
                  │
                  ├── granted ──► lease issued
                  ├── timeout ──► capacity_timeout
                  ├── cancel ───► cancelled
                  └── shutdown ─► shutdown
```

### 6.3 Lease Lifecycle

```
  granted ──► [active lease]
                  │
                  ├── release(actual_usage) ──► released (idempotent)
                  ├── release() without usage ──► released (refund reserved)
                  └── session crash ──► leaked → recovery sweep
```

---

## 7. 持久化设计

### 7.1 delegation_runs 表扩展

新增 JSON 字段（向后兼容，旧记录读取为空对象，不批量改写）：

- `resource_snapshot`: 获批时的资源快照
- `resource_queue_started_at`: 排队开始时间
- `resource_wait_duration_ms`: 等待时长
- `resource_termination`: 终止原因 (`capacity_timeout`, `cancelled`, `released`)

### 7.2 delegation_tasks 表扩展

新增 JSON 字段：

- `resource_requested`: 申请的资源和数量
- `resource_granted`: 实际获批的资源和数量
- `resource_consumed`: 实际消费的资源和数量
- `resource_refunded`: 退款量（token 结算）

---

## 8. API/事件/UI 契约

### 8.1 新增事件

| 事件 | 触发时机 | 携带字段 |
|------|---------|---------|
| `delegation_resource_queued` | 进入队列 | session_id, run_id, task_id, queue_position, resource_kind, requested |
| `delegation_resource_granted` | 获批 | session_id, run_id, task_id, resource_kind, granted, wait_duration_ms |
| `delegation_resource_reconciled` | 结算 | session_id, run_id, task_id, resource_kind, reserved, consumed, refunded |
| `delegation_resource_released` | 释放 | session_id, run_id, task_id, resource_kind |
| `delegation_resource_timeout` | 排队超时 | session_id, run_id, task_id, resource_kind, wait_duration_ms |
| `resource_pressure_changed` | 压力等级变化 | resource_kind, old_pressure, new_pressure, utilization_pct |

### 8.2 API 快照扩展

`GET /api/multi-agent/{session_id}` 的 `DelegationRunProjection` 增加:

```json
{
  "resource": {
    "status": "running",
    "queue_position": null,
    "wait_duration_ms": 0,
    "token_reserved": 40000,
    "token_consumed": 32150,
    "worker_slots": 1,
    "pressure": "normal",
    "reason_code": null
  }
}
```

### 8.3 UI 卡片状态

| 状态 | 显示 | 操作 |
|------|------|------|
| Waiting for capacity | 排队位置 + 已等待时间 | 取消 |
| Running | 并发槽 + token 预留摘要 | 取消 |
| Capacity timeout | 等待超时 + 原因 | 重试 |
| Provider throttled | Retry-After 倒计时 | 自动重试 |

---

## 9. 分阶段实施路线

| Phase | 内容 | 默认模式 | 影响范围 |
|-------|------|---------|---------|
| **0** | 配置统一 + 观测模式 | observe | 配置层、核心类型、无运行行为变化 |
| **1** | Multi-Agent 强制接入 | enforce (multi-agent only) | AgentBatch, delegation scheduler, runtime spawn |
| **2** | 可终止生命周期 + 共享执行器 | enforce | LLM invoker, streaming executor, thread pool |
| **3** | Provider、事件、DB、worktree 治理 | enforce | Provider semaphore, EventBus bounded queue, SQLite writer, worktree manager |
| **4** | 扩展到全服务 | enforce | 根 Agent, Agent Team, 后台恢复, review worker, MCP |

---

## 10. 测试与验收门槛

### 10.1 自动化测试矩阵

| 测试类别 | Phase 0 | Phase 1 | Phase 2 | Phase 3 | Phase 4 |
|---------|:------:|:------:|:------:|:------:|:------:|
| 配置加载/兼容映射/弃用告警 | ✓ | — | — | — | — |
| 原子预算 (无超卖) | — | ✓ | — | — | — |
| 公平排队 (跨 root FIFO) | — | ✓ | — | — | — |
| 边界 (第 65 个排队) | — | ✓ | — | — | — |
| 取消 (排队/运行中) | — | ✓ | — | — | — |
| 重试重新申请 | — | ✓ | — | — | — |
| 重启不恢复租约 | — | ✓ | — | — | — |
| provider 429/熔断 | — | — | — | ✓ | — |
| hung stream / hung tool | — | — | ✓ | — | — |
| 慢消费者/有界队列 | — | — | — | ✓ | — |
| DB 并发写/批量失败 | — | — | — | ✓ | — |
| worktree 低磁盘/配额 | — | — | — | ✓ | — |
| 新旧配置优先级 | ✓ | — | — | — | — |
| 前端排队/获批/超时 | — | ✓ | — | — | — |
| observe 模式不阻塞 | ✓ | — | — | — | — |

### 10.2 压力验收

- 100 个模拟 Multi-Agent 批次后，线程数在预热后保持稳定
- 任意时刻活跃 worker 不超过全局 2、单 root 2
- token 账本满足守恒不变量
- 取消/超时后可控 provider/tool 在限定 drain 时间内退出
- 事件队列、DB writer 队列、admission 队列均不无限增长
- 慢 WebSocket 不推高 agent 线程或阻塞终态持久化
- 关闭期间不出现 `cannot schedule new futures after interpreter shutdown`
- 现有 Multi-Agent、delegation recovery、worktree integration、chat streaming、前端回放测试全部通过

---

## 11. 附录 — 影响面索引

### 调度层
- `agent/session/runtime.py` — SessionRuntime (spawn orchestration)
- `agent/session/runtime_spawn.py` — spawn_agent (concurrency check)
- `agent/session/delegation_scheduler.py` — DelegationRunScheduler (wave executor)
- `agent/session/integration_coordinator.py` — DelegationIntegrationCoordinator
- `agent/session/agent_batch_tool.py` — AgentBatch tool

### LLM 层
- `llm/base.py` — LLMBackend
- `llm/openai_backend.py` — OpenAIBackend (streaming threads)
- `llm/invoker.py` — LLMInvoker (retry, timeout threads)

### 工具层
- `tools/` — 所有工具实现
- `core/streaming_executor.py` — StreamingToolExecutor
- `core/tool_execution.py` — ToolExecutionPipeline

### 事件层
- `server/services/event_bus.py` — EventBus
- `server/events.py` — typed WS events
- `server/routers/websocket.py` — WebSocket router

### 存储层
- `app/storage/sqlite.py` — SqliteStorageBackend
- `agent/session/session_store.py` — SessionStore

### 工作树层
- `agent/session/worktree_manager.py` — WorktreeManager
- `agent/session/worktree_service.py` — typed worktree operations

### 配置层
- `config/schema.py` — AppConfig
- `config/default.yaml` — default config
- `agent/session/multi_agent_config.py` — MultiAgentFeatureConfig

### API 层
- `server/routers/multi_agent.py` — multi-agent REST
- `server/routers/sessions.py` — session REST
- `server/services/multi_agent_service.py` — MultiAgentService

### 前端层
- `web/src/stores/chatStore.ts` — delegation state management
- `web/src/components/ChatView.tsx` — delegation UI
- `web/src/components/MultiAgentRunCard.tsx` — run card component
- `web/src/types/delegation.ts` — delegation types

### 可观测性
- `observability/tracing.py` — tracing
- `observability/failure_policy.py` — failure classification

### 测试
- `tests/test_delegation_integration_coordinator.py`
- `tests/test_integration_full_chain.py`
- `tests/test_stream_event_routing.py`
- `tests/test_agent_batch_runtime.py`
- `tests/test_multi_agent_release_boundaries.py`

---

## 12. 实施状态（2026-07-30）

本轮已完成并接通以下生产链路：

- `ResourceGovernor` 使用单一原子状态锁管理复合资源租约。
- Worker/Worktree 等可再生容量与 Token 等可消费预算分账。
- Spawn 和 Worktree 使用可取消、可超时、释放后自动唤醒的 FIFO admission。
- AgentBatch 与 DelegationRunScheduler 使用 Runtime 共享有界执行器。
- Provider RPM/TPM/并发限制接入真实 LLM 调用和共享 429 backoff。
- EventBus 使用有界、无正文丢失的跨线程背压，并限制慢 WebSocket 发送时间。
- 资源 queued/granted/released/timeout 等事实持久化到 delegation task，
  同时经 WebSocket、前端 reducer 和任务卡片实时展示。
- Worktree 使用原子 lease，保留的工作树继续占用容量，应用或丢弃后释放。
- 默认模式已切换为 `enforce`；`observe` 仍可用于诊断。

验收结果：

- Python 全量测试：617 passed，11 deselected。
- Web 单元测试：44 passed。
- TypeScript 与 Vite production build：通过。
- `git diff --check`：通过。

### 12.1 预算单路收敛（2026-07-30）

运行时预算职责现已按资源语义拆分，并且每种资源只有一个裁决者：

- `TaskContract -> ExecutionBudget` 是 Agent Token、步骤和执行时间的唯一权威；
- 子 Agent 从父 `ExecutionBudget` 原子划拨 Token ceiling；并行与后台任务都会
  占用父级 reserved 额度，结束后按实际消耗结算并退回余额，ToolResult 不再
  对已结算结果重复扣费；
- `ResourceGovernor` 只裁决 worker、worktree 等可续租容量，不再对同一个
  子任务执行第二次 Token 准入；
- 已移除不参与运行时决策的 `resource_governance.token.*` 兼容输入；
- `ProviderGovernor` 是所有真实 LLM 请求的 RPM、TPM、并发和共享 429 backoff
  权威，普通 Agent、语义压缩、证据摘要和 memory 辅助请求共用
  `llm/provider_capacity.py`；
- 语义压缩产生的真实 Token 会记入当前 Agent 的 `ExecutionBudget`，并归类为
  overhead；独立的会话维护压缩没有活跃 Agent 执行预算，但仍受 Provider
  容量治理。

这里的“单路”不是把执行预算、上下文窗口和 Provider TPM 混成同一个数字。
三者分别解决“任务最多花多少”“单次请求最多携带多少上下文”和“供应商单位
时间允许多少流量”，但每个维度只能有一个运行时裁决入口。
