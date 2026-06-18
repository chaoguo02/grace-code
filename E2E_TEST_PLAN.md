# Forge-Agent 端到端验证手册

> 目标：通过真实运行验证所有核心机制是否真正生效，而不是 mock 假装成功。

## 前置准备

```powershell
# PowerShell 加载环境变量
Get-Content .env | Where-Object { $_ -notmatch '^#' -and $_ -ne '' } | ForEach-Object {
    $k,$v = $_ -split '=',2; [Environment]::SetEnvironmentVariable($k,$v,'Process')
}
```

```bash
# Linux/Mac
export $(cat .env | grep -v '^#' | xargs)
```

---

## A. 基础链路 — `run` 模式一次性任务

**命令：**
```bash
python -m entry.cli run --repo . --task "读取 config/schema.py 文件，告诉我里面定义了几个 dataclass" --max-steps 5
```

**预期：**
- [ ] agent 调用 `file_read` 工具读取 config/schema.py
- [ ] 返回正确数量（约 10 个 dataclass）
- [ ] 看到 token 统计输出
- [ ] 退出码 0，无异常

---

## B. 工具真实调用 — `chat` 模式

**启动：**
```bash
python -m entry.cli chat --repo .
```

### B1. 代码搜索
```
输入: 在tools目录下搜索包含"risk_level"的文件
```
- [ ] 调用 `search_text` 工具
- [ ] 返回多个匹配文件（至少 base.py, shell_tool.py）

### B2. Shell 命令
```
输入: 执行 git status
```
- [ ] 调用 `shell_exec` 或 `git_status`
- [ ] 输出当前 git 状态（分支信息 + 文件列表）

### B3. 文件写入
```
输入: 创建一个文件 test_e2e_tmp.txt 内容为 "hello forge-agent"
```
- [ ] 调用 `file_write` 创建文件
- [ ] 文件实际被创建在磁盘上

### B4. 文件读取
```
输入: 读取 test_e2e_tmp.txt
```
- [ ] 调用 `file_read`
- [ ] 输出内容为 "hello forge-agent"

### B5. 符号搜索
```
输入: 找到 ReActAgent 类的定义在哪个文件
```
- [ ] 调用 `find_symbol` 或 `search_text`
- [ ] 返回 agent/core.py

### B6. Git 操作
```
输入: 查看最近3次 git commit 的信息
```
- [ ] 调用 `shell_exec` 执行 git log
- [ ] 返回最近 3 条 commit message

---

## C. Task Anchor 防漂移验证

**命令：**
```bash
python -m entry.cli run --repo . --task "找到 agent/core.py 中 _build_project_context 方法，列出它构建了哪些 context 部分，然后找到 _build_messages 方法说明它的调用流程" --max-steps 15
```

**验证方式：**
```bash
# 查看最新 log 文件中是否注入了 task anchor
grep -i "TASK ANCHOR\|Current Task" logs/*.jsonl | tail -5
```

**预期：**
- [ ] 日志中出现 `[TASK ANCHOR]` 或 `## Current Task` 被注入到 context
- [ ] agent 在多步执行后仍然围绕原始任务回答，没有跑偏

---

## D. 循环检测 + Reflection

### D1. Reflection 触发（测试失败场景）
**命令：**
```bash
python -m entry.cli run --repo . --task "运行 pytest tests/test_compaction.py 如果有失败就修复" --max-steps 20
```

**验证：**
```bash
# 查看最新 log
python -m entry.cli log list
python -m entry.cli log show logs/<最新文件>.jsonl
```

**预期：**
- [ ] 如果测试通过：agent 正常 finish
- [ ] 如果测试失败：日志中出现 `reflection` 类型事件
- [ ] reflection 内容包含对失败原因的分析

### D2. 循环检测
**命令：**
```bash
python -m entry.cli run --repo . --task "反复读取 README.md 文件直到你找到一个不存在的章节叫 'Secret Section'" --max-steps 15
```

**预期：**
- [ ] agent 尝试几次后触发循环检测
- [ ] 日志中出现 "You are repeating the same action" 提示
- [ ] agent 最终 finish 而不是耗尽所有步数

---

## E. Compaction 验证

**启动 chat 模式：**
```bash
python -m entry.cli chat --repo .
```

### E1. 手动 compact
```
输入1: 这个项目是做什么的
输入2: agent/core.py 有多少行代码
输入3: 解释一下 ToolRegistry 的作用
输入4: 列出所有的 agent 模式
输入5: /compact
```

**预期：**
- [ ] 输出类似 "Compacted N messages → summary" 的信息
- [ ] 压缩后继续提问能正常回答

### E2. 带焦点的 compact（新功能）
```
输入6: /compact 重点保留关于工具系统的信息
```

**预期：**
- [ ] 压缩成功不报错
- [ ] 摘要中工具相关信息被优先保留

### E3. 压缩后上下文连续性
```
输入7: 刚才我问过哪些问题？
```

**预期：**
- [ ] agent 能从压缩摘要中回忆出之前讨论过的主题
- [ ] 不会说"我不知道之前聊了什么"

---

## F. Plan Mode 两阶段流程

**启动：**
```bash
python -m entry.cli chat --repo . --mode plan
```

**或 chat 中切换：**
```
输入: /mode plan
输入: 给 tools/base.py 的 ToolRegistry 添加一个工具执行耗时统计功能
```

**预期：**
- [ ] Phase 1（规划）：agent 用只读工具探索代码（file_read, search_text）
- [ ] 输出一个 markdown 格式的实现计划
- [ ] 等待用户审批（提示输入 yes/no）
- [ ] 输入 `yes` 后进入 Phase 2（执行）
- [ ] Phase 2：agent 调用写工具（file_write）实施修改

**验证只读约束：**
- [ ] Phase 1 中不能出现 file_write / shell_exec(危险命令) 调用

---

## G. Memory 系统

**启动 chat 模式：**
```bash
python -m entry.cli chat --repo .
```

### G1. 显式记忆写入
```
输入: 记住：这个项目的测试用 pytest 跑，不需要 unittest
```

**预期：**
- [ ] agent 调用 `memory_write` 工具
- [ ] 保存为 feedback 类型的记忆
- [ ] 磁盘上创建了对应的 .md 文件

**验证：**
```bash
ls ~/.forge-agent/projects/*/memory/
# 或
ls ~/.forge-agent/global/memory/
```

### G2. 记忆列表
```
输入: 列出所有记忆
```

**预期：**
- [ ] 调用 `memory_list` 工具
- [ ] 列出刚才保存的记忆条目

### G3. ProactiveMemory（自动保存）
```
输入: 不要在代码中使用 print 进行调试，用 logging
```

**预期：**
- [ ] ProactiveMemory 检测到纠正性语句（"不要"）
- [ ] 自动保存为 feedback 记忆（不需要 agent 主动调用 memory_write）
- [ ] 查看日志确认：`grep -i "proactive\|auto.*memory" logs/*.jsonl`

### G4. 跨 session 记忆恢复
```
退出 chat（/exit），重新启动 chat
输入: 你记得我之前说过关于测试的什么偏好吗？
```

**预期：**
- [ ] agent 能回忆出 "用 pytest 不用 unittest" 的偏好
- [ ] 这条信息来自持久化的记忆文件

---

## H. HITL 审批流程

**启动 chat 模式：**
```bash
python -m entry.cli chat --repo .
```

### H1. 危险命令触发审批
```
输入: 执行命令 rm -rf /tmp/forge_test_dir
```

**预期：**
- [ ] ShellTool 检测到 `rm -rf`（危险关键词）
- [ ] 弹出确认提示框（Confirmation Required）
- [ ] 显示 Tool/Risk/Params 信息
- [ ] 输入 `n` 拒绝 → agent 收到拒绝反馈，不崩溃

### H2. 安全命令自动通过
```
输入: 执行命令 ls -la
```

**预期：**
- [ ] 在 readonly whitelist 中，自动通过不弹确认
- [ ] 直接输出结果

### H3. 中等风险命令
```
输入: 执行命令 pip install requests
```

**预期：**
- [ ] 检测到 `pip install`（中等风险）
- [ ] 触发确认提示

---

## I. MCP Server 连接与调用

**启动（确保 config 中 demo server 配置存在）：**
```bash
python -m entry.cli chat --repo .
```

### I1. 查看 MCP 状态
```
输入: /mcp
```

**预期：**
- [ ] 显示 demo server 连接状态
- [ ] 列出 demo server 提供的工具

### I2. 调用 MCP 工具
```
输入: 调用 demo server 的 hello 工具
```

**预期：**
- [ ] 成功调用 MCP 工具
- [ ] 返回 demo server 的响应

---

## J. Slash 命令完整性

**chat 模式中逐一验证：**

| 命令 | 预期 |
|------|------|
| `/help` | 显示帮助文本，列出所有命令 |
| `/stats` | 显示 session 统计（rounds, tokens, steps） |
| `/mode` | 显示当前模式 |
| `/mode react` | 切换到 react 模式，无报错 |
| `/model` | 显示当前模型名 |
| `/skill list` | 列出可用 skills |
| `/clear` | 清除对话历史，确认提示 |

**预期：**
- [ ] 所有命令不崩溃
- [ ] 输出信息正确
- [ ] 切换后继续对话正常

---

## K. 边界情况

### K1. 空输入
```
输入: （直接回车）
```
- [ ] 不崩溃，提示重新输入或忽略

### K2. 超长输入
```
输入: 重复一段话超过 1000 字符
```
- [ ] 正常处理不截断

### K3. 中断恢复
```
执行一个多步任务中间按 Ctrl+C
```
- [ ] 优雅退出，不留僵尸进程
- [ ] 日志正确记录中断

---

## 验证结果记录

| 项目 | 状态 | 备注 |
|------|------|------|
| A. 基础链路 | ⬜ | |
| B1. 代码搜索 | ⬜ | |
| B2. Shell 命令 | ✅ | git status 正常 |
| B3. 文件写入 | ⬜ | |
| B4. 文件读取 | ⬜ | |
| B5. 符号搜索 | ⬜ | |
| B6. Git 操作 | ⬜ | |
| C. Task Anchor | ⬜ | |
| D1. Reflection | ⬜ | |
| D2. 循环检测 | ⬜ | |
| E1. Compact | ⬜ | |
| E2. Compact+focus | ⬜ | |
| E3. 压缩连续性 | ⬜ | |
| F. Plan mode | ⬜ | |
| G1. 记忆写入 | ⬜ | |
| G2. 记忆列表 | ⬜ | |
| G3. Proactive | ⬜ | |
| G4. 跨session | ⬜ | |
| H1. 危险命令审批 | ⬜ | |
| H2. 安全命令通过 | ⬜ | |
| H3. 中等风险 | ⬜ | |
| I1. MCP 状态 | ⬜ | |
| I2. MCP 调用 | ⬜ | |
| J. Slash 命令 | ⬜ | |
| K1. 空输入 | ⬜ | |
| K2. 超长输入 | ⬜ | |
| K3. 中断恢复 | ⬜ | |
