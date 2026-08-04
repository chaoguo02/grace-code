# 记忆系统——CC 对齐执行文档

> 日期：2026-08-04
> 目标：用 SQLite 实现 CC 的"文件系统 + LLM 选择"记忆模式
> 原则：不改存储介质（SQLite），改检索模式（算法 → LLM选择）和注入方式（自动 → 目录 + 按需）

---

## 第一部分：CC 对照——YAML 头部字段

### 1.1 CC 每条记忆的 YAML frontmatter

```yaml
---
name: no-mock-database        # kebab-case slug，唯一标识
description: >                 # 一行摘要（≤120 字符）
  集成测试必须使用真实数据库，不能用 mock
type: feedback                 # user | feedback | project | reference
---
```

正文（markdown）跟在 YAML 后面，是记忆的完整内容。LLM 在看完目录后，用 `memory_read(name)` 读取正文。

### 1.2 Grace Code 映射

Grace Code 的 `Memory` 数据模型（`memory/models.py`）已完全对齐 CC 的 YAML 字段：

| CC YAML 字段 | Grace Code Memory 字段 | 说明 |
|---|---|---|
| `name` | `name: str` | kebab-case slug——完全对应 |
| `description` | `description: str` | 一行摘要——完全对应 |
| `type` | `metadata.type: MemoryType` | user / feedback / project / reference——完全对应 |
| （正文） | `content: str` | markdown 正文——完全对应 |
| — | `metadata.status: MemoryStatus` | Grace Code 扩展：active / deprecated |
| — | `metadata.scope: MemoryScope` | Grace Code 扩展：session / project / global |
| — | `metadata.confidence: float` | Grace Code 扩展：0.0–1.0 |
| — | `metadata.importance: float` | Grace Code 扩展：0.0–1.0 |
| — | `anchors: list[Anchor]` | Grace Code 扩展：文件/符号/任务锚点 |
| — | `created_at / updated_at` | Grace Code 扩展：时间戳 |

**结论**：数据模型已经 CC-aligned。3 个核心字段（name/description/type）完全对应。Grace Code 多出的 status / scope / confidence / importance / anchors 是合理的扩展，不是错误。

### 1.3 多出的字段如何处理

**保留，不展示在 MEMORY 目录中**。MEMORY 目录只暴露 CC-aligned 的 3 个字段：

```
- `no-mock-database`: 集成测试必须使用真实数据库，不能用 mock (feedback)
```

Grace Code 扩展字段（status/scope/confidence/importance/anchors）只在数据库内部使用——过滤逻辑用 status=active，提取逻辑用 confidence/anchors。LLM 看到的不变。

---

## 第二部分：MEMORY 目录索引

### 2.1 CC 的 MEMORY.md 格式

```markdown
# Project Memory Index

## user
- `api-design-preference`: 用户偏好 RESTful API 设计

## feedback
- `no-mock-database`: 集成测试必须使用真实数据库，不能用 mock
- `log-all-errors`: 所有异常必须打到 structured log

## project
- `q3-permission-migration`: Q3 目标完成权限系统迁移

## reference
- `auth-wiki`: 认证文档在 wiki.internal.com/auth
```

约束：≤200 行，≤25KB。仅目录——正文通过 `memory_read(name)` 按需加载。

### 2.2 Grace Code 的 `MemorySummary`

`memory/models.py:167` 已有 `MemorySummary`——正好是 MEMORY.md 中每一行的等价物：

```python
@dataclass
class MemorySummary:
    name: str
    description: str
    type: str
    updated_at: str = ""
```

### 2.3 从 SQLite 动态生成 MEMORY 目录

```python
# memory/catalog.py — NEW

def build_memory_catalog(store, max_lines: int = 200, max_bytes: int = 25_000) -> str:
    """从 SQLite 生成 CC MEMORY.md 风格的目录。

    格式：按 type 分组，每行一条 memory（name + description）。
    约束：≤200 行，≤25KB。active 状态才纳入。deprecated 不显示。

    LLM 在 system prompt 中看到这个目录，自主决定调用
    memory_read(name) 读取完整内容。
    """
    from memory.models import MemoryType, MemoryStatus

    # 收集 active 记忆
    summaries = store.list_memories()
    by_type: dict[str, list] = {
        "user": [], "feedback": [], "project": [], "reference": [],
    }
    for s in summaries:
        if getattr(s, 'status', None) == MemoryStatus.DEPRECATED:
            continue
        t = str(getattr(s, 'type', 'project'))
        if t in by_type:
            by_type[t].append(s)

    lines = ["# Project Memory Index"]
    count = 1

    for type_name in ("user", "feedback", "project", "reference"):
        mems = by_type[type_name]
        if not mems:
            continue
        lines.append(f"\n## {type_name}")
        count += 1
        for i, m in enumerate(mems):
            desc = (getattr(m, 'description', '') or '')[:120]
            lines.append(f"- `{m.name}`: {desc}")
            count += 1
            if i >= 14:  # ≤15 per type
                lines.append(f"- ... ({len(mems) - 15} more)")
                count += 1
                break
        if count >= 195:  # 接近 200 行
            lines.append("\n... [truncated at 200 lines]")
            break

    content = "\n".join(lines)
    if len(content) > max_bytes:
        content = content[:max_bytes] + "\n... [truncated at 25KB]"
    return content
```

### 2.4 注入位置

```
会话启动 → system prompt 里注入 MEMORY 目录
  （LLM 看到全部 active 记忆的 name + description + type）

LLM 自主决定需要哪个 → 调用 memory_read("name")
  → MemoryReadTool.execute(params={"name": "no-mock-database"})
  → MemoryStore.read_memory("no-mock-database") → 返回完整 Memory
  → tool_result 注入 conversation → LLM 纳入上下文

压缩后 → 重新从 SQLite 生成 MEMORY 目录 → 重新注入
```

---

## 第三部分：LLM 选择型检索——完整流程

### 3.1 当前（算法型）→ 废弃

```
系统每轮扫描 SQLite
  → recall() 4 路并行收集 (pinned/always/scoped/semantic)
  → _deterministic_score() 评分 (relevance×0.45 + ...)
  → _select_for_injection() (top-8, 3000 token)
  → _format_injection() → 拼成 [MEMORY] section → 注入 user message
```

问题：(1) LLM 没有选择权——系统决定注入什么；(2) 权重无理论支撑；(3) 每轮重建成本高。

### 3.2 目标（LLM选择型）→ 新建

```
会话启动 → build_memory_catalog() → 注入 system prompt（仅这一次）

LLM 对话中自主决定：
  → 不需要记忆 → 不调 memory_read（零额外上下文）
  → 需要某条记忆 → memory_read("no-mock-database")
    → 完整内容注入 conversation
    → 后续轮次 LLM 已经看到内容，不需要重新读

记忆更新：
  → LLM 学到新东西 → memory_write(name=..., description=..., type=..., content=...)
  → MemoryExtractor.write_success_memories()（post-run 自动）
  → 下次会话 MEMORY 目录自动包含新条目
```

### 3.3 与 CC 的完全对应

| 步骤 | CC | Grace Code |
|---|---|---|
| 目录生成 | 扫描 .md 文件 YAML 头 | `build_memory_catalog()` 从 SQLite |
| 目录注入 | 会话启动 + 压缩后 | 会话启动（system prompt） |
| LLM 选择 | LLM 看到目录 → 决定读哪些 | 完全同 |
| 读取内容 | `memory_read("name")` → 读 .md 文件 | `MemoryReadTool` → SQLite `read_memory()` |
| 更新内容 | `memory_write(...)` → 写 .md 文件 | `MemoryWriteTool` → SQLite `write_memory()` |
| 压缩后 | 重新从磁盘读 | 重新从 SQLite 生成 |

---

## 第四部分：文件级改动方案

### 4.1 新建：`memory/catalog.py`

```python
"""Memory catalog generation — CC MEMORY.md equivalent.

Grace Code uses SQLite instead of file-system .md files, but the catalog
format is identical to CC's MEMORY.md: name, description, type grouped
by category, injected into system prompt once per session.

Post-compaction, the catalog is regenerated from SQLite so the LLM
always sees the current active memory set.
"""

def build_memory_catalog(store, max_lines=200, max_bytes=25_000) -> str:
    """Generate CC MEMORY.md style catalog from SQLite."""
    ...
```

### 4.2 废弃标记：`memory/context.py`

改造 `build_memory_section()`——不再自动选择+注入，改为直接调用 `memory/catalog.py:build_memory_catalog()`：

```python
# memory/context.py

class MemoryContext:
    """DEPRECATED PHASE — build_memory_section() replaced by catalog approach.
    
    Kept for backward compat during migration.  New code should use
    memory/catalog.py:build_memory_catalog() instead.
    """
```

### 4.3 废弃标记：`memory/recall.py`

```python
# memory/recall.py module docstring
"""
DEPRECATED — Grace Code algorithm-based recall replaced by CC's LLM-selection model.

CC has no MemoryRecallService equivalent.  The LLM sees the memory catalog
in its system prompt and autonomously decides which memories to read via
the memory_read tool.

Module retained during migration for fallback and web UI recall display.
New code should not add dependencies on MemoryRecallService.
"""
```

### 4.4 接线：`server/services/chat_pipeline.py`

`_execute_native()` conversation 构造：

```python
# 在 system prompt 之后、history 之前注入 MEMORY 目录
from memory.catalog import build_memory_catalog

if hasattr(self._ports, 'memory_context'):
    _catalog = build_memory_catalog(self._ports.memory_context.store)
    if _catalog:
        msgs.append({"role": "system", "content": _catalog})
```

### 4.5 无需改动

| 文件 | 原因 |
|---|---|
| `memory/models.py` | MemoryType 4 种、MemorySummary、Memory——全部 CC-aligned |
| `memory/store.py` | SQLite CRUD——已经提供 `list_memories()` + `read_memory()` + `write_memory()` |
| `memory/extractor.py` | 提取逻辑正确——post-run memory 写入不变 |
| `tools/memory_tool.py` | MemoryReadTool / MemoryWriteTool / MemoryListTool / MemoryDeleteTool 全部正确 |
| `memory/file_backend.py` | .md 文件导出——保留作为可审查备份（后续 Phase） |

---

## 第五部分：实现步骤

| Step | 内容 | 改动 | 行数 |
|---|---|---|---|
| 1 | 新建 `memory/catalog.py` | `build_memory_catalog()` | +50 |
| 2 | 废弃标记 `memory/context.py` | docstring + `build_memory_section()` 改为 delegate 到 catalog | +5 |
| 3 | 废弃标记 `memory/recall.py` | 模块 docstring | +10 |
| 4 | `ChatPipelinePorts` + `agent_service` 接线 | +`memory_context` 字段 + 传入 | +3 |
| 5 | `_execute_native()` 注入 MEMORY 目录 | +6 |
| 6 | `_execute_native()` 注入项目指令 (GRACE.md) | +4 |
| 7 | `_execute_native()` 注入 session context | +3 |
| 8 | `_execute_native()` post-run memory 提取 | +10 |
| 9 | 验证 `memory_read` 在 native 路径可用 | 确认 | — |
| 10 | 全量回归 | — | — |

### 执行批次

```
Batch A (独立): Steps 1, 2, 3  ← memory/ 模块，不影响运行时
Batch B (接线): Steps 4-9       ← 需按顺序
Batch C (验证): Step 10
```

---

## 第六部分：字段对照总表——CC vs Grace Code

```
CC 记忆系统（文件系统）           Grace Code（SQLite）
───────────────────────────     ──────────────────────────
CLAUDE.md                        context/claude_md.py  (✅ CC-aligned)
MEMORY.md 索引                    memory/catalog.py     (🆕 新建)
${name}.md YAML + markdown        memory/models.py Memory (✅ 已对齐)
memory_read 工具                   tools/memory_tool.py  (✅ 已存在)
memory_write 工具                  tools/memory_tool.py  (✅ 已存在)
CC 无                            memory/extractor.py    (🟡 Grace Code 扩展)
CC 无                            memory/recall.py       (🔴 废弃)
CC 无                            memory/context.py      (🟡 重写)
```

---

## 第七部分：验证清单

```bash
# Step 9 — 确认 memory_read 在 _RealTools 中可解析
grep -r "MemoryReadTool\|memory_read" entry/bootstrap/registry_factory.py

# Step 10 — 全量回归
python -m pytest tests/runtime_core/ tests/composition/ tests/integration/ -q

# 确认 Legacy memory 测试仍通过（memory/ 改动不影响旧路径）
python -m pytest tests/test_memory_recall.py tests/test_memory_runtime_integration.py -q
```
