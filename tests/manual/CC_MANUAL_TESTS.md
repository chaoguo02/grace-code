# CCD 对齐 — 手动测试场景

## 测试问题 1: Subagent 派发 + 状态流转（核心场景）

### CLI 命令

```bash
python -m entry.cli run --repo . --agent build --task "
我需要了解项目中的工具系统结构。请按以下步骤：
1. 首先自行阅读 tools/base.py 找到 BaseTool 类
2. 然后派发一个 explore 子代理去分析 tools/search_tool.py 的结构
3. 等待子代理结果后
4. 再派发一个 general 子代理去查看 tools/file_tool.py 有多少行
5. 综合所有信息，给出一份 tools/ 目录的分层架构图
"
```

### 预期行为序列

```
Step 1: build agent 读 tools/base.py（Read 工具调用）
  → Read(path=tools/base.py, offset=1, limit=30)

Step 2: build agent 派发 explore 子代理
  → Agent(subagent_type="explore", description="分析 tools/search_tool.py", ...)
  → Subagent explore started [session-xxx]
  → explore 使用 Read/Grep 分析文件
  → Subagent explore finished: completed (N turns, M tokens)

Step 3: build agent 派发 general 子代理
  → Agent(subagent_type="general", description="查看 file_tool.py 行数", ...)
  → Subagent general started [session-yyy]
  → general 使用 Bash(wc -l tools/file_tool.py) 或 Read 查看
  → Subagent general finished: completed (N turns, M tokens)

Step 4: build agent 综合信息并输出
  → 最终输出包含 tools/ 分层架构
```

### 验证点

| # | 检查项 | 通过条件 |
|---|--------|---------|
| 1 | 工具名使用 CC 规范名 | 看到 `Read`（不是 `file_read`），`Agent`（不是 `task`） |
| 2 | Subagent 启动/完成事件 | 看到 `Subagent explore started` 和 `Subagent explore finished: completed` |
| 3 | explore 只读约束 | explore 只使用 Read/Grep/Glob，不出现 Write/Edit/Bash |
| 4 | general 有完整工具 | general 可以使用 Bash |
| 5 | 父子隔离 | explore 不会看到 build agent 之前读的 base.py 内容 |
| 6 | 状态流转正确 | build agent 读完 → 派发 explore → 收到结果 → 派发 general → 综合 → finish |
| 7 | 最终输出结构化 | 最终消息包含 tools/ 的清晰架构说明 |

---

## 测试问题 2: Plan 模式 + 审批 + Build 执行

### CLI 命令

```bash
python -m entry.cli run --repo . --agent plan --task "
分析一下 tools/base.py 中 ToolRegistry 类的 register 方法实现，给出一个简单的功能说明。
不需要执行代码修改，只需要告诉我这个方法做了什么。
"
```

### 预期行为序列

```
Step 1: plan agent 以 ANALYSIS intent 启动
  → 只使用 Read/Grep/Glob 等只读工具
  → 读取 tools/base.py 分析 ToolRegistry.register

Step 2: plan agent 生成 plan（含 JSON contract）
  → execution_intent: "analysis"
  → 最终输出结构化分析报告

Step 3: 审批弹窗
  ────────────────────────────────────────────
    Plan ready for review
    File: .../plans/plan-xxx.md
  ────────────────────────────────────────────
    [1] Execute plan
    [2] Edit plan file
    [3] Tell the agent what to change (re-plan)
    [4] Save plan and exit (default)
    [5] Abort
  ────────────────────────────────────────────
    Choice [4]:
```

**用户选择 [1] Execute** → 进入 Build agent（EDIT intent）→ 没有文件需要修改 → 直接输出结果 → 完成。

### 验证点

| # | 检查项 | 通过条件 |
|---|--------|---------|
| 1 | plan 只读 | 只有 Read/Grep/Glob 调用，没有 Write/Edit/Bash |
| 2 | JSON contract | 最终 plan 包含 `execution_intent: "analysis"` |
| 3 | 审批弹窗 | 出现 `[1] Execute [2] Edit [3] Re-plan [4] Save [5] Abort` |
| 4 | 选择 Execute 后无二次审批 | build agent 直接输出结果，不再弹审批 |

---

## 测试问题 3: permission_mode=plan 权限拦截

### 前置

在 `.forge-agent/agents/` 下创建一个自定义 agent 文件 `read-only-agent.md`：

```markdown
---
name: read-only-agent
description: 只读分析 agent，用于测试 permission_mode=plan
intent: analysis
permissionMode: plan
tools: Read, Grep, Glob, Write, Edit, Bash
---
你是一个只读分析 agent。即使你有 Write/Edit/Bash 工具，也不能使用它们。
```

### CLI 命令

```bash
python -m entry.cli run --repo . --agent read-only-agent --task "
用 Read 工具读一下 pyproject.toml 的前 10 行。
然后尝试用 Write 创建一个 test.txt 文件。
"
```

### 预期行为

```
Step 1: Read → pyproject.toml 前 10 行 ✅
Step 2: Write → Permission denied: 'Write' is blocked by permission mode 'plan' ❌
```

### 验证点

| # | 检查项 | 通过条件 |
|---|--------|---------|
| 1 | 只读工具可用 | Read 正常返回文件内容 |
| 2 | 写工具被拦截 | Write 调用返回 permission denied 错误 |
| 3 | 错误消息明确 | 错误消息包含 `permission mode 'plan'` |

---

## 测试问题 4: Custom Agent + background + initial_prompt

### 前置

在 `.forge-agent/agents/` 下创建 `bg-analyzer.md`：

```markdown
---
name: bg-analyzer
description: 后台分析 agent
intent: analysis
background: true
initialPrompt: 请先确认自己处于后台模式。
tools: Read, Grep, Glob
---
你是一个后台分析 agent。请简洁高效地完成任务。
```

### CLI 命令

```bash
python -m entry.cli run --repo . --agent bg-analyzer --task "统计项目根目录有多少个 .md 文件"
```

### 预期行为

```
Step 1: agent 启动时先注入 initialPrompt 内容
Step 2: 使用 Glob/Read 工具分析
Step 3: 完成后输出结果
```

### 验证点

| # | 检查项 | 通过条件 |
|---|--------|---------|
| 1 | initial_prompt 注入 | agent 的上下文包含 initial_prompt 内容 |
| 2 | 工具集受限 | 工具列表只包含 Read/Grep/Glob |

---

## 测试问题 5: --agents CLI 注入 session 级 agent

### CLI 命令

```bash
python -m entry.cli run --repo . --agent build --agents '{"quick-look":{"description":"快速文件查看","intent":"analysis","tools":["Read","Glob"],"model":"haiku","prompt":"你是一个快速文件查看器。只使用 Read 读取文件，"}}' --task "
用 quick-look 子代理查看一下 README.md 的前 5 行
"
```

### 预期行为

```
Step 1: build agent 启动
Step 2: 派发 quick-look 子代理（session 级注入）
Step 3: quick-look 使用 Read 读取 README.md 前 5 行
Step 4: 返回结果
```

### 验证点

| # | 检查项 | 通过条件 |
|---|--------|---------|
| 1 | --agents 解析 | build agent 能找到 quick-look 子代理 |
| 2 | Agent 调用 | 看到 `Agent(subagent_type="quick-look", ...)` |
| 3 | 成功返回 | quick-look 完成并返回分析结果 |
