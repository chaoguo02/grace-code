# Memory 系统深度分析

> 对照 Mem0 架构 | 2026-07-23

## 1. 当前架构

### 1.1 四种类型的定义和注入策略

| Type | 含义 | 注入策略 | 示例 |
|---|---|---|---|
| `user` | 用户偏好、习惯、指令 | **Always inject**（全量正文） | "用户喜欢用 TypeScript 而不是 JavaScript" |
| `feedback` | 代码审查发现、错误模式 | **Always inject**（全量正文） | "auth.py 第 42 行有 SQL 注入风险" |
| `project` | 项目知识、架构决策 | **Precision**（top-5，按 confidence 排序） | "这个项目使用 FastAPI + SQLite" |
| `reference` | 文档引用、外部资源 | **On-demand**（LLM 手动调用 memory_read） | "PostgreSQL 官方文档链接" |

```python
# memory/models.py:49-51
ALWAYS_INJECT_TYPES = frozenset({MemoryType.USER, MemoryType.FEEDBACK})
ON_DEMAND_TYPES   = frozenset({MemoryType.PROJECT, MemoryType.REFERENCE})
GLOBAL_TYPES      = frozenset({MemoryType.USER, MemoryType.FEEDBACK})
```

### 1.2 注入流程

```
ReActAgent 每轮对话
  → _build_long_term_context()
    → injection_service.build_injection_context()
      → 1. user/feedback: 全量正文注入 prompt
      → 2. project scope: confidence DESC, top-5
      → 3. global scope: confidence DESC, top-5
      → 4. 索引列表: 所有记忆名称 → LLM 可按需 memory_read
```

### 1.3 存储架构

```
MemoryStore (门面)
  ├── SqliteMemoryBackend (主存储, db_path 不为空时)
  │   ├── memory_entries 表
  │   └── memory_anchors 表
  └── FileMemoryBackend (legacy, markdown 文件)
      ├── ~/.grace/projects/{hash}/memory/*.md
      └── MEMORY.md 索引
```

### 1.4 现有元数据

| 字段 | 类型 | 用途 |
|---|---|---|
| `confidence` | 0.0–1.0 | 提取可信度 |
| `ttl_seconds` | int|null | 生存时间（**定义了但未执行**） |
| `expires_at` | ISO string | 计算好的过期时间（**定义了但未执行**） |
| `access_count` | int | 读取次数 |
| `anchors` | Anchor[] | 文件/符号关联 |
| `scope` | session/project/global | 可见范围 |
| `status` | active/deprecated | 生命周期 |

---

## 2. 发现的问题

### 🔴 P0 — 功能缺失

#### 2.1 decay_confidences() 从未被调用

`memory/sqlite_backend.py:238` 实现了完整的 confidence 衰退逻辑：

```sql
UPDATE memory_entries SET confidence = MAX(0.1, confidence * 0.9)
WHERE access_count < 3
AND updated_at < datetime('now', '-90 days')
AND status='active';

UPDATE memory_entries SET status='deprecated'
WHERE confidence < 0.2 AND status='active';
```

但 `agent_service.py` 从未调用它。**记忆不会自动衰退或过期**。

#### 2.2 TTL 字段只是存储，不执行

`MemoryMetadata.ttl_seconds` 和 `expires_at` 字段被写入 SQLite，但没有任何代码检查 `expires_at` 并自动 `deprecated` 过期记忆。`get_stats()` 只统计 "即将到期" 的数量，不执行清理。

#### 2.3 记忆合并/去重缺失

Mem0 的 ADD-only 模式和 Entity Linking 对比，我们的系统：
- **无去重**：同一条信息可能被多次写入（略微不同的 name）
- **无冲突解决**：新旧矛盾的信息同时存在
- **无实体链接**：跨记忆关系不可检索

### 🟡 P1 — 设计未收口

#### 2.4 类型语义未充分利用

| Type | 应该的行为 | 实际行为 |
|---|---|---|
| `user` | 跨项目全局生效 | 默认 scope=project，只在当前项目注入 |
| `feedback` | 绑定到具体文件和行号，文件修改后自动 deprecated | anchor 定义了但从不校验 content_hash |
| `project` | 项目级知识，confidence 驱动注入 | 按 scope 过滤，未区分 project vs reference |
| `reference` | 纯文档，永不自动注入 | 行为正确但从未被使用 |

#### 2.5 Scope 未被 MemoryView 利用

前端 MemoryView 展示 scope 信息，但不支持按 scope 过滤（session/project/global）。用户看不到哪些记忆是全局的、哪些是项目级的。

#### 2.6 Anchor content_hash 校验缺失

`Anchor.content_hash` 设计为 "文件修改后自动废弃记忆"，但没有任何代码在注入时检查 hash 是否匹配当前文件。

---

## 3. 对照 Mem0 的差距

| 维度 | Mem0 | 我们 | 差距 |
|---|---|---|---|
| **写入模式** | ADD-only，不 UPDATE/DELETE | RunFinalizer 可覆盖 | 简单但不防重复 |
| **检索** | 混合搜索（语义+BM25+实体） | scope + confidence 排序 | 无语义搜索 |
| **实体链接** | 跨记忆实体图谱 | 无 | 记忆间无关联 |
| **时间推理** | 7 种时间查询模式 | 无 | 无法区分 "当前状态" vs "历史事件" |
| **衰退** | 时间加权 + 自动清理 | **定义了但从未执行** | 🔴 |
| **冲突解决** | 新状态使旧状态自动过时 | 无 | 矛盾记忆并存 |
| **维护** | 后台异步维护任务 | Web 模式无维护 | 🔴 |

---

## 4. 修复计划

### 批次 1：让记忆"活起来"（P0）

1. **启动记忆维护任务** — `agent_service.py` 增加后台线程，周期性调用 `decay_confidences()`
2. **TTL 过期检查** — 维护任务同时检查 `expires_at`，自动 deprecated 过期记忆
3. **Anchor hash 校验** — 注入时检查 `content_hash`，文件变更 → 自动 deprecated
4. **Scope 过滤** — MemoryView 增加 scope filter（session/project/global）

### 批次 2：类型充分利用（P1）

5. **User 类型 → 全局 scope** — user 偏好写入时自动 scope=global，跨项目注入
6. **Feedback 类型 → 绑定 anchor** — 修复建议绑定文件+行号，验证 hash
7. **Project 类型 → confidence 驱动** — project 记忆默认 confidence=0.5，LLM 确认后提高到 0.9
8. **Reference 类型 → 仅索引** — 不注入正文，LLM 按需查询

### 批次 3：对标 Mem0（长期）

9. **ADD-only 写入** — 同名记忆不覆盖，追加为新版本，旧版自动 deprecated
10. **简易语义搜索** — 利用已有的 ExternalMemoryStore + fastembed
11. **记忆关联** — 写记忆时自动链接相关记忆（同名 anchor、同文件）
