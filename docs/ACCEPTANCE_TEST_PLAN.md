# StreamingTurn 验收测试计划

> 日期：2026-07-25 | 改动范围：56 files, +3437/-578 lines

## 一、回归测试（P0 — 必须全部通过）

### 1.1 现有单元测试
```bash
cd web && npx vitest run src/types/blocks.test.ts
```
- [x] 21/21 通过

### 1.2 前端 TypeScript 编译
```bash
cd web && npx tsc --noEmit
```
- [x] 零错误

### 1.3 后端导入检查
```bash
python -c "from config.schema import AgentCfg; from agent.mode_switching import handle_plan_mode_transition; from tools.plan_mode_tool import EnterPlanModeTool; print('OK')"
```
- [x] 导入正常

---

## 二、功能验收

### 2.1 流式消息展示

**TC-1.1 发送消息后用户问题立即可见**
1. 打开会话，输入 "你好"，点击发送
2. **预期**：用户消息 "你好" 立即出现在聊天区域（不等待 WS 事件）
3. **验收**：消息在 send 后 100ms 内可见

**TC-1.2 流式回答逐步展示**
1. 发送需要工具调用的 prompt
2. **预期**：thought → tool_call → observation → text 逐步出现
3. **验收**：每个 block 在 WS 事件到达 200ms 内渲染

**TC-1.3 工具调用默认折叠**
1. 等待流式完成
2. **预期**：tool_use block 显示为一行 `✓ Read: agent/core.py [▼]`
3. **验收**：不展开时 tool_use 高度 ≤ 30px

**TC-1.4 工具调用可展开**
1. 点击折叠的 tool_use
2. **预期**：展开显示 output 内容，再次点击折叠
3. **验收**：展开/折叠切换无闪烁

### 2.2 刷新一致性

**TC-2.1 刷新区前后内容一致**
1. 等待一次对话完成
2. F5 刷新页面
3. **预期**：刷新前后的消息内容完全相同（用户问题 + 助手回答 + 工具调用）
4. **验收**：无内容丢失，无额外卡片出现

**TC-2.2 刷新后工具调用保持折叠**
1. 等待对话完成，展开一个 tool_use
2. F5 刷新
3. **预期**：所有 tool_use 恢复默认折叠状态
4. **验收**：正常模式（normal）下无展开的 tool_use

### 2.3 模式切换

**TC-3.1 Mode Tab 切换**
1. 点击 Plan tab → placeholder 变为 "描述要规划的任务…"
2. 点击 Build tab → placeholder 变为 "描述要实现的功能…"
3. **预期**：切换即时生效，不发送请求
4. **验收**：mode 变化在 50ms 内反映到 UI

**TC-3.2 模式切换不持久化**
1. 在 Plan 模式发送一条消息
2. 切换到 Build 模式
3. F5 刷新
4. **预期**：刷新后显示 session 创建时的原始模式（非临时切换的 Build）
5. **验收**：`activeDetail.agent_name` 不变

**TC-3.3 approve 后自动切换到 Build**
1. 在 Plan 模式发送消息，等待 plan_ready
2. 点击 Approve & Build
3. **预期**：模式自动切换为 Build
4. **验收**：`currentMode` 变为 "build"

### 2.4 HITL 审批

**TC-4.1 审批条展示**
1. 触发需要审批的 Bash 命令
2. **预期**：输入框上方出现内联审批条 `⚡ Bash: npm test [N Deny] [Y Approve]`
3. **验收**：审批条高度 ≤ 40px

**TC-4.2 Y/N 快捷键**
1. 审批条可见时，鼠标不聚焦在输入框
2. 按 `Y` → 批准
3. 按 `N` → 拒绝
4. **预期**：快捷键即时生效
5. **验收**：keydown 响应 < 100ms

**TC-4.3 焦点安全检查**
1. 审批条可见时，在输入框内输入 "Yes"
2. **预期**：`Y` 键正常输入字母，不触发批准
3. **验收**：输入框内打字不受影响

**TC-4.4 记忆粒度**
1. 审批条可见时，更改 scope 为 "Session"
2. 点击 Approve
3. **预期**：后续同工具调用自动批准
4. **验收**：第二次同工具不弹审批条

### 2.5 Session 侧边栏

**TC-5.1 单行展示**
1. **预期**：每个 session 显示为一行：状态点 + 标题（截断） + 时间
2. **验收**：session 项高度 ≤ 36px

**TC-5.2 hover 显示操作按钮**
1. 鼠标移到 session 项上
2. **预期**：时间隐藏，显示 ✎ + × 按钮
3. **验收**：hover 150ms 内按钮出现

**TC-5.3 重命名**
1. 点击 ✎ → 弹出 prompt 对话框
2. 输入新名称 → 确认
3. **预期**：标题更新，刷新后保持
4. **验收**：`updateSession` API 调用成功

### 2.6 视图模式

**TC-6.1 Ctrl+O 循环**
1. 按 Ctrl+O → Summary 模式（只显示文字，隐藏所有工具调用）
2. 再按 → Normal 模式（工具调用折叠）
3. 再按 → Verbose 模式（工具调用展开）
4. **验收**：循环正常工作

**TC-6.2 视图模式持久化**
1. 切换到 Summary 模式
2. F5 刷新
3. **预期**：仍为 Summary 模式（localStorage 存储）
4. **验收**：`localStorage.getItem('grace-view-mode') === 'summary'`

---

## 三、边界测试

**TC-EDGE-1 流式中刷新**
1. 发送消息后立即 F5 刷新（在 WS 事件到达前）
2. **预期**：`loadTimeline` 从 DB 加载完整对话，无数据丢失
3. **验收**：用户消息 + 助手回答完整可見

**TC-EDGE-2 快速连发两条消息**
1. 发送 "hello" 后立即发送 "world"
2. **预期**：两条消息各有独立的 `activeTurn`（第二条创建时第一条已被覆盖或移入 completedTurns）
3. **验收**：无重复消息，turn ID 不冲突

**TC-EDGE-3 网络断开重连**
1. 流式中断开网络 3 秒
2. 重连后 WS 补推事件
3. **预期**：`hasGap` 标记为 true，`loadTimeline` 后 remount
4. **验收**：重连后内容完整（DB 数据接管）

**TC-EDGE-4 空消息发送**
1. 输入框为空时点击发送
2. **预期**：不发送，不创建 activeTurn
3. **验收**：`sendChat` 不执行

**TC-EDGE-5 collapsed sidebar**
1. 收起左侧 sidebar
2. 收起右侧 sidebar
3. **预期**：两侧都能正常收起/展开，answer 区域自适应
4. **验收**：grid 列宽正确切换

---

## 四、性能指标

| 指标 | 目标 | 测量方式 |
|------|------|----------|
| 流式 block 渲染 | < 16ms per delta | React DevTools Profiler |
| 20 步 tool call 滚动 FPS | ≥ 55fps | Chrome Rendering |
| DB 加载→blocks 转换 | < 50ms | `performance.mark('extract-blocks')` |
| 完整对话首次渲染 | < 200ms | Lighthouse TTI |
| Tooltip hover 响应 | < 150ms | `performance.mark('hover-tooltip')` |

---

## 五、已发现的 Bug 并已修复

| Bug | 严重度 | 状态 |
|-----|--------|------|
| integrity check 比较全部 DB blocks vs 仅助手 blocks | 🔴 | ✅ 已修复 |
| `completed` handler 提前清空 activeTurn | 🔴 | ✅ 已修复 |
| DB turn 转换丢失 metadata | 🟡 | ✅ 已修复 |
| generation 用 length+1 在未加载时冲突 | 🟡 | ✅ 已修复 |
| failed/cancelled 不清 activeTurn | 🟡 | ✅ 已修复 |
| running handler 清空 streamingBlocks | 🟡 | ✅ 已修复（方案二临时补丁，StreamingTurn 彻底解决） |
