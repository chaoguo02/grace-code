# Claude Code 行为一致性测试集

## 说明

本测试集验证 forge-agent 的运行时行为与 Claude Code 的可观察行为一致。每个场景都提供一个 CLI 命令和预期行为。

---

## 场景 1: Fresh Named Subagent — 隔离父历史

**测试**: 子代理不继承父对话上下文

```bash
python -m entry.cli run --repo . --agent build --task "
先读一下 README.md 的前 20 行，然后用 explore 子代理去分析 tools/ 目录的结构，最后总结。
"
```

**预期**:
- build agent 先调用 Read 读 README.md
- 然后调用 `Agent(subagent_type="explore", ...)`
- explore 子代理启动时只有自己的系统提示，看不到 build agent 刚才读的 README.md 内容
- 子代理完成后返回结构化结果
- build agent 整合并输出

**验证点**: `Subagent started` + `Subagent finished: completed`

---

## 场景 2: Fork — 继承父历史

**测试**: fork 子代理继承父对话的全部消息

```bash
python -m entry.cli run --repo . --agent build --task "
先读 tools/base.py 找到 BaseTool 类的定义行号，然后 fork 一个子代理来完成详细分析：基于已读取的 BaseTool 定义，分析继承它的所有子类。
"
```

**预期**:
- build agent 读 tools/base.py 找 BaseTool 定义
- 调用 `Agent(isolation="fork", ...)`
- fork 子代理拥有父 agent 的全部历史消息
- fork 可以在自己的提示中引用 BaseTool 定义行号而不需要重新读取

**验证点**: 子代理参数包含执行 `isolation="fork"` 或 fork 类型

---

## 场景 3: Fork + Worktree 隔离

**测试**: fork + worktree 隔离

```bash
python -m entry.cli run --repo . --agent build --task "
创建一个测试用的 hello.txt 文件。使用 worktree fork 来验证文件内容。
"
```

**预期**:
- build agent 创建 hello.txt
- 用 worktree fork 检查内容
- worktree 在独立分支上操作
- 完成后自动清理（无变更时）

**验证点**: worktree 相关工具调用

---

## 场景 4: 一对多并行调查

**测试**: 父 agent 同时派发多个只读子代理并综合结果

```bash
python -m entry.cli run --repo . --agent build --task "
同时派发两个 explore 子代理：
1. 分析 tools/ 目录的模块结构
2. 分析 agent/v2/ 目录的模块结构
等它们都完成后，综合两个分析结果给出一个整体架构图。
"
```

**预期**:
- build agent 在同一轮响应中发出两次 Agent 调用
- Runtime 并行执行两个子代理
- 两个结果都返回后，build agent 综合

**验证点**: 两个 `Subagent finished` 消息出现在最终结果之前

---

## 场景 5: 链式 Agent 调用

**测试**: 一个子代理派发另一个子代理

```bash
python -m entry.cli run --repo . --agent build --task "
派一个 general 子代理去分析 tools/base.py 的 ToolRegistry 类，然后用 explore 去搜索所有引用 ToolRegistry 的文件。
"
```

**预期**:
- build agent → general → explore
- general 有权调用 explore
- general 不能调用 Agent（因为 disallowed_tools 包含 Agent）

**验证点**: general 拿到结果后返回给 build agent

---

## 场景 6: Nested Verifier

**测试**: 子代理使用 submit_findings 工具提交结构化结果

```bash
python -m entry.cli run --repo . --agent build --task "
派 code-reviewer 子代理去审查 tools/base.py 的代码，寻找潜在的 bug 或设计问题
"
```

**预期**:
- code-reviewer 子代理调用 `ReportFindings`（原名 submit_findings）
- 返回结构化 Finding 对象（含 file, line, severity, category）
- Runtime 验证 Finding 结构
- 父 agent 收到 <subagent-report> XML 块

**验证点**: 子代理输出包含 `structured_findings`

---

## 场景 7: Background 子代理

**测试**: 子代理在后台运行，父 agent 可以继续工作

```bash
python -m entry.cli run --repo . --agent build --task "
后台派一个 explore 子代理去分析 README.md，同时我继续做其他事。
"
```

**预期**:
- Agent 调用参数包含 `execution_placement: background`
- 立即返回 BackgroundAgentHandle
- build agent 继续执行
- 后台子代理完成时通过 task-notification 通知

**验证点**: 后台执行不阻塞主流程

---

## 场景 8: Resume 已完成的子代理

**测试**: 向已完成的子代理发新消息让它继续工作

```bash
python -m entry.cli run --repo . --agent build --task "
派一个 explore 去分析 README.md 的前半部分。收到结果后，向同一个子代理发送后续任务，让它分析 README.md 的后半部分。
"
```

**预期**:
- 第一个 Agent 调用创建子代理并返回结果
- build agent 调用 `send_agent_message` 或等效机制
- 子代理恢复并处理新任务

**验证点**: 同一个 session_id 被重用

---

## 场景 9: API Partial（Mock 后端）

**测试**: 使用 Mock 后端时 API 无状态返回

```bash
python -m entry.cli run --repo . --agent build --model mock --task "
简单分析一下项目结构。
"
```

**预期**:
- mock 后端每次返回固定响应
- 工具调用正确通过 mock 数据
- 不调用真实 API

**验证点**: 工具调用正常完成

---

## 场景 10: 权限拒绝

**测试**: deny 特定子代理类型

先编辑 settings.json 添加：
```json
{
  "permissions": {
    "deny": ["Agent(explore)"]
  }
}
```

然后：
```bash
python -m entry.cli run --repo . --agent build --task "
派一个 explore 子代理去分析 README.md
"
```

**预期**:
- Agent(explore) 被 PermissionPipeline 拒绝
- agent 收到权限拒绝提示
- agent 可以改派 general 子代理代替

**验证点**: 工具调用被拒绝的提示

完成后恢复 settings.json。

---

## 场景 11: Plan → 只生成计划 → [1] Execute → 转为 Build

**测试**: `--agent plan` 只分析生成计划，选择 Execute 后自动转为 build 执行

```bash
python -m entry.cli run --repo . --agent plan --plan-action execute --task "
分析一下 tools/base.py 中 ToolRegistry 类的 register 方法，输出它的功能描述，不需要修改代码。
"
```

**预期**:
- plan agent 以 ANALYSIS intent 启动
- 只使用 Read/Grep/Glob 等只读工具
- 产生包含 JSON contract 的结构化计划（execution_intent: "analysis"）
- plan-action=execute 自动执行
- 切换到 build agent（EDIT intent）执行
- build agent 发现没有需要修改的代码，直接输出分析结果
- 整个过程不弹交互式审批菜单

**验证点**: 
- 只出现一次审批弹窗（或 plan-action=execute 直接自动通过）
- build agent 不以 ANALYSIS 模式运行（不会再次进入审批循环）
- 最终输出分析结果

---

## 场景 12: Git Facts 决定编辑结果

**测试**: 子代理的 Git 变更影响父 agent 的决策

```bash
python -m entry.cli run --repo . --agent build --task "
修改 README.md 添加一行 '## Test Section'，然后用 git_diff 确认变更。
"
```

**预期**:
- build agent 用 Edit 修改 README.md
- 用 git_diff 查看变更
- 确认变更存在
- 可选：用 git_commit 提交（如果 task 要求）

**验证点**: git_diff 输出包含变更内容

---

## 场景 13: 自定义 Agent 通过 CLI 使用

**测试**: 使用非 build/plan 的 Agent 类型

```bash
# 先确保 .forge-agent/agents/ 目录下有自定义 agent 定义
python -m entry.cli run --repo . --agent explore --task "统计 tools/ 目录有多少个 .py 文件"
```

**预期**:
- explore agent 直接作为主 agent 运行（不是子代理）
- 使用 read-only 工具
- 返回文件统计结果

**验证点**: `Agent   : explore`（不是 build 或 plan）
