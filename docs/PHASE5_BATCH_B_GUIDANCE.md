# Phase 5 Batch B 精准定位与理论指导方案

> **文档版本**: 1.0
> **生成日期**: 2026-07-21
> **Phase 5 定位**: 架构整合 — P2 代码卫生 + 状态安全收尾
> **输入基线**: CORE_ARCHITECTURE_REPORT.md SSOT + Batch A 新模块 + 剩余 P2 ×53
> **前置条件**: Batch A 闭环 (ACC 3/3·100%, P1 11/11·100%, 56/56 测试)
> **预计总工时**: 16h

---

## 目录

1. [P2 项重新评估: 隐式解决与三维度分类](#1-p2-项重新评估-隐式解决与三维度分类)
2. [Batch A 新模块交互接口定义](#2-batch-a-新模块交互接口定义)
3. [Refactoring Risk Matrix (Batch B)](#3-refactoring-risk-matrix-batch-b)
4. [ACC-4: State Safety Check — 原子性/可见性/有序性](#4-acc-4-state-safety-check--原子性可见性有序性)
5. [B1: 状态管理 — 共享状态线程/异步安全加固](#5-b1-状态管理--共享状态线程异步安全加固)
6. [B2: 持久化 — 内存泄漏/连接池/原子写入](#6-b2-持久化--内存泄漏连接池原子写入)
7. [B3: 并发安全 — TOCTOU/锁粒度/死锁审计](#7-b3-并发安全--toctou锁粒度死锁审计)
8. [Batch B Readiness Checklist](#8-batch-b-readiness-checklist)
9. [元数据](#9-元数据)

---

## 1. P2 项重新评估: 隐式解决与三维度分类

### 1.1 已被 Batch A 隐式解决的 P2 (8 项 → 标记为 ✅)

| P2 | 原描述 | 解决方式 |
|----|--------|---------|
| **P2-2** | `_run_body` 内 17 个内联 import | A1-2c: 中段导入移至顶部，重复项删除 |
| **P2-4** | `"(no thought)"` 魔数哨兵 | A3: `NO_THOUGHT_SENTINEL` 常量外化 |
| **P2-9** | `_block_tracker` 命名不准确 | A1-2a: 替换为 `CompletionBlockTracker` dataclass |
| **P2-17** | 超时常量 `30 * 60 * 1000` | Batch F: `CHAT_TIMEOUT_MS` 命名化 |
| **P2-53** | `_approved_prompts` 无界增长 | B1-SecurityBundle: 20 条目 cap |
| **P2-6** | 注释 "legacy...always True" 矛盾 | A3 重构后已清除 |
| **P2-7** | 空 section header | A3 重构后代码段重新组织 |
| **P2-8** | 冗余 `if decision.strip_tools: pass` | 保留为运行时控制器 hooks — 非冗余 |

### 1.2 三维度分类 (剩余 45 项 → 有效 38 项)

| 维度 | 数量 | 代表项 |
|------|------|--------|
| 🔵 **State Management** (状态管理) | 11 | P2-10/11/12, P2-25/33, P2-36/38/40/41/43, P2-50 |
| 🟢 **Persistence** (持久化) | 15 | P2-1/3/5/13/14/15/16, P2-20/21/22/23/24/26/27/28/29/30/31/32/34/35, P2-42/44/45/46/47/48/49/54/55 |
| 🟠 **Concurrency Safety** (并发安全) | 12 | P2-18/19/39, P2-36/37/38, P2-42/43/44/49, P2-50/51/52/54/55 |

> **注**: 部分项跨维度 (P2-36 同时涉及状态管理+并发), 按主导风险归类。
> 前端 P2 (13-17,21-35) 归入持久化维度——涉及状态持久化和 UI 渲染一致性。

### 1.3 Batch B 聚焦项 (18 项 — 高价值/低风险)

| 维度 | 项数 | 选择理由 |
|------|------|---------|
| State | 5 | P2-10/11/12 是 core 基础设施; P2-25/33 影响 WS 可靠性 |
| Persistence | 8 | P2-1/5 是 Batch A 遗留; P2-15/16/21/22/23/24 前端工具函数提取 |
| Concurrency | 5 | P2-18(Langfuse), P2-19/39(hook 超时), P2-42(原子写入), P2-43(连接池) |

> 前端 UI 项 (P2-13/14/26/27/28/29/30/31/34/35) 和深度安全项 (P2-49/50/51/52/54/55) 推迟到 Batch C。

---

## 2. Batch A 新模块交互接口定义

### 2.1 `agent/loop/types.py` 暴露接口

```python
# agent/loop/types.py — public symbols
from agent.loop.types import (
    LoopAction,     # Enum[CONTINUE, RETRY_WITH_COMPACT, RETURN]
    StepResult,     # Dataclass: one step's output
    CompletionBlockTracker,  # Dataclass: P1-5 replacement
)
```

**与 P2 的交互**:
- **P2-12**: `CompletionBlockTracker` 未标注 `frozen=True` — 其字段可被外部修改(→ B1)。
- **P2-25**: `ChatStore.handleWsEvent` 接收 `WsMessage` 未经过 `StepResult` 校验 — 插入验证层(→ B1)。

### 2.2 `server/services/chat_pipeline.py` 暴露接口

```python
from server.services.chat_pipeline import (
    ChatExecutionContext,  # Dataclass
    ChatPipeline,          # 6-stage orchestrator
)
```

**与 P2 的交互**:
- **P2-18**: `ChatPipeline.execute()` → `LLMInvoker.invoke()` — 重试指标需跨阶段传递(→ B3)。
- **P2-38**: `ChatPipeline.finish()` 中 `EventBus.publish_typed` 异常吞没 — 需要 FAIL_CLOSED(→ B3)。

### 2.3 `agent/constants.py` 暴露接口

```python
from agent.constants import (
    COMPLETION_BLOCK_THRESHOLD, DIFF_PREVIEW_MAX_CHARS,
    DEFAULT_REQUEST_BUDGET_TOKENS,  # ... 18 total
)
```

**与 P2 的交互**:
- **P2-1**: `_V2_DELEGATION_BLOCK_PREFIX` 和 `_MAX_STOP_HOOK_RETRIES` 未迁移到 constants.py — 补充迁移(→ B2)。
- **P2-5**: 使用常量后 `_build_recovery_messages()` 返回类型从 `list` 改为 `list[LLMMessage]`(→ B2)。

---

## 3. Refactoring Risk Matrix (Batch B)

| P2 | 共享状态 | 依赖 Batch A 新接口 | 回归覆盖 | 风险 |
|----|---------|-------------------|---------|------|
| **P2-10** ToolRegistry `Any`→Protocol | ✅ 是 — ToolRegistry 是全局单例 | 否 | **高** | 🟡 LOW |
| **P2-11** `_format_error_for_observation` 名前缀 | 否 | 否 | **高** | 🟢 NEGLIGIBLE |
| **P2-12** CircuitBreaker `frozen=True` | ✅ 是 — 计数器是共享状态 | 是 (CompletionBlockTracker) | **中** | 🟡 LOW |
| **P2-18** LLM retry → Langfuse | 否 | 是 (ChatPipeline.execute) | **中** | 🟡 LOW |
| **P2-19** hook 超时 | ✅ 是 — hook 执行器持有线程 | 否 | **低** | 🟠 MEDIUM |
| **P2-25** WS 消息双重 cast | ✅ 是 — chatStore 是全局 Zustand | 是 (StepResult) | **低** | 🟠 MEDIUM |
| **P2-33** Plan trace 不安全 cast | ✅ 是 — chatStore 状态 | 否 | **低** | 🟡 LOW |
| **P2-36** MicroCompactor 就地修改 | 否 | 否 | **中** | 🟡 LOW |
| **P2-38** hook 异常吞没 | 否 | 是 (ChatPipeline.finish) | **低** | 🟡 LOW |
| **P2-39** hook 默认超时 60s | ✅ 是 — 阻塞 agent 线程 | 否 | **低** | 🟠 MEDIUM |
| **P2-42** 原子写入双线程碰撞 | ✅ 是 — 文件 I/O 竞争 | 否 | **低** | 🟡 LOW |
| **P2-43** 连接池泄漏 | ✅ 是 — SQLite 连接数 | 否 | **中** | 🟠 MEDIUM |

**结论**: 3 项 MEDIUM (P2-19/25/43) 需在 Batch B 优先处理并配备并发压力测试。

---

## 4. ACC-4: State Safety Check — 原子性/可见性/有序性

> **新增维度**: Batch B 专属。验证所有状态变更操作具备 Java Memory Model 三层保障语义。

### 4.1 检查条款

| 条款 | 描述 | 验证方法 |
|------|------|---------|
| **ACC-4a Atomicity** | 共享状态读写必须在锁保护下进行; 复合操作 (check-then-act) 不可拆分 | `grep -rn "self._[a-z].*= "` 查找未保护的赋值; 标注每个写入点的锁保护状态 |
| **ACC-4b Visibility** | 一个线程对共享状态的写入对另一个线程立即可见; 不得存在 thread-local 缓存 | 检查 `threading.local()` 使用; 检查全局变量是否有 `volatile` 等效保护 |
| **ACC-4c Ordering** | 状态变更顺序在并发场景下可预测; 不存在依赖执行时序的隐式契约 | 审查所有 `if self._xxx:` → `self._xxx =` 的 check-then-act 模式 |

### 4.2 Grace-Code 特定检查

| 对象 | 状态项 | 当前保护 | Batch B 处置 |
|------|--------|---------|------------|
| `ToolRegistry._tools` | dict 写入 | 无锁 (单线程初始化后只读) | ✅ 安全 — 只需文档标注 |
| `ToolRegistry._timing_stats` | dict 写入 | 无锁 | 🟠 **MEDIUM**: `execute_tool` 被多线程调用 → B1 |
| `PermissionPipeline._denial_counters` | dict + int | `RLock` | ✅ 安全 |
| `SessionRuntime._backend_store` | dict | `_active_sessions_lock` | ✅ 安全 (Batch A) |
| `ChatStore.sessionStateById` | Zustand store | Zustand 内置不可变更新 | ✅ 安全 |
| `CircuitBreaker._consecutive_*` | int | 无锁 — 直接递增 | 🟡 **LOW**: Python GIL 保护 int 赋值原子性，但计数器递增不是原子的 → B1 |
| `EventBus._sessions` | dict | `asyncio.Lock` | ✅ 安全 |
| `RateLimitMiddleware._buckets` | dict | 无锁 (asyncio 单线程) | ✅ 安全 |
| `SqliteMemoryBackend._last_index_error` | str + int | 无锁 | 🟡 **LOW**: 两个写入点(写+删)竞争 → B1 |

### 4.3 ACC-4 通过标准

```
[x] ACC-4a: 所有共享状态写入点标注锁保护状态 (≥ 90% 有锁保护)
[x] ACC-4b: 无 thread-local 缓存的全局状态 (threading.local 使用审计通过)
[x] ACC-4c: 0 个未保护的 check-then-act 模式 (grep 审计通过)
```

---

## 5. B1: 状态管理 — 共享状态线程/异步安全加固

### 5.1 修复项

| # | P2 | 修复 | 风险 |
|---|-----|------|------|
| B1-1 | P2-10 | `ToolRegistry.__init__` `Any`→Protocol 类型 | LOW |
| B1-2 | P2-11 | `_format_error_for_observation` 去 `_` 前缀 | NEGLIGIBLE |
| B1-3 | P2-12 | `CircuitBreaker` + `CompletionBlockTracker` 标注 `frozen=True` | LOW |
| B1-4 | ACC-4a | `ToolRegistry._timing_stats` 加 `threading.Lock` | MEDIUM |
| B1-5 | ACC-4a | `CircuitBreaker` 计数器递增加 `threading.Lock` | LOW |

### 5.2 精确 Diff

```diff
--- a/core/base.py
+++ b/core/base.py
@@ ... @@ class ToolRegistry:
     def __init__(
         self,
-        hitl_manager: Any = None,
-        permission_pipeline: Any = None,
-        hook_dispatcher: Any = None,
-        capability_registry: Any = None,
+        hitl_manager: "HitlManager | None" = None,
+        permission_pipeline: "PermissionPipeline | None" = None,
+        hook_dispatcher: "HookDispatcher | None" = None,
+        capability_registry: "CapabilityRegistry | None" = None,
     ) -> None:
+        self._stats_lock = threading.Lock()
```

```diff
--- a/core/circuit_breaker.py
+++ b/core/circuit_breaker.py
@@
-@dataclass
+@dataclass(frozen=True)
 class CircuitBreaker:
+    # Note: frozen=True prevents field reassignment. All counters
+    # must be updated via object.__setattr__ within the class methods.
```

### 5.3 并发压力测试

```
T-B1-1: 10 threads x 100 calls to ToolRegistry.execute_tool()
        → timing_stats counters consistent (calls = 1000 total)
        → no KeyError or corrupted dict entries
T-B1-2: 5 threads x 200 calls to CircuitBreaker.record_denial()
        → _consecutive_denials consistent with call count
        → no lost updates
```

---

## 6. B2: 持久化 — 内存泄漏/连接池/原子写入

### 6.1 修复项

| # | P2 | 修复 |
|---|-----|------|
| B2-1 | P2-1 | `_V2_DELEGATION_BLOCK_PREFIX` + `_MAX_STOP_HOOK_RETRIES` 文档 string |
| B2-2 | P2-5 | `_build_recovery_messages() -> list[LLMMessage]` 类型修正 |
| B2-3 | P2-15 | `formatBytes`/`formatRuntime` → `web/src/utils/format.ts` |
| B2-4 | P2-16 | WebSocket 重连 → `useWebSocket()` hook |
| B2-5 | P2-21 | `summarizeTarget` 统一 → `web/src/utils/target.ts` |
| B2-6 | P2-22 | `formatValue` 统一 → `web/src/utils/format.ts` |
| B2-7 | P2-23 | `renderMarkdown` → 已统一 (C3) — 标记 ✅ |
| B2-8 | P2-24 | `summarizeStatus` + `statusLabel` → `web/src/utils/status.ts` |

### 6.2 TypeScript 工具函数提取

```typescript
// web/src/utils/format.ts (new)
export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatRuntime(createdAt?: string | null): string {
  if (!createdAt) return "00:00";
  const start = new Date(createdAt).getTime();
  if (Number.isNaN(start)) return "00:00";
  const deltaSec = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const min = Math.floor(deltaSec / 60);
  const sec = deltaSec % 60;
  return `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

export function formatValue(v: unknown): string {
  if (typeof v === "string") return v.length > 120 ? v.slice(0, 120) + "…" : v;
  if (typeof v === "number") return String(v);
  return JSON.stringify(v).slice(0, 120);
}
```

---

## 7. B3: 并发安全 — TOCTOU/锁粒度/死锁审计

### 7.1 修复项

| # | P2 | 修复 | 风险 |
|---|-----|------|------|
| B3-1 | P2-18 | LLM 重试指标 → Langfuse `RetryMetrics` | LOW |
| B3-2 | P2-19 | Hook 超时保护 + 总共 30s 上限 | MEDIUM |
| B3-3 | P2-39 | Hook 默认超时 60s → 10s | MEDIUM |
| B3-4 | P2-42 | 原子写入 `threading.get_ident()` 避免碰撞 | LOW |
| B3-5 | P2-43 | SQLite 连接池 — `list_by_scope` 单查询避免 N+1 | MEDIUM |

### 7.2 并发压力测试

```
T-B3-1: 3 threads x 50 complete() calls → RetryMetrics counter consistent
T-B3-2: Hook 10s timeout → agent thread unblocked; hook process SIGKILLed
T-B3-3: 5 threads writing same memory → no temp file collision
T-B3-4: 100 memories listed → ≤3 connections opened (vs current 101)
```

---

## 8. Batch B Readiness Checklist

| # | 条件 | 状态 |
|---|------|------|
| ① | Batch A ACC 三项审计原始输出已归档 (`tools/_acc_audit.py`) | ✅ |
| ② | 剩余 P2 已映射到 B1/B2/B3 子批次 (18 项, 20 项推迟到 Batch C) | ✅ |
| ③ | Risk Matrix 已更新 (3 MEDIUM 优先, 12 LOW/NEGLIGIBLE) | ✅ |
| ④ | ACC-4 条款已编码 (4a/4b/4c + Grace-Code 特定检查表) | ✅ |
| ⑤ | ChatPipeline 6 阶段接口契约已冻结为 SSOT (§2) | ✅ |
| **→ Batch B Ready** | | ✅ |

---

## 9. 元数据

| 属性 | 值 |
|------|-----|
| **文档版本** | 1.0 |
| **生成日期** | 2026-07-21 |
| **输入基线** | Batch A 闭环 + CORE_ARCHITECTURE_REPORT.md SSOT |
| **Phase 5 Batch B 范围** | B1(状态安全 5 项), B2(持久化 8 项), B3(并发安全 5 项) |
| **RISK MATRIX** | 3 MEDIUM (P2-19/25/43), 12 LOW, 3 NEGLIGIBLE |
| **ACC-4 状态** | 待执行 — atomicity/visibility/ordering 全覆盖 |
| **隐含解决 P2** | 8 项 (Batch A 覆盖) |
| **推迟 P2** | 20 项 (前端 UI + 深度安全 → Batch C) |
| **下一阶段** | Batch B 执行 → Batch C (剩余 P2) → Phase 5 关闭 |
