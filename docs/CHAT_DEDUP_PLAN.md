# Chat 全链路去重与数据一致性修复计划

> 状态：审计完成，计划待实施  
> 日期：2026-07-26  
> 目标：解决用户问一个问题后页面重复显示、内容生成两遍、刷新后不一致的问题

---

## 目录

1. [问题现象](#1-问题现象)
2. [当前架构全景](#2-当前架构全景)
3. [正常流程追踪](#3-正常流程追踪)
4. [根因分析](#4-根因分析)
5. [Claude Code 参考实现](#5-claude-code-参考实现)
6. [目标架构](#6-目标架构)
7. [分批实施计划](#7-分批实施计划)
8. [验收标准](#8-验收标准)

---

## 1. 问题现象

### 1.1 用户报告

1. **重复显示**：用户问一个问题 → 页面显示问题和流式回答 → 回答完成后，问题和回答又在下面追加了一遍
2. **内容生成两遍**：最新改动后，同一个回答内容生成了两遍
3. **刷新不一致**：页面刷新后展示的内容和流式过程中看到的不一样

### 1.2 期望行为

```
用户发送问题
  → 问题显示一遍（用户消息气泡）
  → 工具调用过程正常推进（thought → tool_call → observation 循环）
  → 最终回答显示一遍（assistant 文本气泡）
  → 刷新后展示完全一致
  → DB 中的数据与页面展示一致
```

---

## 2. 当前架构全景

### 2.1 层级结构

```
┌─────────────────────────────────────────────────────────┐
│  Frontend (React + Zustand)                             │
│  ├─ ChatView.tsx      ← 渲染层                          │
│  ├─ chatStore.ts      ← 状态管理 + WS 事件处理          │
│  └─ types/blocks.ts   ← ContentBlock / StreamingTurn    │
├─────────────────────────────────────────────────────────┤
│  Transport                                            │
│  ├─ WebSocket (/ws/sessions/{id})  ← 实时事件流        │
│  └─ REST (/api/sessions/{id}/timeline) ← DB 重放       │
├─────────────────────────────────────────────────────────┤
│  Backend (Python)                                      │
│  ├─ sessions.py        ← REST 路由 + Run/Turn 创建     │
│  ├─ event_bus.py       ← 事件翻译 + 广播 + 持久化      │
│  ├─ chat_pipeline.py   ← 6 阶段编排                    │
│  ├─ agent_service.py   ← 服务入口                      │
│  └─ runtime.py         ← SessionRuntime.run_session()  │
├─────────────────────────────────────────────────────────┤
│  Storage (SQLite)                                      │
│  ├─ session_messages     ← user/assistant 消息         │
│  ├─ session_trace_events ← WS 事件 (带 seq)            │
│  └─ runs                 ← Run 生命周期                │
└─────────────────────────────────────────────────────────┘
```

### 2.2 两套并存的数据模型（核心矛盾）

| | 旧模型：TimelineItem[] | 新模型：StreamingTurn |
|---|---|---|
| 定义位置 | chatStore timeline 字段 | types/blocks.ts |
| 数据结构 | 扁平列表（msg + ws 事件混合） | 配对结构（userMessage + assistantResponse） |
| 构建方式 | DB 消息 + trace events 按时间排序 | sendChat 创建 → WS 事件 mutate |
| 转换函数 | — | `extractBlocksFromTimeline()` |
| 问题 | 无法表达 turn 语义 | 转换函数有分组 bug |

---

## 3. 正常流程追踪

### 3.1 用户发送消息的完整链路

```
用户输入 "fix the bug" → 点击 Send
│
├─[1] ChatView.handleSend()
│   └─ sendChat(activeId, text, intent)
│
├─[2] chatStore.sendChat()
│   ├─ 生成 clientRequestId (UUID)
│   ├─ 移动旧 activeTurn → completedTurns
│   ├─ 创建新 activeTurn (userMessage.blocks = [{type:"text", content:"fix the bug"}])
│   ├─ 添加 timeline item (user message)
│   └─ POST /api/sessions/{id}/messages  {prompt, idempotency_key}
│       └─ 绑定返回的 turn_id / run_id 到 activeTurn
│
├─[3] Backend POST /messages (sessions.py:496)
│   ├─ CAS 检查 (idempotency + concurrency)
│   ├─ 创建 Run (status: queued) + Turn (run_generation++)
│   ├─ 持久化 user message → session_messages (WITH turn_id)
│   └─ service.run_chat_async() → ChatPipeline.run_in_background()
│
├─[4] ChatPipeline 6 阶段 (chat_pipeline.py:481)
│   ├─ Stage 1: submit_user_prompt (hook boundary)
│   ├─ Stage 2: resolve @mentions
│   ├─ Stage 3: apply model switch
│   ├─ Stage 4: inject session context
│   ├─ Stage 5: build callbacks → stream_cb, text_lifecycle_cb
│   └─ Stage 6: execute → SessionRuntime.run_session()
│
├─[5] SessionRuntime.run_session() (runtime.py:849)
│   ├─ CAS Run queued→running → emit run_started WS event
│   ├─ 构建 ReActAgent + ConversationHistory
│   ├─ EventLog 回调: _append_and_emit
│   │   └─ 每个 agent.task.Event → EventBus.publish()
│   │       └─ _translate_event() → WS 消息
│   │           └─ _publish_msg() → persist to session_trace_events + WS broadcast
│   ├─ agent.run(task, log)  ← ReAct 循环
│   │   ├─ thought_delta (streaming)
│   │   ├─ thought (完整)
│   │   ├─ tool_call → tool_result (observation)
│   │   └─ (循环直到完成或达到 max_steps)
│   ├─ 持久化新消息 → session_messages
│   │   ├─ 中间 assistant/tool 消息
│   │   └─ 最终 assistant 消息 (result.summary)
│   └─ finally: _finalize_run()
│       ├─ CAS Run running→completed
│       └─ emit run_terminal WS event
│
├─[6] Frontend WS 事件处理 (chatStore.handleWsEvent)
│   ├─ run_started → isRunning=true
│   ├─ thought_delta → streamingThought 缓冲区 + activeTurn.blocks
│   ├─ thought → activeTurn.blocks (completed)
│   ├─ tool_call → activeTurn.blocks (running)
│   ├─ observation → activeTurn.blocks (success/error)
│   ├─ assistant_text_start/delta/end → activeTurn.blocks (text streaming)
│   └─ run_terminal (completed)
│       ├─ isRunning=false
│       └─ activeTurn 标记为 completed (但不移动到 completedTurns!)
│
├─[7] 渲染 (ChatView.tsx:933-966)
│   ├─ completedTurns.map() → BlocksMessage (历史 turns)
│   └─ activeTurn → BlocksMessage (当前 turn)
│
└─[8] 完成后的 useEffect (ChatView.tsx:310-322)
    └─ 只调用 refreshActive()，不调用 loadTimeline()
       (注释说明: "Full timeline reload would create duplicate")
```

### 3.2 页面刷新时的链路

```
页面刷新
│
├─ useEffect (activeId 变化)
│   ├─ loadTimeline(activeId, signal)  ← afterSeq=0
│   └─ connectWs(activeId)
│
├─ loadTimeline (chatStore.ts:1122)
│   ├─ GET /api/sessions/{id}/timeline?after_seq=0
│   │   └─ 返回: messages (session_messages) + events (session_trace_events)
│   │       └─ 按 timestamp 排序的扁平列表
│   │
│   ├─ extractBlocksFromTimeline(timelineItems)
│   │   ⚠️ 这里出 bug（见根因 #1）
│   │
│   └─ 构建 completedTurns[] (afterSeq=0 → 替换)
│       ⚠️ completedTurns 中的 turn 数据结构错误
│
└─ 渲染: completedTurns (无 activeTurn)
    ⚠️ 显示内容与流式过程中看到的不一致
```

---

## 4. 根因分析

### 根因 #1：`extractBlocksFromTimeline` 分组逻辑错误 🔴 CRITICAL

**文件**: [web/src/stores/chatStore.ts](web/src/stores/chatStore.ts) `extractBlocksFromTimeline()` 函数

**问题**: 该函数按 **消息 ID** (msgId) 分组，而非按 **turn ID** (turnId)。由于 timeline 中各项按时间戳排序，而 assistant DB 消息的时间戳晚于所有 WS 事件，导致 WS 事件被错误归入 user 消息分组。

**具体场景**:

```
Timeline 排序结果 (按 timestamp):
  t1: user DB message     (turn_id=T1, created_at=10:00:00)
  t2: run_started          (timestamp=10:00:01)
  t3: thought_delta        (timestamp=10:00:02)
  t4: thought              (timestamp=10:00:03)
  t5: tool_call Read       (timestamp=10:00:04)
  t6: observation Read     (timestamp=10:00:05)
  t7: tool_call Edit       (timestamp=10:00:06)
  t8: observation Edit     (timestamp=10:00:07)
  t9: assistant_text_delta (timestamp=10:00:08)
  t10: run_terminal         (timestamp=10:00:09)
  t11: assistant DB message (turn_id=T1, created_at=10:00:10) ← 最后才持久化
```

`extractBlocksFromTimeline` 遍历:
1. t1 (msg source) → 开始新分组 msgId1, role=user, blocks=[text: "fix the bug"]
2. t2-t10 (ws source) → **全部 apply 到 msgId1 的 blocks!**
   - 结果: user message 的 blocks 包含了 thought + tool_call + observation + text_delta!
3. t11 (msg source) → 开始新分组 msgId2, role=assistant, blocks=[text: summary]

`loadTimeline` 构建 completedTurns:
- msgId1 → turn_id=T1 → turn: userMessage=全部blocks(含工具!) + assistantResponse=空
- msgId2 → turn_id=T1 → **被 seenTurnIds 跳过** (同 turn_id)

**结果**: completedTurns 中该 turn 的 userMessage 包含了所有工具调用块，assistantResponse 为空。

### 根因 #2：`activeTurn` 完成后不移动到 `completedTurns` 🔴 CRITICAL

**文件**: [web/src/stores/chatStore.ts](web/src/stores/chatStore.ts) `handleWsEvent` 中 `run_terminal` 处理

**问题**: 当 `run_terminal` (status: "completed") 到达时，`activeTurn` 被标记为 `status: "completed"` 但仍保留在 `activeTurn` 字段。它只在下次 `sendChat` 时被移动到 `completedTurns`。

```typescript
// 当前代码 (chatStore.ts:586-616)
if (re.status === "completed") {
  patchSession(sid, (prev) => {
    // ...
    return {
      ...prev,
      isRunning: false,
      activeTurn: {                          // ← 仍留在 activeTurn!
        ...prev.activeTurn,
        assistantResponse: { ...prev.activeTurn.assistantResponse, blocks, status: "completed" },
        meta: { ...prev.activeTurn.meta, completedAt: Date.now() },
      },
    };
  });
}
```

**为什么这导致重复显示**:
- 渲染层同时渲染 `completedTurns` (来自 DB) + `activeTurn` (来自流式)
- 如果 `loadTimeline` 在某处被调用（WS 重连、手动触发），同一个 turn 同时出现在 `completedTurns` 和 `activeTurn` 中
- 用户看到内容显示两遍

**ChatView.tsx:310-322 的临时规避**:
```typescript
// 故意不调用 loadTimeline 来防止重复
useEffect(() => {
    if (prevRunning.current && !isRunning && activeId) {
      // Full timeline reload would create duplicate — skip it
      useSessionStore.getState().refreshActive();
    }
}, [isRunning, activeId]);
```
这个规避措施意味着：**完成后的 DB 数据永远不会同步到前端**。刷新时使用 DB 数据，但 DB 数据的反序列化 (根因 #1) 本身就是错的。

### 根因 #3：DB Timeline 的 "消息+事件混合排序" 存在时序悖论 🟡 MAJOR

**文件**: [server/routers/sessions.py](server/routers/sessions.py) `get_session_timeline()`

**问题**: `/timeline` 端点将 `session_messages` 和 `session_trace_events` 合并为一个按 `timestamp` 排序的扁平列表。但：

- user message 的 `created_at` = POST 请求时间
- assistant message 的 `created_at` = agent 循环结束后的持久化时间
- WS trace events 的 `timestamp` = agent 循环中的实时时间

因此 assistant DB message 永远排在所有 WS trace events **之后**：

```
user_msg (t1) → [所有 WS 事件] → assistant_msg (t_later)
```

任何按消息边界分组的逻辑都会在此排序下出错。

**正确做法**: timeline 端点应该返回 **以 turn 为分组** 的数据结构，由后端完成分组，而非让前端从扁平列表中猜测。

### 根因 #4：`status: "completed"` 与 `run_terminal` 双通道 🟡 MAJOR

**文件**: [server/events.py](server/events.py), [agent/session/runtime.py](agent/session/runtime.py)

**问题**: 虽然当前 `_translate_event` 已经抑制了 `task_start` 和 `task_complete`，但前端 `handleWsEvent` 中仍保留了两套完成处理逻辑：

1. `status: "completed"` handler (chatStore.ts:674-699) — 旧通道
2. `run_terminal` handler (chatStore.ts:586-633) — 新通道

如果有任何代码路径仍然产生 `status: "completed"` 事件（例如 ChatPipeline 的异常处理、旧版 event log 重放），前端就会两次标记 activeTurn 为 completed。

**搜索现状**:
- `_translate_event()` 中 `task_complete` → 返回 `[]` ✓ 已抑制
- `ChatPipeline.finish()` 注释说明不发送 WS 事件 ✓ 
- 但 `ChatPipeline.run_in_background()` 异常处理仍发送 `status: "failed"` 
- `AgentService.cancel_run()` 发送 `status: "cancelled"`

### 根因 #5：三个 Block Builder 各自为政 🟡 MAJOR

**文件**: [web/src/stores/chatStore.ts](web/src/stores/chatStore.ts)

**问题**: 同一份 assistant 回答的 ContentBlock[] 由三个不同函数构建，逻辑重复且不一致：

| Builder | 位置 | 触发时机 | 处理的 WS 事件 |
|---------|------|---------|---------------|
| `applyWsToBlocks()` | chatStore.ts:165 | WS 实时 (thought_delta, thought, tool_call, observation) | 4 种 |
| `handleWsEvent` run_terminal 分支 | chatStore.ts:593-598 | run_terminal 事件 | 仅 summary→text fallback |
| `extractBlocksFromTimeline()` | chatStore.ts:243 | loadTimeline (DB 重放) | 复用 applyWsToBlocks |

**具体冲突**:
- `applyWsToBlocks` 不处理 `assistant_text_delta` (text 流式走独立分支 chatStore.ts:636-672)
- `extractBlocksFromTimeline` 中，DB assistant 消息 text 和 WS text_delta 可能产生重复 text block
- `run_terminal` 的 summary fallback 在 text 已通过 assistant_text_delta 流式传输时仍可能追加

---

## 5. Claude Code 参考实现

### 5.1 核心架构模式

Claude Code 的 chat 架构遵循以下核心原则：

```
User Input → CLI Parser → Query Engine → LLM API → Tool Loop → Terminal UI
                                ↑
                    AsyncGenerator<Event>
                                ↓
                    MessageTranslator
                                ↓
               Named SSE Events (typed)
                                ↓
              Frontend Store → React Components
```

**关键设计决策**:

1. **单一事件源（Single Event Source）**: Query Engine 产生一个 `AsyncGenerator`，所有事件都从这里流出。不存在 "REST 拉取 + WS 推送" 双通道。

2. **MessageTranslator 作为唯一翻译层**: 将内部的 flat event stream 翻译成语义化的 SSE 事件（`text_delta`, `tool_start`, `tool_call`, `tool_result`, `turn_complete`）。前端只消费翻译后的事件。

3. **Turn 是后端原生概念**: 每个 turn 有明确的 `turn_start` / `turn_complete` 边界。前端不需要从扁平列表中重建 turn。

4. **工具调用有完整生命周期**: `pending → streaming_input → running → complete → error`。每个阶段有独立事件。

5. **SQLite 持久化 + Scrollback**: Session 包含 scrollback buffer，per-turn token 统计。客户端 attach 时先重放 scrollback，再接收 live delta。

### 5.2 我们应该借鉴的模式

| Claude Code 模式 | 我们的现状 | 差距 |
|---|---|---|
| AsyncGenerator 单一事件源 | REST + WS 双通道 | 需要统一为 WS-only 实时 + DB 仅为持久化 |
| Turn 边界由后端明确标记 | 前端从扁平列表推断 turn | 后端应在 timeline API 返回 turn-grouped 数据 |
| MessageTranslator 统一入口 | 3 个 builder 各自构建 | 合并为单一 applyWsToBlocks |
| 工具调用有状态机 | tool_use 只有 running/success/error | 增加 pending/streaming_input 阶段 |
| Scrollback 重放 + Live delta | loadTimeline 全量替换 | 改为 afterSeq 增量合并 |

### 5.3 参考的事件命名规范

```
turn_start          → 新 turn 开始
user_message        → 用户输入（已持久化）
thinking_start      → 模型开始思考
thinking_delta      → 思考流式 token
thinking_end        → 思考完成
text_start          → 回答正文开始
text_delta          → 回答流式 token
text_end            → 回答正文完成
tool_start          → 工具调用开始（pending）
tool_input_delta    → 工具参数流式
tool_call           → 工具调用已发送
tool_result         → 工具结果返回
tool_end            → 工具调用完成（含状态）
permission_request  → 需要用户审批
turn_complete       → Turn 完成（含 summary/stats）
```

---

## 6. 目标架构

### 6.1 核心原则

1. **Turn 是原子单元**：一个 turn = user_message + [tool_calls...] + assistant_response。后端和前端都以此为单位。
2. **单一 Block Builder**：`applyWsToBlocks` 是所有事件→ContentBlock 的唯一入口。
3. **WS 是实时通道，DB 是持久化通道**：两者数据格式一致，通过 seq 做增量同步。
4. **Turn 完成后立即归档**：`run_terminal` → activeTurn 移到 completedTurns。
5. **后端返回 turn-grouped 数据**：`/timeline` 返回的结构直接对应 `StreamingTurn[]`。

### 6.2 差距对照表（含覆盖决策）

| # | Claude Code 模式 | 我们的差距 | 计划覆盖 | 决策 |
|---|-----------------|-----------|---------|------|
| 1 | AsyncGenerator 单一事件源 | REST + WS 双通道 | ⚠️ 部分 | REST 发起+WS 推送合理，问题在于 /timeline 混排，Batch 2 已解决 |
| 2 | Turn 边界后端明确标记 | 前端从扁平列表推断 turn | ✅ 完整 | Batch 2 — 后端 /timeline 返回 turn-grouped 数据 |
| 3 | MessageTranslator 统一入口 | 3 个 builder 各自构建 | ✅ 完整 | Batch 1 — `applyWsToBlocks` 成为唯一入口 |
| 4 | 工具调用有状态机 | 只有 running/success/error | ❌ 不覆盖 | 功能增强，跟去重无关，作为后续独立任务 |
| 5 | Scrollback 重放 + Live delta | loadTimeline 全量替换 | ✅ 本次补充 | 见下方 6.3 节详细方案 |

### 6.3 目标数据流

```
用户发送消息
│
├─ 前端: 创建 activeTurn (optimistic)
├─ POST /messages → 后端: 创建 Run + Turn + 持久化 user message
│
├─ WS 实时流:
│   run_started → 确认 run 开始
│   thought_delta → activeTurn.blocks (streaming thought)
│   thought → activeTurn.blocks (completed thought)
│   tool_call → activeTurn.blocks (running)
│   observation → activeTurn.blocks (success/error)
│   assistant_text_delta → activeTurn.blocks (streaming text)
│   run_terminal → 归档 activeTurn → completedTurns
│
├─ 渲染:
│   completedTurns (历史) + activeTurn (进行中)
│   run_terminal 后: activeTurn=null，所有 turns 都在 completedTurns
│
├─ 刷新 (页面加载):
│   GET /timeline?after_seq=0 → 后端返回 turn-grouped 数据
│   → 填充 completedTurns，设置 lastTraceSeq
│   → WS 连接后增量同步
│
└─ WS 重连 (断线恢复):
    GET /timeline?after_seq={lastTraceSeq} → 仅返回新事件
    → 如有新的 run_terminal → 归档对应 turn
    → 不重新加载已有 turns (避免 flicker)
```

### 6.4 关键设计：Scrollback 重放 + Live Delta 的具体方案

这是差距 #5 的解决方案，也是当前 `loadTimeline` 需要改变的核心逻辑。

**当前问题**:
```typescript
// afterSeq=0 时全量替换 completedTurns，丢弃 live streaming 状态
completedTurns: afterSeq > 0
  ? mergeTurnsByTurnId(prev.completedTurns, newCompleted)
  : newCompleted,  // ← 直接替换！
```

**目标行为**: 三种场景，三种合并策略：

```
场景 A: 页面首次加载 (afterSeq=0, activeTurn=null)
  策略: 直接替换 completedTurns，设置 lastTraceSeq
  原因: 没有 live 状态需要保留

场景 B: WS 重连 (afterSeq > 0, activeTurn 可能非 null)
  策略: DB 已有 turns 不重载；仅加载 afterSeq 之后的新 trace events
        → 如果新 events 包含 run_terminal → 归档 activeTurn
        → 如果新 events 包含 turn 中间事件 → mergeTurnsByTurnId
  原因: 避免 flicker，只补充缺失的增量

场景 C: run_terminal 后的 DB 同步 (afterSeq > 0, activeTurn=null)
  策略: 加载 afterSeq 之后的新 trace events
        → 验证 completedTurns[-1] 与 DB 一致
        → 不一致时用 DB 版本替换最后一个 turn
  原因: 确保 DB 数据和前端状态最终一致
```

**`loadTimeline` 修改后的核心逻辑**:

```typescript
loadTimeline: async (sessionId, signal, afterSeq = 0) => {
    const response = await api.getTimeline(sessionId, signal, afterSeq);
    
    patchSession(sessionId, (prev) => {
      // 场景 A: 首次加载 → 直接替换
      if (afterSeq === 0) {
        return {
          ...prev,
          completedTurns: response.turns.map(buildTurnFromTimeline),
          lastTraceSeq: response.last_seq,
          // 保留可能从 run_terminal 来的 activeTurn (unlikely on first load)
          activeTurn: prev.activeTurn,
        };
      }
      
      // 场景 B/C: 增量同步
      const dbTurns = response.turns.map(buildTurnFromTimeline);
      let merged = mergeTurnsByTurnId(prev.completedTurns, dbTurns);
      
      // 如果 activeTurn 对应的 turn 出现在 DB turns 中（run_terminal 已持久化）
      if (prev.activeTurn?.turnId) {
        const dbVersion = dbTurns.find(t => t.turnId === prev.activeTurn!.turnId);
        if (dbVersion) {
          // DB 版本是权威的，但 live streaming blocks 更完整
          // 合并策略：DB blocks 为基础 + live-only blocks 补充
          merged = mergeTurnsByTurnId(merged, [{
            ...dbVersion,
            assistantResponse: {
              ...dbVersion.assistantResponse,
              blocks: mergeBlocks(dbVersion.assistantResponse.blocks, prev.activeTurn.assistantResponse.blocks),
            },
          }]);
          return {
            ...prev,
            completedTurns: merged,
            activeTurn: null,  // 归档完成
            lastTraceSeq: Math.max(prev.lastTraceSeq, response.last_seq),
          };
        }
      }
      
      return {
        ...prev,
        completedTurns: merged,
        lastTraceSeq: Math.max(prev.lastTraceSeq, response.last_seq),
      };
    });
}
```

**关键点**:
- `afterSeq=0` 永远不会在 run 进行中被调用（由 ChatView useEffect 控制）
- `afterSeq>0` 只在 WS 重连或 run_terminal 后的 DB 同步时调用
- 不做全量替换，只做增量合并
- DB 版本是权威基础，live streaming blocks 补充 DB 中缺失的中间状态

### 6.3 目标 Timeline API 响应格式

```typescript
// GET /api/sessions/{id}/timeline 目标格式
{
  session_id: string,
  turns: [
    {
      turn_id: string,
      run_id: string,
      turn_index: number,
      user_message: { id: string, content: string, created_at: string },
      assistant_blocks: [
        { type: "thought", content: "...", summary: "..." },
        { type: "tool_use", id: "tc_1", name: "Read", input: {...}, status: "success", output: "..." },
        { type: "text", content: "final answer markdown..." },
      ],
      meta: { steps: 5, tokens: 1234, started_at: "...", completed_at: "..." },
    }
  ],
  last_seq: number,
  active_run: { run_id: string, status: string } | null,
}
```

---

## 7. 分批实施计划

### Batch 1: 修复前端 Block Builder 统一入口 ✅ (1-2 天)

**目标**: 消除 3 个 builder 的重复逻辑，建立 `applyWsToBlocks` 作为唯一入口。

**改动范围**: 仅 `chatStore.ts`

**具体步骤**:

1. **扩展 `applyWsToBlocks` 支持所有事件类型**
   - 添加 `assistant_text_start/delta/end` 处理（从 handleWsEvent 独立分支移入）
   - 添加 `status` / `run_terminal` 的 summary→text fallback
   - 添加 `run_started` / `subagent_start/stop` 等非 block 事件的 no-op

2. **删除 `handleWsEvent` 中的独立 text streaming 分支** (chatStore.ts:636-672)
   - 将 text streaming 逻辑合并到 `applyWsToBlocks`
   - `handleWsEvent` 中只保留：调用 `applyWsToBlocks` + 元数据更新

3. **删除 `handleWsEvent` `run_terminal` 中的 summary fallback** (chatStore.ts:593-598)
   - 移到 `applyWsToBlocks` 中统一处理

4. **重写 `extractBlocksFromTimeline` 使用新的 `applyWsToBlocks`**
   - 增量重构（Batch 2 会彻底替换）

**验收**:
- 所有现有 WS 事件类型在 `applyWsToBlocks` 中有对应处理
- `handleWsEvent` 不再直接操作 blocks 数组
- 流式过程中 blocks 展示与改动前一致

---

### Batch 2: 后端 Timeline API 返回 turn-grouped 数据 ✅ (2-3 天)

**目标**: `/timeline` 返回按 turn 分组的数据，前端不再需要从扁平列表重建。

**改动范围**: `sessions.py`, `session_service.py`, `chatStore.ts`

**具体步骤**:

1. **新增后端方法 `build_turn_timeline()`** (session_service.py 或 sessions.py)
   ```python
   def build_turn_timeline(session_id: str) -> list[dict]:
       """按 turn_id 分组，返回结构化的 turn 列表。
       
       每个 turn:
         - user message (从 session_messages)
         - assistant blocks (从 session_trace_events 重建)
         - meta (从 runs 表)
       """
   ```
   - 关键：**不再按时间戳混排** messages 和 events
   - 用 `turn_id` 关联 user message、runs、trace events
   - trace events 按 seq 排序后通过 `_translate_event` 转为 WS 格式
   - 以 turn 为单位输出，包含 user_message + assistant_blocks

2. **更新 `/timeline` 端点** (sessions.py:440)
   - 返回 `{ session_id, turns: [...], last_seq, active_run }`
   - 保留旧格式兼容性（`items` 字段），前端渐进迁移

3. **重写前端 `loadTimeline`** (chatStore.ts:1122) — 三种场景合并策略
   - **场景 A (afterSeq=0)**: 直接替换 `completedTurns`，设置 `lastTraceSeq`
   - **场景 B (afterSeq>0, WS 重连)**: 只加载新 trace events，`mergeTurnsByTurnId` 增量合并，避免 flicker
   - **场景 C (afterSeq>0, run_terminal 后同步)**: 验证最后一个 turn 与 DB 一致，不一致时用 DB 修正
   - 详见 6.4 节

4. **删除 `extractBlocksFromTimeline`**
   - 不再需要从前端扁平列表重建 turn 结构
   - 后端 `/timeline` 直接返回 turn-grouped 数据

**验收**:
- `/timeline` 返回的 turns 数量 = 已完成的 turn 数量
- 每个 turn 的 user_message 只有 text block，assistant_blocks 包含所有工具调用和回答
- 刷新页面后展示内容与流式过程中一致
- WS 重连不产生 flicker 或重复
- 旧 `items` 格式仍可用（兼容性测试）

---

### Batch 3: 修复 Turn 生命周期管理 ✅ (1-2 天)

**目标**: `run_terminal` 后将 activeTurn 正确归档到 completedTurns。

**改动范围**: `chatStore.ts`, `ChatView.tsx`

**具体步骤**:

1. **修复 `run_terminal` (completed) 处理** (chatStore.ts:586)
   ```typescript
   // 修改后：completed 时也移动到 completedTurns
   if (re.status === "completed") {
     patchSession(sid, (prev) => {
       if (!prev.activeTurn) return { ...prev, isRunning: false };
       const completedTurn = {
         ...prev.activeTurn,
         turnId: re.turn_id || prev.activeTurn.turnId,
         runId: re.run_id || prev.activeTurn.runId,
         assistantResponse: { ...prev.activeTurn.assistantResponse, status: "completed" },
         meta: { ...prev.activeTurn.meta, completedAt: Date.now() },
       };
       return {
         ...prev,
         isRunning: false,
         steps: re.steps_taken ?? prev.steps,
         tokens: re.total_tokens ?? prev.tokens,
         streamingThought: "",
         activeTurn: null,
         completedTurns: upsertByTurnId(prev.completedTurns, completedTurn),
       };
     });
   }
   ```

2. **移除 `sendChat` 中的旧 activeTurn 归档逻辑** (chatStore.ts:959-978)
   - 因为 `run_terminal` 已经归档了，`sendChat` 时不应再有 activeTurn
   - 添加防御性检查：如果仍有 activeTurn（异常情况），则归档它

3. **移除 ChatView 中的规避 useEffect** (ChatView.tsx:310-322)
   - 删除当前的规避逻辑（"Full timeline reload would create duplicate — skip it"）
   - 改为：`run_terminal` 后调用 `loadTimeline(activeId, undefined, lastTraceSeq)` 做增量 DB 同步
   - 由于 `afterSeq > 0`，走场景 C：只验证最后一个 turn，不一致时修正
   - 确保 DB 和前端状态最终一致，但不产生重复

4. **移除 `sendChat` 中的旧 activeTurn 归档逻辑** (chatStore.ts:959-978)
   - 因为 `run_terminal` 已经将 activeTurn 归档到 completedTurns
   - `sendChat` 时 activeTurn 应该已经是 null
   - 保留防御性检查：如果意外仍有 activeTurn，归档为 error 状态

4. **统一 `status: "completed"` 和 `run_terminal` 处理**
   - `status: "completed"` → 降级为仅设置 isRunning=false（向后兼容）
   - `run_terminal` → 唯一的 turn 归档入口
   - 添加日志：如果同一 turn 收到两次 terminal 事件，记录 warning

**验收**:
- 用户发一个问题 → 回答完成后 → activeTurn 为 null，completedTurns 包含该 turn
- 页面不再出现重复显示
- 刷新后展示与完成时一致
- `sendChat` 时不再需要处理残留的 activeTurn

---

### Batch 4: 消除重复事件源 ✅ (1 天)

**目标**: 确保每个 run 只产生一条 terminal 事件。

**改动范围**: `runtime.py`, `event_bus.py`, `chat_pipeline.py`, `agent_service.py`

**具体步骤**:

1. **审计所有发送 `status` 事件的代码路径**
   ```bash
   grep -rn "status.*completed\|status.*failed\|publish_raw.*status" server/ agent/
   ```
   - 确保只有 `_finalize_run` 发送 run 完成事件
   - ChatPipeline 异常处理 (`chat_pipeline.py:504`) → 改为发送 `run_terminal`

2. **移除 `_translate_event` 中遗留的旧 task_complete 处理**
   - 当前已被抑制（返回 `[]`），确认无回归

3. **前端添加 terminal 事件幂等性**
   - 同一 run_id 的 `run_terminal` 只处理一次
   - 同一 run_id 的 `status: "completed"` 在 `run_terminal` 已处理后忽略

**验收**:
- 一次 run 只产生一个 terminal 事件（抓包或日志验证）
- 发送一条消息，前端 state 中 activeTurn 只归档一次

---

### Batch 5: 端到端测试 + 回归修复 ✅ (1-2 天)

**目标**: 确保所有改动不引入回归，覆盖关键场景。

**测试场景**:

1. **基本场景**
   - [ ] 用户发一条消息 → 正常流式 → 回答显示一次 → 刷新后一致
   - [ ] 用户连续发多条消息 → 每条正常显示 → 无重复 → 刷新后一致

2. **边界场景**
   - [ ] 发送消息后立即刷新（agent 运行中）
   - [ ] 网络断开后重连（WS reconnect）
   - [ ] 发送后立即取消（cancel）
   - [ ] plan mode → approve → build 完整流程

3. **DB 一致性**
   - [ ] `session_messages` 中的 assistant 消息与页面展示一致
   - [ ] `session_trace_events` 中的事件可通过 `/timeline` 完整重放
   - [ ] 删除 session 后无残留数据

4. **性能**
   - [ ] Timeline API 返回时间 < 200ms (100 条消息以内)
   - [ ] WS 事件处理不造成 UI 卡顿

---

## 8. 验收标准

### 8.1 总体验收

- [ ] 用户发一条消息，问题和回答各显示**恰好一遍**
- [ ] 工具调用过程正常推进（thought → tool_call → observation 循环可见）
- [ ] 回答完成后不出现重复内容
- [ ] 页面刷新后展示与流式过程中**完全一致**
- [ ] 连续多轮对话无重复、无丢失
- [ ] `session_messages` 中数据与页面展示**完全一致**
- [ ] 取消、异常等边界情况正确处理

### 8.2 代码质量

- [ ] `extractBlocksFromTimeline` 被删除
- [ ] `applyWsToBlocks` 是所有 event→block 转换的唯一入口
- [ ] `handleWsEvent` 不直接操作 blocks 数组
- [ ] `activeTurn` 在 `run_terminal` 后为空 (null)
- [ ] Timeline API 返回 turn-grouped 数据
- [ ] 无新增 TypeScript 编译错误
- [ ] 无新增 Python lint 错误

### 8.3 可观测性

- [ ] 重复 terminal 事件有 warning 日志
- [ ] Timeline API 有性能日志
- [ ] Turn 归档有 debug 日志

---

## 附录 A：关键文件清单

| 文件 | 角色 | 改动批次 |
|------|------|---------|
| `web/src/stores/chatStore.ts` | 前端状态管理 + WS 处理 + Block Builder | Batch 1, 2, 3, 4 |
| `web/src/components/ChatView.tsx` | 渲染层 | Batch 3 |
| `web/src/types/blocks.ts` | ContentBlock / StreamingTurn 类型 | Batch 1 (可能扩展) |
| `web/src/types/events.ts` | WS 事件类型 | Batch 4 (可能清理) |
| `server/routers/sessions.py` | REST API + Timeline 端点 | Batch 2 |
| `server/services/session_service.py` | 会话查询服务 | Batch 2 |
| `server/events.py` | WS 事件定义 | Batch 4 (审计) |
| `server/services/event_bus.py` | 事件翻译 + 广播 | Batch 4 (审计) |
| `server/services/chat_pipeline.py` | 6 阶段编排 | Batch 4 (审计) |
| `agent/session/runtime.py` | run_session + _finalize_run | Batch 4 (审计) |
| `app/storage/sqlite.py` | DB 持久化 | Batch 2 (可能需要新增查询) |

## 附录 B：依赖关系

```
Batch 1 (Block Builder 统一)
  ↓
Batch 2 (Timeline API 重构) ← 可以与 Batch 1 并行
  ↓
Batch 3 (Turn 生命周期修复) ← 依赖 Batch 1 + 2
  ↓
Batch 4 (消除重复事件源)   ← 可以与前序并行
  ↓
Batch 5 (端到端测试)       ← 依赖所有前序
```

---

> **核心理念**: 数据流应该是单向的、可预测的。每个 turn 只创建一次，只归档一次，只渲染一次。当发现需要"规避"一个 bug 时（如 `useEffect` 中故意不调用 `loadTimeline`），说明上游的数据模型设计有问题，应该修复上游而非在下游规避。
