# Hook 系统 CC-Native 重建设计

> 文档版本：2.0.0
> 日期：2026-08-02
> 状态：DRAFT
> 参考：[Claude Code Hooks Reference](https://code.claude.com/docs/en/hooks)

---

## 0. Claude Code Hook 设计原则（对齐目标）

从 CC 参考文档和源码分析中提取的核心设计：

1. **Matcher 使用 permission-rule 语法**，不是 regex：
   - `"*"` 或 `""` = 所有工具
   - `"Bash"` = 精确匹配
   - `"Edit|Write"` = 竖线分隔的列表（OR）
   - `"mcp__server__*"` = 前缀通配符（仅 `*` 在末尾）
   - MCP 工具命名约定：`mcp__<server>__<tool>`
   - Subagent 工具名：`Task`

2. **决策优先级**：`deny > defer > ask > allow`
   - 不是简单的 allow/block 二元对立
   - `defer` = "我不决定，让下一个 hook 或 permission 系统决定"

3. **Hook 处理器类型**（5 种）：
   - `command` — shell 子进程，stdin JSON
   - `http` — POST JSON 到 URL
   - `mcp_tool` — 调用 MCP 服务器工具
   - `prompt` — 单轮 LLM 判断（返回 yes/no）
   - `agent` — 多轮 subagent（实验性）

4. **退出码语义**：
   - Exit 0 = success，stdout 被解析为 JSON
   - Exit 2 = blocking error，stderr 反馈给 Claude
   - 其他 = non-blocking error

5. **事件按 Cadence 分类**：
   - Per-session：SessionStart, SessionEnd
   - Per-turn：UserPromptSubmit, Stop, StopFailure
   - Per-tool-call：PreToolUse, PostToolUse, PostToolUseFailure, PostToolBatch

---

## 1. 事件模型（完整 10 → 对齐 CC）

### 1.1 当前 10 事件 vs CC 事件

| 当前事件 | CC 对齐 | 说明 |
|---|---|---|
| PreToolUse | ✅ 完全对齐 | permissionDecision = allow/deny/ask/defer |
| PostToolUse | ✅ 对齐 | decision: block 可反馈但不可撤销 |
| PostToolUseFailure | ✅ 对齐 | 新增 error_message, error_type |
| PermissionRequest | ✅ 对齐 | 自动批准/拒绝 permission 弹窗 |
| **PermissionDenied** | ❌ 缺失 | 自动模式拒绝工具时触发，可 retry |
| UserPromptSubmit | ✅ 对齐 | block/transform user input |
| **UserPromptExpansion** | ❌ 缺失 | slash command/skill 展开时触发 |
| Stop | ✅ 对齐 | stop_hook_active 防无限循环 |
| **StopFailure** | ❌ 缺失 | API error 导致 turn 结束 |
| SessionStart | ✅ 对齐 | inject context via stdout |
| **SessionEnd** | ❌ 缺失 | session 结束通知 |
| SubagentStart | ✅ 对齐 | match on agent type |
| SubagentStop | ✅ 对齐 | blockable |
| PostResponse | → **Notification** | CC 用 Notification 事件替代 |
| **PreCompact** | ❌ 缺失 | compaction 前通知 |
| **PostCompact** | ❌ 缺失 | compaction 后通知 |
| **PostToolBatch** | ❌ 缺失 | 并行工具批次完成后 |

### 1.2 本阶段的保留与新增

**保留全部 10 个当前事件**（用户要求）。

**新增 CC 对齐事件（6 个）**：
- `PermissionDenied` — 自动模式拒绝工具时允许 hook 重试
- `StopFailure` — API 错误时通知
- `SessionEnd` — session 结束清理
- `PreCompact` — compaction 前最后检查
- `PostCompact` — compaction 后通知
- `PostToolBatch` — 并行工具批次完成（一次性而非每工具触发）

**暂不新增**（当前架构不支持或优先级低）：
- UserPromptExpansion（无 slash command 展开机制）
- TaskCreated/TaskCompleted（无 Agent Team）
- TeammateIdle（无 Agent Team）
- ConfigChange/CwdChanged/FileChanged/InstructionsLoaded（无实时文件监控）
- WorktreeCreate/WorktreeRemove（无 worktree 管理）
- Elicitation/ElicitationResult（无 MCP 交互式请求）

---

## 2. Matcher 设计（CC-aligned，无 Regex）

### 2.1 语法规则

```
matcher  ::= "*" | "" | TERM ("|" TERM)*
TERM     ::= EXACT | PREFIX_WILDCARD
EXACT    ::= [a-zA-Z0-9_]+              # "Bash", "Write", "Edit"
PREFIX   ::= EXACT "__" EXACT "__" "*"   # "mcp__github__*"
```

**明确不用 regex**。旧系统的 pipe-split 已是正确方向，新系统规范化它：
- `"*"` / `""` → 匹配所有工具
- `"Bash"` → 精确匹配
- `"Edit|Write|NotebookEdit"` → 竖线分隔 OR
- `"mcp__server__*"` → 前缀匹配 MCP 服务器所有工具
- `"Task"` → 匹配所有 subagent 调用

### 2.2 工具命名约定

```
内置工具:  Bash, Read, Write, Edit, Glob, Grep, Task, Skill, ...
MCP 工具:  mcp__<server_name>__<tool_name>
Skill:     通过 PreToolUse 的 tool_name="Skill" 触发
Subagent:  通过 PreToolUse 的 tool_name="Task" 触发
```

**所有工具类型——内置、MCP、Skill、Subagent——都通过 PreToolUse/PostToolUse 走同一个 hook 路径。** 这是 CC 的核心设计：hook 不关心工具来自哪里，只关心工具名。

---

## 3. 决策模型

### 3.1 PreToolUse 四态决策

```
deny  >  defer  >  ask  >  allow
```

| 决策 | 含义 | 使用场景 |
|---|---|---|
| `deny` | 阻止执行，给出原因 | 安全策略（阻止 rm -rf） |
| `defer` | 不决定，交给下一层 | "我不确定，让用户决定" |
| `ask` | 要求用户确认 | "这个操作需要人工审查" |
| `allow` | 明确批准 | "我知道这个操作是安全的" |

### 3.2 默认行为

- Hook 返回 exit 0 无 stdout → 不改变决策流，继续正常 permission 流程
- Hook 返回 `deny` → 工具被阻止，不管 permission 设置
- Hook 返回 `allow` → 工具被批准，但**不覆盖** permission 规则中的 deny

### 3.3 Stop 事件防无限循环

```
stop_hook_active == true → hook 不得再次 block
```

CC 的 Stop hook 必须检查 `stop_hook_active` 字段。当 CC 已经因为 hook block 而强制继续后，再次触发 Stop 时此字段为 true，hook 必须放行。

---

## 4. 整体架构

```
settings.json / agent frontmatter
        │
        ▼
┌──────────────────────────────────────┐
│            HookRegistry              │
│  (per-event lists, matcher-indexed)  │
└────────────┬─────────────────────────┘
             │
             ▼
┌──────────────────────────────────────┐
│          HookDispatcher              │
│  match → sort → execute → merge     │
│                                      │
│  5 handler types:                    │
│  command │ http │ mcp_tool │        │
│  prompt │ agent                      │
└────────────┬─────────────────────────┘
             │
    ┌────────┼────────┬────────┐
    ▼        ▼        ▼        ▼
PreToolUse  Stop   PostToolUse SessionStart ...
(blockable) (block) (transform) (context inject)
```

### 4.1 Hook 处理器类型

| 类型 | 实现 | 超时 |
|---|---|---|
| `command` | subprocess, stdin JSON | 60s default |
| `http` | POST JSON → URL | 30s default |
| `mcp_tool` | MCP client call | 30s default |
| `prompt` | 单轮 LLM | 30s default |
| `agent` | 多轮 subagent | 60s default |

本阶段先实现 `command` 和内部 Python callable（相当于 `prompt` 的简化版）。`http`、`mcp_tool`、`agent` 后续补齐。

---

## 5. 实现阶段

### Phase H1: 基础类型系统
- Event 枚举（16 events：10 当前 + 6 新增）
- Matcher（无 regex：exact + pipe + prefix wildcard）
- Decision 枚举（deny/defer/ask/allow + precedence）
- 每事件 typed Input/Decision

### Phase H2: HookDispatcher
- Match hooks by event + matcher
- Execute in priority order
- Merge decisions（deny wins, transform merges）
- Total deadline enforcement
- Failure policy（fail-closed/open/turn, event_default）

### Phase H3: Handler 类型
- Internal callable handler（Python 函数）
- Command handler（subprocess, stdin JSON, exit code protocol）
- HTTP handler（POST JSON）

### Phase H4: 接入生产
- hook_bootstrap.py 切换到新 HookDispatcher
- 8 个调用点不变（接口兼容层）
- 新增事件（PreCompact, PostCompact, SessionEnd 等）的可选接入

### Phase H5: 端到端验证
- PreToolUse deny 阻止工具
- Stop block 强制继续
- PostToolUse transform 输出
- 新旧 hook 混用
- 全量回归
