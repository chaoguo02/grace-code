# Grace Code 审计 TODO 追踪

> **生成日期**: 2026-07-21  
> **Phase**: 2 — 深度审计  
> **方法论**: Vibe Coding 反模式识别 + 安全审计 + 权限管线审查 + 前端代码质量  
> **理论来源**: Clean Code／Clean Architecture (Robert C. Martin), Claude Code CVE 披露 (CVE-2025-58764/66032/64755/59829), Loop Engineering Patterns (2026), SQLite WAL 官方文档

---

## 📊 统计摘要

| 优先级 | 数量 | 关键领域 |
|--------|------|---------|
| 🔴 P0 (立即修复) | 13 | SQLite 线程安全×3、backend 竞态×2、TOCTOU×2、break 误用、guard swallow、路径遍历、XSS 风险、AbortController 缺失 |
| 🟠 P1 (近期修复) | 33 | 魔数泛滥、_run_body 1470 行、深层嵌套、重复逻辑、异常吞没、前端竞态×5、缺失 error state×3、可访问性×4、限流缺失、LLM 超时/重试风暴×2、ArtifactStore 内存、权限绕过×3 |
| 🟡 P2 (持续改进) | 53 | 内联 import、私有属性访问、UI 硬编码、类型缺失、重复工具函数×5、inline styles、magic numbers×12、dead code、SQLite 连接泄漏、Hook 排序/超时、Pipeline 绕过×6、路径 TOCTOU |
| **总计** | **99** | |

### 按模块分布

| 模块 | P0 | P1 | P2 | 合计 |
|------|----|----|-----|------|
| agent/core.py | 2 | 9 | 8 | 19 |
| server/ (AgentService + routers) | 4 | 3 | 2 | 9 |
| core/ (base.py, circuit_breaker.py) | 0 | 0 | 3 | 3 |
| hitl/ (pipeline.py) | 0 | 1 | 1 | 2 |
| memory/ (sqlite_backend.py) | 1 | 0 | 0 | 1 |
| app/storage/ (sqlite.py) | 1 | 1 | 1 | 3 |
| agent/session/ (session_store, runtime) | 2 | 1 | 0 | 3 |
| web/ (API, stores, components) | 2 | 10 | 20 | 32 |
| context/ + hooks/ + llm/ | 0 | 0 | 2 | 2 |

---

## 🔴 P0 — 立即修复（10 项：安全／数据完整性／逻辑错误）

### 安全与线程

- [ ] **P0-1** [agent/session/session_store.py:42-45] SQLite 连接未启用 WAL 模式 — 并发读写互斥
  | 文件: `agent/session/session_store.py:42`
  ```python
  def _connect(self) -> sqlite3.Connection:
      conn = sqlite3.connect(self._db_path)   # ← 无 WAL、无 busy_timeout
      conn.row_factory = sqlite3.Row
      return conn
  ```
  **问题**: Session store 每调用创建新连接（线程安全），但**没有** `PRAGMA journal_mode=WAL`。同一数据库文件被 `memory/sqlite_backend.py:54`（已正确设 WAL）和 `SessionStore._connect()`（未设）打开，不一致的 journal_mode 在并发读写下产生 `SQLITE_BUSY`。
  **理论**: SQLite WAL 支持单写+多读；无 WAL 时读者阻塞写者。[官方文档](https://runebook.dev/en/docs/sqlite/wal)
  **修复**: 添加 `conn.execute("PRAGMA journal_mode=WAL")` 和 `conn.execute("PRAGMA busy_timeout=10000")`。

- [ ] **P0-2** [server/services/agent_service.py:118,620] LLM Backend 共享可变状态 — 多 session 竞态
  | 文件: `server/services/agent_service.py` (lines 118, 620)
  ```python
  # Line 118: 初始化全局单例
  self._backend = create_backend_from_config({...})
  # Line 620: daemon 线程中 reassign — 非原子！
  self._backend = create_backend_from_config({...})
  ```
  **问题**: `self._backend` 在 daemon 线程 `_run_and_notify()` 中直接 reassign，同一时刻主线程可能正为**另一个 session** 使用同一 `self._backend` 进行 LLM 调用。模型/API key 在调用中途变更 → 数据泄漏或计费错误。
  **理论**: [FastAPI Thread Safety Best Practices (2025)](https://stackoverflow.com/questions/79805542) — "Global singletons risk race conditions. Per-session instances provide isolation."
  **修复**: 将 backend 从 AgentService 移到 SessionRuntime 管理，每次 `run_session()` 创建 scope-local backend。

- [ ] **P0-3** [server/services/agent_service.py:620] 模型切换时 API key/base_url 不会保存，回退到默认值
  | 文件: `server/services/agent_service.py:620-627`
  ```python
  self._backend = create_backend_from_config({
      "provider": _provider or self._config.llm.provider,   # ← 只用静态 config
      "model": _model,
      "api_key": self._config.llm.api_key or None,
      "base_url": self._config.llm.base_url or None,        # ← 上次覆盖丢失
  })
  ```
  **风险场景**: 用户通过 CLI 覆盖 `--base-url=http://custom` → 运行一期 → 切换模型 → base_url 回退到 config/default.yaml 默认值。

- [ ] **P0-4** [server/routers/sessions.py:401-418] Session 执行 TOCTOU — 状态检查与获取之间无原子保证
  | 文件: `server/routers/sessions.py:400-418`
  ```python
  if rec.status == SessionStatus.RUNNING:     # ← Check
      raise HTTPException(409, ...)
  # ...  race window here ...
  service.run_chat_async(...)                  # ← Use
  ```
  **问题**: HTTP handler 中检查 `RUNNING` 与 `run_chat_async()` 调用之间不是原子的。两个并发请求可能**同时通过检查**。`run_chat_async()` 内部的 `try_acquire_session()` 用 `_active_sessions` set + lock 防护，但该 set 与数据库 `SessionStatus.RUNNING` 列可能不同步。
  **修复**: 在 `try_acquire_session()` 中使用 `BEGIN IMMEDIATE` 事务，或使用 `threading.Lock` per session_id。

- [ ] **P0-5** [server/services/agent_service.py:563] `try_acquire_session()` 抛出 `RuntimeError` → HTTP 500 而非 409
  | 文件: `server/services/agent_service.py:563`
  **修复**: catch `RuntimeError` → 转换为 HTTP 409 JSONResponse。

- [ ] **P0-6** [memory/sqlite_backend.py:123,135] 语义搜索索引失败完全静默 — 记忆"保存成功"但不可搜索
  | 文件: `memory/sqlite_backend.py:123,135`
  ```python
  try: self._indexer.index_memory(memory)
  except Exception: pass           # ← 索引器的所有错误在这里消失
  ```
  **修复**: 添加 `_index_error` 状态字段；至少 `logger.warning()`。

- [ ] **P0-7** [app/storage/sqlite.py:225-234] Session/Batch 删除无事务包裹 — 部分删除留下孤儿数据
  | 文件: `app/storage/sqlite.py:225-231` (delete_session), `241-254` (batch_delete)
  ```python
  with self._store._connect() as conn:
      conn.execute("DELETE FROM session_messages WHERE session_id = ?", ...)
      conn.execute("DELETE FROM agent_notifications WHERE parent_session_id = ?", ...)
      # ← 如果这里失败，第一条已执行
  ```
  **修复**: 包裹在 `conn.execute("BEGIN IMMEDIATE")` / `COMMIT` 中。

### 逻辑错误（agent/core.py）

- [ ] **P0-8** [agent/core.py:1294] 控制流 Bug — `break` 退出整个 for-step 循环，应使用 `continue`
  | 文件: `agent/core.py:1294`
  ```python
  # Line 1272-1297: Tool call validation failure path
  if action.action_type == ActionType.TOOL_CALL and action.tool_calls and tools:
      _validation = validate_tool_calls(action.tool_calls, tools)
      if not _validation.valid:
          # ... builds synthetic error observation ...
          observations = [_observation]
          log.log_action(step=step, action=action, ...)
          break    # ← 退出整个 step 循环！ LLM 永远不会看到这个错误
  ```
  **问题**: `break` 跳出 `for step in range(1, max_steps+1):`（line 812），落到 `max_steps` 处理逻辑返回 `MAX_STEPS`，而非让 LLM 看到错误后自修正。
  **修复**: 改为 `continue`，使 LLM 在下一轮看到错误 observation 并自我修正。

- [ ] **P0-9** [agent/core.py:1512-1519] Guard 函数异常被静默吞没 — 安全守卫故障无感知
  | 文件: `agent/core.py:1512-1519`
  ```python
  for _guard_fn in _reflection_guards:
      try:
          _gr = _guard_fn(_guard_ctx)
          if _gr.inject_message:
              _reflection_msg += _gr.inject_message + "\n\n"
      except Exception:
          pass    # ← TSM guard 崩了 → 没人知道
  ```
  **安全性影响**: TSM guard 是 runtime 安全屏障。失败的 guard = 缺失的安全栅栏。
  **修复**: `logger.error("TSM guard function %s failed", _guard_fn.__name__, exc_info=True)`。

- [ ] **P0-11** [web/src/api/client.ts:10-19] API client 无 AbortController — 所有飞行请求在导航切换时泄露
  | 文件: `web/src/api/client.ts:10-19`
  | `request<T>()` 使用裸 `fetch`，无 `signal` 参数。所有调用 API 的组件卸载时**无法取消飞行中的请求**。快速切换 tab/session 时，过时响应覆盖当前状态。
  | **修复**: 为 `request()` 添加 `AbortSignal` 参数，组件在 useEffect cleanup 中中止。

- [ ] **P0-12** [web/src/components/MessageBubble.tsx:80-83] `dangerouslySetInnerHTML` + 正则 markdown → XSS 攻击面
  | 文件: `web/src/components/MessageBubble.tsx:80-83` 和 `MemoryView.tsx:391-394`
  | 两个组件使用 `dangerouslySetInnerHTML` 配合自定义 `renderMarkdown`。虽然 `escapeHtml` 先执行，但正则替换引入的 HTML 顺序可能被精心构造的内容利用。在渲染任意 LLM 输出的聊天 UI 中存在安全风险。
  | **理论**: [Claude Code "Lies-In-The-Loop" attack](https://checkmarx.com/zero-post/bypassing-ai-agent-defenses-with-lies-in-the-loop/) — LLM 输出中的恶意内容可绕过渲染安全措施。
  | **修复**: 使用 DOMPurify 或替换为 markdown-to-JSX 库（无需 raw HTML）。

- [ ] **P0-13** [memory/file_backend.py:53-54] 路径遍历漏洞 — 记忆名称未消毒化
  | 文件: `memory/file_backend.py:53-54`
  ```python
  def _file_path(self, name: str) -> Path:
      return self._store_dir / f"{name}.md"
  ```
  | `name` 参数来自 Web API (`body.name`)，直接拼接到文件路径中，完全未消毒化。`name="../../.env"` → 写入 `store_dir/../../.env.md`，逃逸存储目录。
  | **修复**: 校验 `name` 仅含 `[a-zA-Z0-9_-]` 字符，或 resolve 路径后验证 `resolved.relative_to(self._store_dir)`。

- [ ] **P0-10** [agent/core.py:346-351] `_capture_git_state()` 用过于宽泛的 `except Exception` 捕获所有错误
  | 文件: `agent/core.py:346-351`
  ```python
  try:
      import git
      repo = git.Repo(repo_path)
      ...
  except Exception:    # ← 捕获 ImportError、MemoryError、权限错误等全部混为一谈
      state.is_git_repo = False
  ```
  **修复**: 捕获具体异常 `(ImportError, git.InvalidGitRepositoryError, git.NoSuchPathError)`。

---

## 🟠 P1 — 近期修复（17 项：反模式／架构债）

### agent/core.py — 结构与重复

- [ ] **P1-1** [agent/core.py:519-1989] `_run_body()` 1470 行 — 项目最大单体函数
  | 文件: `agent/core.py:519-1989`
  **问题**: 包含 agent 主循环 + 10+ 个关切的全部代码：setup、pre-step checks、LLM invocation、tool execution、completion、reflection、git tracking、stats、memory。违反单一职责原则 10 倍以上。
  **理论**: [Vibe Coding Anti-Patterns (2025)](https://xebia.com/blog/vibe-coding-github-copilot-maintenance/) — "AI agents tend to place all code into a single Python file with one enormous class."
  **修复方案**:
  - `_setup_run_context()` — lines 519-690
  - `_finish_run()` — lines 692-799 (当前为嵌套闭包，应改为方法)
  - `_pre_step_checks()` — lines 817-920
  - `_pre_llm_trimming()` — lines 930-1014
  - `_invoke_llm()` — lines 1073-1191
  - `_handle_truncation_recovery()` — lines 1199-1229
  - `_handle_finish_action()` — lines 1323-1580
  - `_execute_tool_batch()` — lines 1595-1905
  - `_handle_reflection()` — lines 1929-1968

- [ ] **P1-2** [agent/core.py:692-799] `_finish_run` 为嵌套 107 行闭包 — 不可独立测试
  | 文件: `agent/core.py:692-799`
  **修复**: 提取为 `ReActAgent._build_run_result(...)` 方法，传递一个 `RunState` dataclass。

- [ ] **P1-3** [agent/core.py:1095-1167] Prompt-too-long 恢复逻辑在 streaming + classic 两条路径重复
  | 文件: `agent/core.py:1095-1123` (streaming), `1138-1167` (classic)
  两处是几乎相同的 3-tier waterfall (Drain → Full compact)。约 30 行完全重复。
  **修复**: 提取 `_attempt_reactive_compact(history, total_tokens) -> bool`。

- [ ] **P1-4** [agent/core.py:1344-1423] `fact_check` 和 `verify_callback` 是相同的结构 — 两次重复
  | 文件: `agent/core.py:1344-1382` + `1387-1423`
  **修复**: 提取 `_apply_completion_check(callback_fn, step, total_tokens, ...)`。

- [ ] **P1-5** [agent/core.py:586-587] `_block_tracker` dict 用哨兵字符串 `'_last_reason'` 存状态
  | 文件: `agent/core.py:586-587`
  ```python
  _block_tracker['_last_reason'] = guard_result.blocked_reason
  ```
  如果完成守卫返回的 block_reason 恰好是 `"_last_reason"`，语义冲突。
  **修复**: 分离为 `_last_block_reason: str | None` + `_block_count_by_reason: dict[str, int]`。

- [ ] **P1-6** [agent/core.py:708] Git diff 摘要截断魔数 `3000` 无命名常量
  共 22 个魔数散布各处：详见 [P2-1~P2-8]。

- [ ] **P1-7** [agent/core.py:1203,1210,1213] `getattr(self._cfg, "max_tokens", 32000)` 重复 3 次
  | 文件: `agent/core.py:1203,1210,1213`
  每步循环调用 3 次。计算一次保有局部变量 `_max_tokens` 即可。

- [ ] **P1-8** [agent/core.py:308] 模块导入置于文件中间 — 违反 PEP 8
  | 文件: `agent/core.py:308`
  ```python
  from agent.context_trimming import _snip_history, _ToolResultBudgetState, ...
  ```
  **修复**: 移到文件顶部，添加 `# deferred import — circular dependency` 注释。

- [ ] **P1-9** [agent/core.py:986-987, 1035, 2221, 2510, 2598] 5 处直接访问其他对象的私有属性 `._messages`/`._max`/`._store`/`._reflection_done`
  **修复**: 向 `ConversationHistory`、`TaskStateMachine`、`MemoryContext` 添加公共访问器方法。

### server/ — 架构

- [ ] **P1-10** [server/services/agent_service.py:501-780] `run_chat_async()` 280 行，8+ 个关注点
  职责: prompt 解析、模型切换、权限热加载、session 上下文、stream callback、agent 运行、plan 持久化 → 全部杂糅。
  **修复**: 拆解为 `ChatPipeline` / `ChatExecutionBuilder` 管道模式。

- [ ] **P1-11** [server/routers/sessions.py:660-666] `asyncio.ensure_future()` 在同步上下文中无 loop 保证
  | 文件: `server/routers/sessions.py:662-664`
  在 batch_delete 中调用；如果没有 running event loop → RuntimeError。

- [ ] **P1-12** [app/storage/sqlite.py:59-80] `_init_stats_tables()` 使用 `executescript()` — 无幂等性保证
  | 文件: `app/storage/sqlite.py:58`
  `executescript()` 遇到第一条失败即停止，而非错误恢复。改为多条 `execute()` with `IF NOT EXISTS`。

### web/ — 前端

- [ ] **P1-13** [web/src/components/ChatView.tsx:238-254] WebSocket connect/disconnect 在 useEffect cleanup 中 — activeId 快速切换时泄漏
  | 文件: `web/src/components/ChatView.tsx:238-254`
  `connectWs(activeId)` 创建 WS 并注册回调；cleanup 调用 `disconnectWs()`。但如果在 `onopen` 触发前切换 → 旧 WS 可能被误关。
  **修复**: 在 `connectWs` 内部每个回调用 `if (get()._wsSessionId !== sessionId) return` 守卫。

- [ ] **P1-14** [web/src/stores/chatStore.ts:479-488] 30 分钟硬编码超时 — 长任务误杀
  长任务（如大型重构）被前端强制标记失败，后端仍在正常执行。
  **修复**: 由后端 WS 事件 `status: timeout` 驱动超时判断。

### hitl/

- [ ] **P1-15** [hitl/pipeline.py:444-451] ASK 规则在 plan 模式下行为不一致
  对于非 Write/Edit 工具（如 `Skill`）的 ASK 匹配，会在 plan 模式中继续触发 Layer 6 审批卡片，与 plan 模式理念冲突。
  **修复**: plan 模式分支中添加 `if self._force_interactive: return DENY`。

### agent/session/

- [x] **P1-16** [agent/session/session_store.py:42-45] 缺少 WAL 模式 — ✅ 已在 P0-1 (批次 A) 修复
  所有 SQLite 连接应统一使用 WAL 模式。`memory/sqlite_backend.py` 已正确设置但 `SessionStore` 未设。

- [ ] **P1-17** [agent/core.py:2609] 文件长度 2609 行 — 超过 Clean Code 建议的 600 行上限 4.3 倍
  **修复**: 按 P1-1 的拆分计划执行。

### web/ — 前端可靠性

- [ ] **P1-18** [web/src/components/StatsDashboard.tsx:35-51] 缺少错误状态 — Promise.all 无 .catch
  | 若三个 API 中任意一个失败 → Promise reject → `loading=false`，但无任何错误提示。面板显示空白且用户无感知。

- [ ] **P1-19** [web/src/components/SessionSidebar.tsx:36-265] SessionSidebar 缺少错误/重试状态
  | `sessionStore.error` 字段存在但 Sessionsidebar 从不渲染。若 `loadSessions()` 失败，用户看到"No sessions yet."且无重试机制。

- [ ] **P1-20** [web/src/components/SessionStatsDrawer.tsx:33-49] 缺少 loading/error 状态
  | 抽屉打开时静默获取数据，无 loading indicator。若所有 promise reject，显示全零且无提示。

- [ ] **P1-21** [web/src/components/DiffReviewView.tsx:54-60] 审批提交竞态 — `submittingId` 只防同 diff 重复点击
  | 用户可同时点击 Diff A "Approve" + Diff B "Reject"，两请求并行。若 API 报错，`submittingId` 永久卡住（finally 清除但错误未显示）。

- [ ] **P1-22** [web/src/components/ChatView.tsx:207-212] `updateDraft` 闭包过时 — 本地状态与 store 可能分叉
  | React functional updater 获取最新 state，但 `setStoredDraft(resolved, activeId)` 用闭包中的过时 `draft`。快速更新时 store 草稿落后于本地状态。

- [ ] **P1-23** [web/src/App.tsx:125] EventSidebar 渲染在 ErrorBoundary 外 — 单一异常可崩溃全应用
  | `EventSidebar`、`SessionSidebar`、`SessionTree`（lines 80-81, 125）都在 `<ErrorBoundary>` 包裹外。

- [ ] **P1-24** [web/src/components/SessionSidebar.tsx:150-205] Session 列表项无法键盘操作
  | 每个 session 项是 `<div onClick>` → 缺少 `role="button"`、`tabIndex`、`onKeyDown`。纯键盘用户无法导航。

- [ ] **P1-25** [web/src/components/ConfirmModal.tsx:16] Modal overlay 缺少键盘焦点捕获 + `role="dialog"`
  | Escape 键只在 `!loading` 时有效；无 `aria-modal="true"`；Tab 键可逃离 modal 到背景元素。

### server/ — 可靠性

- [ ] **P1-26** [server/main.py] 全文无频率限制 — 任何端点可能被滥用
  | 无任何 rate limiting (IP/session/token-bucket)。`POST /api/sessions/{id}/chat` 可被高频调用 → 生成无限 agent 线程 → LLM API 额度耗尽 → 拒绝服务。
  | **修复**: 添加 FastAPI middleware 或依赖（如 slowapi），至少限制 chat 创建为 10 req/min/session。

- [ ] **P1-27** [server/routers/sessions.py:400-418] `run_chat_async` 的 RuntimeError 在路由层未处理 → 500 而非 409
  | `run_chat_async()` 内部 `try_acquire_session` 失败抛出 RuntimeError，路由层无 try/except 包裹 → HTTP 500。

- [ ] **P1-28** [context/artifacts.py:91,206] ArtifactStore 无限制内存增长
  | 50 个 artifact 各存储 `full_content`，无单 artifact 大小上限。50 × 1MB = 50MB RAM。磁盘也写无限制 JSON。
  | **修复**: 添加 `max_content_bytes` (100KB/artifact) 和 `max_total_bytes` (10MB)。

- [ ] **P1-29** [llm/invoker.py:76-132] LLM 重试无 jitter — 多 session 部署中雷群效应
  | `delay *= 2`（line 131）无随机化。所有 session 同时发起 → 在相同的 2 的幂时间点重试 → 提供商限流。
  | **修复**: `delay *= 2; delay += random.uniform(0, delay * 0.3)`。

- [ ] **P1-30** [llm/invoker.py:76-93] LLM 调用无请求级超时 — 提供商挂起会无限阻塞
  | `invoke()` 调用 `self.backend.complete()` 无 deadline 执行。若提供商连接挂起 → agent 线程永久阻塞。
  | **修复**: 在 ThreadPoolExecutor 中包裹后端调用，使用安全上限（如 300s）超时。

### hitl/ — 权限管线绕过（来自安全深度审计）

- [ ] **P1-31** [hitl/pipeline.py:813-835] `_match_approved_prompt` 单 token 交集匹配太弱
  | Prompt `"Run the test suite"` 的 tokens `{run, the, test, suite}` 中任意**单个** token 出现在 tool params 中即匹配。模型声明的 `Bash("Run tests")` 会匹配任何包含 "run" 或 "test" 的 Bash 命令，包括 `"rm -rf / # test"`。
  | **严重性**: Plan 审批 → 任意命令执行权限升级。
  | **修复**: (a) 要求 >50% approved tokens 匹配；(b) `_approved_prompts` cap 为 20 条目并随 plan 周期清除；(c) Bash commands 强制 Layer 6 交互审批，不通过 Layer 4.5。

- [ ] **P1-32** [tools/shell_tool.py + core/process.py:398-412] Bash 命令参数不受工作区沙箱限制
  | Subagent worktree 仅通过 `_resolve_cwd()` 限制工作目录。Shell 命令自身可引用绝对路径：`Bash(command="cat /etc/shadow")` 或 `Bash(command="cat", args=["../../../.env"])`。ShellTool 的 `path_parameter=""`，PolicyAwareToolRegistry 和 PermissionPipeline Layer 5 均不检查。
  | **修复**: 短期 — 解析 bash 重定向目标路径并应用路径沙箱；长期 — Docker/Podman 容器化子代理执行。

- [ ] **P1-33** [core/policy_registry.py:223-266] `_check_tool_call` 不检查 Bash 命令内容 — `strict_file_scope` 虚拟限制
  | 配置 `strict_file_scope=True` + `allowed_write_paths=["src/"]` 时，Write/Edit 受策略限制但 Bash 完全绕过。代理可执行 `Bash(command="echo pwned > /tmp/exploit.sh")`。
  | **修复**: 在策略层的 Bash 命令中提取文件目标并校验 `allowed_write_paths`。

---

## 🟡 P2 — 持续改进（53 项：代码卫生／文档／前端细节）

### agent/core.py — 代码卫生

- [ ] **P2-1** [agent/core.py:89-90] `_V2_DELEGATION_BLOCK_PREFIX` 和 `_MAX_STOP_HOOK_RETRIES` 缺少文档 string
- [ ] **P2-2** [agent/core.py:572-665] `_run_body` 内有 17 个内联 import — 掩盖依赖关系
- [ ] **P2-3** [agent/core.py:1240] `import hashlib as _call_hash` — 内联导入 + 误导性别名
- [ ] **P2-4** [agent/core.py:1873] `"(no thought)"` 魔数哨兵字符串
- [ ] **P2-5** [agent/core.py:2264] `_build_recovery_messages() -> list` — 返回 `list[LLMMessage]` 但标注为裸 `list`
- [ ] **P2-6** [agent/core.py:2382] 注释 "legacy_analysis_prompting_disabled is always True" 与代码逻辑矛盾
- [ ] **P2-7** [agent/core.py:2557-2562] 空 section header "权限模式切换" 下没有任何代码
- [ ] **P2-8** [agent/core.py:888-890] 冗余 `if decision.strip_tools: pass` — 仅注释无操作
- [ ] **P2-9** [agent/core.py:586] `_block_tracker` 命名不准确 — 实际为 "计数器" 而非 "追踪器"

### core/

- [ ] **P2-10** [core/base.py:486-504] `ToolRegistry.__init__()` 5 个参数全为 `Any` 类型 — 应使用 Protocol
- [ ] **P2-11** [core/base.py:89-93] `_format_error_for_observation()` 名前缀 `_` 暗示私有但被 `to_observation()` 在同一类调用
- [ ] **P2-12** [core/circuit_breaker.py:70] `CircuitBreaker` 缺少 `frozen=True` — 标注 dataclass 字段不可手动修改

### web/ — 前端

- [ ] **P2-13** [web/src/components/ChatView.tsx:29-33] `MODEL_OPTIONS` 硬编码模型列表 — 应从 `/api/config` 动态获取
- [ ] **P2-14** [web/src/components/ChatView.tsx:86-91] `SUGGESTED_PROMPTS` 硬编码通用提示 — 应与项目上下文关联
- [ ] **P2-15** [web/src/components/ChatView.tsx:109-123] `formatBytes`/`formatRuntime` 应提取为 `utils/format.ts`
- [ ] **P2-16** [web/src/stores/chatStore.ts:655-734] WebSocket 重连逻辑 80 行驻留 store — 提取为 `useWebSocket()` hook
- [ ] **P2-17** [web/src/stores/chatStore.ts:479-488] 超时常量 `30 * 60 * 1000` 应命名 `CHAT_TIMEOUT_MS`

### web/ — 重复代码与类型安全

- [ ] **P2-21** [web/src/components/WsEventBlock.tsx:110-119] `summarizeTarget` 在 3 个组件中重复
  | 相同逻辑: `WsEventBlock.tsx`, `ToolCallCard.tsx:26-31`, `ToolApprovalCard.tsx:43-51` → 提取为 `utils/target.ts`。

- [ ] **P2-22** [web/src/components/WsEventBlock.tsx:101-108] `formatValue` 在 2 个组件中重复
  | 相同逻辑: `WsEventBlock.tsx`, `ToolApprovalCard.tsx:33-39` → 提取为 `utils/format.ts`。

- [ ] **P2-23** [web/src/components/MessageBubble.tsx:11-36] `renderMarkdown` 在 2 个组件中重复
  | `MessageBubble.tsx` 和 `MemoryView.tsx:42-63` 各有独立实现，功能集和转义逻辑不同 → 统一使用单一 markdown 渲染器。

- [ ] **P2-24** [web/src/components/ChatView.tsx:100-107] `summarizeStatus` 与 SessionSidebar `statusLabel` 重复 — 提取。

- [ ] **P2-25** [web/src/stores/chatStore.ts:668-670] WebSocket 消息解析用双 `as unknown as` 强制转换 — 无运行时校验
  | 服务器发送畸形事件（缺失 `type`、未知 type）时不做验证即处理，可能污染 timeline。

- [ ] **P2-26** [web/src/components/SubagentDetail.tsx:84-203] Extensively inline styles — 其他组件用 CSS classes
  | `SubagentDetail`、`SubagentProgress`、`SessionTree` 三大组件全部使用 `style={{...}}`，而其他组件用 CSS 变量。不一致导致主题适配困难。

- [ ] **P2-27** [web/src/components/ChatView.tsx:830-836] Timeline keys 使用数组 index → React reconciliation 错误
  | timeline 项被 prepend/reorder 时，React 无法正确匹配。事件可能显示在错误位置。

- [ ] **P2-28** [web/src/components/SessionSidebar.tsx:230-234] 硬编码用户身份 `"Alex Morgan"` / `"alex@example.com"` — 地标。

- [ ] **P2-29** [web/src/components/EventSidebar.tsx:64-95] EventSidebar 两次 `fetch()` 无 AbortController — 请求在清理后继续。

- [ ] **P2-30** [web/src/api/memory.ts:41-68] `buildOverview` 已定义但从未调用 — 死代码。

- [ ] **P2-31** [web/src/components/ToolCallCard.tsx:65-67] HTML 双重转义 — `escapeHtml()` 在 React JSX 环境中重复转义 → 显示 `&amp;lt;` 而非 `<`。

- [ ] **P2-32** [web/src/api/stats.ts:8] `getSessionSteps() -> Promise<any[]>` — 应该使用 `StepLog[]` 类型。

- [ ] **P2-33** [web/src/stores/chatStore.ts:618-629] Plan trace 恢复中的 `as unknown as` 不安全强制转换 — 事件 shape 变更时静默产生错误数据。

- [ ] **P2-34** [web/src/App.tsx:105-107] "Share" 按钮无 `onClick` — 死 UI。

- [ ] **P2-35** [web/src/components/ThemeToggle.tsx:20-28] 缺少 `aria-label` — 屏幕阅读器无法识别按钮用途。

### context/ + hooks/ + llm/

- [ ] **P2-18** [llm/invoker.py] LLM 调用重试指标未记录到 Langfuse — 添加 `RetryMetrics`
- [ ] **P2-19** [hooks/dispatcher.py] Hook 执行无超时保护 — 一个 hook 挂起会阻塞整个调度
- [ ] **P2-36** [context/compaction.py:1022] MicroCompactor 就地修改输入列表 — 副作用
  | `compact()` 修改调用者传入的列表 `messages[i] = {...}`，使用共享引用的调用者看到数据被静默修改。
- [ ] **P2-37** [context/token_budget.py:62-81] Token 计数遗漏每条消息的 ~5 个 overhead token — 100 轮会话少计 400-700 token
- [ ] **P2-38** [hooks/dispatcher.py:80-81] 内部 Hook 异常静默吞没 — 对于 blockable 事件应默认 DENY
- [ ] **P2-39** [hooks/executor.py:48-52] Hook 超时默认 60s — 多个 hook 线性累积阻塞 agent 线程 → 减至 10s + 总计 30s 上限
- [ ] **P2-40** [llm/tool_call_validator.py:55-56] Tool call validator 只验证 required fields，不验证参数类型 — 声明为 `string` 的参数可传入数组
- [ ] **P2-41** [llm/invoker.py:124-125] 重试分类用子串匹配 — `"400" in exc_str` 会误伤 `"prompt exceeds 400K"` → 应直接检查 HTTP status
- [ ] **P2-42** [memory/_utils.py:58-63] 原子写入临时文件名仅 `os.getpid()` — 同进程双线程写同一记忆时碰撞 → 添加 `threading.get_ident()`
- [ ] **P2-43** [memory/sqlite_backend.py:51-56] 每次 CRUD 操作新建连接 — `list_by_scope()` 100 条记忆产生 100+ 连接
- [ ] **P2-44** [memory/context.py:259] 记忆内容哈希未规范化行尾符 — git autocrlf 可能导致哈希永不匹配

### server/ — 输入校验

- [ ] **P2-45** [server/routers/sessions.py] Session ID 无格式校验 — 文档声明"12-char hex"但参数 `session_id: str` 无 regex 约束
- [ ] **P2-46** [server/routers/sessions.py:676] Session settings 接受 `body: dict[str, Any]` — 应用 Pydantic 校验 effort/permission_mode
- [ ] **P2-47** [server/routers/attachments.py:76] 附件文件名 `file.filename` 未消毒化 — 去路径分隔符 + 拒绝 `..`
- [ ] **P2-48** [server/services/session_service.py:93-121] Session 列表对每个 session 加载全部消息统计消息数 — 50 session × 1000+ 消息 → 性能灾难 → 用 `SELECT COUNT(*)` 或缓存列

### app/storage/

- [ ] **P2-20** [app/storage/sqlite.py:267] `title[:200]` 硬编码截断 — 定义 `_SESSION_TITLE_MAX_LENGTH`

### hitl/ — 权限管线防御深度（来自安全深度审计）

- [ ] **P2-49** [memory/session_memory.py:160,253] Session Memory 绕过 ToolRegistry 直接调用 `tool.execute()`
  | `SessionMemoryTracker` 和 `SessionMemorySubagentRunner` 实例化裸 `FileWriteTool` 并直接调用 `.execute()`，完全绕过 PermissionPipeline。没有 PreToolUse hooks、PostToolUse hooks、capability interception。
  | **缓解**: self-enforced `allowed_paths` 限制写入目标范围。但违反 defense-in-depth 原则。
  | **修复**: 通过 session-scoped ToolRegistry 路由这些调用，确保 hooks 和 pipeline 生效。

- [ ] **P2-50** [agent/session/runtime.py:1947-1949] `bypassPermissions` 无条件传播到所有子代理
  | `_resolve_child_permission_mode()` 强制从父代继承 `bypassPermissions` 到所有子代理。父代的"跳过提示"变成整个子代理树的"无限制访问"。
  | **严重性**: 中等 — 设计如此，但在 bypass 模式下父代单个配置失误即放大 blast radius。
  | **修复**: 为子代理 `bypassPermissions` 添加可配置上限。默认 cap 为 `acceptEdits`（子代理不能比 `acceptEdits` 更宽松）。

- [ ] **P2-51** [hitl/pipeline.py:696-715] `_ROOT_REMOVAL_PATTERNS` 子串匹配可被简单绕过
  | 拦截 `rm -rf / ~` 变体，但 `find / -delete`、`rm -rf --no-preserve-root /`、`cd / && rm -rf .`、`chmod 000 -R /` 全部绕过。同 shell_tool.py 的 `_BLOCKED_PATTERNS`。
  | **修复**: 不作为安全控制向用户宣传。添加文档警告这些检查只作为"提示性护栏"而非安全边界。

- [ ] **P2-52** [hitl/pipeline.py:311-313] `scoped()` 浅拷贝在各 session 间共享 `_web_confirm_callback`
  | 注释说 "intentionally shared (thread-safe)"，但 `for_agent()` 的派生 pipeline 也共享回调。并发子代理同时提示时存在决策错配风险。
  | **缓解**: `_prompt_lock` (RLock) 序列化 TTY 提示。Web 回调使用 per-session broker。
  | **修复**: 确认 broker 模式在各并发生命周期中提供足够隔离；添加集成测试。

- [ ] **P2-53** [hitl/pipeline.py:217-231] `_approved_prompts` 列表无界增长 — 与 Finding P1-31 组合放大
  | 经过 5 个 plan/build 周期的各种已批提示，常见 token 如 "file"、"run"、"test" 全在批准集中。几乎所有 Bash 命令至少命中一个 token。
  | **修复**: Cap 为 20 条目；每个 plan 周期结束时清除；记录警告当超过 50% 已批 prompts 在单次运行中匹配时。

- [ ] **P2-54** [agent/session/worktree_manager.py:198-201] Worktree `discard()` 非 TOCTOU 安全
  | `wt_path.relative_to(self._worktree_root)` 在 `.resolve()` 之后检查 — 而 `.resolve()` 跟随 symlinks。攻击窗口虽小（git 创建真实目录），但模式本身不 TOCTOU 安全。
  | **修复**: 使用 `os.path.realpath()` 进行解析前验证，或使用 `Path.resolve(strict=False)` 然后检查路径组件中无 symlink。

- [ ] **P2-55** [core/base.py:434-437] Windows 上 `safe_open_for_write` 存在 TOCTOU 竞争
  | `p.is_symlink()` 与 `_os.open()` 之间的竞态。POSIX 用 `O_NOFOLLOW` 原子性保护；Windows 无等价物。创建 symlinks 需管理员权限（降低风险）。
  | **修复**: 在代码中添加更显著的警告注释说明此已知限制。长期：Windows 上使用 `CreateFileW` + `FILE_FLAG_OPEN_REPARSE_POINT`。

---

## ✅ 已完成（来自上一轮审计，18 项）

- [x] **P0-1** StatsDashboard `!cancelled` → `cancelled`
- [x] **P0-2** 移除空壳 Tasks/Events Tab
- [x] **P0-4** Draft 跨 session 持久化
- [x] **P1-1** plan_ready 纳入 agent loop 标准事件管道
- [x] **P1-2** Plan 和 Build 分离 session_id
- [x] **P1-3** ChatView 移除硬编码假数据
- [x] **P1-4** tool_summary 序列化边界不一致
- [x] **P1-5** Subagent tool_call 移除回退逻辑
- [x] **P1-6** EventSidebar 移除 3s debounce
- [x] **P1-7** sendChat planApproval 临界窗口加固
- [x] **P2-1** Memory API source 改用 `getattr`
- [x] **P2-2** confirm() → ConfirmModal
- [x] **P2-3** `_wsSessionId` `string` → `string | null`
- [x] **P2-4** Token 估算 `//2` → `//3`
- [x] **P2-5** 删除未使用组件
- [x] **B3-1** WS event_id 去重
- [x] **thought_delta** 实时流式渲染
- [x] 第二轮 StatsRecorder 传入实际 tool_params

---

## 📈 汇总

| 严重度 | 已完成 (旧) | 待处理 (新) | 累计 |
|--------|------------|------------|------|
| P0 | 4 | 13 | 17 |
| P1 | 7 | 33 | 40 |
| P2 | 5 | 53 | 58 |
| 增强 | 2 | 0 | 2 |
| **总计** | **18** | **99** | **117** |

### P0 列表速览

| # | 文件 | 关键发现 |
|---|------|---------|
| P0-1 | session_store.py:42 | SessionStore SQLite 无 WAL 模式 |
| P0-2 | agent_service.py:118,620 | Backend 全局单例在多 daemon 线程中竞态 |
| P0-3 | agent_service.py:620-627 | 模型切换丢弃自定义 api_key/base_url |
| P0-4 | sessions.py:401-418 | Session 创建有 TOCTOU 竞争 |
| P0-5 | agent_service.py:563 | try_acquire_session 报 RuntimeError → HTTP 500 |
| P0-6 | sqlite_backend.py:123 | 语义搜索索引失败静默吞没 |
| P0-7 | sqlite.py:225-234 | Session 删除无事务包裹 |
| P0-8 | core.py:1294 | `break` 误用 — 应改为 `continue` |
| P0-9 | core.py:1512 | TSM guard 异常被静默吞没 |
| P0-10 | core.py:346 | Git 状态捕获过于宽泛 |
| P0-11 | api/client.ts:10 | AbortController 缺失 — 请求泄露 |
| P0-12 | MessageBubble.tsx:80 | dangerouslySetInnerHTML XSS 攻击面 |
| P0-13 | file_backend.py:53 | 路径遍历 — 记忆名称未经消毒化可写入任意文件 |

---

*本文档随项目演进实时更新。最后修订: 2026-07-21*
