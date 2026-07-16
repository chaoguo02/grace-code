# Plan Mode + Subagent 验证

---

## 测试 1: Plan 模式（只读分析 + 生成计划）

```bash
python -m entry.cli run --repo . --agent plan --task "分析一下 tools/ 目录的整体架构，每个文件是做什么的。给出一个结构化的总结。"
```

**预期**:
- 只在工具列表中看到 Read、Grep、Glob、find_symbol、git_status、git_diff 等只读工具
- 不会出现 Write、Edit、Bash
- 输出一个结构化的分析报告

**验证点**: `Agent   : plan` (计划模式 agent)

---

## 测试 2: Build agent 派发 Explore subagent

```bash
python -m entry.cli run --repo . --agent build --task "我需要了解 agent/v2/ 目录的代码架构。派一个 explore 子代理去分析这个目录的结构和关键组件，然后向我汇报。"
```

**预期**:
- Build agent 调用 `Agent(subagent_type="explore", ...)` 派发子代理
- 子代理用 read-only 工具分析目录
- 子代理完成后汇报结果给父代理
- 父代理整合后输出

**验证点**:
- `ToolCall [X] Agent → explore` (不是 `task`)
- `Subagent explore started [...]`
- 子代理只用了只读工具
- `Subagent explore finished: completed`

---

## 测试 3: Build agent 派发 General subagent（编辑操作）

```bash
python -m entry.cli run --repo . --agent build --task "在项目根目录创建一个 hello.txt 文件，内容是 'Hello from forge-agent subagent test'。用 general subagent 来做这件事。"
```

**预期**:
- Build agent 调用 `Agent(subagent_type="general", ...)`
- General subagent 有完整的 Write/Bash 工具
- 创建成功

**验证点**:
- `ToolCall [X] Agent → general` (不是 `task`)
- 工具调用用了 Write（不是 file_write）
- 文件确实被创建

---

## 测试 4: 简单任务（不需要 subagent）

```bash
python -m entry.cli run --repo . --agent build --task "查看当前 git 状态"
```

**预期**: Build agent 直接调用 `git_status` 完成任务，不派发 subagent。

**验证点**: 没有 Agent/task 工具调用，直接使用 git_status。
