# MemoryView 前端展示缺口

## 数据流总览

```
后端 GET /api/memory → { items, overview }
  items[].name, description, type, status, scope, confidence, access_count, updated_at

后端 GET /api/memory/{name} → { ... + content, source, source_session_id, anchors }
```

前端 `MemoryItem` 类型定义了 15 个字段，后端只返回 8 个。

---

## 缺口 1: 目录列表显示不存在的字段 🔴

**文件**: `MemoryView.tsx:258`

```tsx
<span>{item.layer}</span>
```

`layer` 字段后端不返回。每个记忆行都会显示 "undefined"。

**同一行其他字段**: `scope` ✅ 有、`access_count` ✅ 有。

**修复**: 删掉 `item.layer` 的渲染，或者后端在 overview 中返回它。

---

## 缺口 2: 搜索使用不存在的字段 🔴

**文件**: `MemoryView.tsx:119`

```tsx
const text = `${item.name} ${item.description} ${item.preview ?? ""}`.toLowerCase();
```

`preview` 字段后端不返回。搜索条件始终包含空字符串，不影响结果但不准确。

**修复**: 改为 `${item.description}` 搜索，或者后端返回 preview 字段。

---

## 缺口 3: Markdown 内容未渲染 🟡

**文件**: `MemoryView.tsx:302-305`

```tsx
<div className="memory-preview-body" style={{ whiteSpace: "pre-wrap", fontFamily: "var(--font-mono)", fontSize: 13 }}>
  {selectedDetail?.content ? (selectedDetail.content as string) : selected.preview || "Loading..."}
</div>
```

记忆正文是 Markdown 格式，但当前按纯文本 `pre-wrap` 显示。标题、列表、代码块全部是原始文本。

**修复**: 使用 Markdown 渲染库（如 `react-markdown`）或自定义轻量渲染。

---

## 缺口 4: 锚点未展示 🟡

**文件**: `server/routers/memory.py` 详情端点返回 `anchors`，但前端 `MemoryView.tsx` 从未读取。

```json
{
  "anchors": [
    { "kind": "file", "path": "package.json", "content_hash": "sha256..." }
  ]
}
```

后端完整返回，前端不展示。用户看不到记忆关联了哪些文件。

**修复**: 在详情面板加 "Anchors" 区块，显示每个 anchor 的 kind、path、content_hash。

---

## 缺口 5: 来源信息未展示 🟡

后端 `GET /api/memory/{name}` 返回 `source` 和 `source_session_id`，但前端不展示。

用户可以知道"这条记忆是 Web API 创建的，来源 session 是 abc123"，但目前看不到这些信息。

---

## 缺口 6: 缺少创建时间 🟡

详情面板只显示 "Updated"（updated_at），没有 "Created"（created_at）。后端 entry 表有 `created_at` 字段，但 `MemoryStore.read_memory()` 返回的 `Memory` 对象没有 `created_at` 属性（Memory 模型只有 `updated_at`）。

**需要后端修复**: `Memory` 数据类需要加 `created_at` 字段。

---

## 缺口 7: 缺少新建入口 🟢

目前只能通过 API（`curl -X POST`）或 LLM 工具（`memory_write`）创建记忆。前端没有"新建记忆"按钮。

---

## 缺口 8: 缺少编辑功能 🟢

有 Delete 按钮但没有 Edit 按钮。用户不能修改记忆的描述或内容。

**后端已有**: `PATCH /api/memory/{name}` 端点就绪。

**前端缺少**: 编辑弹窗或行内编辑。

---

## 缺口 9: 缺少刷新按钮 🟢

`useEffect` 只在组件挂载时加载一次。如果后端记忆有变化（其他 session 或 LLM 工具创建了记忆），前端不会自动更新。

---

## 优先级建议

| 缺口 | 严重性 | 影响 | 工作量 |
|------|--------|------|--------|
| 1. layer 字段显示 "undefined" | 🔴 | 每行都显示错误值 | 1 行 |
| 2. 搜索用了 preview 字段 | 🔴 | 搜索范围不准 | 1 行 |
| 3. Markdown 未渲染 | 🟡 | 内容不可读 | 1 组件 |
| 4. 锚点未展示 | 🟡 | 用户看不到文件关联 | 1 区块 |
| 5. 来源信息未展示 | 🟡 | 缺少上下文 | 1 区块 |
| 6. 缺少创建时间 | 🟡 | 信息不全 | 2 文件 |
| 7-9. 新建/编辑/刷新 | 🟢 | 功能性增强 | 各半天 |
