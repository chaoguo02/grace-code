# Grace-Code P0 CC-Native 重构路线图

> 状态：设计修正完成 | 日期：2026-08-01
> 实施顺序：P0_3 → P0_1 → P0_2
> 第一批: P0_3 Batch 1 (5.5 人日) — CancellationHandle + ProcessRegistry + 终态 CAS

---

## 1. 实施顺序与依赖

```
P0_3: Cancellation Pipeline     ← 第一批 (副作用安全 + 低耦合)
  └─ Batch 1: CancellationHandle + ProcessRegistry + 终态 CAS (5.5 人日)
  └─ Batch 2+: CancellableToolPipeline + Executor + Shell argv (6 人日)

P0_1: ContextWindowManager      ← 第二批 (依赖 P0_3 的 CancellationHandle)
  └─ TokenCounter 二层契约 + TokenPlanner + CompactionStrategy 链 (13.5 人日)

P0_2: MCP Transport Layer       ← 第三批 (SDK 替换 + conformance)
  └─ StreamableHttpBridge + Resource/Prompt + old code deletion (10.5 人日)
```

**理由**:
- P0_3 直接涉及副作用安全 (取消后进程不应继续运行)，且与 Context/MCP 耦合最低
- P0_3 的 `CancellationHandle` 可被 P0_1 的 `APICompactor` 和 P0_2 的 MCP `close()` 复用
- P0_2 的 SDK 驱动 Streamable HTTP 工作量最小 (10.5 人日) 但前置 SDK 版本确认

---

## 2. P0_3 Batch 1 严格范围

第一批**只交付**取消基础设施的 4 个核心组件：

| 阶段 | 交付物 | 文件 | 工时 | 关键约束 |
|------|--------|------|------|---------|
| B1-P1 | `CancellationHandle` (锁安全) + `ProcessRegistry` (run 级隔离) | `core/cancellation.py` | 2 人日 | lock-and-snapshot; callback 在锁外; kill_run by (session_id, generation, run_id) |
| B1-P2 | 旧新 token 适配器 | `core/cancellation_adapter.py` | 0.5 人日 | 旧代码零改动; 适配器桥接 |
| B1-P3 | 终态 CAS | `core/streaming_executor.py` + `agent_service.py` | 1.5 人日 | cancelled → completed 拒绝; cancel_run CAS 检查 |
| B1-P4 | 三个关键测试 | `tests/test_cancellation.py` | 1.5 人日 | 真实进程 kill; cancelled 不被覆盖; 跨 run 隔离 |

**不做**: Shell argv 重构、通用超时、Shell mode explicit opt-in、旧代码删除、FailurePolicy 集成

---

## 3. 设计修正清单 (v1.1)

在设计审批前修正的 7 个契约点：

| # | 修正点 | 模块 | 修正前风险 | 修正后契约 |
|---|--------|------|-----------|-----------|
| 1 | 回调锁语义 | P0_3 | on_cancel 在锁内调用 callback → 死锁 | lock-and-snapshot; 回调在锁外执行 |
| 2 | ProcessRegistry 键空间 | P0_3 | 按 session_id kill → 旧 run 误杀新 run | (session_id, generation, run_id, invocation_id); kill_run 默认范围 |
| 3 | Sibling Abort 策略 | P0_3 | 无条件 abort → 独立只读 tool 被误杀 | FailurePolicy: ABORT_DEPENDENTS (默认), FAIL_FAST, CONTINUE_INDEPENDENT |
| 4 | TokenCounter I/O 分层 | P0_1 | 同步接口隐藏网络 I/O | LocalTokenEstimator (sync) + ProviderTokenCounter (async) |
| 5 | APICompactor 熔断降级 | P0_1 | 熔断抛异常 → 请求失败 | 降级到 DeterministicTrimmer; 不变量: context_tokens + output_room ≤ provider_limit |
| 6 | TaskContext 显式契约 | P0_1 | 只有 task 字符串 | +workspace_scope, constraints, artifact_refs, expected_output, parent_run_id, context_provenance |
| 7 | MCP sse 映射 | P0_2 | 静默映射 → 协议不兼容被掩盖 | sse → 显式报错; http → 尝试 streamable + 诊断日志; sse-legacy 显式选择 |

---

## 4. 跨 P0 交叉验证

| 场景 | 涉及 P0 | 预期行为 |
|------|---------|---------|
| 子 Agent 取消级联 | P0_3 | 父 handle.cancel() → child 自动取消; child handle 不可超过深度 3 |
| APICompactor 取消 | P0_1 + P0_3 | LLM 摘要进行中 → 取消 → CancellationHandle 中断 API 调用 |
| MCP close 时取消 | P0_2 + P0_3 | SDK session close → CancellationHandle 传播 → inflight calls drain + cancel |
| 大上下文 + 取消 | P0_1 + P0_3 | build_context 中途取消 → 不返回半成品 ContextAssembly |

---

## 5. 文件索引

| 设计规范 | 文件 | 状态 |
|---------|------|------|
| P0_1 Context Window Manager | [P0_1_CONTEXT_WINDOW_MANAGER_DESIGN.md](P0_1_CONTEXT_WINDOW_MANAGER_DESIGN.md) | v1.1 修正完毕 |
| P0_2 MCP Transport Layer | [P0_2_MCP_TRANSPORT_DESIGN.md](P0_2_MCP_TRANSPORT_DESIGN.md) | v1.1 修正完毕 |
| P0_3 Cancellation Pipeline | [P0_3_CANCELLATION_PIPELINE_DESIGN.md](P0_3_CANCELLATION_PIPELINE_DESIGN.md) | v1.1 修正完毕 |
| (原修补式计划 — 已废弃) | `P0_1_TOKEN_BUDGET_UNIFICATION_PLAN.md` | 被 CC-Native 设计替代 |
| (原修补式计划 — 已废弃) | `P0_2_MCP_STREAMABLE_HTTP_MIGRATION_PLAN.md` | 被 CC-Native 设计替代 |
| (原修补式计划 — 已废弃) | `P0_3_CANCELLATION_PROPAGATION_PLAN.md` | 被 CC-Native 设计替代 |
