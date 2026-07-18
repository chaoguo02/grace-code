# Memory — 长期记忆 API 设计

## 概述

Memory 模块管理 agent 的跨会话长期记忆。每条记忆是一个带 YAML frontmatter 的 Markdown 文件，存储在 `~/.grace/projects/<project-hash>/memory/`。

当前 memory 只能通过 LLM 内部工具（`memory_read`/`memory_write`/`memory_list`/`memory_delete`）访问，用户无法直观看到 agent "记得什么"。本文档定义一组 REST API 将 memory 暴露给前端。

## 数据模型

### 核心结构

```python
@dataclass
class Memory:
    name: str               # slug，也是文件名（如 "build-commands"）
    description: str         # 一行摘要
    content: str             # Markdown 正文
    metadata: MemoryMetadata
    updated_at: str          # ISO-8601
    anchors: list[Anchor]   # 关联的文件/符号/任务类型

@dataclass
class MemoryMetadata:
    type: MemoryType        # "user" | "feedback" | "project" | "reference"
    status: MemoryStatus    # "active" | "deprecated"
    scope: MemoryScope      # "session" | "project" | "global"
    confidence: float       # 0.0~1.0
    ttl_seconds: int | None
    expires_at: str
    access_count: int
    validated_at: str

@dataclass
class Anchor:
    kind: str              # "file" | "symbol" | "task"
    path: str | None       # 文件路径（file 类型）
    name: str | None       # 符号名（symbol 类型）
    value: str | None      # 任务类型（task 类型）
    content_hash: str      # 文件 SHA256，用于"代码即真理"校验
```

### 类型说明

| MemoryType | 说明 | 注入策略 | 作用域 |
|-----------|------|---------|--------|
| `user` | 用户身份、偏好、专长 | 始终注入 | global |
| `feedback` | 纠正、已确认的规则 | 始终注入 | global |
| `project` | 架构决策、构建命令 | 按需注入 | project |
| `reference` | 外部系统/文档指针 | 按需注入 | project |

### 存储结构

```
~/.grace/projects/<project-hash>/memory/
├── MEMORY.md                # 索引文件（启动时注入前 200 行）
├── build-commands.md        # 主题文件
├── debugging.md
└── archive/                 # 已废弃记忆
```

每条 memory 对应一个 `.md` 文件：

```markdown
---
name: build-commands
description: Build, test, and lint commands
metadata:
  type: project
  status: active
  confidence: 0.9
---

## Build
npm run build

## Test
npm test
```

---

## 接口列表

### 1. `GET /api/memory` — 记忆列表

列出所有记忆，支持过滤和分页。

**Query Parameters:**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `type` | string | — | 过滤：`user` / `feedback` / `project` / `reference` |
| `status` | string | `active` | 过滤：`active` / `deprecated` |
| `scope` | string | — | 过滤：`session` / `project` / `global` |
| `limit` | int | 50 | 最大返回数 |
| `offset` | int | 0 | 分页偏移 |

**Response 200:**

```json
{
  "memories": [
    {
      "name": "build-commands",
      "description": "Build, test, and lint commands",
      "type": "project",
      "status": "active",
      "scope": "project",
      "confidence": 0.9,
      "access_count": 12,
      "updated_at": "2026-07-18T12:00:00+00:00"
    }
  ],
  "total": 8,
  "by_type": {
    "user": 1,
    "feedback": 2,
    "project": 4,
    "reference": 1
  }
}
```

**实现：** `MemoryStore.list_memories()` 返回 `list[MemorySummary]`，`count_by_type()` 返回各类别统计。

---

### 2. `GET /api/memory/{name}` — 记忆详情

返回单条记忆的完整内容。

**Response 200:**

```json
{
  "name": "build-commands",
  "description": "Build, test, and lint commands",
  "content": "## Build\nnpm run build\n\n## Test\nnpm test\n",
  "type": "project",
  "status": "active",
  "scope": "project",
  "confidence": 0.9,
  "access_count": 12,
  "updated_at": "2026-07-18T12:00:00+00:00",
  "anchors": [
    { "kind": "file", "path": "package.json", "content_hash": "sha256..." }
  ]
}
```

**Error:** 404 — 记忆不存在。

**实现：** `MemoryStore.read_memory(name)` 返回 `Memory | None`。

---

### 3. `POST /api/memory` — 新建记忆

**Request Body:**

```json
{
  "name": "deploy-commands",
  "description": "Deployment commands and procedures",
  "content": "## Deploy\nnpm run deploy\n",
  "type": "project",
  "confidence": 0.8,
  "anchors": [
    { "kind": "file", "path": "deploy.sh" }
  ]
}
```

必填：`name`, `description`, `content`。`type` 默认 `project`。

**Response 201:**

```json
{
  "name": "deploy-commands",
  "status": "created"
}
```

**Error:** 409 — 同名记忆已存在。

**实现：** `MemoryStore.write_memory(memory, source="web_api")`。

---

### 4. `PATCH /api/memory/{name}` — 更新记忆

**Request Body:**

```json
{
  "description": "Updated description",
  "content": "## Build\nnpm run build -- --prod\n",
  "confidence": 0.95,
  "status": "active"
}
```

所有字段可选，只更新提供的字段。

**Response 200:**

```json
{
  "name": "build-commands",
  "status": "updated"
}
```

**Error:** 404 — 记忆不存在。

**实现：** 先 `read_memory()`，合并字段后 `write_memory()`。

---

### 5. `DELETE /api/memory/{name}` — 删除记忆

**Response 200:**

```json
{
  "name": "build-commands",
  "deleted": true
}
```

**Error:** 404 — 记忆不存在。

**实现：** `MemoryStore.delete_memory(name)`。

---

## 文件改动

| 文件 | 操作 | 说明 |
|------|------|------|
| `server/routers/memory.py` | **新建** | 5 个 REST 端点 |
| `server/schemas/memory.py` | **新建** | Pydantic 请求/响应模型 |
| `server/main.py` | 修改 | 注册 memory router |

## 验证

```bash
# 列表
curl -s http://127.0.0.1:8765/api/memory | python -m json.tool

# 详情
curl -s http://127.0.0.1:8765/api/memory/build-commands | python -m json.tool

# 新建
curl -s -X POST http://127.0.0.1:8765/api/memory \
  -H "Content-Type: application/json" \
  -d '{"name":"test-mem","description":"Test","content":"# Test"}' | python -m json.tool

# 更新
curl -s -X PATCH http://127.0.0.1:8765/api/memory/test-mem \
  -H "Content-Type: application/json" \
  -d '{"confidence":0.5}' | python -m json.tool

# 删除
curl -s -X DELETE http://127.0.0.1:8765/api/memory/test-mem | python -m json.tool
```
