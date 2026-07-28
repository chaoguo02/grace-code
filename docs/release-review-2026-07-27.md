# 发布前四角色审查报告

> 审查日期：2026-07-27 | 目标版本：master `4736dbe`

---

## 👨‍💻 前端工程师审查

### F1. TypeScript 编译 ✅
- `tsc --noEmit` 零错误

### F2. 组件架构

| 组件 | 状态 | 问题 |
|---|---|---|
| ChatView.tsx | ✅ | inline styles 过多（40+处），建议后续提取 CSS |
| StateMachineInspector.tsx | ✅ | 简洁，无外部依赖 |
| StatsDashboard.tsx | ✅ | 重建完成，error/loading/empty 三态齐全 |
| MemoryView.tsx | ✅ | edges/revisions/anchors 面板完整 |
| PlansView.tsx | ✅ | CRUD 完整 |
| TraceView.tsx | ✅ | 事件流 + TSM 查看器 |
| ContextUsageBar | ✅ | 用量条 + 压缩建议 |

### F3. 状态管理
- ✅ chatStore.sessionStateById — per-session 隔离正确
- ✅ sessionStore.detailById — 缓存一致性
- ✅ refreshActive() + loadSessions() 在 completion/compaction 后触发

### F4. 待改进项（P2，不阻塞发布）
- 🟡 ChatView inline styles 40+ 处，应考虑提取为 CSS class
- 🟡 `contextTotal` 默认 200K 硬编码，应从模型配置动态获取
- 🟡 StateMachineInspector 当前只在 TraceView 内嵌，考虑独立入口

---

## 👨‍🔧 后端工程师审查

### B1. 语法检查 ✅
- 所有修改文件通过 `ast.parse()`
- 已知问题：`app/storage/sqlite.py` 预存 BOM 字符（非本次引入）

### B2. 数据一致性

| 路径 | 写入顺序 | WS 事件 | 验证 |
|---|---|---|---|
| message append | DB 先写 | 事件后发 | ✅ session_store.py:416 |
| round complete | metadata + updated_at | 事件后发 | ✅ agent_service.py:943 |
| compaction | updated_at → WS compacted | 事件后发 | ✅ agent_service.py:838 |
| memory write | SQLite → _accumulate | 事件后发 | ✅ 异步队列天然延迟 |

### B3. 并发与线程安全

| 组件 | 状态 | 说明 |
|---|---|---|
| EventBus._publish_lock | ✅ | threading.Lock 保护 subscriber dict |
| TOCTOU 防护 | ✅ | try_acquire_session() 原子操作 |
| 维护线程 | ✅ | threading.Event 控制，daemon=True |

### B4. 待改进项
- 🟡 `_memory_maintenance_loop` 6h 间隔偏长——考虑改为 1h 或可配置
- 🟡 `compact_session_async` 的 `replace_messages_with_compaction` 无事务包裹（DELETE + INSERT 之间可能断）
- 🔴 **B4.1**: `replace_messages_with_compaction` 在 `app/storage/sqlite.py:743` — DELETE 后 INSERT 之间无事务，崩溃会丢失所有消息

---

## 🧪 测试工程师审查

### T1. 测试覆盖总览

| 区域 | 测试文件 | 测试用例 | 覆盖率评估 |
|---|---|---|---|
| tool_exec | 3 | ~47 | ✅ 充分 |
| permission | 4 | ~17 | ✅ 充分 |
| memory | 4 | ~51 | ✅ 充分 |
| subagent | 5 | ~19 | ✅ 充分 |
| context | 3 | ~12 | ✅ 基本覆盖 |
| replay | 2 | ~32 | ✅ 充分 |
| compaction | 1 | 5 | ⚠️ 偏少 |
| eval | 3 | ~19 | ✅ 充分 |
| trace | 1 | 8 | ⚠️ 偏少 |
| state_machine | 1 | 5 | ⚠️ 偏少 |
| **stats** | **0** | **0** | 🔴 **无测试** |

### T2. 缺失的测试场景
- 🔴 **T2.1**: StatsDashboard 无任何测试（组件渲染、数据加载、错误处理、retry）
- 🔴 **T2.2**: `replace_messages_with_compaction` 无事务安全测试
- 🔴 **T2.3**: `_validate_anchors_stale` 路径解析测试（修复前 bug 无测试保护）
- 🟡 **T2.4**: `_memory_maintenance_loop` 无单元测试（依赖 sleep，难以测试）
- 🟡 **T2.5**: ContextUsageBar 用量百分比边界测试（0/100/200 tokens）

### T3. 回归测试
- ✅ 459 个现有测试全部通过
- ✅ `test_memory_api.py` 13→18 个用例（覆盖 edges/revisions）

---

## 📋 产品工程师审查

### P1. 功能完整性

| 功能 | 状态 | 备注 |
|---|---|---|
| Session CRUD | ✅ | 创建/切换/删除/改名 |
| Chat 对话 | ✅ | composer + timeline + blocks |
| Plan 工作流 | ✅ | plan→approve→build 完整 |
| Plans Library | ✅ | 浏览/查看/编辑/删除 |
| Memory 管理 | ✅ | 四分类型分组 + TTL + edges/revisions |
| Trace 回放 | ✅ | 实时 + 历史 + ReplayLab |
| Stats 统计 | ✅ | token/tool/session 面板 |
| Context 压缩 | ✅ | 手动 `/compact` + 自动 + 建议 |
| Subagent 委托 | ✅ | build→explore/code-reviewer 等 6 种 |
| TSM 查看器 | ✅ | Trace 页内嵌 |
| 权限审批 | ✅ | ToolApprovalCard |
| 安全边界 | ✅ | 认证（loopback）+ 限流 |

### P2. UI/UX 一致性
- 🟡 新导航（Module/View）和旧 Tab 导航并存 —— 两套系统混合
- 🟡 `StatsDashboard` 使用旧版 CSS class（`plan-page`, `stats-page`），风格与新版不一致
- ✅ ContextUsageBar 颜色语义正确（蓝=80%+, 红=95%+）

### P3. 用户流程
- ✅ 创建 session → chat → compaction 闭环完整
- ✅ Plan 生成 → 审批 → Build 执行流程完整
- ✅ Memory 浏览 → 编辑 → 过期自动清理完整
- 🟡 `/compact` 斜杠命令已添加，但首次用户可能不知其存在（依赖 ContextUsageBar 提示）

### P4. 发布阻塞项

| # | 严重度 | 问题 | 阻塞发布？ |
|---|---|---|---|
| B4.1 | 🔴 P0 | compaction 消息替换无事务 | **是** |
| T2.1 | 🔴 P0 | Stats 区域零测试 | 否（不阻塞功能） |
| T2.3 | 🟡 P1 | anchor 路径 bug 测试 | 否 |
| P2.UI | 🟡 P2 | 新旧导航混合 | 否 |

---

## 行动清单

**发布前必须修复（1 项）**：
- 🔴 B4.1：`replace_messages_with_compaction` 加 BEGIN/COMMIT 事务包裹

**强烈建议修复（2 项）**：
- 🔴 T2.1：为 StatsDashboard 添加渲染测试
- 🔴 T2.3：为 `_validate_anchors_stale` 添加路径解析测试

**发布后可改进（4 项）**：
- 🟡 F4: inline styles 提取 CSS
- 🟡 B4: 维护间隔可配置化
- 🟡 P2.UI: 统一导航系统
- 🟡 T2.4/T2.5: 补充测试覆盖
