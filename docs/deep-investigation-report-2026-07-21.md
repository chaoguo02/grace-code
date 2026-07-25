# 深度调查：待处理问题根因分析

> 基于 master 分支 `ab70813` 代码 | 调查日期：2026-07-21
> 前序审计：[web-audit-report-2026-07-21.md](web-audit-report-2026-07-21.md)

---

## 先决结论：master 已解决的问题

以下 v2 修复已自动在 master 上生效，无需再次处理：

| 修复项 | 状态 | master 证据 |
|---|---|---|
| P0-1 StatsDashboard `!cancelled` | ✅ 已修复 | `if (cancelled) return;` + 新增 error state + retry |
| P0-2 空壳 Tasks/Events Tab | ✅ 已修复 | TABS 仅 5 个、无 PlaceholderView |
| P1-6 EventSidebar 3s debounce | ✅ 已修复 | 无 setTimeout，on-mount 一次性 fetch |
| Share 按钮 | ✅ 已移除 | App.tsx 中无 Share 相关内容 |
| session_service token 估算 | ✅ 已修复 | `//3` + 注释 |
| event_log.py contract 参数 | ✅ 已移植 | `log_task_complete(steps, summary, contract=None)` |
| _translate_event plan_ready 检测 | ✅ 已移植 | 检测 `payload.contract` 并产出 `WsPlanReady` |
| ErrorBoundary 包裹 | ✅ 新增 | sidebar、EventSidebar 均有 ErrorBoundary |
| SessionSidebar 可访问性 | ✅ 新增 | role/tabIndex/aria/keyboard |
| watchdog 定时器管理 | ✅ 改进 | 模块级 `clearWatchdog()` + 多处清除 |

---

## 问题 1: plan_ready 双重发射 (P1-1 深化)

### 表面现象
plan session 完成时，前端可能收到两个 `plan_ready` WS 事件。

### 根因链路

**路径 A — EventLog 翻译管道：**
```
agent loop 完成 → log_task_complete(steps, summary, contract)  [agent/core.py:1570]
  → EventLog._append(Event(TASK_COMPLETE, payload={steps, summary, contract}))
  → event_callback(Event)  →  EventBus.publish(Event)
  → _translate_event(Event)  [event_bus.py:128-144]
  → contract 非空 → 返回 [WsPlanReady(...)]  →  WS queue →  前端
```

**路径 B — ChatPipeline.finish() 直接推送：**
```
ChatPipeline._pipeline()  [chat_pipeline.py:327-348]
  → execute(ctx) → result
  → finish(ctx, result)  [chat_pipeline.py:287-319]
  → _has_plan = (_is_plan or bool(result.contract))  # line 297
  → _has_plan 为 True → publish_typed(WsPlanReady(...))  # line 303 →  WS queue →  前端
```

**结论：plan_ready 被发射两次。** 路径 A 是正确的（通过事件管道），路径 B 是冗余的（ChatPipeline 不应再手动发射）。

### 涉及代码

| 文件 | 行号 | 角色 |
|---|---|---|
| `agent/core.py` | 1570 | `log_task_complete(contract=...)` — 写入 EventLog |
| `server/services/event_bus.py` | 128-144 | `_translate_event` — 检测 contract → `WsPlanReady` |
| `server/services/chat_pipeline.py` | 297-313 | `finish()` — **冗余** `publish_typed(WsPlanReady)` |

### 次级问题：非 plan session 无完成事件

当 `_has_plan = False` 时：
- `_translate_event` 对无 contract 的 `task_complete` 返回 `[]`（空数组）— [event_bus.py:144](server/services/event_bus.py#L144)
- `finish()` 的 else 分支执行 `pass` — [chat_pipeline.py:319](server/services/chat_pipeline.py#L319)
- **结果：无 `status: completed` WS 事件**。前端依靠模型最后一条 assistant message 隐式判断完成，或等待 30 分钟 watchdog 超时

### 修复方向

1. **删除 `finish()` 中的 `publish_typed(WsPlanReady)` 调用** — 路径 A 已经正确处理
2. **恢复非 plan session 的 `status: completed` 事件发射** — `_translate_event` 对无 contract 的 `task_complete` 应返回 `WsStatus(status="completed")` 而非空数组
3. **`finish()` 应只负责清理工作**（如 release_session），事件发射全权交给 EventLog 管道

---

## 问题 2: Plan/Build 共享 session_id (P1-2 深化)

### 表面现象
PlanView 在 approve 后无法显示已审批的 plan，因为 `agent_name` 已变为 "build"。

### 根因链路

```
用户点击 Approve & Build
  → POST /api/sessions/{id}/approve  [approvals.py:36]
  → update_agent_name(session_id, "build")  [approvals.py:97]  ← DB 写入
  → remove_plan_file(session_id)  [approvals.py:102]  ← 删除 .grace/plans/{id}.md
  → run_chat_async(session_id, agent_name="build")  [approvals.py:107]  ← 同 session 启动 build
```

前端 PlanView 状态判断逻辑：
```javascript
// PlanView.tsx:70-72
const isPlanSession = activeDetail?.agent_name === "plan";  // approve 后 → false
const hasPlan = !!planApproval?.isWaiting;                   // sendChat 清除 → false
const isCompleted = !hasPlan && isPlanSession && ...;        // isPlanSession=false → false
const showPlanCard = hasPlan || isCompleted;                 // → false
```

**结果：`showPlanCard = false` → PlanView 显示 "No plan has been generated yet"**，即使 session 刚刚完成了一次 plan → build 转换。

### 真正的影响

| 层面 | 影响 |
|---|---|
| PlanView | approve 后 plan card 消失，用户看不到已审批的 plan 内容 |
| 对话历史 | plan 分析消息和 build 执行消息混在同一 timeline 中 |
| `agent_name` | 从 "plan" → "build"，丢失了 session 最初是 plan session 的元信息 |
| plan 文件 | `remove_plan_file()` 删除磁盘文件，无法事后查看 |
| session list | 无法区分"纯 build session"和"plan→build session" |

### 设计评估

这不是简单的 bug，而是架构决策：**是否应该让 plan 和 build 共享 session_id？**

Claude Code 的做法：plan 和 build 在**同一对话**中进行（plan 消息后紧跟 build 消息），但通过 metadata 标记阶段转换。Grace Code 模仿了这个模式，但缺少 metadata 保留。

**短期可行方案**（不拆 session）：
1. `approve` 时不删除 plan file — 保留到 session 结束
2. `update_agent_name` 时同时写入 `metadata.plan_approved_at` 时间戳
3. 新增 `metadata.plan_phase = "build"` 标记当前阶段
4. PlanView 增加判断：`activeDetail?.metadata?.plan_approved_at != null` → 显示 "Plan approved — build in progress"
5. `planText` 优先从 plan file 读取（如果还存在），fallback 到 `activeDetail.summary`

**长期方案**（拆 session）：plan 为父 session，approve 时 spawn 子 build session。复杂但语义清晰。

---

## 问题 3: tool_summary 序列化边界 (P1-4 深化)

### 表面现象
SessionStats 的 `tool_summary` 字段在 TypeScript 类型中定义为 `Record<string, number>`，但 API 返回的是 JSON 字符串。

### 全链路追踪

```
写入：
  StatsRecorder.record_session_end()            [stats_recorder.py:60-92]
    → 从 step_log 表聚合 tool_summary: dict[str, int]
    → StatsService.record_session_complete(tool_summary=dict)
    → json.dumps(tool_summary)                  [stats_service.py:46]
    → StorageBackend.upsert_session_stats(tool_summary=str)  [sqlite.py:354]
    → SQLite TEXT 列存储

读取（直接路径）：
  StorageBackend.get_session_stats()            [sqlite.py:469-478]
    → dict(row) — tool_summary 仍是 JSON 字符串
    → StatsService 无解析层
    → /api/stats/sessions 直接返回              [stats.py:45-50]
    → 前端收到 tool_summary: string
    → TypeScript 类型声明: Record<string, number>  ← 不一致！

读取（tools 端点有解析）：
  /api/stats/tools                               [stats.py:84-93]
    → json.loads(stats.get("tool_summary", "{}"))  ← 手动解析
    → 正确返回 Record<string, number>
```

### 实际影响

**当前无功能影响** — `StatsDashboard.tsx` 和 `SessionStatsDrawer.tsx` 都不使用 `tool_summary` 字段（只展示 steps/tokens/duration/status）。但如果未来任何前端代码尝试 `Object.entries(session.tool_summary)`，会在运行时收到 string 而非 object，导致渲染崩溃。

### 修复方向

**方案 A（推荐）— 后端统一解析**：在 `StatsService.get_session_stats()` 中执行 `json.loads(tool_summary)`，确保 API 契约返回 object

**方案 B — 前端兼容**：将 TypeScript 类型改为 `string`，使用时 `JSON.parse()`

方案 A 更干净，但需要检查 `tool_summary` 的解析是否会破坏其他调用方（如 `/api/stats/tools` 已自行解析）。

---

## 问题 4: sendChat planApproval 双重点击 (P1-7)

### 实际状态：已充分防护

经过三层防护，双重点击的风险极低：

| 层 | 机制 | 代码位置 |
|---|---|---|
| UI | 按钮 `disabled={isRunning}` | [ChatView.tsx](web/src/components/ChatView.tsx) |
| Store | `isRunning: true` 在 `sendChat` 首行设置 | [chatStore.ts:487](web/src/stores/chatStore.ts#L487) |
| Backend | `try_acquire_session()` TOCTOU 锁 | [agent_service.py:586](server/services/agent_service.py#L586) |

唯一理论上的窗口：用户在 Zustand 状态更新到 React 重新渲染之间的微秒级间隔内点击第二次。Zustand 是同步更新的，React 18 的自动批处理进一步缩小了这个窗口。

**结论：P1-7 不需要额外修复。** 已有三层充分防护。

---

## 问题 5: WS 事件重复 (B3-1 深化)

### 真正的重复来源：不是 event_id 问题，是架构重复

前端 handleWsEvent 不需要 event_id 去重，因为正常路径下事件不会重复。**真正的重复来自问题 1（plan_ready 双重发射）**。

修复问题 1 后，正常的事件流不会产生重复。因此 B3-1 的 event_id 去重优先级降低。

### 额外的去重场景

一个值得关注的去重场景：**`loadTraceEvents` + WS 实时流的时间窗口**。
- 页面加载时：`loadTraceEvents` 加载历史事件到 timeline
- 同时：WS 连接可能重放最后一个事件
- 结果：timeline 末尾出现重复的最后一个事件

修复方案：`handleWsEvent` 在追加到 timeline 前，检查 `prev.timeline` 的最后一项是否与当前事件相同（按 event_id 比较）。

---

## 问题 6: 新增问题 — ChatPipeline.finish() 不应承担事件发射

### 问题

[chat_pipeline.py:287-319](server/services/chat_pipeline.py#L287) 的 `finish()` 方法承担了事件发射责任，但这应该是 EventLog 管道的职责。当前架构中：

- `_translate_event` → 负责从 EventLog 事件翻译为 WS 消息（正确的关注点分离）
- `finish()` → 也负责发射 WS 事件（违反单一职责，导致问题 1 的双重发射）

### 修复方向

`finish()` 应简化为纯清理方法：
```python
def finish(self, ctx, result):
    # Only cleanup — event emission is handled by EventLog → _translate_event pipeline
    self._runtime.release_session(ctx.session_id)
    self._runtime.release_backend_for_session(ctx.session_id)
```

所有 WS 事件发射由 `EventLog._append → event_callback → EventBus.publish → _translate_event` 管道统一处理。

---

## 问题 7: 新增问题 — 非 plan session 无完成信号

### 根因

[event_bus.py:142-144](server/services/event_bus.py#L142)：
```python
# Non-plan completion: the model's last assistant message IS the
# completion notification — no redundant WsStatus needed.
return []
```

设计假设："模型最后一条消息就是完成通知"。但在以下场景会出问题：
1. 模型输出被截断（max_tokens 耗尽）— 无完成消息
2. 网络中断 — 最后几条 WS 事件丢失
3. 快速完成的简单任务 — 前端可能在收到全部事件前已渲染 "running" 状态

### 修复方向

恢复 `status: completed` 事件，无论是否有 plan contract：
```python
if ev_type == "task_complete":
    msgs = [WsStatus(status="completed", result={...}).to_dict()]
    contract = payload.get("contract")
    if contract:
        msgs.append(WsPlanReady(...).to_dict())
    return msgs
```

---

## 完整问题清单（按优先级排序）

| # | 问题 | 严重度 | 涉及文件 | 修复复杂度 |
|---|---|---|---|---|
| 1 | **plan_ready 双重发射** — finish() 与 _translate_event 重复 | P0 | `chat_pipeline.py:287-319`, `event_bus.py:128-144` | 低（删除 6 行） |
| 2 | **非 plan session 无完成事件** — _translate_event 返回空数组 | P1 | `event_bus.py:142-144` | 低（改 2 行） |
| 3 | **审批后 PlanView 无法显示 plan** — agent_name 变更 + plan file 删除 | P1 | `approvals.py:97,102`, `PlanView.tsx:70-72` | 中（metadata 标记 + PlanView 逻辑） |
| 4 | **tool_summary 类型不一致** — API 返回 string，TS 类型声明 object | P2 | `stats_service.py`, `stats.py:45-50`, `stats.ts:9` | 低（加 json.loads） |
| 5 | **ChatPipeline.finish() 职责过载** — 不应发射事件 | P2 | `chat_pipeline.py:287-319` | 低（随 #1 一起修） |
| 6 | **loadTraceEvents + WS 重连边界** — 最后一个事件可能重复 | P2 | `chatStore.ts:handleWsEvent` | 中（加去重逻辑） |
| 7 | **Plan/Build 共享 session_id** — 长期架构债务 | P3 | `approvals.py`, `PlanView.tsx` | 高（需大重构） |

---

## 立即可执行的修复计划

### 批次 1：修复双重发射 + 完成信号（3 个文件，~10 行改动）

1. **`server/services/chat_pipeline.py:287-319`** — 删除 `finish()` 中的事件发射逻辑，仅保留清理
2. **`server/services/event_bus.py:142-144`** — `task_complete` 无 contract 时返回 `WsStatus(status="completed")` 而非空数组
3. **`web/src/stores/chatStore.ts:646-666`** — `loadTraceEvents` 中 plan_ready 恢复逻辑保持不变（不受影响，因为事件仍在 EventLog 中）

### 批次 2：PlanView 审批后显示修复（2 个文件）

1. **`server/routers/approvals.py:97,102`** — 不删除 plan file，写入 `metadata.plan_approved_at`
2. **`web/src/components/PlanView.tsx:70-72`** — 增加 `planApprovedAt` 判断，显示 "Plan approved — build in progress"

### 批次 3：序列化 + 去重（3 个文件）

1. **`server/services/stats_service.py`** — `get_session_stats` 中 json.loads(tool_summary)
2. **`web/src/stores/chatStore.ts`** — `handleWsEvent` 追加去重（检查 event_id）
