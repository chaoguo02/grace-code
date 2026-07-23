# Grace Code 审计 TODO 追踪

> **生成日期**: 2026-07-21 · **最终对齐**: 2026-07-23
> **Phase**: 2 — 深度审计
> **方法论**: Vibe Coding 反模式识别 + 安全审计 + 权限管线审查 + 前端代码质量
> **理论来源**: Clean Code／Clean Architecture (Robert C. Martin), Claude Code CVE 披露, Loop Engineering Patterns (2026), SQLite WAL 官方文档

---

## ⚙️ 维护约定（强制）

1. **任何修复提交必须同步更新此文件。**
   修复 commit 的 message 中应引用对应的 TODO ID（如 `fix(P0-2): ...`）。
2. **更新状态时需附带 commit hash。**
   格式：`✅ [hash]` / `⚠️ [hash] + 剩余问题` / `❌`。
3. **禁止使用模糊描述。** 所有标记必须精确到文件:行号 + 修复内容。
4. **本文件随项目演进实时更新。** 在 Phase 切换、Batch 完成、或门禁通过时同步修订。

---

## 📊 统计摘要 (2026-07-23 对齐后)

| 优先级 | 未修复 (❌) | 部分修复 (⚠️) | 已修复 (✅) | 合计 |
|--------|------------|--------------|-----------|------|
| 🔴 P0 | 1 | 1 | 11 | 13 |
| 🟠 P1 | 11 | 5 | 17 | 33 |
| 🟡 P2 | 35 | 2 | 19 | 56 |
| **总计** | **47** | **8** | **47** | **102** |

### 按模块分布（未修复 + 部分修复）

| 模块 | P0 | P1 | P2 | 合计 |
|------|----|----|-----|------|
| agent/core.py | 1 | 6 | 7 | 14 |
| server/ (AgentService + routers) | 0 | 2 | 3 | 5 |
| core/ (base.py, circuit_breaker.py) | 0 | 0 | 3 | 3 |
| hitl/ (pipeline.py) | 0 | 4 | 2 | 6 |
| memory/ | 0 | 0 | 1 | 1 |
| app/storage/ (sqlite.py) | 0 | 1 | 1 | 2 |
| agent/session/ (session_store, runtime) | 0 | 0 | 2 | 2 |
| web/ (API, stores, components) | 0 | 0 | 11 | 11 |
| context/ + hooks/ + llm/ + tools/ | 0 | 1 | 5 | 6 |

---

## 🔴 P0 — 立即修复（13 项：安全／数据完整性／逻辑错误）

### 安全与线程

- [x] **P0-1** ✅ d841fba [agent/session/session_store.py:44-45] SQLite WAL + busy_timeout
  | `conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA busy_timeout=10000")`

- [x] **P0-2** ✅ 59ecec2 [server/services/agent_service.py:137, chat_pipeline.py:176] LLM Backend 共享可变状态修复
  | `chat_pipeline.py` 为每个 session 创建 per-session backend（`set_backend_for_session()`）。全局 `self._backend` 降级为 fallback。

- [x] **P0-3** ✅ 59ecec2 [server/services/agent_service.py:123, chat_pipeline.py:167] 模型切换时 API key/base_url 保留
  | `_effective_llm_config` dict 保存 CLI 覆盖项；chat_pipeline 在模型切换时使用其。

- [x] **P0-4** ✅ 59ecec2 [agent/session/runtime.py:241-244] Session 执行 TOCTOU 修复
  | `try_acquire_session()` with `threading.Lock` 原子性 check-and-acquire。HTTP handler 中的 DB 状态检查仅为快速反馈。

- [x] **P0-5** ✅ 59ecec2 [server/routers/sessions.py:425-426] RuntimeError → HTTP 409
  | `except RuntimeError as exc: raise HTTPException(status_code=409, detail=str(exc))`

- [x] **P0-6** ✅ df4d4fc [memory/sqlite_backend.py:159-166] 语义搜索索引失败静默修复
  | `_last_index_error` + `_index_error_count` + `logger.warning`

- [x] **P0-7** ✅ 59ecec2 [app/storage/sqlite.py:229,249] Session 删除有事务包裹
  | `conn.execute("BEGIN IMMEDIATE")` / `COMMIT` for delete_session + batch_delete

### 逻辑错误（agent/core.py）

- [x] **P0-8** ✅ c01e941 [agent/core.py:1287-1300] `break` 误用修复
  | break 已移除；tool call 验证失败时向对话历史注入错误 observation，LLM 下一轮可见并自修正。

- [x] **P0-9** ✅ 59ecec2 [agent/core.py] Guard 异常静默吞没修复
  | `_reflection_guards` 代码段已移除（grep 无匹配）。

- [x] **P0-10** ✅ e614cb4 [agent/core.py:344-391, 409-448, completion_guard.py:21-36] `_capture_git_state()` 精确异常捕获修复
  | M1: `_GitState` 新增 `_last_git_error` + `_refresh_error_logged`
  | M2: `except Exception` → `ImportError` / `_git_exc` / `OSError(EACCES,EPERM)→raise`
  | M3: `_refresh_git_state()` `pass` → `is_git_repo=False` + 日志风暴控制（首次WARNING后续DEBUG）
  | M4: `runtime_message_source` `except Exception` → `(ValueError, TypeError, RuntimeError)`
  | M5: `completion_guard.check()` `git_state: Any` → `git_state: GitStateLike | None` Protocol
  | 测试: 17/17 PASS + 41 回归 PASS (58/58, zero regressions)

- [x] **P0-11** ✅ ab70813 [web/src/api/client.ts] AbortController 缺失 → Batch 1：apiGet/apiPost 全部透传 signal

- [x] **P0-12** ✅ ab70813 [web/src/components/MessageBubble.tsx] XSS 攻击面 → Batch 1：统一 `<MarkdownRenderer />`（escape-before-format）；Batch 2-5：进一步巩固

- [x] **P0-13** ✅ 59ecec2 [memory/file_backend.py:64] 路径遍历修复
  | `_NAME_PATTERN` 正则校验（仅 `[a-zA-Z0-9_-]{1,128}`）+ `path.resolve()` defense-in-depth 检查

---

## 🟠 P1 — 近期修复（33 项：反模式／架构债）

### agent/core.py — 结构与重复

- [ ] **P1-1** ⏸️ 推迟 [agent/core.py:722-1996] `_run_body()` 1275 行单体函数。
  | **决策**: 架构债，非功能缺陷。需先建立 _run_body 的单元测试基础设施再拆解。P1-2 已完成子部件提取。
  | **风险**: 在不具备测试安全网的情况下拆分核心执行路径，可能引入死锁/状态漂移/回归漏检。

- [x] **P1-2** ✅ 44fae55 [agent/core.py:344-363, 607-714, 895-907] `_finish_run` 嵌套闭包提取
  | `_FinishRunContext` dataclass (12 fields) + `_build_run_result()` 方法
  | 21 call sites replaced. Tests: 3/3 PASS + 68 regression (71/71)

- [x] **P1-3** ✅ 59ecec2 [agent/core.py:2522] Prompt-too-long 恢复逻辑重复修复
  | `_attempt_reactive_compact()` 已提取为独立方法，streaming + classic 双向调用。

- [x] **P1-4** ✅ 662451a [agent/core.py:1427-1448] 统一为 `for _check_fn in (...)` 循环 — fact_check + verify_callback 合并

- [x] **P1-5** ✅ 59ecec2 [agent/core.py:602] `_block_tracker` 哨兵字符串修复
  | 替换为 `CompletionBlockTracker` dataclass（`_last_block_reason` + `_block_count_by_reason` 分离）。

- [x] **P1-6** ✅ 662451a [agent/constants.py] 22 个魔数全部命名完毕 — `BUDGET_COMPACT_PCT`, `DIFF_PREVIEW_MAX_CHARS`, `DEFAULT_REQUEST_BUDGET_TOKENS` 等

- [x] **P1-7** ✅ 662451a [agent/core.py:1247-1261, agent/constants.py:16] `getattr(...32000)` → `DEFAULT_MAX_OUTPUT_TOKENS = 32_000` + `TRUNCATION_BUFFER_TOKENS = 100`

- [x] **P1-8** ✅ 03d78df [agent/core.py:99-106] 模块导入从行 319-326 移至顶部（# deferred import — circular dependency 注释）

- [x] **P1-9** ✅ 4e57e14 [context/history.py:315-323, agent/core.py:1061-1063, 2601-2603, 2568] 私有属性访问消除
  | `ConversationHistory.replace_messages()` 公共方法 + `MemoryContext.store` 属性

### server/ — 架构

- [x] **P1-10** ✅ 59ecec2 [server/services/chat_pipeline.py] `run_chat_async()` 280 行拆分完成
  | 提取为 `ChatPipeline`（6 阶段管道：preflight → model_switch → session_context → permission_inject → build_runtime → execute）

- [x] **P1-11** ✅ 30131b3 [server/routers/sessions.py:41-56, 637-639, 678-680] `asyncio.ensure_future()` 无 loop 守卫修复
  | 提取 `_fire_and_forget_cleanup()` helper（get_running_loop 预检 + RuntimeError 静默）
  | Site A + Site B 统一使用 helper，消除分叉。测试: 4/4 PASS + 64 回归 (68/68)

- [x] **P1-12** ⚠️ [app/storage/sqlite.py:60] `executescript()` 仍在使用，但所有语句都有 `IF NOT EXISTS`。风险降低但未完全消除。

### web/ — 前端（全部已修复）

- [x] **P1-13** ✅ ab70813 [web/src/stores/chatStore.ts] WS connect/disconnect 竞态
  | Batch 5：watchdog + `_wsSessionId` 守卫。

- [x] **P1-14** ✅ ab70813 [web/src/stores/chatStore.ts] 30 分钟超时误杀
  | Batch 5：watchdog 由 WS 终端事件驱动清除（不再在 `api.chat()` 返回时清除）。

### hitl/

- [x] **P1-15** ✅ b583ac4 [hitl/pipeline.py:769-777] ASK 规则在 plan 模式下行为修复
  | plan mode: `_force_interactive` → 直接 DENY（bypass-immune），不再回退到 Layer 6

### agent/session/

- [x] **P1-16** ✅ d841fba [agent/session/session_store.py:44-45] 缺少 WAL 模式 — 已在 P0-1 中修复

- [ ] **P1-17** ⏸️ 推迟 — P1-1 衍生物（文件长是因为 `_run_body` 长，根因相同）

### web/ — 前端可靠性（全部已修复）

- [x] **P1-18** ✅ ab70813 [web/src/components/StatsDashboard.tsx] 错误状态 → 红色 banner + Retry

- [x] **P1-19** ✅ ab70813 [web/src/components/SessionSidebar.tsx] 错误/重试 → 已验证 error 横幅已渲染

- [x] **P1-20** ✅ ab70813 [web/src/components/SessionStatsDrawer.tsx] loading/error → 三态处理

- [x] **P1-21** ✅ ab70813 [web/src/components/DiffReviewView.tsx] 审批竞态 → catch 内联错误 + finally 正确清除

- [x] **P1-22** ✅ ab70813 [web/src/components/ChatView.tsx] updateDraft 闭包 → `latestDraftRef`

- [x] **P1-23** ✅ ab70813 [web/src/App.tsx] ErrorBoundary → 已验证全部组件在边界内

- [x] **P1-24** ✅ ab70813 [web/src/components/SessionSidebar.tsx] 键盘 a11y → `role="button"` + `aria-current`

- [x] **P1-25** ✅ ab70813 [web/src/components/ConfirmModal.tsx] 焦点陷阱 → Tab 循环 + auto-focus

### server/ — 可靠性

- [x] **P1-26** ✅ 59ecec2 [server/main.py:62] Rate limiter 已添加
  | `RateLimiter` 类：token-bucket，chat 10 req/60s/session，其他 60 req/60s/IP

- [x] **P1-27** ✅ 59ecec2 [server/routers/sessions.py:425] RuntimeError → 409（与 P0-5 相同）

- [x] **P1-28** ✅ 59ecec2 [context/artifacts.py:89-90] ArtifactStore 内存限制
  | `max_total_bytes=10_000_000` + `max_content_bytes=1_000_000` + FIFO 逐出

- [x] **P1-29** ✅ 59ecec2 [llm/invoker.py:219] LLM 重试 jitter
  | `random.uniform(0, base * 0.3)` + exponential backoff

- [x] **P1-30** ✅ 59ecec2 [llm/invoker.py:75] LLM 请求超时
  | `_call_with_timeout()` + `ThreadPoolExecutor`，默认 300s

### hitl/ — 权限管线绕过

- [x] **P1-31** ✅ 662451a [hitl/pipeline.py:838-871] `_match_approved_prompt` 单 token 匹配修复
  | >50% token overlap ratio + Bash→Layer 6 强制 + cap 20 + 日志警告。全部三项修复已落地。

- [x] **P1-32** ✅ e039c02 [tools/shell_tool.py:26-56, 219-223, 330-400, hitl/pipeline.py:713-722] Bash sandbox 加固
  | M1: `_BLOCKED_PATTERNS` 8→17 项（新增 find/delete, chmod 000, nvme overwrite, rm /*, rm -r /）
  | M2: `_validate_workspace_paths()` 路径沙箱（绝对路径逃逸 + dotdot≥3 层拒绝）
  | M4: `_ROOT_REMOVAL_PATTERNS` 6→14 项同步
  | M3: 安全边界文档标注（advisory ≠ security boundary）
  | 测试: 19/19 PASS + 45 回归 PASS (64/64, zero regressions)
  | 遗留: 解释器级别绕过（python -c）不可解 — Docker 是真正的安全边界

- [x] **P1-33** ✅ 662451a [core/policy_registry.py:340-375] `_check_tool_call` Bash 命令内容检查修复
  | `_extract_shell_file_targets()` 提取 shell 重定向/命令目标并校验 `allowed_write_paths`（strict_file_scope 模式下）。全部三项修复已落地。

### 🆕 审计遗漏（2026-07-23 核查新增）

- [ ] **P1-34** ⚠️ df4d4fc [memory/store.py, server/services/agent_service.py:274] prune 已实现但启动时同步调用 — 大型记忆库启动可能延迟。改为后台线程即可，估时 30 分钟。

---

## 🟡 P2 — 持续改进（56 项：代码卫生／文档／前端细节）

### agent/core.py — 代码卫生

- [ ] **P2-1** ❌ [agent/core.py:89-90] `_V2_DELEGATION_BLOCK_PREFIX`、`_MAX_STOP_HOOK_RETRIES` 缺少文档
- [ ] **P2-2** ❌ [agent/core.py:572-665] `_run_body` 内 17 个内联 import
- [x] **P2-3** ✅ 61ec3ca [agent/core.py:1326,1593] `import hashlib as _call_hash` → `import hashlib`；`_hlib` → `hashlib`
- [x] **P2-4** ✅ 662451a [agent/constants.py] `"(no thought)"` → `NO_THOUGHT_SENTINEL`（已在 constants.py 中）
- [ ] **P2-5** ❌ [agent/core.py:2264] `_build_recovery_messages() -> list` — 应为 `list[LLMMessage]`
- [ ] **P2-6** ❌ [agent/core.py:2382] 注释与代码逻辑矛盾
- [x] **P2-7** ✅ 61ec3ca [agent/core.py:2570] 空 section header "权限模式切换" 已移除
- [x] **P2-8** ✅ 662451a [agent/core.py:1146] `decision.strip_tools` 实际被使用（line 1146），不是冗余 pass
- [ ] **P2-9** ❌ [agent/core.py:586] `_block_tracker` 命名不准确 — 应为 "计数器"

### core/

- [ ] **P2-10** ❌ [core/base.py:486-504] `ToolRegistry.__init__()` 5 个参数全为 `Any` 类型
- [ ] **P2-11** ❌ [core/base.py:89-93] `_format_error_for_observation()` 名前缀 `_` 不当
- [ ] **P2-12** ❌ [core/circuit_breaker.py:70] `CircuitBreaker` 缺少 `frozen=True`

### web/ — 前端（P2 剩余项）

- [x] **P2-13** ✅ ab70813 [ChatView.tsx] MODEL_OPTIONS → 动态获取（Batch 1）
- [ ] **P2-14** ❌ [ChatView.tsx:86-91] `SUGGESTED_PROMPTS` 硬编码 — 未关联项目上下文
- [x] **P2-15** ✅ ab70813 已提取到 `utils/format.ts`（Batch 1）
- [x] **P2-16** ✅ ab70813 [hooks/useWebSocket.ts] WS 重连逻辑已提取（Batch 1）
- [x] **P2-17** ✅ ab70813 已命名为 `CHAT_TIMEOUT_MS`（Batch 5）

### web/ — 重复代码与类型安全

- [ ] **P2-21** ❌ `summarizeTarget` 在 WsEventBlock / ToolCallCard / ToolApprovalCard 中重复
- [ ] **P2-22** ❌ `formatValue` 在 WsEventBlock / ToolApprovalCard 中重复（但 WsEventBlock 现在 import 自 `utils/format` — 实际仅 ToolApprovalCard 独立实现）
- [x] **P2-23** ✅ ab70813 `renderMarkdown` 重复 → 统一 `<MarkdownRenderer />`（Batch 1）
- [ ] **P2-24** ❌ `summarizeStatus` 与 SessionSidebar `statusLabel` 重复
- [ ] **P2-25** ❌ [chatStore.ts:668-670] WS 消息解析用双 `as unknown as` — 无运行时校验
- [ ] **P2-26** ❌ [SubagentDetail.tsx, SubagentProgress.tsx, SessionTree.tsx] Inline styles 不一致（部分已在 Batch 3 中清理）
- [ ] **P2-27** ❌ [ChatView.tsx] Timeline keys 使用数组 index
- [x] **P2-28** ✅ 已移除 — "Alex Morgan"/"alex@example.com" 地标已删除
- [x] **P2-29** ✅ ab70813 EventSidebar fetch → api layer with signal（Batch 1）
- [x] **P2-30** ✅ 已移除 — `buildOverview` 死代码不再存在于 memory.ts
- [x] **P2-31** ✅ ab70813 HTML 双重转义 → MarkdownRenderer 统一处理（Batch 1）
- [x] **P2-32** ✅ 已修复 — `getSessionSteps()` 返回 `Promise<StepLog[]>` (line 8)
- [ ] **P2-33** ❌ [chatStore.ts:618-629] Plan trace 恢复中 `as unknown as` — 不安全
- [x] **P2-34** ✅ 已移除 — "Share" 按钮不再存在于 App.tsx
- [x] **P2-35** ✅ 已修复 — ThemeToggle 已有 `aria-label="Toggle theme"` (line 24)

### context/ + hooks/ + llm/

- [ ] **P2-18** ❌ [llm/invoker.py] LLM 重试指标未记录到 Langfuse
- [ ] **P2-19** ❌ [hooks/dispatcher.py] Hook 执行无超时保护
- [ ] **P2-36** ❌ [context/compaction.py:1022] MicroCompactor 就地修改输入列表 — 副作用
- [ ] **P2-37** ❌ [context/token_budget.py:62-81] Token 计数遗漏 overhead token
- [ ] **P2-38** ❌ [hooks/dispatcher.py:80-81] Hook 异常静默吞没 — blockable 事件应默认 DENY
- [ ] **P2-39** ❌ [hooks/executor.py:48-52] Hook 超时 60s 过长
- [ ] **P2-40** ❌ [llm/tool_call_validator.py:55-56] 只验证 required fields — 不验证参数类型
- [ ] **P2-41** ❌ [llm/invoker.py:124-125] 重试分类用子串匹配 — `"400"` 误伤
- [x] **P2-42** ❌ [memory/_utils.py:58-63] 临时文件名仅 `os.getpid()` — 同进程双线程可能碰撞
- [x] **P2-43** ✅ df4d4fc [memory/sqlite_backend.py:36] 连接泄漏修复 — `_rows_to_memories()` 批量转换
- [ ] **P2-44** ❌ [memory/context.py:259] 记忆哈希未规范化行尾符

### server/ — 输入校验

- [ ] **P2-45** ❌ [server/routers/sessions.py] Session ID 无格式校验（应为 12-char hex regex）
- [ ] **P2-46** ❌ [server/routers/sessions.py:676] Session settings 接受 `body: dict[str, Any]` — 应用 Pydantic
- [ ] **P2-47** ❌ [server/routers/attachments.py:76] 附件文件名未消毒化
- [ ] **P2-48** ❌ [server/services/session_service.py:93-121] Session 列表对每个 session 加载全部消息 — 性能灾难

### app/storage/

- [x] **P2-20** ✅ 已修复 — `_SESSION_TITLE_MAX_LENGTH` 常量已定义并使用 (line 285)

### hitl/ — 权限管线防御深度

- [ ] **P2-49** ⚠️ [memory/session_memory.py:160,253] Session Memory 绕过 ToolRegistry 直接调用 `tool.execute()`
  | self-enforced `allowed_paths` 限制写入目标。违反 defense-in-depth。未路由通过 ToolRegistry。

- [ ] **P2-50** ⚠️ [agent/session/runtime.py:2014-2026] `bypassPermissions` 传播到子代理
  | 这是 CC-compatible 的 intentional design（父代 bypass → 子代 bypass）。TODO 建议添加可配置上限（cap 为 `acceptEdits`），但这会破坏 CC 兼容性。
  | **决策**: 维持现状；添加文档说明 blast radius。

- [ ] **P2-51** ❌ [hitl/pipeline.py:713-716, tools/shell_tool.py:26] `_ROOT_REMOVAL_PATTERNS`/`_BLOCKED_PATTERNS` 子串匹配可被绕过
  | `find / -delete`、`chmod 000 -R /` 等完全绕过拦截。应作为"提示性护栏"而非安全边界宣传。

- [ ] **P2-52** ❌ [hitl/pipeline.py:311-313] `scoped()` 浅拷贝共享 `_web_confirm_callback`
  | 注释说 "intentionally shared (thread-safe)"；确认 broker 模式隔离充分。

- [x] **P2-53** ✅ 662451a [hitl/pipeline.py:234] cap 20 已通过 P1-31 实现

- [ ] **P2-54** ❌ [agent/session/worktree_manager.py:198-201] Worktree `discard()` 非 TOCTOU 安全

- [ ] **P2-55** ❌ [core/base.py:434-437] Windows `safe_open_for_write` TOCTOU 竞争

### 🆕 审计遗漏（2026-07-23 核查新增）

- [ ] **P2-56** 🆕 [server/services/chat_pipeline.py] 6 阶段管道无集成测试。纯新代码，无回归覆盖。

- [ ] **P2-57** 🆕 [server/routers/plans.py] Plans Library DELETE 端点无 soft-delete — 永久删除不可逆。

- [ ] **P2-58** 🆕 [agent/session/runtime.py:164-167] `shutdown()` 时清空 `_backend_store`/`_active_sessions`/`_approval_brokers`，但未等待进行中的 session 优雅完成。

---

## ✅ 已完成（本轮审计累计 47 项）

### Frontend Audit Batch 1–5 (2026-07-22, ab70813) — 18 项
- P0-11 AbortController · P0-12 XSS 缓解 · P1-13 WS 竞态 · P1-14 超时 · P1-18 StatsDashboard 错误 · P1-19 SessionSidebar 错误 · P1-20 StatsDrawer loading · P1-21 DiffReviewView 竞态 · P1-22 updateDraft 闭包 · P1-23 ErrorBoundary · P1-24 SessionSidebar a11y · P1-25 ConfirmModal 焦点陷阱 · P2-13 MODEL_OPTIONS · P2-15 formatBytes · P2-16 useWebSocket · P2-17 CHAT_TIMEOUT_MS · P2-23 MarkdownRenderer · P2-29 EventSidebar abort · P2-31 双重转义

### Backend Audit (2026-07-22~23) — 24 项
**d841fba:** P0-1 WAL · P1-16 WAL 标记
**59ecec2:** P0-2 per-session backend · P0-3 effective_llm_config · P0-4 try_acquire_session · P0-5 RuntimeError→409 · P0-7 BEGIN IMMEDIATE · P0-9 guard 移除 · P0-13 路径遍历 · P1-3 _attempt_reactive_compact · P1-5 CompletionBlockTracker · P1-10 ChatPipeline · P1-26 RateLimiter · P1-27 RuntimeError→409 · P1-28 ArtifactStore 限制 · P1-29 jitter · P1-30 timeout · P1-31 cap 20
**df4d4fc:** P0-6 索引错误 · P2-43 连接泄漏
**c01e941:** P0-8 break→error injection
**bdbea6f:** PlanView 已保存/已中止计划状态识别 · 计划恢复修复

---

## 📈 P0 列表速览

| # | 文件 | 关键发现 | 状态 |
|---|------|---------|------|
| P0-1 | session_store.py:44 | SessionStore SQLite WAL | ✅ d841fba |
| P0-2 | agent_service.py:118, chat_pipeline.py:168 | Backend 全局单例竞态 | ✅ 59ecec2 |
| P0-3 | agent_service.py:123, chat_pipeline.py:167 | 模型切换丢弃配置 | ✅ 59ecec2 |
| P0-4 | runtime.py:241 | Session TOCTOU | ✅ 59ecec2 |
| P0-5 | sessions.py:425 | RuntimeError → 409 | ✅ 59ecec2 |
| P0-6 | sqlite_backend.py:159 | 索引失败静默 | ✅ df4d4fc |
| P0-7 | sqlite.py:229 | 删除无事务 | ✅ 59ecec2 |
| P0-8 | core.py:1287 | break 误用 | ✅ c01e941 |
| P0-9 | core.py | Guard 异常吞没 | ✅ 59ecec2 |
| P0-10 | core.py:366 | Git 状态异常捕获宽泛 | ❌ |
| P0-11 | api/client.ts | AbortController 缺失 | ✅ ab70813 |
| P0-12 | MessageBubble.tsx | XSS 攻击面 | ✅ ab70813 |
| P0-13 | file_backend.py:64 | 路径遍历 | ✅ 59ecec2 |

**真正剩余的 P0：1 项**（P0-10 — git 异常捕获过宽）+ 1 项部分修复（P0-4 的 HTTP handler 层快速反馈与 runtime 层原子操作的完整性待审计）

---

*本文档随项目演进实时更新。最后修订: 2026-07-23*
