# 调试日记

> 日期: 2026-07-18
> 目的: 记录端到端测试中发现的问题、排查过程、根因和修复

---

## 测试场景

所有测试使用真实 DeepSeek API（`deepseek-v4-flash`），在 Windows 11 + PowerShell 上运行。

```powershell
python -m entry.cli run --repo . --task "<任务描述>" --max-steps N
```

| 编号 | 场景 | 命令 |
|------|------|------|
| 1 | 基础 ReAct：读文件 | `读取 README.md，说出它的第一行是什么` |
| 2 | Grep + Read 组合 | `找到所有 .py 文件中含有 'class AgentTurnState' 的位置，并读出所在的文件内容` |
| 3 | Plan mode | `--agent plan --plan-action save` |
| 4 | 子代理 fan-out | `分析 entry/ 和 agent/ 两个目录的结构差异` |
| 5 | 交互式 Chat | `python -m entry.cli chat --repo .` |

---

## 问题 1：token_budget_continuation 导致重复 FINISH

### 现象

测试 1 中，Agent 正确读取文件并 FINISH，但 Nudge 机制认为"预算没用完"持续触发，模型重复回答相同内容直到 `max_steps`。

```
✅ Finish [2]  ← 正确回答
✅ Finish [3]  ← nudge 触发，模型重复
✅ Finish [4]  ← nudge 触发，模型重复
✅ Finish [5]  ← nudge 触发，模型重复
V2 run finished with status: max_steps
```

### 排查

1. 查看 `RecoveryState.should_nudge()` 逻辑：剩余预算 > 10% 且非 diminishing returns 时触发
2. 计算：`budget_tokens=160000`，`total_tokens ≈ 5000` → 仅用 3%，远低于 90% 阈值
3. Diminishing returns（`nudge_count >= 3` 且 `delta < 500`）在第 4 次触发才生效，但 `max_steps=5` 已经到顶

### 根因

Nudge 机制对简单任务过于激进。模型正确回答了问题，但 nudge 认为"预算没用完，继续工作"。

### 修复

将 `token_budget_continuation` 改为 opt-in（默认关闭）：

```
entry/cli.py:  FORGE_NUDGE=0（默认）
entry/chat.py: FORGE_NUDGE=0（默认）
```

用户需要时设置 `FORGE_NUDGE=1` 启用。

---

## 问题 2：Grep 遍历目录无限卡死

### 现象

测试 2 中，Grep 工具长时间（>2 分钟）无响应。Agent 显示 `ToolCall [1] Grep` 后一直等待。

### 排查

1. 检查 `SearchTextTool.execute()` 实现——使用 `rglob()` 遍历目录
2. 检查 `.gitignore` ——项目根目录下存在数百个 `.pytest-batch*` 和 `.tmp-*` 目录
3. 用 `os.walk` 手动测试：遍历这些目录需要 2 分钟以上

### 根因

`rglob()` 会完整遍历所有子目录（包括 .pytest-batch*、.tmp-*），遍历范围约 10 万+ 文件。过滤只在路径生成后执行，无法提前剪枝。

### 修复

将 `rglob()` 替换为 `os.walk(topdown=True)` 配合 `dirnames[:]` 原地剪枝：

```python
# BEFORE: rglob 遍历所有目录后再过滤
for filepath in sorted(root.rglob("*")):
    if should_skip(filepath): continue

# AFTER: os.walk 剪枝跳过临时目录
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if not should_skip(d)]
```

效果：251 个 .py 文件在 0.0 秒内遍历完成。

---

## 问题 3：Grep 子进程编码错误（Windows 特有）

### 现象

测试 2 中，Grep 返回 `No matches found for 'class AgentTurnState'`，但 `class AgentTurnState` 确实存在于 `agent/core.py:729`。手动搜索可以找到。

### 排查

1. 添加路径日志确认 `workspace_root` 正确指向 `D:\...\forge-agent`
2. 用纯 Python 直接调用 `_iter_files` + `re.search` 能找到匹配
3. 日志显示 `Grep using subprocess: grep` ——说明走了 Git Bash 的 `grep`
4. 测试 Git Bash `grep` 编码：`grep -rn "class AgentTurnState" .` 返回空

### 根因

Windows 上 Git Bash 的 `grep` 默认使用系统 locale（中文 Windows 为 GBK）读取文件，而 Python 源码是 UTF-8 编码。GBK 无法正确解码 UTF-8 中的非 ASCII 字节，导致匹配失败。

尝试设置 `LC_ALL=C.UTF-8` 环境变量，但 Git Bash 在 Windows 上忽略该变量。

### 修复

Windows 上跳过子进程，强制使用纯 Python 路径：

```python
if platform.system() == "Windows":
    return None  # 跳过子进程 grep
```

纯 Python 路径使用 `read_text(encoding="utf-8")` 正确读取 UTF-8 文件。

---

## 问题 4：StreamingExecutor 工具重复执行（Double Enqueue）

### 现象

测试 2 中，Grep 前 4 次返回 0 匹配，第 5 次开始正确返回。但前 4 次的日志显示 `match_count=0` 且 `file_count=0`。不过直接调用工具能正确找到结果。

### 排查

1. 直接在 `execute()` 方法入口加 `match_count` 日志：确认 `match_count=0` 不是输出格式问题
2. 用 `concurrent.futures.ThreadPoolExecutor` 模拟并发调用——正常
3. 用 `StreamingToolExecutor` 直接调用——正常
4. 用 `PolicyAwareToolRegistry` + `with_run_context` 包装——正常
5. 写独立测试文件模拟完整调用链——正常
6. 对比 standalone 和 agent 循环的差异点：`StreamingToolExecutor.enqueue()` 被调用两次
7. 第一次来自 `_stream_and_dispatch()`（流式解析到 tool_use 时立即 enqueue）
8. 第二次来自工具执行段（`for _tc in effective_tool_calls: executor.enqueue(_tc)`）

### 根因

Streaming dispatch 路径和 post-stream 工具执行段都会对同一个工具调用 `enqueue()`。当 LLM 响应的 `tool_call.id` 为 `None` 时，去重检查失败（只检查 `tool_call.id`），导致工具被 enqueue 两次，创建两个 `TrackedTool` 条目。

第二次 enqueue 时 admission control 判断工具正在执行（第一次），拒绝启动。但 `collect()` 等待所有条目完成，第一次完成后第二次永远无法启动（因为没人重新尝试），造成死等。

### 修复

两个修复：

1. **去重键扩展到 name+params hash**：当 `tool_call.id` 为 `None` 时使用 `md5(name + json(params))` 作为去重键。

2. **`_execute_one` 完成后调用 `process_queue()`**：当一个工具完成执行后，重新扫描队列中因 admission control 被阻塞的工具，尝试启动它们。

```python
def _execute_one(self, tracked):
    result = self._registry.execute_tool(...)
    ...
    self.process_queue()  # 尝试启动因独占锁被阻塞的工具
```

---

## 问题 5：Grep glob 模式 `**/*.py` 不匹配

### 现象

测试 2 中，LLM 传入 `glob="**/*.py"`，Grep 返回 0 匹配。日志显示 `file_count=0`。但 `glob="*"` 时返回 573 个文件。

### 排查

1. 日志明确显示 `glob='**/*.py'` 时 `file_count=0`
2. 手动测试 `fnmatch.fnmatch("agent/core.py", "**/*.py")`——返回 `False`
3. `fnmatch` 只支持简单的 shell glob 模式，不支持 `**/` 递归前缀

### 根因

Python 的 `fnmatch.fnmatch()` 不支持 `**/` 递归通配符。但 `os.walk` 已经递归遍历了所有子目录，所以 `**/` 前缀是多余的。

### 修复

在 `_iter_files()` 中预处理 glob 模式：如果以 `**/` 开头则去掉该前缀。

```python
if glob_pattern.startswith("**/"):
    glob_pattern = glob_pattern[3:]
```

---

## 修复验证

### 验证方法

1. **单元测试**：`python -m pytest tests/test_cc_alignment_features.py` — 93 测试全部通过
2. **回归测试**：`python -m pytest tests/test_cc_alignment_features.py tests/test_plan_approval.py tests/test_cli_v2_orchestration.py tests/test_agent_v2_mcp_integration.py tests/test_chat.py` — 全部通过
3. **end-to-end 测试**：用真实 DeepSeek API 运行测试场景，验证完整生产路径

### 最终测试输出（问题 2 验证通过）

```
🛠 ToolCall [1] Grep → class AgentTurnState
    │ D:\...\agent\core.py:729: class AgentTurnState:
    │ [Showing 1 matches]

✅ Finish [3]
V2 run completed successfully.
```

第一次 Grep 调用即正确返回，3 步完成，无需重试。

---

## 修复文件清单

| 文件 | 改动 |
|------|------|
| `agent/core.py` | `streaming_tool_execution` 默认 True；`token_budget_continuation` 默认 False |
| `entry/cli.py` | `FORGE_NUDGE` 环境变量（默认 0） |
| `entry/chat.py` | `FORGE_NUDGE` 环境变量（默认 0） |
| `core/streaming_executor.py` | 去重键扩展到 (name+params) hash；`_execute_one` 后调用 `process_queue()`；共享线程池 |
| `tools/search_tool.py` | `os.walk` 剪枝替换 `rglob`；Windows 跳过子进程；`**/*.py` glob 预处理；移除调试日志 |
| `tests/test_cli_v2_orchestration.py` | 测试环境变量 `FORGE_STREAMING=0` + `FORGE_NUDGE=0` |
