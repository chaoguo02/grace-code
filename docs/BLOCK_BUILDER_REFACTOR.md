# Block Builder 职责重构方案

> 后端视角批判性评审 — 2026-07-25

## 问题 1：三个 builder 构建同一份数据

同一份 assistant 回答，当前由三个不同函数拼凑 blocks：

| Builder | 位置 | 输入 | 输出 |
|---------|------|------|------|
| `applyWsToBlocks()` | `chatStore.ts:156-212` | WS 实时事件 (thought_delta, tool_call, observation) | thought + tool_use blocks |
| `handleWsEvent` completed 分支 | `chatStore.ts:606-616` | `ev.result.summary` | text block（刚加的补丁） |
| `extractBlocksFromTimeline()` | `chatStore.ts:229-268` | DB timeline (messages + WS events) | text + thought + tool_use blocks |

**根因**：没有统一的 block 构建入口。三个 builder 各自理解事件结构，各自决定"什么该转成什么 block"。

```typescript
// 位置 1: chatStore.ts:156 — WS delta → blocks
function applyWsToBlocks(blocks, ev, messageId) {
  if (ev.type === "thought_delta") { ... }
  if (ev.type === "tool_call") { ... }
  if (ev.type === "observation") { ... }
  // ev.type === "status" → 无视
}

// 位置 2: chatStore.ts:606 — completed status 单独补 text block
activeTurn: {
  assistantResponse: {
    blocks: [...prev.blocks,
      ...(ev.result?.summary ? [{type:"text", content:ev.result.summary}] : [])
    ],
  }
}

// 位置 3: chatStore.ts:229 — DB timeline → blocks（逻辑与 applyWsToBlocks 重复）
function extractBlocksFromTimeline(items) {
  for (const item of items) {
    if (item.source === "message") { blocks.push({type:"text", ...}); }
    else if (item.source === "ws") { applyWsToBlocks(blocks, item.ws, msgId); }
  }
}
```

### 正确方案

**`applyWsToBlocks` 作为唯一入口**。所有 WS 事件类型（包括 status）都经过它处理。

```typescript
function applyWsToBlocks(blocks, ev, messageId) {
  if (ev.type === "thought_delta" || ev.type === "thought") { ... }
  if (ev.type === "tool_call") { ... }
  if (ev.type === "observation") { ... }
  // ← 新增：最终回答文本
  if (ev.type === "status" && ev.status === "completed" && ev.result?.content) {
    blocks.push({ type: "text", content: ev.result.content });
  }
}
```

`handleWsEvent` completed 分支不再单独构建 text block，只负责状态标记。

---

## 问题 2：后端 WS 事件缺少 `content` 字段

当前 `task_complete` 事件结构：

```python
# server/events.py:38-41
class WsStatus:
    status: str       # completed
    result: dict | None  # {summary, steps_taken, total_tokens}
```

`result.summary` 是给通知用的简短描述，不是 assistant 的完整回答正文。但前端从 `summary` 猜"这是不是 text block"。语义错位。

### 后端实际数据流

```python
# agent/core.py:854 — RunResult 包含完整回答
summary = (
    f"[{_tag}. Code changes were made but NOT independently verified.]\n\n"
    f"{summary}"
)
result = RunResult(summary=summary, ...)

# server/services/event_bus.py — _translate_event(task_complete)
_result = {
    "summary": payload.get("summary", ""),
    "steps_taken": payload.get("steps", 0),
    "total_tokens": payload.get("total_tokens", 0),
}
```

`RunResult.summary` 实际上就是 assistant 的最终输出——包含了 UNVERIFIED 标签和完整回答。但它在 WS 事件里被塞进了 `result.summary` 字段，前端不知道这应该作为正文渲染。

### 正确方案

**后端 `task_complete` 事件增加 `content` 字段**，明确区分"消息正文"和"通知摘要"。

```python
# server/events.py — WsStatus 新增 content
class WsStatus:
    status: str
    result: dict | None  # {summary, content, steps_taken, total_tokens}

# server/services/event_bus.py — _translate_event
if ev_type == "task_complete":
    _result = {
        "summary": payload.get("summary", ""),
        "content": payload.get("summary", ""),  # ← 当前 summary 实际就是 content
        "steps_taken": payload.get("steps", 0),
        "total_tokens": payload.get("total_tokens", 0),
    }

# server/events.py — WsStatus.to_dict()
return {
    "type": "status",
    "status": self.status,
    "result": {
        "summary": ...,    # 通知栏短文本（现有）
        "content": ...,    # assistant 完整回答正文（新增）
        "steps_taken": ...,
    },
}
```

前端不需要改动——`applyWsToBlocks` 已经处理了 `content` 字段（见问题 1 的正确方案）。

---

## 问题 3：`extractBlocksFromTimeline` 与 `applyWsToBlocks` 逻辑重复

`extractBlocksFromTimeline` 遍历 timeline items：遇到 message → 直接创建 text block；遇到 ws → 调用 `applyWsToBlocks`。

但 message 路径创建 text block 的逻辑是独立的——不经过 `applyWsToBlocks`。如果 message 的 content 包含特殊结构（如未来支持多模态），这个分支也需要同步更新。

### 当前代码

```typescript
// chatStore.ts:242-248 — DB message → text block（不走 applyWsToBlocks）
if (role === "assistant" && item.msg.content) {
  currentRole = "assistant";
  blocks.push({ type: "text", content: item.msg.content });
}
```

### 正确方案

**长期**：DB 直接存储序列化好的 blocks，`extractBlocksFromTimeline` 退化为简单反序列化。

**短期**：将 message content 也模拟为一个"事件"交给 `applyWsToBlocks` 处理，保持单一路径。

```typescript
// 短期：统一入口
if (item.source === "message" && item.msg.content) {
  applyWsToBlocks(blocks, {
    type: "status",
    status: "completed",
    result: { content: item.msg.content },
  }, msgId);
}
```

---

## 改动清单

| 优先级 | 文件 | 改动 | 行数 |
|--------|------|------|------|
| P0 | `server/events.py` | `WsStatus.result` 增加 `content` 字段 | 2 |
| P0 | `server/services/event_bus.py` | `_translate_event` 传递 `content` | 2 |
| P1 | `web/src/stores/chatStore.ts` | `applyWsToBlocks` 处理 `status.completed` → text block | 4 |
| P1 | `web/src/stores/chatStore.ts` | completed handler 移除单独构建 text block | -4 |
| P2 | `web/src/stores/chatStore.ts` | `extractBlocksFromTimeline` 统一走 `applyWsToBlocks` | 5 |

**总改动量**：4 个文件，~15 行，不碰架构。

---

## 修复后的数据流

```
WS 事件流:
  thought_delta ──┐
  tool_call ──────┤
  observation ────┼─→ applyWsToBlocks() ──→ activeTurn.assistantResponse.blocks
  status.completed┘    (唯一入口)              [thought, tool_use, text]

DB 回放:
  timeline items ──→ applyWsToBlocks() ──→ completedTurns[i].blocks
                     (同一个人口)
```

`completed` handler 不再碰 blocks：
```typescript
// 只做状态标记
activeTurn: { ...prev.activeTurn,
  assistantResponse: { ...prev.assistantResponse, status: "completed" },
  meta: { ...prev.meta, completedAt: Date.now() },
}
```
