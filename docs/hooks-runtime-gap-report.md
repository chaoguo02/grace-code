# Hooks + RuntimeController CC 对比报告

> 调研: CC 官方 docs + DeepWiki 源码分析
> 对比: forge-agent hooks/ + agent/runtime_controller.py

---

## 一、RuntimeController 对比

### CC 的做法

CC 没有独立的 "RuntimeController" 类。主循环 (`src/query.ts`) 中的检查是**分散内联**的：

```
for each turn:
  → build query from state
  → call model API
  → parse response
  → has tool calls?
      NO  → exit loop (implicit finish)
      YES → execute tools
  → append tool results
  → check if compaction needed
  → loop back
```

停止条件由以下机制分散处理：
- `maxTurns` — 步数上限
- token budget — 由 `QueryEngineConfig.maxBudgetUsd` 控制
- `/goal` command — 单独的 Haiku 模型评估完成条件
- `Stop hooks` — 可以 block agent 停止
- `abortController.signal` — 取消传播

### 我们的做法

我们有显式的 `RuntimeController` 类 (`agent/runtime_controller.py`)——在每轮 LLM 调用前集中执行 5 项检查：

```python
# agent/core.py:584 — 每步都被调用
decision = _runtime_controller.check(
    step, total_tokens, history, log,
    context_size, request_budget, consecutive_failures,
)
```

检查顺序：Circuit breaker → Max steps → Budget → Context window → Consecutive failures

### 对照结论

| 方面 | CC | forge-agent | 评价 |
|------|-----|-------------|------|
| 集中控制器 | 无 (分散内联) | 有 (RuntimeController) | ✅ 我们的更清晰 |
| Circuit breaker | 无显式 | 有 | ✅ 额外保护 |
| 步数上限 | maxTurns | max_steps | ✅ 一致 |
| Token 预算 | USD-based budget | token-based budget | ✅ 等价 |
| Context window | compaction 触发 | compaction + inject message | ✅ 一致 |
| 连续失败 | 无 | 有 (max_consecutive_failures=3) | ✅ 额外保护 |
| PTL 降级 | Reactive Compact + Truncation | ❌ 无 | ⚠️ 缺失 |
| /goal 命令 | Haiku 评估完成条件 | ❌ 无 | ⚠️ 缺失 |

**结论**: 我们的 RuntimeController 实际上比 CC 更**结构清晰**——CC 是分散内联，我们集中管理。唯一缺失的是 PTL 降级和 /goal 命令。

---

## 二、Hooks 事件对比

### CC 的 29 个事件

```
SessionStart, SessionEnd
UserPromptSubmit, UserPromptExpansion
PreToolUse, PermissionRequest, PermissionDenied
PostToolUse, PostToolUseFailure, PostToolBatch
Stop, StopFailure
SubagentStart, SubagentStop
TaskCreated, TaskCompleted
TeammateIdle
PreCompact, PostCompact
InstructionsLoaded, ConfigChange
CwdChanged, FileChanged
WorktreeCreate, WorktreeRemove
Elicitation, ElicitationResult
Notification
```

### 我们的 8 个事件

```
PRE_TOOL_USE, POST_TOOL_USE, POST_TOOL_USE_FAILURE
STOP
SESSION_START
USER_PROMPT_SUBMIT
SUBAGENT_START, SUBAGENT_STOP
```

### 对照

| CC 事件 | forge-agent | 状态 |
|---------|-------------|------|
| PreToolUse | ✅ PRE_TOOL_USE | |
| PostToolUse | ✅ POST_TOOL_USE | |
| PostToolUseFailure | ✅ POST_TOOL_USE_FAILURE | |
| Stop | ✅ STOP | |
| SessionStart | ✅ SESSION_START | |
| UserPromptSubmit | ✅ USER_PROMPT_SUBMIT | |
| SubagentStart | ✅ SUBAGENT_START | |
| SubagentStop | ✅ SUBAGENT_STOP | |
| PermissionRequest | ❌ | 缺失 |
| PostToolBatch | ❌ | 缺失 |
| PreCompact | ❌ | 缺失 |
| PostCompact | ❌ | 缺失 |
| TaskCreated/Completed | ❌ | 缺失 |
| Elicitation | ❌ | 缺失 (无需求) |
| 其余 12 个 | ❌ | 多属于 UI/工作区事件 |

---

## 三、Hooks 能力对比

### PreToolUse

| 能力 | CC | forge-agent | 状态 |
|------|-----|-------------|------|
| 阻止工具执行 | ✅ deny | ✅ BLOCK | |
| 允许执行 | ✅ allow | ✅ APPROVE | |
| 询问用户 | ✅ ask | ❌ | 缺失 |
| 修改工具输入 | ✅ updatedInput | ❌ | 缺失 |
| 注入上下文 | ✅ additionalContext | ❌ | 缺失 |
| 异步执行 | ✅ async:true | ❌ | 缺失 |
| 优先级 deny>defer>ask>allow | ✅ | ❌ 只有 block/approve | 缺失 |

### PostToolUse

| 能力 | CC | forge-agent | 状态 |
|------|-----|-------------|------|
| 阻止 (feedback) | ✅ block | ❌ | 缺失 |
| 替换工具输出 | ✅ updatedToolOutput | ❌ | 缺失 |
| 注入上下文 | ✅ additionalContext | ❌ | 缺失 |

### Stop

| 能力 | CC | forge-agent | 状态 |
|------|-----|-------------|------|
| 阻止停止 | ✅ block | ❌ | ⚠️ 部分 (有 stop hook 但只做 verification) |
| SubagentStop 自动转换 | ✅ | ❌ | 缺失 |

### Handler 类型

| 类型 | CC | forge-agent | 状态 |
|------|-----|-------------|------|
| command (shell) | ✅ | ✅ | |
| internal (Python callable) | ❌ | ✅ | 我们独有的 |
| HTTP | ✅ | ❌ | 缺失 |
| MCP tool | ✅ | ❌ | 缺失 |
| prompt (LLM) | ✅ | ❌ | 缺失 |
| agent (subagent) | ✅ | ❌ | 缺失 |

### Matcher 支持

| 类型 | CC | forge-agent | 状态 |
|------|-----|-------------|------|
| 精确字符串 | ✅ | ✅ | |
| \| 分隔列表 | ✅ | ✅ | |
| , 分隔列表 | ✅ | ❌ | 缺失 |
| 正则表达式 | ✅ | ❌ | 缺失 |

### 配置来源

| 来源 | CC | forge-agent | 状态 |
|------|-----|-------------|------|
| settings.json | ✅ | ✅ | |
| settings.local.json | ✅ | ❌ | 缺失 |
| Plugin hooks | ✅ | ❌ | 缺失 |
| Skill frontmatter | ✅ | ⚠️ 解析但未消费 | K1/K3 已修复 |
| Agent frontmatter | ✅ | ❌ (仅解析, 未接入) | S5 部分 |

---

## 四、总结

### ✅ 做得对的

1. **RuntimeController 集中管理**——比 CC 的分散内联更清晰
2. **PreToolUse BLOCK/APPROVE**——核心功能正确
3. **8 个核心事件**——覆盖了最常用的生命周期点
4. **Internal hooks (Python callable)**——比 CC 的 shell-only 更灵活
5. **退出协议 (ExitCode 0/2)**——与 CC 的 exit 0=success, 2=block 一致

### ⚠️ 缺失但不需要的

1. Elicitation/ElicitationResult——headless 模式无需求
2. 12 个 UI/工作区事件——chat 模式不需要
3. HTTP/MCP/prompt/agent handler——目前 command + internal 够用
4. 正则 matcher——\| 分隔列表够用

### ❌ 缺失且需要的

| 缺失项 | 优先级 | 影响 |
|--------|--------|------|
| PreToolUse: 修改工具输入 (updatedInput) | P1 | Hook 不能改写 tool params |
| PostToolUse: 替换工具输出 (updatedToolOutput) | P1 | Hook 不能改 tool 的输出 |
| Stop: SubagentStop 自动转换 | P1 | 子代理的 Stop hook 行为不对 |
| PostCompact hook | P1 | 压缩后无法通知外部 |
| PreCompact hook | P2 | 压缩前无法注入 |
| PTL 降级 | P2 | 压缩失败时无降级策略 |
| settings.local.json 加载 | P2 | 无本地配置来源 |

### ⚠️ 做错了的

| 错误 | 正确做法 |
|------|---------|
| Stop hook 只检查 verification (git_diff) | 应支持通用 block/reason 语义 |
| Agent frontmatter hooks 解析了但在 S5 中注册到全局共享 registry | 应绑定到 agent session context |
| 没有 PermissionRequest 事件 | PreToolUse 和 PermissionRequest 是分开的——PreToolUse 阻止执行, PermissionRequest 控制弹窗 |
