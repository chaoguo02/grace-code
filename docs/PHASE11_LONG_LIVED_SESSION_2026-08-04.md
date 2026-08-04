# Phase 11 异常点分析——CC 对照

> 日期：2026-08-04
> 目标：找出 Phase 11 方案中可能出错的地方，对照 CC 实际行为做判断——修正 or 保留

---

## 整体判断

7 个异常点：3 个需按 CC 修正，2 个可保留（Grace Code 合理扩展），2 个不在 Phase 11 范围（后续 Phase）。

---

## 1. 🔴 Hook Permission——需要按 CC 修正

### Phase 11 的假设

长生命周期 async task 中，服务端 `_RealHooks.check()` 同步返回 `HookGateResult(allowed=True/False)`。"Ask" 场景需要 asyncio 兼容的确认机制。

### CC 实际情况

CC 有三种 hook 结果：

| Hook 结果 | 含义 | 机制 |
|---|---|---|
| exit 0 | continue | 工具直接执行 |
| exit 2 | **block** | 工具被拒绝，stderr 文本给 LLM 看原因。**不阻塞事件循环——拒绝是立即的。** |
| exit 1 | hook error | 工具继续执行（non-blocking） |

CC 的 PreToolUse hook **从来不会"等待用户输入"**。hook 是 shell 命令——运行、读 stdout、返回。如果 hook 需要用户确认，它返回 `{"permissionDecision": "ask"}`——这触发 CC 内置的**权限交互层**，不是 hook 自己等。

### Grace Code 当前状态

```python
# _RealHooks.check()
gate_result = self._ports.hooks.check("PreToolUse", hook_input, tool_name=tc.name)
# gate_result.allowed: True = 执行, False = 跳过
```

当前没有 "ask" 路径——`False` 直接跳过工具。`_RealHooks` 内部调 `PermissionPipeline`，它返回的 `PermissionDecision` 有三种：ALLOW / DENY / ASK。但 ASK 被当作 DENY 处理（因为 web 模式没有交互能力）。

### 判定

**这不是 Phase 11 引入的问题——这是 Phase 0a 就存在的 gap。** Phase 11 只是让它在 async task 中再次暴露。

按 CC 修正：**`all=False` 时，`HookGateResult` 应该带 `reason` 文本注入 conversation**（CC 的 stderr 给 LLM 看），而不是静默跳过。CC 的 "ask" → interactive prompt → allow/deny 流程通过 `PermissionRequest` 事件 + `control_request`/`control_response` 协议处理——**那是 web bridge 层的事，不是 hook 层的事。**

### 修正方案

`_RealHooks.check()` 返回 `HookGateResult(allowed=False, reason="...")` 时，StepLoop 不再静默跳过工具——改为**将 reason 作为 tool_result(is_error=True) 注入 conversation**。模型看到拒绝原因后可以改方案（CC 的 denial tracking 防止无限重试）。

**不改**：不在此 Phase 实现 "ask → 弹窗 → 用户确认" 的交互协议。那是 web bridge 的独立 Phase。

---

## 2. 🔴 `max_steps` 语义——需要按 CC 修正

### Phase 11 的假设

`RuntimeExecution.max_steps` 是单次 `execute()` 循环的上限。长生命周期 session 中，多轮对话会消耗 steps。

### CC 实际情况

CC 的 max_turns 是整个 queryLoop() 的生命周期上限——**一次 generator 调用的总 turn 数**，不是 per-turn。

但 CC 的 turn 定义是 "一次 model call → 工具执行 → 下一轮 model call"。当 LLM 返回纯文本（AssistantText），turn 计数增加但**不退出 loop**——退出条件只有：

```
model 返回文本 → 检查 max_turns 是否达到？
  是 → terminal_state(max_turns)
  否 → 等待下一个用户输入 → continue
```

### Grace Code 当前代码

```python
# native_step_loop.py:152
for turn in range(context.max_steps):
    ...
    if isinstance(model_action, AssistantText):
        return self._finalize_outcome(...)  # 直接退出
```

这里有两个问题：(1) 每个 turn 消耗一个 step——但返回文本后应该**等待输入**而不是退出，(2) 长生命周期后多轮才应该是 max_turns 的总上限，不是一轮的 step。

### 修正方案

`max_steps` 改为在 `SessionAgent` 级别管理——整个 session 的 turn 上限。`NativeStepLoop` 内部仍然是 `for turn in range(max_steps)`，但在 `AssistantText` 返回后不退出（等待输入），只在 `max_steps` 达到后才退出。

---

## 3. 🔴 Denial Tracking——需要按 CC 修正

### Phase 11 的假设

没考虑 denial tracking。

### CC 实际情况

```typescript
// denialTracking.ts
maxConsecutive: 3 per tool
maxTotal: 20 overall
```

CC 记录每个工具被拒绝的次数。超过上限时注入 "Your previous tool call was rejected..." 强制模型改变策略。`recordSuccess()` 重置计数器。

### 判定

**这是 Phase 0a 就存在的 gap——Phase 11 只是让它更容易被触发**（长生命周期中模型可能反复尝试相同的被拒绝操作）。

### 修正方案

在 `_RealHooks` 或 `NativeStepLoop` 的工具执行前加简单的 denial counter：`tool_name → consecutive_denials`。超过 3 次连续拒绝同一工具 → 注入 warning 到 conversation。

---

## 4. 🟡 ConversationState 并发安全——可保留（Grace Code 设计）

### Phase 11 的假设

HTTP handler 调用 `SessionAgent.post_message()` → `state.add_user_message()`，同时 StepLoop 在内部 `to_conversation()` 读取 state。两者在同一个 asyncio event loop 中——Python asyncio 单线程，不会有真正的 race condition。**但如果 StepLoop 在同步循环中（当前是 sync for loop），HTTP handler 的 `post_message` 需要等 for loop 到达等待点。**

### CC 实际情况

CC 的 `queryLoop()` 是 async generator——在 `await` 点（model call）释放事件循环。CC 的 REPL 在 `while` 循环中调用 `query.submitMessage(prompt)`，每个 turn 等待 generator 产出结果。

### 判定

Grace Code 的 `NativeStepLoop.execute()` 是同步 for loop——`backend.invoke()` 是同步调用（等 Anthropic SDK 返回）。这期间事件循环被阻塞。但在 `PendingInput.wait()`（asyncio）点，事件循环释放——HTTP handler 可以 `post_message`。

唯一需要确保：`add_user_message` 不在 Loop 读 `to_conversation` 的同时被调用。**在单线程 asyncio 中这天然安全。** Python GIL 也提供额外保护。

**保留 Grace Code 的同步 for loop + asyncio Event 等待模式。** CC 的 async generator 不是必须的——单线程同步循环 + asyncio 等待点有同样效果。

---

## 5. 🟡 Memory Catalog 时效性——可保留（后续优化）

### Phase 11 的假设

MEMORY catalog 在 session 启动时注入一次。

### CC 实际情况

CC 的 MEMORY.md 也是在 session 启动时注入一次。CC 不会在 session 中间重新注入 memory——LLM 通过 `memory_read` 工具主动查询新记忆。

### 判定

**与 CC 行为一致。** 如果全量压缩触发，CC 会从磁盘重新读 MEMORY.md。Grace Code 的压缩（`ContextBudgetManager`）触发后应该重新从 SQLite 生成 catalog → 注入。

**保留 Phase 11 设计，在 `ContextBudgetManager.ensure_budget()` 返回 `messages_trimmed > 0` 时重新注入 catalog。** 这是独立优化，不阻塞 Phase 11。

---

## 6. ⬜ 进程重启恢复——不在 Phase 11

### 问题

进程重启后 `SessionAgentRegistry` 清空。恢复需要从 `ConversationStore` 重建。

### CC 实际情况

CC 的 `--resume` 从 JSONL transcript 恢复。恢复后重新注入 CLAUDE.md？**CC 文档没有明确说明——已知 issue 记录恢复后 CLAUDE.md 可能丢失。**

### 判定

**恢复机制在 Phase 11 文档 §3.4 已设计——`rebuild_from_store` → `ConversationSnapshot` → 新建 `SessionAgent`。** 但需要额外处理 system_prompt + GRACE.md + catalog 的重注入——CC 没有完美解决，Grace Code 可以做到（从磁盘/SQLite 重读静态上下文层）。

**不在 Phase 11 Step 1-5 做。** 作为 Step 6（降级恢复）独立实现。

---

## 7. ⬜ WebSocket / SSE 协议——不在 Phase 11

### 问题

长生命周期 session 的流式输出和事件推送需要 WebSocket 通道。

### CC 实际情况

CC 的 SDK/bridge 使用 WebSocket + SSE 双通道——WebSocket for server-push，HTTP POST for client-initiated actions。SSE 替代方案用于某些 transport。

### 判定

**Phase 11 不涉及 WebSocket 协议**——那是 web bridge 的独立 Phase。Phase 11 只确保 `text_callback`（Phase 10 已存在）可以在 `SessionAgent._session_loop` 中被调用——WebSocket 推流是 Step 6。

---

## 汇总——审查结论（修订版）

> 2026-08-04 审查：3 个"需修正"中 1 个方向反了、1 个已实现、1 个诊断错了。真正的问题在别处。

### 修订后的修正清单

| # | 原判 | 修订结论 | 动作 | 工作量 |
|---|---|---|---|---|
| 1 | ask 当 deny，需注入 reason | **诊断错+方向反**：reason 已注入；真问题是 ask fail-open（权限泄漏） | ① 把 `web_confirm_callback` 接进 `assemble()` 的 `PermissionPipeline`；② headless 无 callback 时 fail-closed（对齐 CC auto-deny） | 中 |
| 2 | max_steps 语义错，需改 session 级 | **方向反了**：CC 的 `maxTurns` 是单次 `submitMessage()` 预算，不是跨消息累计。Grace Code 的 `for turn in range(max_steps)` 已对齐 | **不改代码**。SessionAgent 的每次 `post_message` = 独立 `run()` = 独立 max_steps 预算 | 改文档 |
| 3 | 无 denial tracking，需新增 | **已完整实现**：`PermissionPipeline` 含 total_denials(20) + consecutive(3) + success reset。CC 的 `denialTracking.ts` 完整映射 | **不改代码**。补长生命周期测试验证 circuit breaker + denial counter 正确触发 | 测试 |
| 4 | 并发可保留 | **有 bug**：① 串行工具 `tool.execute()` 同步阻塞事件循环；② `asyncio.run()` 在 Phase 11 长生命周期 running loop 中会炸 `RuntimeError` | 把 `NativeStepLoop.execute()` 用 `asyncio.to_thread` 包一层——整体移出事件循环线程 | 中 |
| 5 | Catalog 时效性可保留 | **判断对，但有前置依赖** | native 生产路径先补记忆注入（独立 Phase），再谈压缩后重注入 | 大（独立 Phase） |
| 6/7 | 不在 Phase 11 | **正确** | 第 6 点注：CC resume 会重注入根 CLAUDE.md + memory，Grace Code 方案能比 CC 做得更好 | 改文档 |

### 真正需要写进 Phase 11 的代码修正

1. **ask fail-open → 权限泄漏**（#1）：`assemble()` 的 `PermissionPipeline` 缺少 `web_confirm_callback` → ask 规则被静默放行
2. **asyncio.run 冲突**（#4）：长生命周期 running loop 中 `asyncio.run()` 会炸，需用 `asyncio.to_thread` 包 `NativeStepLoop.execute()`
3. **文档修正**（#2 + #6）：max_steps 语义不变、CC resume 行为补充
