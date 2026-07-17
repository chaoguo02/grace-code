# 自测指南

在项目根目录执行以下命令。`--repo .` 表示用当前目录。

---

## 1. 基础 ReAct — 读文件

最简单的完整链路测试：读取一个文件并报告内容。

```powershell
python -m entry.cli run --repo . --task "读取 README.md，说出它的第一行是什么" --max-steps 5
```

预期：Agent 调用 Read 工具，读取后 FINISH。输出 `V2 run completed successfully`。

---

## 2. 基础 ReAct — 搜索 + 读取

验证 Grep/Glob + Read 的组合：

```powershell
python -m entry.cli run --repo . --task "找到所有 .py 文件中含有 'class AgentTurnState' 的位置，并读出所在的文件内容" --max-steps 8
```

预期：多个工具调用（Glob/Grep/Read），最终 FINISH。

---

## 3. 流式 dispatch（默认开启）

验证流式 dispatch 在 CLI 中默认启用：

```powershell
python -m entry.cli run --repo . --task "列出 src 目录的结构" --max-steps 5
```

观察输出是否在工具执行的同时流式渲染文本（"Answer" 部分逐字出现）。如果想关闭流式对比：

```powershell
set FORGE_STREAMING=0
python -m entry.cli run --repo . --task "列出 src 目录的结构" --max-steps 5
```

---

## 4. Plan Mode — 分析+保存

验证 Plan 的 JSON contract + 审批保存流程：

```powershell
python -m entry.cli run --repo . --agent plan --intent analysis --plan-action save --auto-approve --task "分析项目的入口文件结构，给出重构建议" --max-steps 8
```

预期：Agent 使用只读工具分析（Read/Grep/Glob），生成包含 JSON contract 的 plan 并保存。输出 `Plan saved`。

---

## 5. 子代理派发 — fan-out

验证两个子代理并行执行：

```powershell
python -m entry.cli run --repo . --agent build --intent edit --auto-approve --task "同时分析 entry/ 和 agent/ 两个目录的结构差异" --max-steps 10
```

预期：Agent 调用 Agent 工具派发两个 explore 子代理，各自读文件，父代理综合结果后 FINISH。

---

## 6. 子代理 worktree 隔离

验证 worktree 隔离 + 结果应用：

```powershell
python -m entry.cli run --repo . --agent build --intent edit --auto-approve --task "在 agent/session/ 中新建一个空文件 test_marker.txt，通过 worktree 子代理完成" --max-steps 10
```

预期：Agent 派发 worktree 子代理创建文件，父代理审查后 apply。

---

## 7. 输出截断恢复

验证 max_output_tokens 自动提升+恢复机制。用短 max_tokens 触发截断：

```powershell
python -m entry.cli run --repo . --task "详细分析 README.md 的每个段落，逐个描述" --max-steps 10
```

预期：如果输出被截断，Agent 自动恢复并继续。正常运行即可。

---

## 8. 交互式 Chat

验证完整的交互式会话：

```powershell
python -m entry.cli chat --repo .
```

进入后尝试：

```text
/help
解读一下这个项目的核心架构
```

```text
/mode plan
分析一下 tests/ 目录的测试覆盖情况
```

```text
/mode react
帮我写一个 .gitignore
```

---

## 9. Memory — 写入和回忆

验证持久记忆：

```powershell
python -m entry.cli chat --repo .
```

```text
记住，我喜欢用 pytest --tb=short 来跑测试
```

（重启 Chat 后）

```text
我平时怎么跑测试的？
```

预期：重启后仍能回忆出记忆内容。

---

## 预期结果

| 测试 | 预期 | 关键标志 |
|------|------|---------|
| 1-2 | ✅ 工具调用 + FINISH | `V2 run completed successfully` |
| 3 | ✅ 流式渲染 | 文字逐段出现而非一次性 |
| 4 | ✅ 只读分析 + plan 保存 | `Plan saved` |
| 5 | ✅ 2 个子代理并行 + 综合 | `<task-notification>` × 2 |
| 6 | ✅ worktree apply | `applied` 状态 |
| 7 | ✅ 正常完成 | 截断恢复日志 `escalation` |
| 8 | ✅ 交互流畅 | 无异常退出 |
| 9 | ✅ 记忆持久 | 重启后回忆正确 |
