# Native 上下文 & Memory —— CC 对齐度审计 + 接入方案（v3）

> 日期：2026-08-04
> 当前基线：Phase 0-10 完成
> v3：用户纠正 CC 记忆系统理解，重写审计 + 方案

---

## 第一部分：CC 记忆系统——正确理解

### 1.1 四种类型

| 类型 | 用途 | 示例 |
|---|---|---|
| `user` | 用户画像、角色偏好、知识水平 | "用户偏好 RESTful API 设计" |
| `feedback` | 该做什么、不该做什么 | "集成测试必须用真实数据库" |
| `project` | 项目动态、截止日期、协作信息 | "Q3 目标：完成权限系统迁移" |
| `reference` | 外部指针，哪里能找到什么信息 | "认证文档在 wiki.internal.com/auth" |

### 1.2 存储格式

每条记忆是独立的 `.md` 文件，YAML frontmatter：

```markdown
---
name: no-mock-database
description: 集成测试必须使用真实数据库，不能用 mock
type: feedback
---

在集成测试中，必须启动真实数据库实例（通过 Testcontainers），
不允许使用 Mock 或 H2 内存库。Mock 不能验证 SQL 方言差异、
迁移脚本、和连接池行为。
```

### 1.3 MEMORY.md 索引

一个 ≤200 行 / 25KB 的轻量目录文件，聚合所有记忆的 YAML 头部：

```markdown
# Project Memory Index

## user
- `api-design-preference`: 用户偏好 RESTful API，不喜欢 GraphQL

## feedback  
- `no-mock-database`: 集成测试必须使用真实数据库，不能用 mock
- `log-all-errors`: 所有异常必须打到 structured log，不吞

## project
- `q3-permission-migration`: Q3 目标完成权限系统迁移，截止 9/30

## reference
- `auth-wiki`: 认证文档在 wiki.internal.com/auth
```

### 1.4 检索与注入流程

```
会话启动
  → 系统加载 MEMORY.md → 注入 system prompt
  → LLM 看到完整的记忆目录（name + description + type）
  → LLM 自主决定是否需要读取某个记忆的完整内容
  → 如果需要：调用 memory_read("name") 工具
  → 系统读取该 .md 文件 → 返回完整内容作为 tool_result
  → LLM 将内容纳入上下文用于决策

压缩后
  → MEMORY.md 从磁盘重新注入
  → 已读取的记忆内容可能已被裁剪 → LLM 可重新调用 memory_read
```

**关键**：检索者是 LLM，不是系统算法。系统只提供目录，LLM 决定读什么。

### 1.5 触发时机

- 会话启动：注入 MEMORY.md 索引
- 压缩后：重新注入 MEMORY.md 索引
- 对话中：LLM 自主调用 `memory_read` / `memory_write` 工具

---

## 第二部分：SQLite 能达到同样效果吗？

### 2.1 直接回答：能

CC 模式的三个操作在 SQLite 上有完全相同物：

| CC 操作 | SQLite 等价 | 说明 |
|---|---|---|
| 列举 | `SELECT name, description, type FROM memory_entries WHERE status='active'` | SQL 查询——更快、更可靠 |
| 选择 | LLM 从清单中选 → `["name1", "name2"]` | 完全相同的 LLM 调用 |
| 加载 | `SELECT content FROM memory_entries WHERE name IN (...)` | SQL 查询——比文件 I/O 更快 |

SQLite 不给 CC 模式增加任何阻力。反而在并发、事务、查询上更优。

### 2.2 但需要改变的是什么

不是存储介质（SQLite vs 文件系统），是**检索模式**：

| 维度 | 当前实现 | CC 模式 | 怎么改 |
|---|---|---|---|
| 谁来检索 | 系统算法（4 路 recall + 评分公式） | **LLM**（看到目录后自主选择） | 废弃 `memory/recall.py` 的算法评分 |
| 谁来注入 | 系统自动拼装 `[MEMORY]` section | **LLM** 调 `memory_read` 工具 | 废弃 `build_memory_section()` |
| 注入频率 | 每轮 | 会话启动一次 + 压缩后 + 按需 | 降频 |
| 用户可见性 | SQLite 黑盒 | 文件系统透明 | 加 `GRACE.md` 导出（后续） |

### 2.3 结论

**用 SQLite 实现 CC 的 "LLM 选择" 模式——不改存储介质，改检索模式。** 具体做法：

1. `memory/recall.py` → 废弃（算法评分不再需要）
2. `memory/context.py` → 简化为 "列举记忆目录 → 注入 system prompt"（不做自动选择）
3. 确保 `memory_read` / `memory_write` 工具在 native 路径可用
4. MEMORY.md 索引风格 → 从 SQLite 动态生成，注入 system prompt

---

## 第三部分：五层上下文窗口管理——我们的对照

### Layer 1：超大工具结果存磁盘

**CC**：单次工具结果 > 阈值 → 写磁盘 → 上下文里只放 `[Tool output saved to /tmp/...]` 引用。

**Grace Code**：`ContextBudgetManager` 不做此操作。**🟡 缺失，但这是优化项——对功能正确性无影响。**

### Layer 2：移除远古消息

**CC**：窗口快满时，移除最早的 user/assistant 消息对。

**Grace Code**：`ContextBudgetManager.ensure_budget()` —— 180k token 限制，早期消息被占位符替代。**✅ 已对齐。**

### Layer 3：时间衰减裁剪工具结果

**CC**：旧的 tool_result 被裁剪（只保留前 N 字符）。

**Grace Code**：`ContextBudgetManager` 做了 tool_result 2000 字符截断。**✅ 已对齐。**

### Layer 4：读时投影，延迟压缩

**CC**：不主动压缩——只在下次需要空间时触发。

**Grace Code**：`ContextBudgetManager.ensure_budget()` 每轮 model call 前检查。**✅ 已对齐。**

### Layer 5：全量摘要 + 压缩后恢复

**CC**：LLM 为被移除的消息生成 summary → 替代原消息 → 重新注入 CLAUDE.md + MEMORY.md。

**Grace Code**：`ContextBudgetManager` 用 `[MESSAGE TRIMMED]` 占位符。**🟡 占位符 vs LLM summary——优化项。**

### 5 层总评

```
Layer 1: 🟡 缺失（优化项，不阻塞）
Layer 2: ✅
Layer 3: ✅
Layer 4: ✅
Layer 5: 🟡 优化项
```

---

## 第四部分：当前代码中需要废弃/重写的模块

### 废弃

| 模块 | 原因 | 替代 |
|---|---|---|
| `memory/recall.py` `MemoryRecallService` | 算法评分检索——CC 是 LLM 选择 | LLM 通过 `memory_read` 自主选择 |
| `memory/context.py` `build_memory_section()` | 自动拼装 `[MEMORY]` section——CC LLM 自己决定读什么 | 简化为 directory listing |
| `memory/context.py` `_build_precision_section()` | scope+confidence 自动过滤——CC 不做 | LLM 自己从目录中判断 |
| `memory/context.py` `_deterministic_score()` | 硬编码权重 0.45/0.25/0.20/0.10 | 不需要评分 |
| `memory/context.py` `_build_always_inject_section()` | 自动注入 user/feedback 类型 | LLM 从目录中选择 |
| `memory/recall.py` `memory_recalls` SQLite 表 | CC 无 recall tracking | 不需要 |

### 保留

| 模块 | 原因 |
|---|---|
| `memory/extractor.py` | 提取逻辑正确——LLM reflection → parse → discipline → write |
| `memory/store.py` | SQLite 存储——我们的底层存储 |
| `memory/models.py` | 数据模型——4 种 type、YAML frontmatter 等价字段 |
| `memory/chunker.py` | 不太需要——但保留 |
| `context/claude_md.py` | CLAUDE.md 加载——CC-aligned |

### 重写

| 模块 | 从什么 | 改为什么 |
|---|---|---|
| `memory/context.py` | 4 路自动 recall + 自动注入 | **目录列举 + 注入 system prompt**（LLM 看到目录后自主调用 `memory_read`） |
| `memory/recall.py` | 算法评分 + 自动选择 + 自动注入 | **废弃**——被 LLM 选择替代 |

---

## 第五部分：Native 路径接线方案（最终版）

### 5.1 注入内容

`_execute_native()` 构造的 conversation 顺序（CC 对齐）：

```
1. Primary system prompt     (efc1ce9 已有)
2. GRACE.md project rules    (context/claude_md.py)
3. MEMORY.md 风格目录         (从 SQLite 动态生成)
4. Session context            (plan context / 变更追踪)
5. Cross-turn history         (session_service.get_messages)
6. Current prompt             (user input)
```

### 5.2 Memory 目录生成

```python
def _build_memory_catalog(store) -> str:
    """从 SQLite 生成 CC MEMORY.md 风格的目录。
    
    CC 对齐：≤200 行 / 25KB 的轻量目录，注入 system prompt。
    LLM 看到后可以自主调用 memory_read 工具读取完整内容。
    """
    summaries = store.list_memories()
    active = [s for s in summaries if getattr(s, 'status', None) != 'deprecated']
    
    by_type: dict[str, list] = {"user": [], "feedback": [], "project": [], "reference": []}
    for s in active:
        t = str(getattr(s, 'type', 'project'))
        if t in by_type:
            by_type[t].append(s)
    
    lines = ["# Project Memory Index"]
    total = 0
    for type_name in ("user", "feedback", "project", "reference"):
        mems = by_type[type_name]
        if not mems:
            continue
        lines.append(f"\n## {type_name}")
        for m in mems[:15]:  # 每类最多 15 条
            desc = getattr(m, 'description', '') or ''
            lines.append(f"- `{m.name}`: {desc[:120]}")
        total += len(lines)
        if total >= 180:  # 接近 200 行上限
            break
    
    content = "\n".join(lines)
    if len(content) > 25_000:
        content = content[:25_000] + "\n... [truncated]"
    return content
```

### 5.3 注入到 `_execute_native()`

```python
# chat_pipeline.py _execute_native()

msgs = []

# 1. Primary system prompt
if _def and _def.system_prompt:
    msgs.append({"role": "system", "content": _def.system_prompt})

# 2. GRACE.md project rules (CC-aligned)
from context.claude_md import load as load_project_instructions
_project_rules = load_project_instructions(self._ports.repo_path)
if _project_rules:
    msgs.append({"role": "system", "content": _project_rules})

# 3. Memory catalog (CC MEMORY.md style, Grace Code extension)
if hasattr(self._ports, 'memory_context'):
    _catalog = _build_memory_catalog(self._ports.memory_context.store)
    if _catalog:
        msgs.append({"role": "system", "content": _catalog})

# 4. Session context
if prepared.session_context_text:
    msgs.append({"role": "user", "content": prepared.session_context_text})

# 5. History + prompt
msgs.extend(session_service.get_messages(session_id, limit=50))
msgs.append({"role": "user", "content": prompt})
```

### 5.4 记忆提取（post-run）

保留 `memory/extractor.py`——提取逻辑正确。post-run 调用 `write_success_memories()`。

### 5.5 需要确认的工具

LLM 要能调用 `memory_read` / `memory_write` / `memory_search` 工具——这些是否已注册到 `_RealTools`？

<待确认：`memory_read` 等工具是否在 native 路径可用>

### 5.6 `memory_read` 在 native 路径的可用性

`build_registry()` 注册了 `memory_read` 等工具——通过 `tool_registry` 参数传入 `assemble()`。`_RealTools` 做了 `tool_registry` → `execute()` 映射。所以 LLM **可以**调用 `memory_read`。

流程是：
```
LLM 看到 MEMORY.md 目录 → 决定读 "no-mock-database"
  → 调用 memory_read("no-mock-database")
  → _RealTools.execute("memory_read", {"name": "no-mock-database"})
  → MemoryStore.read_memory("no-mock-database") → 返回完整内容
  → tool_result 注入 conversation
```

---

## 第六部分：实现步骤

| Step | 内容 | 改动 | 行数 |
|---|---|---|---|
| 1 | 项目指令注入 | `_execute_native()` + `context/claude_md.load()` | +4 |
| 2 | `ChatPipelinePorts` + `agent_service` 接线 | +`memory_context` 字段 + 传入 | +3 |
| 3 | Memory catalog 生成 | 新函数 `_build_memory_catalog()` | +30 |
| 4 | Memory catalog 注入 + session context | `_execute_native()` 完整 conversation 构造 | +15 |
| 5 | Memory 提取（post-run） | `_execute_native()` finalize 后 | +10 |
| 6 | 废弃标注 | `memory/recall.py` + `memory/context.py` docstring | +10 |
| 7 | 全量回归 | — | — |

### 文件范围

| 文件 | 改动 |
|---|---|
| `server/services/chat_pipeline.py` | `ChatPipelinePorts` + `_execute_native()` + `_build_memory_catalog()` |
| `server/services/agent_service.py` | `run_chat_async()` 传 `memory_context` |
| `memory/recall.py` | 模块 docstring 标注废弃 |
| `memory/context.py` | 模块 docstring 标注废弃（`build_memory_section` → 待删除） |

### 关于 `memory_read` 工具

如果 `memory_read` 不在 native 路径可用，需要作为独立 Step 注册到 `_RealTools`。先确认后决定是否需要此步。

---

确认后逐步执行？
