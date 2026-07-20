# Web 模式全链路架构分析

> 从浏览器 → HTTP → FastAPI → SessionRuntime → agent loop → LLM → DB → WebSocket → 浏览器，每一步的完整追踪。

---

## 一、启动链路

```
server/main.py::create_app()
  ├─ load_config() → AppConfig
  ├─ create_backend_from_config(config) → LLMBackend (HTTPS → api.deepseek.com)
  ├─ build_registry(config, repo_path) → ToolRegistry (37 tools)
  │    └─ PermissionPipeline(rules, confirm_callback, approval_mode)
  ├─ AgentRegistryV2(project_dir) → 加载 _BUILTIN_AGENTS + .grace/agents/*.md
  ├─ SessionStore(db_path) → SQLite (sessions + session_messages 表)
  ├─ EventBus(repo_path) → per-session asyncio.Queue + drain tasks
  ├─ AgentService(repo_path) → 单例
  │    ├─ SessionRuntime(store, backend, registry, agent_registry, ...)
  │    │    ├─ _is_web_mode = True
  │    │    ├─ _active_sessions = set() + Lock (TOCTOU guard)
  │    │    ├─ _stream_callbacks = {}
  │    │    ├─ _web_confirm_callbacks = {}
  │    │    └─ _approval_brokers = {}
  │    ├─ PlanRevisionService(storage)
  │    └─ StatsService(storage)
  └─ FastAPI app
       ├─ /api/sessions/* (CRUD + chat + compact + model + settings)
       ├─ /api/ws/sessions/{id} (WebSocket)
       ├─ /api/sessions/{id}/approve|reject|save-plan|abort-plan
       └─ /api/stats/* + /api/memory/* + /api/skills
```

---

## 二、Session 创建 → 空白页面

```
浏览器: 点击 "New Session"
  │
  ├─ POST /api/sessions {agent_name:"build", repo_path:"."}
  │    └─ agent_service.create_session() → storage.create_session()
  │         └─ SQL: INSERT INTO sessions (id, agent_name, status="queued", ...)
  │         └─ 返回 12-char hex session_id
  │
  ├─ GET /api/sessions → 刷新侧边栏列表
  │
  ├─ openSession(new_id)
  │    ├─ GET /api/sessions/{id} → SessionRecord → SessionDetail JSON
  │    └─ set({ activeId: new_id, activeDetail: detail })
  │
  └─ ChatView useEffect(activeId)
       ├─ connectWs(activeId)
       │    └─ WebSocket /api/ws/sessions/{id}
       │         ├─ websocket.accept()
       │         ├─ session_service.get_session(id) → 验证存在
       │         └─ event_bus.subscribe(id, ws)
       │              └─ SessionSubscriber(id, queue, ws_set)
       │                   └─ drain task 启动 → 从 queue 推送到 ws
       │
       ├─ loadTraceEvents(activeId)
       │    └─ GET /api/sessions/{id}/trace/events
       │         └─ session_service.get_events(id)
       │              ├─ glob("{id}_*.jsonl")     ← Layer 1: 文件名前缀
       │              ├─ fallback: glob("*.jsonl") ← 向后兼容
       │              └─ raw.task_id==id OR raw.session_id==id ← Layer 2: 字段过滤
       │              └─ 新 session → 返回 []
       │
       └─ loadMessages(activeId)
            └─ GET /api/sessions/{id}/messages
                 └─ session_service.get_messages(id)
                      └─ SQL: SELECT * FROM session_messages WHERE session_id=?
                      └─ 新 session → 返回 []
```

**关键：** 新 session 空白 = `get_events()` 双层过滤 + `get_messages()` SQL WHERE。

---

## 三、发送消息 → Agent 执行

```
用户输入 "fix the bug" → 点击发送
  │
  ├─ chatStore.sendChat(sessionId, prompt, intent)
  │    ├─ patchSession: isRunning=true, planApproval=null
  │    └─ api.chat(sessionId, prompt, intent, currentMode)
  │
  ├─ POST /api/sessions/{id}/messages {prompt, intent?, agent_name?}
  │    ├─ session_service.get_session(id) → rec
  │    ├─ effective_agent = body.agent_name or rec.agent_name
  │    ├─ if intent=="analysis": effective_agent = "plan"
  │    ├─ if rec.status == RUNNING → 409 Conflict (TOCTOU guard #1)
  │    ├─ event_bus.create_session(id) → 确保订阅者存在
  │    └─ agent_service.run_chat_async(session_id, prompt, agent_name, intent)
  │
  └─ run_chat_async() [同步返回 202，后台线程]
       ├─ runtime.try_acquire_session(id) → True (TOCTOU guard #2)
       ├─ _is_plan = (agent_name=="plan" or intent=="analysis")
       ├─ if _is_plan and agent_name!="plan": agent_name="plan"
       │
       └─ _run_and_notify() [Thread-1]
            ├─ _resolve_mentions(prompt) → @path → [FILE: ...]
            ├─ pop_pending_model / pop_pending_effort / pop_pending_perm
            ├─ _inject_session_context(id) ← session_summary.md (首次)
            ├─ set_web_confirm_callback(id, cb) ← ApprovalBroker
            ├─ set_stream_callback(id, cb) ← WsThoughtDelta
            │
            ├─ runtime.run_session(session_id, agent_name, task_description, ...)
            │    ├─ store.update_status(id, RUNNING)
            │    ├─ AgentFactory.create(agent_name, backend, registry, ...)
            │    │    ├─ spec = agent_registry.get(agent_name)
            │    │    ├─ contract = TaskContract.for_plan|for_build(cfg)
            │    │    ├─ registry = build_registry_for_session(spec, session, ...)
            │    │    │    ├─ base_registry.scoped(exec_ctx) → per-session clone
            │    │    │    ├─ .filtered(declared | mcp_tool_names) → agent tools
            │    │    │    ├─ .excluding_roles({DELEGATE})
            │    │    │    └─ attach_delegation_tools(registry, spec, session, ...)
            │    │    │         ├─ agent_registry.delegatable_by(spec) → children
            │    │    │         ├─ if children: register AgentTool + controls
            │    │    │         └─ register worktree tools if needed
            │    │    └─ agent = ReActAgent(backend, registry, agent_cfg)
            │    │
            │    ├─ pop stream_callback → agent_cfg.stream_callback = cb
            │    ├─ inject permission_mode → pipeline.set_permission_mode(mode)
            │    ├─ history = ConversationHistory(max_messages)
            │    │    └─ injected_msgs + persisted_msgs_from_DB
            │    ├─ Task(task_id=session_id, description, max_steps, budget_tokens)
            │    ├─ EventLog.create(task, log_dir) → {session_id}_{ts}.jsonl
            │    │
            │    └─ agent.run(task, log) [主循环]
            │         └─ for step in 1..max_steps:
            │              ├─ token_budget.check()
            │              ├─ if budget>80%: warn
            │              ├─ if budget>100%: auto-compact (3-tier waterfall)
            │              ├─ ContextCollapse (read-time projection)
            │              ├─ LLM call → stream_callback(text) → WsThoughtDelta
            │              ├─ Parse response → thought + tool_calls
            │              ├─ Control plane: validate tool exists in registry
            │              ├─ Execute tools → observations
            │              ├─ Check _pending_mode_switch (EnterPlanMode/ExitPlanMode)
            │              ├─ Append messages to history
            │              └─ event_callback(event) → EventBus.publish()
            │                   └─ _translate_event → WS message dict
            │                        └─ session_subscriber.publish(msg)
            │                             └─ asyncio.Queue → drain → WebSocket
            │
            ├─ result = RunResult(status, summary, steps_taken, total_tokens, contract)
            ├─ _accumulate_session_stats(id, result) → metadata 累计
            │
            ├─ if _has_plan (_is_plan or bool(result.contract)):
            │    ├─ save plan revision
            │    ├─ clear _pending_plan_contract
            │    └─ emit WsPlanReady(plan_text, contract, revision, result)
            │
            ├─ else: emit WsStatus(status="completed", result={...})
            │
            └─ finally: release_session(id) → TOCTOU guard 释放
```

---

## 四、Plan 审批 → Build 执行

```
浏览器: 点击 "Approve & Build"
  │
  ├─ chatStore.approvePlan(sid)
  │    ├─ guard: !planApproval?.isWaiting → return
  │    ├─ patchSession: isRunning=true, isWaiting=false
  │    └─ api.approveSession(sid, comment)
  │
  ├─ POST /api/sessions/{id}/approve {comment}
  │    ├─ rec = session_service.get_session(id)
  │    ├─ plan_text = rec.summary
  │    ├─ if not plan_text: 400 "No plan found"
  │    ├─ build [PLAN CONTEXT] + comment + plan_text
  │    ├─ storage.append_message(id, [PLAN CONTEXT])
  │    ├─ _clear_plan_metadata(id)
  │    ├─ plan_revisions.mark_status(id, rev+1, "approved")
  │    ├─ session_service.update_agent_name(id, "build")
  │    ├─ event_bus.create_session(id)
  │    └─ run_chat_async(id, plan_context, agent_name="build", intent="edit")
  │         └─ 回到 §三 的 _run_and_notify 流程
  │
  └─ 前端收到 status:completed → planApproval=null

"Save" 按钮:
  └─ POST /api/sessions/{id}/save-plan
       ├─ mark_status("saved")
       ├─ update_agent_name("build")
       └─ 不启动 build ← 与 approve 的唯一区别

"Discard" 按钮:
  └─ POST /api/sessions/{id}/abort-plan
       ├─ mark_status("aborted")
       ├─ _clear_plan_metadata(id)
       └─ 不启动 replan

"Revise" 按钮:
  └─ POST /api/sessions/{id}/reject {reason}
       ├─ mark_status(current_rev+1, "rejected")
       ├─ append_revision(id, summary, parent_rev, change_request)
       ├─ [PLAN REVISION REQUEST] + reason
       ├─ update_plan_revision(rev+1)
       ├─ update_agent_name("plan")
       └─ run_chat_async(id, feedback, agent_name="plan", intent="analysis")
```

---

## 五、Subagent 执行

```
LLM 调用 Agent(subagent_type="explore", description="...", prompt="...")
  │
  ├─ AgentTool.execute(params)
  │    ├─ _plan_from_params → validate subagent_type in allowed names
  │    ├─ _validate_run_context → check delegation token, step limit, phase policy
  │    ├─ _resolve_execution_placement → FOREGROUND/BACKGROUND
  │    ├─ _build_spawn_request → AgentSpawnRequest.named(...)
  │    └─ runtime.spawn_agent(parent_session_id, request, budget, ...)
  │
  └─ runtime_spawn.py::spawn_agent()
       ├─ 校验: cancellation_token, parent_policy, origin, spawn_context
       ├─ child = store.create_session(parent_id, root_id, agent_name, ...)
       │    └─ metadata: entrypoint, agent_kind, budget, policy, snapshot, model, ...
       ├─ _resolve_child_permission_mode → metadata["permission_mode_override"]
       ├─ connect_agent_servers (MCP, if not fork)
       ├─ emit SUBAGENT_START → EventBus → WS → 前端 SubagentProgress
       │
       └─ [FOREGROUND or BACKGROUND thread]
            └─ _execute_child_session()
                 ├─ fork tool contract validation (schemas match)
                 ├─ parent pipeline state snapshot → _inherited_state
                 └─ subagent.py::run_child_agent()
                      ├─ Git worktree isolation (if WORKTREE mode)
                      ├─ build_restricted_registry (named) or inherit (fork)
                      ├─ parent pipeline inheritance → apply_inherited_state
                      ├─ web_confirm_callback injection (bubble to parent WS)
                      ├─ agent.run(task, log)
                      │    └─ event routing: child events → parent session WS
                      └─ _build_fork_result → AgentRunResult
                           └─ finally: _cancellation_tokens.pop, worktree finalize
```

---

## 六、EventBus 数据流

```
SessionRuntime thread (同步)
  │
  ├─ event_callback(event)
  │    └─ EventBus.publish(event)
  │         ├─ _translate_event(event) → list[dict] (WS message dicts)
  │         │    ├─ ACTION → WsThought + WsToolCall + WsStatus
  │         │    ├─ OBSERVATION → WsObservation (+ diff for Edit/Write)
  │         │    ├─ SUBAGENT_START/STOP → WsSubagentStart/WsSubagentStop
  │         │    └─ TASK_COMPLETE/FAILED → WsStatus
  │         ├─ for each msg in translated:
  │         │    ├─ if observation + Edit/Write: compute git diff
  │         │    └─ subscriber.publish(msg)
  │         └─ subscriber = sessions.get(event.session_id)
  │              └─ if has_subscribers: queue.put_nowait(msg)
  │
asyncio event loop
  │
  └─ drain task (per session)
       └─ while not complete:
            └─ msg = await queue.get()
                 └─ for ws in subscribers: await ws.send_json(msg)
```

---

## 七、数据存储

```
SQLite (sessions.db)
  ├─ sessions 表
  │    ├─ id TEXT PRIMARY KEY
  │    ├─ agent_name, status, mode, summary, error
  │    ├─ parent_id, root_id, agent_kind, context_origin
  │    ├─ execution_placement, workspace_mode, agent_depth, generation
  │    ├─ metadata_json TEXT  ← 累计统计: total_tokens, total_steps, round_count
  │    │                        ← plan_revision, permission_mode_override
  │    │                        ← session_context_injected
  │    └─ created_at, updated_at, completed_at
  │
  ├─ session_messages 表
  │    ├─ id, session_id, role, content
  │    ├─ tool_calls_json, tool_call_id, tool_name
  │    └─ created_at
  │
  └─ (其他表: agent_notifications, plan_revisions, stats, memory)

JSONL 文件 (.grace/v2/logs/)
  └─ {session_id}_{timestamp}.jsonl
       └─ 每行: {"event_id","event_type","task_id","session_id","timestamp","payload"}
```

---

## 八、关键不变量 (修复后)

| 不变量 | 机制 |
|--------|------|
| 新 session 空白 | `task_id=session_id` + 双层过滤 `{id}_*.jsonl` + `raw.session_id==id` |
| 同一 session 不并发 | `try_acquire_session` 原子 set + Lock |
| plan 不漏检 | `_has_plan = _is_plan or bool(result.contract)` 覆盖四种组合 |
| 异常路径 plan 不丢失 | except 块也检查 `result.contract` |
| Token 超标自动压缩 | 3-tier waterfall: Snip→Micro→LLM compact，节流 `_auto_compacted` |
| Thought 实时流式 | `stream_callback → WsThoughtDelta → EventBus → WS` |
| 跨轮统计持久化 | metadata 累计 total_tokens/steps/rounds，每次 run_session 后更新 |
| Session 上下文连续 | `session_summary.md` 首次注入，metadata guard 防重复 |
| Subagent 事件不串扰 | 子 agent 事件 routed to parent session WS |
| Subagent 权限继承 | `_resolve_child_permission_mode` + metadata override + pipeline apply |
| 删除 session 资源清理 | cancel token + destroy EventBus + pop brokers/callbacks/tokens |
