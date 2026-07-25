# 执行预算与文件安全机制差距分析（修订版）

> 日期：2026-07-25 | 对比基准：Claude Code 原生实现
>
> **修订说明**：初版错误地认为 Read-before-Edit "完全缺失"。经完整代码审计确认，`FileEditTool` **已实现**该检查（`tools/file_edit_tool.py:123-132`），但 `FileWriteTool` **未实现**。本文档更正了此结论。

---

## 一、Read-before-Edit 强制检查

### Claude Code 做法

- `FileReadTool` 记录 mtime 到 `readFileState`
- `FileEditTool` / `FileWriteTool` 执行前验证 mtime
- 未读或 mtime 变化 → 返回错误，强制重读

### 我们的现状

| 工具 | Read-before-Edit | 位置 |
|------|-----------------|------|
| `FileEditTool` | ✅ 已实现 | `tools/file_edit_tool.py:123-132` |
| `FileWriteTool` | ❌ 缺失 | — |

**`FileEditTool` 的实现**（对齐度 🟢）：

```python
# tools/file_edit_tool.py:123-132
if path.exists() and old_str and self._read_cache is not None:
    cache_info = self._read_cache.get(str(path.resolve()))
    if cache_info is None:
        return ToolResult(
            success=False, output="",
            error=f"Read-before-Edit: '{path}' has not been read in this session. "
                  "Read the file first, then edit it.",
        )
```

- 缓存来源：`FileReadTool` + `FileViewTool` 每次读取时调用 `_read_cache.store()` 写入
- mtime 验证：`FileReadCache.get()` 会对比当前 mtime 与存储的 mtime，不一致即清除缓存并返回 None（强制重读）
- 对齐度：与 Claude Code 的 mtimeMs staleness guard **完全等价**

**`FileWriteTool` 的缺失**（差距 🔴）：

`FileWriteTool.execute()` 完全没有缓存检查，可以直接覆盖未读过的文件。Prompt 里有 "Always read the file first before writing"，但这是一条建议，不是强制检查。

### 根因分析：用户看到的错误链

```
用户请求：修改 README.md
  ↓
Agent 调用 FileEditTool（未先 Read）
  ↓
FileEditTool 拒绝："Read-before-Edit: README.md has not been read" ← 正确行为
  ↓
Agent 重新 Read + Edit，但 budget 逼近上限
  ↓
再次读文件、搜索、编辑循环... 
  ↓
RuntimeController.check() 检测到 budget EXHAUSTED
  ↓
"Execution budget exhausted — the agent was unable to complete..."
```

**两个错误的叠加效应**：
1. Read-before-Edit 拦截 → Agent 被迫重读 → 消耗额外的 token 和 step
2. Budget 太低（80k/40steps）→ 重读几轮后耗尽预算 → 任务失败

### G1 修复边界条件（关键）

`path.exists()` 判断是正确的，但需要显式文档化并测试以下边界：

| 场景 | path.exists() | 行为 | 是否正确 |
|------|-------------|------|----------|
| 新建文件 | False | 跳过检查 ✅ | ✅ |
| 覆盖已有文件 | True | 检查缓存 ✅ | ✅ |
| 覆盖空文件（0 字节） | True | 检查缓存 | ✅ 仍需检查——空文件也是"已存在" |
| 符号链接（目标存在） | True（跟随链接） | 检查缓存 | ✅ `resolve()` 后的路径做 key |
| 符号链接（目标已删） | False | 跳过检查 | ⚠️ 需先用 `is_symlink()` 判断 |

**行动项**：G1 修复中补充单元测试，覆盖"新建文件"、"覆盖空文件"、"符号链接"三个场景。

### G2 Per-Tool Gate 的双重惩罚防护

当前 per-step 检查仍保留（每轮开始时注入 WARNING/CRITICAL 消息）。新增 per-tool 检查时，**工具层只做硬阻断（EXHAUSTED），软提示仍由 per-step 独占**：

```python
# core/tool_execution.py — 正确的 per-tool gate
budget_status = self._budget.check()
if budget_status.level == BudgetLevel.EXHAUSTED:  # ← 仅硬阻断
    return ToolResult.from_error(ToolErrorType.UNAVAILABLE, ...)
# WARNING / CRITICAL 不在工具层注入消息——per-step 检查已处理
```

**理由**：如果 step 开始时 budget 是 WARNING 已注入 "wrapping up"，然后该轮第一个工具又触发 CRITICAL 注入 "finish NOW"，Agent 会在同一轮收到两条冲突指令。

### G4: FileReadCache 内存上限（新发现 P0）

**确认**：`FileReadCache` **没有任何大小限制**。无 `max_entries`、无 `max_total_bytes`、无 LRU 淘汰、无 `clear()` 方法。只有 `invalidate()` 在文件被修改时清理单个条目。

**风险**：长 session 中读取 50+ 大文件，cache 无限增长 → 潜在 OOM。

**修复**：
```python
MAX_CACHE_ENTRIES = 200
MAX_CACHE_BYTES = 50 * 1024 * 1024  # 50 MB

def store(self, ...):
    # Evict oldest if over limit
    while len(self.entries) >= MAX_CACHE_ENTRIES:
        oldest = next(iter(self.entries))
        del self.entries[oldest]
    ...
```

### G5: 预算消耗归因监控（P2）

```python
@dataclass
class BudgetUsage:
    productive_steps: int    # 工具调用成功的 step
    retry_steps: int         # 安全拦截触发的重试
    overhead_tokens: int     # 系统消息/错误消息消耗的 token
```

### 修订后的优先级矩阵

| # | 差距 | 优先级 | 理由 |
|---|------|--------|------|
| G1 | FileWriteTool 缺 Read-before-Write | P0 | 安全漏洞 |
| **G4** | **Read Cache 无大小限制** | **P0** | 潜在 OOM，与 G1 同批修 |
| G2 | Per-tool budget gate（仅硬阻断） | P1 | 需配合防双重惩罚约束 |
| G3 | 默认预算过低 | P1 | 与 G2 同步调整 |
| G5 | 预算消耗归因监控 | P2 | 数据基础 |

### 修复建议

**G1: FileWriteTool 补上 Read-before-Write（P0）**

```python
# tools/file_tool.py FileWriteTool.execute() 中，文件写入前：
if path.exists() and self._read_cache is not None:
    cache_info = self._read_cache.get(str(path.resolve()))
    if cache_info is None:
        return ToolResult(
            success=False, output="",
            error=f"Read-before-Write: '{path}' has not been read. Read it first.",
        )
```

---

## 二、执行预算系统

### 完整调用链

```
agent/core.py:_prepare_step()
  → agent/loop/turns.py:evaluate_runtime_step_gate()
    → agent/runtime_controller.py:RuntimeController.check()
      → budget.check()  ← 这里检查 token/step/time 三维限制
        → BudgetLevel.EXHAUSTED → 返回 TERMINATE
        → BudgetLevel.CRITICAL → 注入 "finish NOW" 消息
        → BudgetLevel.WARNING → 注入 "wrapping up" 消息
```

| 维度 | 默认值 | CC 典型值 |
|------|--------|----------|
| `token_limit` | 80,000 | 200,000 |
| `step_limit` | 40 | 100 |
| `time_limit_s` | 600 | 1800 |

### 差距

**G2: 检查粒度是 per-step 而非 per-tool（P1）**

`budget.check()` 在每轮 LLM 调用前执行，不在每个工具调用前执行。一轮中 LLM 可能调用 5 个工具，前 4 个成功，第 5 个超支——但 budget 只在下一轮开始前检测。

**G3: 默认预算值过低（P1）**

80k tokens / 40 steps 对复杂多文件编辑不够。Agent 在被 Read-before-Edit 拦截后需要额外 step 重读文件，这些 step 也计入预算，导致有效工作 step 只有 ~25 步。

### 修复建议

**G2**: 在 `ToolExecutionPipeline.execute()` 中增加 budget gate：
```python
# core/tool_execution.py
budget_status = self._budget.check()
if budget_status.is_exhausted:
    return ToolResult.from_error(ToolErrorType.UNAVAILABLE, detail=budget_status.inject_message)
```

**G3**: 提高默认预算
```python
ExecutionBudgetConfig(token_limit=200_000, step_limit=100, time_limit_seconds=1800)
```

---

## 三、修正后的差距总表

| # | 差距 | 严重度 | 影响 |
|---|------|--------|------|
| G1 | FileWriteTool 缺少 Read-before-Write | 🔴 | 可覆盖未读文件 |
| G2 | Budget gate per-step 而非 per-tool | 🟡 | 一轮中可超支 |
| G3 | 默认预算 80k/40steps/600s | 🟡 | 复杂任务提前耗尽 |
