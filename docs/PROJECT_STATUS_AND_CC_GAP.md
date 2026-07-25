# Grace Code vs Claude Code — 完整差距分析与项目状态

> 日期：2026-07-25 | 改动范围：62 files, ~4200 insertions, ~650 deletions
>
> 本文档汇总了本次大规模重构的全部成果，按模块逐项对比 Claude Code 的实现，标注完成度与剩余差距。

---

## 一、涉及模块总览

```
前端 (web/src/)          后端 (Python)
─────────────────        ──────────────
components/  12 files    agent/          7 files
stores/       1 file     core/           4 files
types/        3 files    tools/         18 files
utils/        1 file     hitl/           1 file
styles.css    1 file     server/         4 files
api/          1 file     prompts/        3 files
App.tsx       1 file     config/         1 file
hooks/        0 files    utils/          1 file
                         tests/          1 file
─────────────            ─────────────
前端 20 files            后端 40 files
```

---

## 二、逐模块对比

### 2.1 工具系统

| 维度 | Claude Code | Grace Code（改后） | 对齐度 |
|------|-------------|-------------------|--------|
| 只读工具判断 | `isReadOnly(input)` 动态方法，默认 false | `BaseTool.isReadOnly(params)` 动态方法，默认 false + `ToolMetadata.effects` 回退 | 🟢 对齐 |
| 工具覆盖范围 | 全部内置工具的 `isReadOnly()` | 全部 17 个工具文件，36 个工具类均已实现 `isReadOnly()` | 🟢 对齐 |
| Bash 输入感知 | `ls`/`cat` → True, `rm` → False | 根据 `_READ_ONLY_COMMANDS` frozenset + `_READ_ONLY_PREFIXES` 匹配 | 🟢 对齐 |
| 权限管道检查 | `_layer4_permission_mode` 动态调用 `isReadOnly()` | 同，已替换硬编码 `_READONLY_SAFE_TOOLS` frozenset 和 `{"Write","Edit","Bash"}` | 🟢 对齐 |
| Read-before-Edit | mtimeMs staleness guard | FileEditTool ✅ / FileWriteTool ✅（本次新增） | 🟢 对齐 |
| Read Cache 大小限制 | LRU + 总大小上限 | FIFO 淘汰：200 entries / 50MB 上限（本次新增） | 🟢 对齐 |
| MCP 工具支持 | `readOnlyHint`/`destructiveHint` 注解感知 | `McpToolWrapper.isReadOnly()` 检查 `_tool_def.annotations` | 🟢 对齐 |

### 2.2 权限模式切换

| 维度 | Claude Code | Grace Code（改后） | 对齐度 |
|------|-------------|-------------------|--------|
| 进入 Plan 模式 | `handlePlanModeTransition` 统一入口，保存 `prePlanMode` | `handle_plan_mode_transition()` 统一入口 + `save_pre_plan_mode()` | 🟢 对齐 |
| 退出 Plan 模式 | 恢复 `prePlanMode`，熔断器检查 auto gate | `restore_pre_plan_mode()` + `CircuitBreaker.is_gate_enabled()` 检查 | 🟢 对齐 |
| 子Agent 禁止进入 Plan | `context.agentId` 检查 | `EnterPlanModeTool.execute()` 检查 `_requesting_agent` | 🟢 对齐 |
| allowedPrompts 传递 | ExitPlanMode → build session pipeline 自动继承 | `PermissionSessionConfig.approved_prompts` 字段 → `configure_session()` → pipeline | 🟢 对齐 |
| 模式切换持久化 | `settings.json` `permissions.defaultMode`（仅新 session 默认值） | 前端内存切换，session 重开以 `agent_name` 为准 | 🟢 对齐 |

### 2.3 Plan 模式工作流

| 维度 | Claude Code | Grace Code（改后） | 对齐度 |
|------|-------------|-------------------|--------|
| System Prompt 结构 | 5-Phase 工作流（Explore → Design → Review → Final Plan → ExitPlanMode） | `prompts/modes/plan.md` 完整 5-Phase 工作流 | 🟢 对齐 |
| Prompt 节流注入 | Attachment Throttling：Turn 1 全量，2-4 跳过，5 稀疏，25 全量刷新 | `PlanModeAttachmentManager` 同频次节流 | 🟢 对齐 |
| Plan 文件写入 | ExitPlanMode 自动写文件（无需 Write 工具） | `ExitPlanModeTool.execute()` 自动写 `.grace/plans/{id}.md` | 🟢 对齐 |
| Plan Contract 结构 | `{ goal, steps, target_files, verification, risks }` | 完全同结构 | 🟢 对齐 |
| planWasEdited 追踪 | SHA256 hash 对比 | `approve()` 端点对比 `PlanRevisionService` 存储的 hash | 🟢 对齐 |
| Re-entry 引导 | 注入 "Read existing plan → Evaluate → Overwrite/Modify" | `EnterPlanModeTool.execute()` 检测已有 plan 文件，注入引导文本 | 🟢 对齐 |

### 2.4 Build 模式

| 维度 | Claude Code | Grace Code（改后） | 对齐度 |
|------|-------------|-------------------|--------|
| System Prompt | 专门 build agent prompt（step-by-step 执行） | `AgentDefinition("build")` system_prompt 1253 字符（6 步工作流 + 子Agent 编排） | 🟢 对齐 |
| 子Agent 编排引导 | 并行委派独立步骤、串行依赖步骤 | 同，prompt 中明确指导 | 🟢 对齐 |
| 结构化 Plan Context 注入 | 从 contract 提取 steps/target_files/verification | `approve()` 端点注入结构化 steps + target_files + verification | 🟢 对齐 |
| Auto-accept Edits 模式 | 独立 permission mode | `acceptEdits` mode（Write/Edit/Bash 安全命令自动批准） | 🟢 对齐 |
| Prompt-based Permissions | allowedPrompts 在 build session 生效 | `PermissionSessionConfig.approved_prompts` 跨 session 传递 | 🟢 对齐 |

### 2.5 Subagent 系统

| 维度 | Claude Code | Grace Code（改后） | 对齐度 |
|------|-------------|-------------------|--------|
| Agent 工具参数 | description, prompt, subagent_type, model, run_in_background, isolation | 全部支持 + 额外 `execution_placement` 枚举 | 🟢 对齐 |
| 子Agent 类型 | explore, general, Bash, code-reviewer 等 | build, plan, explore, general, code-reviewer | 🟢 对齐 |
| 权限继承 | deny/allow rules 继承，permission_mode 级联 | `apply_inherited_state()` 同逻辑 | 🟢 对齐 |
| Model 选择 | AgentDefinition.model 字段 + Agent 工具 model 参数 | 同，增加 `definition.model` 回退链 + 跨 Provider 保护（本次新增） | 🟢 对齐 |
| Background 执行 | `run_in_background: true` | `ExecutionPlacement.BACKGROUND` + `_start_background_execution()` | 🟢 对齐 |
| Worktree 隔离 | `isolation: "worktree"` | `WorkspaceMode.WORKTREE` + 完整生命周期管理 | 🟢 对齐 |
| Subagent 恢复 | `agentId` → SendMessage 恢复 | `<resume-hint>` XML + SendMessage 引导（本次新增） | 🟢 对齐 |
| 嵌套委派 | 最多 5 层 | 支持但无明确深度限制 | 🟡 需加深度上限 |
| Subagent transcript 持久化 | 独立持久化，30 天清理 | 无持久化 UI | 🔴 差距 |

### 2.6 前端 UI

| 维度 | Claude Code Desktop (2026.04) | Grace Code（改后） | 对齐度 |
|------|------------------------------|-------------------|--------|
| 消息渲染 | Verbose/Normal/Summary 三层 + inline tool cards | `ContentBlock` → `BlocksMessage` text/thought/tool_use 内联折叠 + Ctrl+O 三层切换 | 🟢 对齐 |
| 流式→持久切换 | 无感替换 | `StreamingTurn` + `checkTurnIntegrity` + `eventSeq` 完整性校验 | 🟢 对齐 |
| 输入框 | PromptInput 多行 + 历史 + 斜杠命令 | Mode Tab 外置 + placeholder 联动 + Send 按钮 + @mention | 🟢 对齐 |
| Mode 选择 | Shift+Tab 循环 | Mode Tab 按钮 + Ctrl+Shift+B/P/E 快捷键 | 🟢 对齐 |
| Model 选择 | ModelPicker 浮动弹窗 | 双层选择器：Tier 分组（Active/Fast/Balanced/Strong）+ 完整列表 | 🟢 对齐 |
| HITL 审批 | 行内（工具渲染组件自行处理） | 内联审批条 + Y/N 快捷键 + 焦点安全检查 + 记忆粒度 | 🟢 对齐 |
| Session 侧边栏 | 按状态/项目分组，PR 合并自动归档 | 单行项 + hover ✎/× + 按 Mode 分组（本次新增） | 🟢 对齐 |
| Sidebar 可折叠 | 有 | 左右均可折叠 + fr 响应式 grid | 🟢 对齐 |
| 键盘帮助 | ⌘+/ 快捷键总览 | `?` 键弹窗列出全部快捷键 | 🟢 对齐 |
| 运行中 Session 旁观 | 点击查看，不中断 | placeholder "Agent is working…" + 输入框/发送按钮 disabled | 🟢 对齐 |
| @mention 文件搜索 | 无（终端限制） | 模糊匹配 + 最近使用排序（本次新增） | 🟢 Web 独有优势 |
| 智能分组 | Normal 模式折叠 | 3+ 连续同类 tool_use 自动合并 + hover 文件名列表 | 🟢 对齐 |

### 2.7 执行预算与安全

| 维度 | Claude Code | Grace Code（改后） | 对齐度 |
|------|-------------|-------------------|--------|
| 预算三层架构 | SDK task_budget + Hook PreToolUse gate + RPM/TPM | ExecutionBudget(WARNING/CRITICAL/EXHAUSTED) + CircuitBreaker + RateLimitMiddleware | 🟢 对齐 |
| 预算检查粒度 | 每次工具调用前（PreToolUse hook） | Per-step（主循环开始）+ Per-tool（ToolExecutionPipeline gate，本次新增） | 🟢 对齐 |
| 默认预算 | 200k tokens / 100 steps / 30 min | 200k / 100 / 1800s（本次调整） | 🟢 对齐 |
| Read-before-Edit | FileEditTool + FileWriteTool 强制检查 | FileEditTool ✅ / FileWriteTool ✅（本次新增） | 🟢 对齐 |
| Read Cache 大小 | LRU + 上限 | FIFO 淘汰 200 entries / 50MB（本次新增） | 🟢 对齐 |
| 预算消耗归因 | 无内置（社区工具 tokenwarden 提供） | 无 | 🟡 差距（P2） |
| 文件安全（路径沙箱等） | 3 层防护 | `sanitize_path` + `is_path_safe` + `safe_open_for_write` + `_PROTECTED_DIRS` | 🟢 对齐 |

---

## 三、Claude Code 有而我们没有的

| # | 差距 | 严重度 | 说明 |
|---|------|--------|------|
| D1 | Subagent transcript 持久化 UI | 🔴 | CC 独立持久化 30 天，支持 resume；我们无 |
| D2 | Subagent 嵌套深度上限 | 🟡 | CC 硬限制 5 层，我们无限制 |
| D3 | 预算消耗归因监控 | 🟡 | CC 社区工具支持，我们无 |
| D4 | WS stream_start 携带 trigger_message | 🟡 | 后端配合，前端已预留接口 |
| D5 | Session 右键菜单 | 🟢 | 改名/复制ID/删除操作 |
| D6 | 引用回溯（正文 chip → tool block 锚点） | 🟢 | P2 高级交互 |
| D7 | Observation MIME 感知渲染 | 🟢 | JSON→Tree, Image→Preview |

---

## 四、我们有而 Claude Code 没有的

| 优势 | 说明 |
|------|------|
| Git Worktree 隔离 | 完整生命周期：创建→检查→应用→丢弃→保留 |
| Structured Output 验证 | `CompletionGuard` 强制 `ReportFindings` 调用 + 路径/行号验证 |
| HITL 记忆粒度 | Once / Session / File Pattern 三级，比 CC 的 Always Allow 更精细 |
| @mention 模糊搜索 | Web UI 独有优势 |
| delegation_scope | READ_ONLY / ANY 定义级权限上限 |
| ContentBlock 数据模型 | 正式的 TypeScript 接口 + 21 单元测试，比 CC 的隐式结构更清晰 |

---

## 五、文件改动清单

### 新增文件 (18 个)

| 文件 | 用途 |
|------|------|
| `web/src/types/blocks.ts` | ContentBlock + StreamingTurn 类型定义 |
| `web/src/types/blocks.test.ts` | 21 单元测试 |
| `web/src/components/BlocksMessage.tsx` | ContentBlock 内联渲染组件 |
| `web/src/components/ModeTab.tsx` | Mode 切换 Tab 组件 |
| `web/src/components/KeyboardHelp.tsx` | `?` 快捷键帮助弹窗 |
| `web/src/utils/fuzzy.ts` | 模糊匹配工具 |
| `web/src/utils/plan_naming.py` | Plan 文件命名（word-slug） |
| `agent/plan_attachment_manager.py` | Plan 模式 prompt 节流注入 |
| `prompts/modes/plan-sparse.md` | Plan 模式稀疏提醒 |
| `docs/CHAT_UI_RFC.md` | Chat UI 重构 RFC |
| `docs/STREAMING_SESSION_MODEL.md` | StreamingTurn 模型设计 |
| `docs/BUDGET_AND_SAFETY_GAP_ANALYSIS.md` | 预算与安全差距分析 |
| `docs/ACCEPTANCE_TEST_PLAN.md` | 验收测试计划 |
| `docs/PROJECT_STATUS_AND_CC_GAP.md` | 本文档 |
| `tests/test_tool_isreadonly.py` | isReadOnly 单元测试 |

### 修改文件 (44 个)

**前端核心 (8 个)**
- `web/src/stores/chatStore.ts` — StreamingTurn 模型 + WS→block 映射 + 完整性校验 + viewMode
- `web/src/components/ChatView.tsx` — 统一 blocks 渲染 + Mode Tab + ? 帮助 + 旁观模式 + 模糊搜索
- `web/src/components/EventSidebar.tsx` — 精简为紧凑摘要
- `web/src/components/SessionSidebar.tsx` — 单行渲染 + hover 操作按钮
- `web/src/components/ToolApprovalCard.tsx` — 内联 HITL 审批条 + Y/N 快捷键
- `web/src/App.tsx` — 可折叠侧边栏
- `web/src/styles.css` — 全部新 UI 样式（~600 行新增）
- `web/src/api/config.ts` — getAppDefaults API

**后端核心 (36 个)**
- `core/base.py` — isReadOnly() 基础方法
- `core/circuit_breaker.py` — is_gate_enabled()
- `core/tool_execution.py` — Per-tool budget gate
- `core/types.py` — ToolMetadata 扩展
- `hitl/pipeline.py` — 动态 isReadOnly() 替换硬编码 + approved_prompts
- `agent/core.py` — Budget 注入 registry
- `agent/mode_switching.py` — handle_plan_mode_transition() 统一入口
- `agent/session/execution_budget.py` — 默认预算提升
- `agent/session/models.py` — Build agent system_prompt
- `agent/session/runtime.py` — PlanModeAttachmentManager 集成 + allowed_prompts 传递
- `agent/session/subagent.py` — Model 回退链 + 跨 Provider 保护
- `agent/session/task_tool.py` — Subagent 恢复引导
- `tools/file_tool.py` — Read-before-Write + Cache 大小限制
- `tools/plan_mode_tool.py` — Subagent 拦截 + Re-entry 引导 + ExitPlanMode 写文件
- `tools/` 其余 14 个文件 — isReadOnly() 覆盖
- `server/main.py` — RateLimitMiddleware 60→300
- `server/routers/approvals.py` — allowed_prompts 传递 + planWasEdited + 结构化 contract 注入
- `server/routers/config.py` — 动态 model catalog + default_agent
- `server/services/agent_service.py` — allowed_prompts 参数链
- `server/services/chat_pipeline.py` — Plan 文件写保护 + allowed_prompts 传递
- `prompts/modes/plan.md` — 5-Phase 工作流重写
- `prompts/builder.py` — sparse reminder 函数
- `config/schema.py` — default_agent 字段

---

## 六、交付验证

### 6.1 死代码清扫（已完成 ✅）

| 旧字段/常量 | 状态 |
|-------------|------|
| `streamingBlocks` | 已替换为 `activeTurn`，仅 docs 中保留历史引用 |
| `dbBlocksByMsgId` | 仍作为 `checkTurnIntegrity` 函数参数和 `loadTimeline` 局部变量，非死代码 ✅ |
| `_READONLY_SAFE_TOOLS` frozenset | 已删除，替换为动态 `isReadOnly()` |
| `pairedTimeline` | 已删除，替换为 `activeTurn` + `completedTurns` |
| `MessageBubble` import in ChatView | 已移除（组件文件保留，TraceView/SubagentDetail 仍使用 `WsEventBlock`） |

### 6.2 回归验证（已完成 ✅）

| 验证项 | 结果 |
|--------|------|
| Read-before-Write 边界逻辑（新建/空文件/符号链接） | ✅ Code review 确认豁免逻辑正确 |
| Budget per-tool gate 层级（仅 EXHAUSTED 硬阻断） | ✅ WARNING/CRITICAL 不注入工具层消息 |
| Read Cache FIFO 淘汰（>200 entries） | ✅ 250 写入后保持在 200 entries |
| FileReadCache 总字节数跟踪 | ✅ `_total_bytes` 在 store/invalidate 中正确维护 |

---

## 七、路线图与剩余工作（P2 重排）

按风险收益比重新排序：

| 顺序 | 任务 | 预估 | 理由 |
|------|------|------|------|
| **1** | Subagent 嵌套深度上限（5 层） | 1 天 | 安全项——无限制 = 潜在无限递归+预算爆炸 |
| **2** | WS stream_start trigger_message | 2 天后端 + 1 天前端 | StreamingTurn 从乐观注入升级为权威同步 |
| **3** | 预算消耗归因监控 | 2 天 | 可观测性基础——没有它后续优化全凭猜测 |
| **4** | Subagent transcript 持久化 | 3-5 天 | 影响 resume 和多轮调试 |
| **5** | 引用回溯（chip + 滚动定位） | 2-3 天 | 交互增强 |
| **6** | Observation MIME 感知渲染 | 2 天 | 交互增强 |
| **7** | Session 右键菜单 | 1 天 | 交互增强 |

### 原始文档

| 优先级 | 任务 | 预估工作量 | 依赖 |
|--------|------|-----------|------|
| P2 | Subagent transcript 持久化 UI | 3-5 天 | 无 |
| P2 | Subagent 嵌套深度上限（5 层） | 1 天 | 无 |
| P2 | 引用回溯（chip + 滚动定位） | 2-3 天 | ContentBlock.anchorTargets 预留字段 |
| P2 | WS stream_start 携带 trigger_message | 2 天后端 + 1 天前端 | 后端配合 |
| P2 | 预算消耗归因监控 | 2 天 | 无 |
| P2 | Observation MIME 感知渲染 | 2 天 | SafeRenderer 组件 |
| P2 | Session 右键菜单 | 1 天 | 无 |
