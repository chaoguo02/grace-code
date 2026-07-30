# Edit Diff 左右对比视图实施方案

> 目标: Edit 工具执行后，前端展示 side-by-side diff，左边旧代码、右边新代码，格式对齐 `git diff`

---

## 1. 数据流设计

```
FileEditTool.execute()
  │
  ├─ old_str, new_str, 文件原始内容, 修改后内容, 起始行号
  │
  ▼
_generate_diff_hunks(old_content, new_content, old_str, start_line)
  │
  ├─ Python difflib.SequenceMatcher 逐行对比
  ├─ 取 edit 位置前后各 3-5 行上下文
  ├─ 产生 structured hunks
  │
  ▼
ToolResult.metadata["diff"] = { hunks, file_path, additions, deletions }
  │
  ▼
ToolExecutionPipeline → execute_tool() → ToolResult
  │
  ▼
agent loop → observation Event → EventBus → WebSocket
  │
  ▼
前端 chatStore.handleWsEvent → observation → ContentBlock
  │
  ▼
DiffView 组件渲染
```

## 2. 后端: `FileEditTool` 生成 diff

### 2.1 新增方法: `_build_diff_hunks()`

```python
# tools/file_edit_tool.py

import difflib

def _build_diff_hunks(
    old_content: str,
    new_content: str,
    old_str: str,
    match_pos: int,       # old_str 在 old_content 中的字节偏移
    start_line: int,      # 1-based
    context_lines: int = 3,
) -> dict:
    """Produce structured hunks for a side-by-side diff view.

    Returns:
        {
            "file_path": str,
            "hunks": [
                {
                    "header": "@@ -40,3 +40,5 @@",
                    "old_start": 40, "new_start": 40,
                    "lines": [
                        {"type": "context", "content": "    unchanged", "old_ln": 40, "new_ln": 40},
                        {"type": "delete",  "content": "    old_code",  "old_ln": 41},
                        {"type": "insert",  "content": "    new_code",  "new_ln": 41},
                        {"type": "context", "content": "    unchanged", "old_ln": 42, "new_ln": 42},
                    ]
                }
            ],
            "additions": 5,       # total lines added
            "deletions": 2,       # total lines deleted
        }
    """
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    # Find the line range of the edit in old content
    prefix = old_content[:match_pos]
    old_start_line = prefix.count("\n") + 1
    old_end_line = old_start_line + old_str.count("\n")

    # Find the corresponding range in new content
    new_start_line = old_start_line
    new_end_line = new_start_line + old_str.count("\n")

    # Compute context window (with bounds checking)
    ctx_start = max(0, old_start_line - 1 - context_lines)
    ctx_end_old = min(len(old_lines), old_end_line + context_lines)
    ctx_end_new = min(len(new_lines), new_end_line + context_lines)

    # Slice context
    old_slice = old_lines[ctx_start:ctx_end_old]
    new_slice = new_lines[ctx_start:ctx_end_new]

    # Use SequenceMatcher for line-level diff
    matcher = difflib.SequenceMatcher(None, old_slice, new_slice)
    hunks = []
    current_hunk = {"old_start": ctx_start + 1, "new_start": ctx_start + 1, "lines": []}
    additions = deletions = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                current_hunk["lines"].append({
                    "type": "context",
                    "content": old_slice[k].rstrip("\n"),
                    "old_ln": ctx_start + k + 1,
                    "new_ln": ctx_start + k + 1,
                })
        elif tag == "delete":
            deletions += i2 - i1
            for k in range(i1, i2):
                current_hunk["lines"].append({
                    "type": "delete",
                    "content": old_slice[k].rstrip("\n"),
                    "old_ln": ctx_start + k + 1,
                })
        elif tag == "insert":
            additions += j2 - j1
            for k in range(j1, j2):
                current_hunk["lines"].append({
                    "type": "insert",
                    "content": new_slice[k].rstrip("\n"),
                    "new_ln": ctx_start + k + 1,
                })
        elif tag == "replace":
            deletions += i2 - i1
            for k in range(i1, i2):
                current_hunk["lines"].append({
                    "type": "delete",
                    "content": old_slice[k].rstrip("\n"),
                    "old_ln": ctx_start + k + 1,
                })
            additions += j2 - j1
            for k in range(j1, j2):
                current_hunk["lines"].append({
                    "type": "insert",
                    "content": new_slice[k].rstrip("\n"),
                    "new_ln": ctx_start + k + 1,
                })

    hunks.append(current_hunk)
    return {"hunks": hunks, "additions": additions, "deletions": deletions}
```

### 2.2 修改 `execute()` 返回值

在成功分支 (Case 4) 的 return 前，构建 diff 并塞入 metadata:

```python
# Line ~275, before the return ToolResult(success=True, ...)
diff_data = _build_diff_hunks(content, new_content, old_str, match_pos, start_line)
diff_data["file_path"] = str(path)

return ToolResult(
    success=True,
    output=f"Edited {path} (line {start_line}){delta_str}",
    metadata={
        "diff": diff_data,
        "additions": diff_data["additions"],
        "deletions": diff_data["deletions"],
    },
)
```

### 2.3 确保 diff 数据透传到前端

`ToolExecutionPipeline` 的 `execute()` 返回的 `ToolResult` 已经包含 `metadata`。Agent loop 在构造 observation 事件时，`payload.observation.metadata` 会包含 `diff`。无需额外改动 pipeline。

---

## 3. 前端: `DiffView` 组件

### 3.1 组件结构

```
src/components/DiffView.tsx
  ┌─────────────────────────────────────────────┐
  │  📄 src/foo.py    +5 -2    [collapse ▲]    │  ← Header
  ├──────────────────┬──────────────────────────┤
  │  old             │  new                     │  ← Side-by-side panels
  │                  │                          │
  │  40  def foo():  │  40  def foo():          │  ← context (gray)
  │  41  - old_code  │      + new_code          │  ← delete (red) / insert (green)
  │  42    return x  │  41    return y          │  ← context (gray)
  │                  │  42  + added_line        │  ← insert (green)
  └──────────────────┴──────────────────────────┘

Styles:
  .diff-delete: background #fce4e4, border-left 3px #c62828
  .diff-insert: background #e6f4ea, border-left 3px #1a7f3f
  .diff-context: no background
  .diff-line-num: color #888, min-width 40px, text-align right, user-select none
```

### 3.2 数据提取

在 `chatStore.ts` 或 `ChatView.tsx` 中，从 observation 事件提取 diff:

```typescript
// In handleWsEvent or blocks rendering:
if (ev.type === "observation" && ev.metadata?.diff) {
  const diff = ev.metadata.diff as FileEditDiff;
  // Render DiffView component
}
```

### 3.3 DiffView 组件 (React/TypeScript)

```tsx
// web/src/components/DiffView.tsx

interface DiffLine {
  type: "context" | "delete" | "insert";
  content: string;
  old_ln?: number;
  new_ln?: number;
}

interface DiffHunk {
  old_start: number;
  new_start: number;
  lines: DiffLine[];
}

interface FileEditDiff {
  file_path: string;
  hunks: DiffHunk[];
  additions: number;
  deletions: number;
}

function DiffView({ diff, collapsed, onToggle }: Props) {
  if (collapsed) {
    return (
      <div className="diff-collapsed" onClick={onToggle}>
        📄 {diff.file_path} +{diff.additions} -{diff.deletions} (click to expand)
      </div>
    );
  }

  return (
    <div className="diff-container">
      <div className="diff-header" onClick={onToggle}>
        <span>📄 {diff.file_path}</span>
        <span className="diff-stats">
          <span className="diff-additions">+{diff.additions}</span>
          <span className="diff-deletions">-{diff.deletions}</span>
        </span>
        <button className="diff-collapse-btn">▲ collapse</button>
      </div>
      {diff.hunks.map((hunk, hi) => (
        <div key={hi} className="diff-hunk">
          <div className="diff-hunk-header">
            @@ -{hunk.old_start} +{hunk.new_start} @@
          </div>
          <div className="diff-panels">
            {hunk.lines.map((line, li) => (
              <div key={li} className={`diff-row diff-${line.type}`}>
                <span className="diff-old-ln">{line.old_ln ?? ""}</span>
                <span className="diff-old-content">{line.type !== "insert" ? line.content : ""}</span>
                <span className="diff-new-ln">{line.new_ln ?? ""}</span>
                <span className="diff-new-content">{line.type !== "delete" ? line.content : ""}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
```

### 3.4 CSS 关键样式

```css
/* web/src/styles.css */

.diff-container {
  border: 1px solid var(--border);
  border-radius: 8px;
  margin: 8px 0;
  overflow: hidden;
  font-family: "Cascadia Code", "Fira Code", monospace;
  font-size: 12px;
  line-height: 1.5;
}

.diff-header {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 12px;
  background: var(--bg-strong);
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  user-select: none;
}

.diff-stats { font-weight: 700; }
.diff-additions { color: #1a7f3f; margin-right: 4px; }
.diff-deletions { color: #c62828; }

.diff-hunk-header {
  padding: 2px 12px;
  background: #f0f0f0;
  color: #666;
  font-size: 11px;
}

.diff-panels {
  display: grid;
  grid-template-columns: 40px 1fr 40px 1fr;  /* old-ln | old-content | new-ln | new-content */
}

.diff-row {
  display: contents;  /* each row spans the grid columns */
}

.diff-delete {
  background: #fce4e4;
  .diff-old-content { background: #f9cccc; }
}

.diff-insert {
  background: #e6f4ea;
  .diff-new-content { background: #c8e6c9; }
}

.diff-old-ln, .diff-new-ln {
  color: #999;
  text-align: right;
  padding-right: 8px;
  user-select: none;
  min-width: 0;
}

.diff-old-content, .diff-new-content {
  white-space: pre;
  overflow-x: auto;
  padding: 0 4px;
}
```

### 3.5 挂载点

在 `ChatView.tsx` 的 `BlocksMessage` 渲染中，当渲染 observation block 时检测 diff:

```tsx
// In the observation rendering section:
{block.type === "observation" && block.metadata?.diff && (
  <DiffView diff={block.metadata.diff} collapsed={true} onToggle={...} />
)}
```

默认折叠（节省空间），点击展开查看完整 diff。

---

## 4. 实现步骤

| 步 | 文件 | 内容 | 预估 |
|:--:|------|------|:--:|
| 1 | `tools/file_edit_tool.py` | 添加 `_build_diff_hunks()` + 修改 `execute()` 返回值携带 diff | 30 行 |
| 2 | `web/src/components/DiffView.tsx` | 新建组件 | 80 行 |
| 3 | `web/src/styles.css` | 添加 diff 样式 | 60 行 |
| 4 | `web/src/components/ChatView.tsx` (或 BlocksMessage) | 挂载 DiffView | 5 行 |
| 5 | `web/src/stores/chatStore.ts` | 确保 observation metadata 进入 ContentBlock | 检查/微调 |
| 6 | 测试 | 后端 diff 生成 + 前端渲染 | — |

---

## 5. 设计决策

### 为什么用 difflib 而不是 git diff？

- `git diff` 需要文件在磁盘上（workspace），但 Edit 工具的内存中已有 old/new content
- `difflib` 是 Python 标准库，零依赖，直接对比两个字符串
- `difflib.SequenceMatcher.get_opcodes()` 产生 `(tag, i1, i2, j1, j2)` 元组，天然适合行级 diff

### 为什么默认折叠？

- Edit 的输出可能很大（几百行），占据聊天空间
- 用户只需快速确认改了什么，不需要看每一行
- 类似 Claude Code 的行为：显示 `+N -M` 摘要，点击展开

### 为什么用 CSS Grid 而不是 table？

- Grid 天然支持 side-by-side 布局，4 列精确控制
- `display: contents` 让每行横跨 4 列，保持对齐
- table 在高亮删除/插入行时会有跨列问题（delete 行右侧为空）

### 只做 Edit，不做 Write？

- Write 是全文件替换，diff 就是整个文件，意义不大
- Edit 是小范围精确替换，diff 展示修改点，信息密度高
- 可以后续扩展到 Write（显示全文件前后对比），但首期聚焦 Edit

---

## 6. 风险

| 风险 | 缓解 |
|------|------|
| diff metadata 增大 WebSocket 消息 | diff 只包含 context ±3 行，典型 < 2KB |
| 大文件编辑时 old_content 内存占用 | old_content 已在 `execute()` 中全部读取，切片后 diff 只引用相关行 |
| 前端 Grid + monospace 渲染性能 | 默认折叠；展开时 hunks 通常 < 20 行 |
| diff 内容可能包含敏感代码 | 与现有 observation 输出同通道，不引入新的泄露面 |
