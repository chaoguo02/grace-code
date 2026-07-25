# Grace Code Chat UI RFC — Agent IDE 体验升级蓝图

> 状态：**设计阶段** | 作者：guochao | 日期：2026-07-25
>
> 合并了 `chat-display-redesign.md` 和 `chat-composer-redesign.md` 两份方案，
> 并基于 Claude Code Desktop Redesign (2026.04) 和 TUI 源码分析进行了设计对齐。

---

## 前言：Claude Code 设计参考

在撰写本方案前，我们调研了 Claude Code 的 Terminal UI 和 2026 年 4 月发布的 Desktop Redesign。

### Claude Code TUI 核心组件

| 组件 | 位置 | 用途 |
|------|------|------|
| **PromptInput** | 底部 | 多行输入，历史导航，斜杠命令，Vim 模式 |
| **ModelPicker** | 浮动弹窗 | 模型选择 + Effort 等级 |
| **Mode 切换** | `Shift+Tab` 循环 | default → acceptEdits → plan → auto → bypass |
| **权限审批** | 行内（与工具渲染并列） | 每个工具渲染自己的权限 UI，不是独立 dock |
| **Conversation View** | 主区域 | 可滚动消息列表（User/Assistant/System/Tool） |

### Claude Code Desktop Redesign (2026.04) 新增

| 功能 | 设计 |
|------|------|
| **三种视图模式** | **Verbose**（全部工具调用）/ **Normal**（工具折叠摘要）/ **Summary**（只显示最终回答和变更） |
| **侧边栏** | 所有活跃 + 最近 session，按状态/项目/环境过滤，按项目分组，PR 合并后自动归档 |
| **拖拽面板** | Chat / Diff Viewer / Browser / Terminal / File Editor / Plan / Tasks / Subagent 自由组合 |
| **Side Chat** | `⌘+;` 分支对话，从主线程拉上下文但不回写 |
| **Usage 按钮** | 一眼看到上下文窗口 + 会话用量 |

### 对 Grace Code 设计的启示

1. **Verbose/Normal/Summary 三层** → 对应我们的 ContentBlock 折叠粒度（展开全部 / 默认折叠 / 只显示 text blocks）
2. **Mode 切换用快捷键而非 Tab** → 我们的 Mode Tab 是合理的 Web 映射，但应保留键盘快捷键
3. **权限审批与工具渲染并列** → 我们的 HITL 内联条设计方向正确，但应更靠近对应的工具调用而非固定在输入框上方
4. **Session 列表分组** → 我们的 Session 侧边栏应支持按 Mode 分组（Build/Plan/Explore）
5. **Usage 按钮** → 应在状态栏显示上下文窗口用量

---

## 目录

- [第一部分：消息展示重构](#一部分消息展示重构) — 解决"看"的问题
- [第二部分：输入与操作重构](#二部分输入与操作重构) — 解决"操作"的问题
- [第三部分：实施路线图](#三部分实施路线图)

---

# 第一部分：消息展示重构

## 1. 当前问题

### 1.1 流式内容与刷新后内容不一致
WS 事件（thought / tool_call / observation）和 DB 消息（assistant markdown）是两套数据源、两套渲染组件，输出的 UI 完全不同。用户刷新的瞬间看到的是另一个版本。

### 1.2 事件信息过载
每个工具调用渲染为独立大卡片。10 次调用 = 20 张卡片，把真正的回答挤到最下面。右侧 EventSidebar 再重复展示一遍。

---

## 2. 核心数据模型：ContentBlock

```typescript
type BlockId = string; // "b_{messageId}_{index}"

interface AssistantMessage {
  id: string;
  role: 'assistant';
  blocks: Array<
    | { type: 'text';    content: string }
    | { type: 'thought'; content: string; summary: string; phase: 'streaming' | 'completed' }
    | { type: 'tool_use'; id: BlockId; name: string; input: Record<string,unknown>;
        status: 'running'|'success'|'error'; output?: string; error?: string;
        groupedWith?: string[]; retryOf?: string; anchorTargets?: string[] }
  >;
  metadata: { steps: number; tokens: number; durationMs: number };
}
```

**ID 规范**：`b_{messageId}_{index}`，流式和 DB 阶段共享，保证组件在数据替换时不 remount。

---

## 3. 渲染策略

### 3.1 乐观渲染 + 最终一致性（P0）

流式中：`StreamingMessage` 直接 mutate blocks 数组。WS delta 映射为 blocks 操作。
DB 加载时：执行 `integrity_check`（block count + last block hash）。
- ✅ 一致 → 静默替换属性，不 remount
- ❌ 不一致 → 允许 remount（100% 正确 > 100% 无感）

### 3.2 流式→持久无感切换（P0 验收指标）

| 指标 | 目标值 | 测量方式 |
|------|--------|----------|
| 切换耗时 | < 16ms (1帧) | `performance.measure('silent-replace')` |
| 布局偏移 | CLS = 0 | Layout Shift API |
| 20步工具调用渲染 | ≥ 55fps | Chrome DevTools |
| Observation 展开 | < 100ms | `performance.mark('obs-expand')` |
| 历史消息加载 | < 50ms/条 | `performance.mark('blocks-deserialize')` |

---

## 4. 消息内联渲染

### 4.1 Text Block
正文 Markdown，正常渲染。

### 4.2 Thought Block
```
▸ Analyzed project structure and identified 3 modules  [展开 ▼]
```
- 默认折叠：一行灰显文字
- 完成后显示**智能摘要**（从 Thought 中提取第一句），而非泛泛的 "Thinking…"
- 流式中：微弱的呼吸灯动画（`pulse` 1s），让用户知道 Agent 活着
- 完成后：自动折叠

### 4.3 Tool Use Block
```
✓ Read 5 files (agent/core.py, server/main.py, …)  [展开 ▼]
```
- 默认折叠：一行摘要 + 状态图标（✓ 成功 / ✗ 失败 / ⏳ 运行中）
- 失败的工具调用：**默认半展开** + 红色左边框 + ⚠️ 图标
- 展开后内容区：`max-height: 400px` + 虚拟滚动（防长输出卡死）
- 安全渲染：使用 `SafeRenderer`，非 `dangerouslySetInnerHTML`

### 4.4 智能分组（P1）
连续 3 次以上同类 Read/Grep → `✓ Read 5 files (…more)` 合并为一行。
**可逆性**：Hover 弹出 Tooltip 列出全部文件名，无需点击展开即可扫描。

### 4.5 错误状态
```
✗ Write: docs/overview.md → Retried → ✓ Success  [展开 ▼]
```
- 失败+重试成功 → 合并为一行展示
- 最终失败 → 保留展开 + 醒目的错误原因

### 4.6 引用回溯（P2）
正文中 `[config.yaml ↗]` chip，点击后平滑滚动到对应 tool block 并自动展开/高亮 2 秒。

---

## 5. 三种视图模式（CC-aligned Verbose / Normal / Summary）

对标 Claude Code Desktop 的三种视图模式，映射到 ContentBlock 的折叠粒度：

| 模式 | 快捷键 | ContentBlock 渲染 |
|------|--------|-------------------|
| **Verbose** | `Ctrl+O` 循环 | 所有 tool_use + thought 默认展开，显示完整 output |
| **Normal**（默认） | — | tool_use + thought 默认折叠，点击展开 |
| **Summary** | — | 只渲染 text blocks，隐藏所有 tool_use + thought |

**实现**：在 `SessionUiState` 增加 `viewMode: 'verbose' | 'normal' | 'summary'` 字段。`BlocksMessage` 根据 viewMode 决定默认展开/折叠状态。切换快捷键 `Ctrl+O`。

**视觉提示**：在状态栏显示当前视图模式（如 `···` 表示 Summary，`□` 表示 Normal，`≡` 表示 Verbose）。

---

## 6. EventSidebar 重新定位

从完整的 Live Trace → 精简为**执行状态摘要 + 异常指示器 + Usage**（CC-aligned）：

```
┌───────────────────┐
│ ◌ Live        [›] │
│ Step 5 / 10       │
│ ████████░░ 50%    │
│ 00:45 · 12.3K tok │
│ Recent:           │
│ ✓ Read config     │
│ ✓ Grep "ReAct"    │
│ ⏳ Write overview  │
│ ⚠ 1 error         │  ← 异常指示器，点击跳转
│ Context: 45K/200K │  ← Usage 用量（CC-aligned）
│ [Open Full Trace] │  ← 链接到 TraceView
└───────────────────┘
```

- 正常执行时安静，异常时才引起注意
- 不再重复主区域已有的事件列表
- 完整 Trace 保留在 `TraceView` 标签页

---

# 第二部分：输入与操作重构

## 6. 输入框（Composer）

### 当前问题
- 底部有两行冗余信息（meta + runtime-summary）
- Mode/Model/Thinking/Auto-edit/Effort 五个 pill 挤在一行
- 快速工具按钮几乎不用但占用一整行
- 输入框高度过大

### 设计方案

```
  [Build]   [Plan]   [Explore]        Model: deepseek ▾
  ┌───────────────────────────────────────────────────┐
  │ 描述你想要做的事情…                    [➤]        │
  └───────────────────────────────────────────────────┘
  Shift+Enter 换行 · Enter 发送
```

**布局规则**：

| 元素 | 规范 |
|------|------|
| Mode Tab | 三个并排轻量按钮，选中下划线高亮。放在输入框**上方**，不是内部。**Mode 是会话级上下文，不是消息级属性** |
| 输入框 | `min-height: 36px; max-height: 120px; transition: height 0.1s ease; padding: 8px 12px; font-size: 13px` |
| Model 选择器 | 输入框右侧，`11px` 灰色小字。点击弹出双层菜单：**推荐档位** (Fast/Balanced/Strong) + **完整列表** (所有可用模型 ID) |
| 发送按钮 | `32x32px` 圆形，`➤` 图标。空内容时隐藏（`opacity: 0`），有内容时 `fade-in` 出现 |
| Placeholder | 随 Mode 切换：Build→"描述要实现的功能…" / Plan→"描述要规划的任务…" / Explore→"询问代码库相关问题…" |
| 换行提示 | 输入框下方一行 `10px` 灰色小字："Shift+Enter 换行 · Enter 发送" |

### Mode 切换行为
- 点击 Mode Tab → `setMode()` → 内存生效，不持久化
- 快捷键：`Ctrl+Shift+B` = Build, `Ctrl+Shift+P` = Plan, `Ctrl+Shift+E` = Explore（对标 CC 的 `Shift+Tab` 循环）
- 切换时自动联动推荐 Model：Build→Strong, Plan→Balanced, Explore→Fast（允许手动覆盖）
- `/build` `/plan` `/explore` 命令等同点击 Tab

### @Mention 文件搜索（P1）
- 输入 `@` → 弹出文件搜索面板
- 支持**模糊匹配**：`@auth` 命中 `src/auth/login.ts`
- 最近使用排序优先
- P2 扩展：`@web` `@docs` 等非文件上下文

---

## 7. HITL 审批（对标 CC 的行内权限渲染）

> CC 做法：每个工具的渲染组件自行处理权限 UI，不是独立 dock。权限提示 "与工具渲染并列"。
> Grace Code：由于是 Web UI（无终端 stdin 阻塞），采用 "输入框上方内联条"，但视觉上应对齐工具调用的位置。

### 当前问题
- `ToolApprovalCard` 是独立大卡片，阻断式弹窗体验
- 占据大量空间，遮挡对话

### 设计方案

```
┌──────────────────────────────────────────────────────────┐
│ ⚡ Bash: npm test                               [Y] [N]  │
│    Run unit tests for the auth module                    │
│    Risk: Medium · [This Session ▾]                       │
└──────────────────────────────────────────────────────────┘
```

**布局**：
- **单行高度**（`min-height: 40px`），在输入框正上方
- 左边：⚡ 图标 + 工具名 + 参数摘要
- 右边：`[Y]` `[N]` 两个 `kbd` 风格按钮（暗示键盘快捷键）
- 第二行：风险等级 + **记忆粒度**下拉：`This Call` / `This Session` / `This File Pattern`

### 键盘快捷键（核心效率）

| 按键 | 操作 | 焦点安全规则 |
|------|------|-------------|
| `Y` | 批准本次 | 焦点不在 `<input>`/`<textarea>`/`[contenteditable]` 内 |
| `N` | 拒绝 | 同上 |
| `Shift+Y` | 批准 + 按选定的记忆粒度 | 同上 |

**焦点安全检查**：在 `keydown` handler 中检查 `event.target.tagName` 和 `isContentEditable`。当用户正在输入框中打字时，`Y`/`N` 键正常输入，不会误触审批。

### 视觉
- 背景：浅黄色 `var(--warning-soft)`
- 左边框：`3px solid var(--warning)` / High 风险 → 红色
- 多个审批排队：最多显示 2 个，超过显示 "… and 2 more"

### 记忆粒度选择
```
This Call       → 只批准当前这一次调用
This Session    → 本次会话内均自动批准
This File Pattern → 匹配文件模式（如 npm test → Bash 中匹配 "npm test" 的命令）
```
**避免**一个 `Always Allow` 导致所有 Bash 命令都被放行。

---

## 8. Session 侧边栏

### 设计方案

```
┌────────────────────┐
│ ⬡ Grace Code   [‹] │
│ [+New Session]     │
│ Sessions (12)      │
│ ● Fix auth bug     │  ← 选中：蓝左边框 + 浅蓝背景
│   build · 2h ago   │
│ ○ Add OAuth        │
│   plan · 5h ago    │
│ ◉ Optimize queries │  ← 运行中：绿点 + 脉冲动画
│   build · running  │
└────────────────────┘
```

**布局**：
- 每个 Session 项两行：Title (`13px/600`) + Meta (`10px/400`)
- 选中态：`3px solid var(--accent)` 左边框 + 浅蓝背景
- 状态点：运行中=绿+脉冲 · 完成=灰 · 失败=红 · 排队=黄

### 运行中 Session 交互（P1）
- 点击运行中的 session → **旁观模式**（只读查看历史，输入框禁用显示 "Agent is working…"）
- **不是中断**。中断通过 Stop 按钮显式操作
- Hover 时显示最近一条消息的前 30 字

### +New Session 按钮
- 单击 → 创建 Build 模式 session（读取 `config default_agent`）
- 长按/右键 → 弹出 Mode 选择（Build/Plan/Explore）

### Session 分组（CC-aligned）
- 按 `agent_name`（Mode）分组：Build / Plan / Explore 三个折叠组
- 每组标题显示当前运行数 + 总数：`Build (3, 1 running)`
- 默认展开 Build 组，其他折叠

### Side Chat（P2，对标 CC `⌘+;`）
- 在主 session 中 `⌘+;` 打开侧聊面板
- 侧聊从主线程拉上下文，但不回写
- 适用于 "这个函数是干什么的" 之类的一次性问题

---

# 三部分：实施路线图

## P0（核心交互闭环）— 目标：可发布

```
├── ContentBlock 数据模型 + 21 单元测试 ✅ (已完成)
├── chatStore 流式 blocks + 完整性校验 ✅ (已完成)
├── BlocksMessage 内联渲染组件 ✅ (已完成)
├── 输入框基础布局 (36px + auto-expand + transition)
├── Mode Tab 切换 + 联动 placeholder
├── HITL 内联审批条 + Y/N 快捷键 (含焦点安全检查)
└── Session 列表两行式渲染
```

## P1（体验打磨）— 目标：专业感

```
├── @mention 模糊搜索 + 最近使用
├── Model 双层选择器 (档位 + 完整列表)
├── 智能分组 (连续同类合并 + hover 文件名列表)
├── HITL 记忆粒度 (Session / File Pattern)
├── 运行中 Session 旁观模式
├── 失败工具默认半展开 + 错误重试合并
└── EventSidebar 精简为执行摘要
```

## P2（高级特性）— 目标：差异化

```
├── 引用回溯 (chip + 滚动定位)
├── @web / @docs 非文件上下文
├── Thought 智能摘要生成
├── Observation MIME 感知渲染 (JSON→Tree, Image→Preview)
├── Session 右键菜单 (改名/复制ID/删除)
├── 输入框附件/图片拖拽
└── 自定义快捷键绑定
```

---

## 附录 A：CSS 变量体系

```css
:root {
  --accent: #3b82f6;       --accent-soft: rgba(59,130,246,0.08);
  --warning: #f59e0b;      --warning-soft: #fff8e1;
  --error: #ef4444;        --error-soft: rgba(239,68,68,0.08);
  --ok: #2da44e;
  --text: #1a1a2e;         --text-muted: #6b7280;
  --bg: #f6f8fe;           --bg-elev: rgba(255,255,255,0.9);
  --border: #e5e7eb;
}
[data-theme="dark"] {
  --text: #e5e7eb;         --text-muted: #9ca3af;
  --bg: #111827;           --bg-elev: rgba(30,41,59,0.9);
  --border: #374151;
  --accent-soft: rgba(59,130,246,0.15);
  --warning-soft: rgba(245,158,11,0.15);
  --error-soft: rgba(239,68,68,0.15);
}
```

## 附录 B：键盘快捷键总表

| 快捷键 | 作用域 | 操作 |
|--------|--------|------|
| `Enter` | 输入框聚焦 | 发送消息 |
| `Shift+Enter` | 输入框聚焦 | 换行 |
| `Y` | HITL 审批可见 + 焦点不在编辑区 | 批准 |
| `N` | HITL 审批可见 + 焦点不在编辑区 | 拒绝 |
| `Shift+Y` | HITL 审批可见 + 焦点不在编辑区 | 批准 + 记忆 |
| `/` | 输入框为空 | 触发斜杠命令菜单 |
| `@` | 输入框内 | 触发文件搜索面板 |
| `Escape` | 全局 | 关闭面板/取消 |
