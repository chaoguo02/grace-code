# StreamingSession 模型 — 流式会话数据所有权重构

> 前置：方案二（乐观注入）已作为临时补丁生效。本文档定义长期方案。

## 问题定义

当前 `SessionUiState` 使用三个独立字段描述一轮对话：

```typescript
streamingBlocks: ContentBlock[];        // WS 实时增量
dbBlocksByMsgId: Record<string, {...}>;  // DB 加载的完整消息
timeline: TimelineItem[];               // 旧格式（已废弃渲染，仅用于欢迎屏判断）
```

**根因**：这三个字段没有生命周期关联。一轮对话从 `sendChat` 开始、到 `loadTimeline` 替换结束，中间经历了三个数据源的接力——但没有任何一个对象"拥有"这轮对话的完整上下文。

## 目标模型：StreamingTurn

```typescript
interface StreamingTurn {
  /** Stable turn ID — survives streaming→DB transition without remount. */
  id: string;                    // turn_{sessionId}_{generation}

  /** User message (always present from sendChat). */
  userMessage: {
    id: string;                  // tempId → DB realId on loadTimeline
    blocks: ContentBlock[];      // [{type:"text", content:"你好"}]
  };

  /** Assistant response (built incrementally from WS). */
  assistantResponse: {
    id: string;                  // tempId → DB realId
    blocks: ContentBlock[];      // text + thought + tool_use, mutated by WS
    status: 'streaming' | 'completed' | 'error';
  };

  /** Metadata — accumulated as WS events arrive. */
  meta: {
    steps: number;
    tokens: number;
    startedAt: number;           // Date.now() at sendChat
    completedAt?: number;
    eventSeq: number;            // monotonic WS event counter, starts at 1
    hasGap: boolean;             // true when seq discontinuity detected
  };
}

interface SessionUiState {
  // Replaces: streamingBlocks + dbBlocksByMsgId + timeline
  activeTurn: StreamingTurn | null;
  completedTurns: StreamingTurn[];  // past turns from DB
  // ... other fields unchanged
}
```

### 生命周期

```
sendChat("你好")
  │
  ├─ 创建 StreamingTurn {
  │    id: "turn_sess123_5",
  │    userMessage: { id: "temp_001", blocks: [{type:"text", content:"你好"}] },
  │    assistantResponse: { id: "temp_002", blocks: [], status: 'streaming' },
  │  }
  │
  ├─ WS thought_delta → append to assistantResponse.blocks
  ├─ WS tool_call    → push to assistantResponse.blocks (status:'running')
  ├─ WS observation  → update matching tool_use block (status:'success'/'error')
  │
  ├─ WS completed    → assistantResponse.status = 'completed'
  │
  └─ loadTimeline()  → DB returns real messages
       │
       ├─ integrity_check(activeTurn, dbResponse)
       │   ├─ ✅ pass → replace tempIds with realIds, keep block content
       │   └─ ❌ fail → move activeTurn to completedTurns, create from DB
       │
       └─ activeTurn = null (turn is now in completedTurns)
```

### 关键差异 vs 当前方案

| | 当前（方案二） | StreamingTurn |
|---|---|---|
| 用户消息来源 | sendChat 写入 streamingBlocks[0] | activeTurn.userMessage |
| 助手消息来源 | WS 追加到 streamingBlocks[1..] | activeTurn.assistantResponse.blocks |
| DB 替换 | 清空 streamingBlocks，用 dbBlocksByMsgId | integrity_check 通过后原地替换 ID |
| 临时 ID | 无追踪，靠 block count 变化触发替换 | 显式 tempId，替换时可精确匹配 |
| 多轮历史 | dbBlocksByMsgId 平铺所有消息 | completedTurns 数组，按轮次组织 |
| 渲染 | blocks-container 遍历两个来源 | 仅遍历 activeTurn + completedTurns |

### 渲染简化

```tsx
// 当前：两个来源，两个循环
{streamingBlocks.length > 0 && <BlocksMessage blocks={streamingBlocks} />}
{Object.entries(dbBlocksByMsgId).map(([id, entry]) => <BlocksMessage ... />)}

// 改为：单一来源，单一循环
{completedTurns.map(turn => (
  <TurnBlock key={turn.id} turn={turn} />
))}
{activeTurn && <TurnBlock key={turn.id} turn={activeTurn} streaming />}
```

---

## 完整性校验升级：eventSeq（防止静默数据丢失）

### 为什么仅靠 block count + hash 不够

```
场景：流式中网络断开 3s → WS 重连后端补推 delta →
前端因乱序漏掉中间 2 个 tool_call → 最后一条是 text block，
hash 恰好匹配 → integrity_check 通过 → 2 个 tool_call 永久丢失，
用户看到不完整的回答，且无任何提示。
```

"校验通过但内容残缺"比"校验失败触发 remount"更危险——它无声无息。

### 方案：单调递增序列号

```typescript
interface StreamingTurn {
  meta: {
    steps: number;
    tokens: number;
    startedAt: number;
    eventSeq: number;     // ← WS 事件单调递增，从 1 开始
    hasGap: boolean;      // ← seq 不连续时标记
  };
}
```

**WS 侧**：每个 delta 事件携带 `seq: number`（后端维护，同一个 turn 内从 1 开始递增）。

**前端侧**：`handleWsEvent` 中检查 `seq === meta.eventSeq + 1`。不等 → `hasGap = true`。

**integrity_check 升级**：

```typescript
function checkIntegrity(active: StreamingTurn, db: DbTurn): boolean {
  if (active.meta.hasGap) return false;           // 已知丢包，直接 fail
  if (active.meta.eventSeq !== db.totalEvents) return false;  // 事件数不匹配
  if (active.assistantResponse.blocks.length !== db.blockCount) return false;
  if (lastBlockHash(active) !== db.lastBlockHash) return false;
  return true;
}
```

| 维度 | 仅 block count + hash | + eventSeq |
|------|----------------------|------------|
| 丢包检测 | 可能静默通过 | 必定捕获 |
| 乱序检测 | 无法感知 | seq 不连续即标记 |
| 调试成本 | 用户报"不完整"难复现 | hasGap=true 直接定位 |

### 与现有方案的兼容性

不改变 StreamingTurn 核心结构，只加一个字段、一行校验。Phase 1 可先不加（当前方案上线），Phase 2 配合后端 `stream_start` 时一并引入 `eventSeq`。两者完全正交。

---

## 后端配合：WS stream_start 事件

```
sendChat 的 HTTP 响应中返回 turn_id。
WS 连接建立后，第一个事件携带 trigger_message：

{
  "type": "stream_start",
  "turn_id": "turn_sess123_5",
  "trigger_message": {
    "id": "msg_user_xyz",
    "role": "user",
    "content": "你好"
  }
}
```

**前端处理**：
- `stream_start` 到达时，将 `activeTurn.userMessage.id` 从 tempId 替换为真实 ID
- 如果 `stream_start` 在 `sendChat` 之前到达（WS 重连乱序），创建 activeTurn
- `trigger_message` 缺失时，回退到 tempId（当前方案二的逻辑）

---

## 实施路线

### Phase 1：StreamingTurn 模型 + 前端迁移（不改后端）

1. 定义 `StreamingTurn` 类型
2. `sendChat` 创建 `activeTurn`，替代当前的 `streamingBlocks` 注入
3. `handleWsEvent` 操作 `activeTurn.assistantResponse.blocks`
4. `loadTimeline` 执行 integrity_check，将 activeTurn 转换为 completedTurn
5. 渲染层只遍历 `completedTurns + activeTurn`
6. 删除 `streamingBlocks`、`dbBlocksByMsgId`、`timeline` 渲染相关代码

**改动量**：约 200 行 chatStore.ts + 50 行 ChatView.tsx。不改后端。

### Phase 2：后端 stream_start（需要后端配合）

1. `POST /api/sessions/{id}/messages` 响应中返回 `turn_id`
2. WS 首个事件改为 `stream_start { turn_id, trigger_message }`
3. 前端移除 tempId 逻辑，完全依赖后端权威数据

**改动量**：后端 ~30 行 + 前端 ~20 行。

---

## 为什么这是"根本修复"

| 维度 | 当前（方案二） | StreamingTurn |
|------|---------------|---------------|
| **数据所有权** | sendChat 写 streamingBlocks，WS 追加，loadTimeline 覆盖——三个写手 | activeTurn 是单一真相源，WS 和 DB 都是它的更新通道 |
| **ID 稳定性** | tempId 无追踪，靠 block count 变化隐式触发替换 | 显式 tempId→realId 映射，integrity_check 精确比对 |
| **生命周期** | 三个数组，无生命周期关联 | 一个 turn 对象，从 birth（sendChat）到 death（loadTimeline）清晰可追踪 |
| **可测试性** | 依赖 WS 时序，难以单测 | turn 状态机可独立单测，WS 只是输入事件 |
| **扩展性** | 无法支持"流式中编辑"或"多轮预览" | turn 数组天然支持多轮上下文 |
