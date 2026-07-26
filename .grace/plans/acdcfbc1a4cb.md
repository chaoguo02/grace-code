## 系统优化优先级 Top 3

根据对审计文档（`CORE_AUDIT_FOUR_PHASE.md`、`BUDGET_AND_SAFETY_GAP_ANALYSIS.md`、`cli-vs-web-gap-analysis.md`、`PROJECT_STATUS_AND_CC_GAP.md`）和代码现状的综合分析，当前系统**最需要优化的三件事**如下：

---

### 🔴 #1 — Web 模式缺少自动上下文压缩（P0）

| 维度 | 详情 |
|------|------|
| **来源** | `docs/cli-vs-web-gap-analysis-2026-07-23.md` 第 1 项 |
| **影响** | Web 模式中 `compact_session_async()` 存在但从未在 ChatPipeline 完成后自动调用。CLI 每轮结束后检查 `auto_compact_after_round` 配置触发压缩，Web 端长会话会超出 context window → 截断/失败 |
| **涉及文件** | `server/services/chat_pipeline.py`（需加压缩触发）、`server/services/agent_service.py`（已有 `compact_session_async`） |
| **修复量级** | 小型改动（~20 行） |
| **为什么优先** | 直接影响所有 Web 用户的长会话体验，是 P0 唯一未修的差距 |

---

### 🟠 #2 — Subagent 嵌套深度无限制（安全项）

| 维度 | 详情 |
|------|------|
| **来源** | `docs/PROJECT_STATUS_AND_CC_GAP.md` §2.5 + §7（排第 1） |
| **影响** | Claude Code 硬限制 5 层嵌套，我们的 `AgentSpawnRequest` / `ChildTurnPhase` 无深度上限。子代理递归创建子代理 → 无限递归 + 预算爆炸 |
| **涉及文件** | `agent/session/models.py`（`AgentDefinition.permits_subagent` 或 `AgentSpawnRequest` 加深度字段）、`agent/session/runtime.py`（调用链加检查） |
| **修复量级** | 小改动（~15 行 + 一个测试） |
| **为什么优先** | 安全项——无限制 = 生产环境中一个 bug 就能导致资源耗尽 |

---

### 🟡 #3 — 预算消耗归因监控缺失（可观测性盲区）

| 维度 | 详情 |
|------|------|
| **来源** | `docs/BUDGET_AND_SAFETY_GAP_ANALYSIS.md` §G5 + `PROJECT_STATUS_AND_CC_GAP.md` §7（排第 3） |
| **影响** | 没有 `BudgetUsage` 数据结构区分 **productive_steps**（工具调用成功）、**retry_steps**（安全拦截重试）、**overhead_tokens**（系统错误消息）。优化预算配置只能靠猜。Claude Code 社区有 `tokenwarden`，我们无 |
| **涉及文件** | `agent/session/execution_budget.py`（新增 `BudgetUsage` dataclass）、`core/tool_execution.py`（归因记录点）、`server/services/stats_recorder.py`（暴露给统计） |
| **修复量级** | 中型改动（~80 行 + 测试） |
| **为什么优先** | 没有数据就无法后续优化预算默认值、无法诊断"预算耗尽"类 issue |

---

### 次优候选（短期可暂缓）

| 候选 | 理由 |
|------|------|
| ~~FileWriteTool Read-before-Write~~ | ✅ 已在 7/25 批次修复 |
| ~~Read Cache 大小限制~~ | ✅ 已在 7/25 批次修复（FIFO 200 entries / 50MB） |
| ~~Per-tool budget gate~~ | ✅ 已在 7/25 批次新增 |
| Core 架构 P0（summary 净化） | 📋 `CORE_AUDIT_FOUR_PHASE.md` 列出的 P0-1/P0-2/P0-3 涉及 session storage 数据污染，但前提是前端 workaround 已删除，且后端改动有依赖链 |

---

**总结**：当前系统已通过 7/25 大规模重构弥合了大部分 Claude Code 差距（62 files, ~4200 insertions）。剩余的最优优先级是：**#1 Web 自动压缩**（直接影响用户）、**#2 Subagent 深度上限**（安全问题）、**#3 预算归因监控**（可观测性基础）。三个均为独立改动，可并行执行。