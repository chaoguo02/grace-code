# Grace-Code: 前后端核心流程理解报告

> **版本**: v0.1.0  
> **生成日期**: 2026-07-21  
> **目的**: 作为后续所有开发和重构的"单一事实来源 (SSOT)"  
> **Phase**: 1 — 全局认知与核心流程建模

---

## 目录

1. [项目概览](#1-项目概览)
2. [技术栈](#2-技术栈)
3. [目录结构与模块职责](#3-目录结构与模块职责)
4. [核心流程: ReAct 代理引擎](#4-核心流程-react-代理引擎)
5. [数据流: 前端↔后端↔LLM](#5-数据流-前端↔后端↔llm)
6. [API 契约](#6-api-契约)
7. [状态管理 (前端)](#7-状态管理-前端)
8. [鉴权与权限链路](#8-鉴权与权限链路)
9. [核心业务逻辑](#9-核心业务逻辑)
10. [子系统体系](#10-子系统体系)

---

## 1. 项目概览

**Grace-Code** 是一个 **Claude Code 架构对齐的自主编程智能体框架**。它实现了一个完整的 AI 编程代理系统，包含：

- **ReAct 主循环引擎** — 思考→行动→观察的核心决策循环
- **Web 交互界面 (React + Vite)** — 支持实时流式渲染的聊天式编程助手
- **SessionRuntime V2** — 子代理编排、后台运行、Worktree 隔离
- **MCP 协议支持** — 4 种 transport 的外部工具集成
- **权限管线** — 5 层权限评估，对标 Claude Code 的 permission 系统
- **上下文管理** — 5 层压缩管道 (Budget → Snip → Micro → Collapse → AutoCompact)
- **持久记忆系统** — 基于文件/MEMORY.md 的长期记忆管理

核心对标目标：**Claude Code (CC) 的架构与用户体验**。

---

## 2. 技术栈

### 后端 (Python 3.10+)

| 层 | 技术 | 用途 |
|----|------|------|
| Web 框架 | FastAPI + Uvicorn | REST API + WebSocket 服务 |
| LLM 后端 | DeepSeek / OpenAI / Anthropic | 多 Provider 抽象，统一 backends |
| 数据库 | SQLite (via `SqliteStorageBackend`) | Session/Message/Event 持久化 |
| 配置 | YAML + 环境变量 (python-dotenv) | 多层配置 (user → project → local) |
| 观测 | Langfuse (可选) | LLM Trace + 评分 |
| Git | GitPython | Diff 跟踪、Worktree 管理 |
| MCP | 自研 stdio/HTTP/SSE/WS transport | 外部工具服务器 |

### 前端 (TypeScript + React 19)

| 层 | 技术 | 用途 |
|----|------|------|
| 框架 | React 19 + Vite 6 | SPA 构建 |
| 状态管理 | Zustand 5 | 全局 chatStore + sessionStore |
| 样式 | CSS Variables (自定义主题) | 内置 light/dark 双主题 |
| 实时通信 | WebSocket (原生) | 流式事件推送 |

### 基础设施

| 工具 | 用途 |
|------|------|
| Git Worktree | 子代理文件系统隔离 |
| SQLite | Session 持久化 |
| MCP stdio | 外部工具进程通信 |
| Langfuse | 可观测性 backend |

---

## 3. 目录结构与模块职责

```
grace-code/
├── agent/                       # 🔵 ReAct 引擎 + Session 运行时
│   ├── core.py                  #    ReActAgent 主循环 (2609 行，项目核心)
│   ├── task.py                  #    Task/Action/Event/RunResult 数据模型
│   ├── agent_config.py          #    AgentConfig 配置对象
│   ├── recovery.py              #    恢复状态机 (RecoveryState/Transition)
│   ├── runtime_controller.py    #    运行时控制门 (StepDecision)
│   ├── completion_guard.py      #    完成守卫 (验证 agent 是否真正完成)
│   ├── mode_switching.py        #    Plan/Execute 模式切换
│   ├── run_finalizer.py         #    运行结束处理 (记忆提取)
│   ├── event_log.py             #    事件日志 (JSON lines)
│   ├── context_trimming.py      #    上下文裁剪 (Budget/Snip/Micro)
│   ├── observation_rendering.py #    工具输出格式化
│   ├── mcp/                     #    MCP 子模块
│   │   ├── client.py            #    MCP 客户端 (4 种 transport)
│   │   ├── config.py            #    MCP 配置
│   │   ├── registry.py          #    MCP 服务器注册
│   │   ├── sync_bridge.py       #    同步桥接
│   │   ├── tool_adapter.py      #    工具适配器
│   │   ├── tool_types.py        #    工具类型定义
│   │   ├── types.py             #    MCP 协议类型
│   │   └── allowlist.py         #    MCP 工具白名单
│   └── session/                 #    V2 Session 运行时
│       ├── runtime.py           #    SessionRuntime (子代理编排核心)
│       ├── models.py            #    Session 数据模型
│       ├── agent_definition.py  #    Agent 定义
│       ├── agent_factory.py     #    Agent 工厂 (DI)
│       ├── agent_registry.py    #    AgentRegistryV2
│       ├── run_context.py       #    RunContext / CancellationToken
│       ├── subagent.py          #    子代理执行器
│       ├── session_store.py     #    SQLite Session 存储
│       ├── task_state_machine.py#    任务状态机
│       ├── execution_budget.py  #    执行预算 (token/step 双重限制)
│       └── ...
│
├── core/                        # 🟢 基础设施层
│   ├── base.py                  #    ToolResult / BaseTool / ToolRegistry
│   ├── types.py                 #    核心数据类型 (Action, Observation, ToolMetadata)
│   ├── errors.py                #    错误类型 (ToolError, ToolErrorType, ToolRetryDirective)
│   ├── policy.py                #    任务策略 (TaskPolicy, PhasePolicy)
│   ├── policy_registry.py       #    策略感知工具注册表
│   ├── circuit_breaker.py       #    熔断器 (连续失败检测)
│   ├── streaming_executor.py    #    流式工具执行器 (parallel-safe 分区)
│   ├── project_environment.py   #    项目环境探测 (CapabilitySnapshot)
│   ├── state_paths.py           #    项目状态路径管理
│   ├── process.py               #    进程执行器
│   ├── process_invoker.py       #    进程调用器
│   ├── goal.py                  #    目标系统
│   ├── sibling_abort.py         #    兄弟代理中断
│   ├── utf8.py                  #    UTF-8 工具
│   └── web_utils.py             #    Web 工具
│
├── context/                     # 🟣 上下文管理
│   ├── manager.py               #    ContextManager (统一上下文组装)
│   ├── history.py               #    ConversationHistory / ConversationSnapshot
│   ├── compaction.py            #    ConversationCompactor (LLM 驱动的对话压缩)
│   ├── token_budget.py          #    Token 预算管理
│   ├── artifacts.py             #    ArtifactStore (大输出序列化)
│   ├── collapse.py              #    CollapseStore (折叠压缩)
│   ├── repo_map.py              #    仓库地图 (给 LLM 的项目结构概览)
│   ├── evidence.py              #    证据账本 (结构化结论追踪)
│   ├── stats.py                 #    上下文统计
│   ├── structured.py            #    结构化上下文层
│   └── workspace_facts.py       #    工作区事实
│
├── llm/                         # 🟡 LLM 抽象层
│   ├── base.py                  #    LLMBackend 抽象基类 + LLMMessage/LLMResponse
│   ├── router.py                #    后端路由器 (按 provider 选择 backend)
│   ├── openai_backend.py        #    OpenAI 兼容后端 (DeepSeek/Groq/Ollama)
│   ├── anthropic_backend.py     #    Anthropic 后端 (原生 tool_use)
│   ├── invoker.py               #    LLM 调用器 (retry/cache/prompt_metadata)
│   └── tool_call_validator.py   #    工具调用校验器
│
├── server/                      # 🟠 Web 服务层 (FastAPI)
│   ├── main.py                  #    App 工厂 + CLI 入口
│   ├── events.py                #    WebSocket 事件类型定义
│   ├── services/
│   │   ├── agent_service.py     #    AgentService (核心服务，94KB)
│   │   ├── session_service.py   #    Session 查询服务
│   │   ├── event_bus.py         #    事件总线 (WS 发布)
│   │   ├── approval_broker.py   #    审批代理 (线程同步)
│   │   ├── plan_revision_service.py # Plan 修订管理
│   │   ├── stats_service.py     #    统计服务
│   │   └── stats_recorder.py    #    统计记录器
│   ├── routers/
│   │   ├── sessions.py          #    Session CRUD + Chat API
│   │   ├── websocket.py         #    WebSocket 端点
│   │   ├── approvals.py         #    审批端点
│   │   ├── config.py            #    配置端点
│   │   ├── attachments.py       #    附件端点
│   │   ├── stats.py             #    统计端点
│   │   ├── diffs.py             #    Diff 端点
│   │   └── memory.py            #    记忆端点
│   └── schemas/                 #    API Schema 定义
│
├── web/                         # 🔴 React 前端
│   └── src/
│       ├── App.tsx              #    应用根组件 (Tab 导航)
│       ├── main.tsx             #    React 入口
│       ├── api/                 #    API 客户端
│       │   ├── client.ts        #    HTTP + WS 基础客户端
│       │   ├── sessions.ts      #    Session API
│       │   ├── diffs.ts         #    Diff API
│       │   ├── memory.ts        #    Memory API
│       │   └── stats.ts         #    Stats API
│       ├── stores/
│       │   ├── chatStore.ts     #    Zustand: 聊天/WS 事件/审批/计划 (38KB)
│       │   └── sessionStore.ts  #    Zustand: Session 列表/树
│       ├── components/
│       │   ├── ChatView.tsx     #    聊天视图 (核心组件)
│       │   ├── SessionSidebar.tsx    # Session 侧边栏
│       │   ├── SessionTree.tsx       # Session 树 (父子关系)
│       │   ├── PlanView.tsx          # 计划审批视图
│       │   ├── DiffReviewView.tsx    # Diff 审查视图
│       │   ├── EventSidebar.tsx      # 事件流侧边栏
│       │   ├── ToolApprovalCard.tsx  # 工具审批卡片
│       │   ├── ToolCallCard.tsx      # 工具调用展示
│       │   ├── MessageBubble.tsx     # 消息气泡
│       │   ├── SubagentProgress.tsx  # 子代理进度
│       │   ├── SubagentDetail.tsx    # 子代理详情
│       │   ├── ConfirmModal.tsx      # 确认弹窗
│       │   └── ...
│       └── types/
│           ├── index.ts         #    通用类型
│           ├── events.ts        #    WS 事件类型
│           ├── session.ts       #    Session 类型
│           ├── memory.ts        #    Memory 类型
│           └── stats.ts         #    Stats 类型
│
├── prompts/                     # 🟤 Prompt 工程
│   └── builder.py               #    System Prompt 构建器
│
├── hooks/                       # ⚪ Hook 系统
│   ├── events.py                #    Hook 事件定义
│   ├── dispatcher.py            #    Hook 分发器
│   ├── registry.py              #    Hook 注册表
│   ├── protocol.py              #    Hook 协议 (HookControl)
│   ├── matcher.py               #    Hook 匹配器
│   └── executor.py              #    Hook 执行器
│
├── memory/                      # 🟤 长期记忆
│   ├── store.py                 #    MemoryStore (基于文件系统)
│   ├── context.py               #    MemoryContext
│   ├── session_memory.py        #    SessionMemoryTracker
│   ├── injection_service.py     #    记忆注入服务
│   └── external_store.py        #    外部语义搜索存储
│
├── hitl/                        # 🟡 人机交互 (HITL)
│   ├── pipeline.py              #    权限管线 (5 层评估)
│   └── permission_rule.py       #    权限规则
│
├── config/                      # 🟢 配置
│   ├── default.yaml             #    默认配置
│   └── schema.py                #    配置 Schema
│
├── observability/               # 🔵 可观测性
│   ├── tracing.py               #    追踪
│   ├── models.py                #    观测模型
│   ├── scores.py                #    评分
│   └── datasets.py              #    数据集
│
├── entry/                       # 入口点
│   └── cli.py                   #    CLI 入口
│
├── app/                         # 应用层
│   └── storage/
│       └── sqlite.py            #    SQLite 存储后端
│
└── docs/                        # 文档 (40+ 份架构/审计/计划文档)
    ├── todo.md                  #    活跃 TODO 追踪
    ├── web-audit-report-*.md    #    Web 审计报告
    ├── web-architecture.md      #    Web 架构文档
    └── ...                      #    大量架构与重构计划文档
```

---

## 4. 核心流程: ReAct 代理引擎

### 4.1 主循环 (`agent/core.py` → `ReActAgent.run()`)

```
┌─────────────────────────────────────────────────────────┐
│                     ReActAgent.run()                     │
│                                                         │
│  1. Policy 包裹 (PolicyAwareToolRegistry)                │
│  2. Git 基线捕获 (_capture_git_state)                    │
│  3. 上下文初始化 (ConversationHistory, RepoMap,          │
│     TokenBudget, EvidenceLedger)                         │
│  4. for step in 1..max_steps:                          │
│       ├── Cancellation 检查                             │
│       ├── Circuit Breaker 检查                          │
│       ├── RuntimeController.check() (强制门)            │
│       ├── TSM Guard 评估                                │
│       ├── 上下文裁剪 (Budget → Snip → Micro)            │
│       ├── 上下文折叠 (Collapse)                         │
│       ├── 自动压缩 (AutoCompact, >100% budget)          │
│       ├── 组装 Messages (_build_messages)               │
│       ├── LLM 调用 (streaming 或 classic)               │
│       ├── Action 分发:                                  │
│       │   ├── FINISH   → 完成守卫 → TSM 状态转换 → 返回 │
│       │   ├── GIVE_UP  → 返回失败                       │
│       │   └── TOOL_CALL → StreamingToolExecutor         │
│       │       ├── Batch 去重                            │
│       │       ├── 并发安全分区 (partition_tool_calls)   │
│       │       ├── 逐工具 execute + observability        │
│       │       ├── 权限拦截 (ENVIRONMENT_UNAVAILABLE)    │
│       │       ├── Memory role 处理                      │
│       │       └── Observation 写入历史                  │
│       └── Reflection 触发判断 (test_failed, missing_test)│
│  5. 超出 max_steps → MAX_STEPS 返回                     │
└─────────────────────────────────────────────────────────┘
```

### 4.2 关键设计决策

| 决策 | 说明 |
|------|------|
| **不可变 Turn State** | `AgentTurnState` 每次更新创建新实例 (CC-aligned) |
| **Runtime Controller 强制门** | 在每步 LLM 调用前执行，模型无法覆盖 |
| **TSM (TaskStateMachine)** | Runtime 的中心化任务生命周期管理 |
| **StreamingToolExecutor** | CC-aligned: LLM 流式输出时同步 dispatch 工具调用 |
| **完成守卫** | 4 种 completion check: fact_check → verify_callback → stop_hook → completion_guard |
| **Git 基线** | 运行前捕获 commit → 完成后 diff 增量对比 |

### 4.3 恢复路径 (4 种)

```
RecoveryState 管理 4 种 CC 对齐的恢复路径：
  A. Output Truncation → 8K→64K escalation → resume injection
  B. Prompt Too Long  → 3-tier waterfall (Drain → Full Compact)
  C. Token Budget     → Nudge (remaining tokens reminder)
  D. Reactive Compact → 紧凑后重试 LLM 调用
```

---

## 5. 数据流: 前端↔后端↔LLM

### 5.1 完整请求生命周期

```
用户输入 Prompt (ChatView.tsx)
    │
    ▼
chatStore.sendChat(sessionId, prompt)
    │
    ▼
POST /api/sessions/{id}/chat?prompt=...&intent=...&mode=...
    │
    ▼
server/routers/sessions.py → chat_session()
    │
    ▼
AgentService.run_chat_async(session_id, prompt)
    │
    ├── [HTTP 201] 立即返回 accepted
    │
    ├── [后台线程] _run_and_notify()
    │   ├── _maybe_reload_rules()           # 热加载权限规则
    │   ├── _resolve_mentions(prompt)       # 解析 @path 引用
    │   ├── pop_pending_model()             # 应用待决模型切换
    │   ├── _inject_session_context()       # 注入上轮摘要
    │   ├── build_web_confirm_callback()    # 构建审批回调 (阻塞)
    │   ├── build_stream_callback()         # 构建流式回调 (WS)
    │   └── SessionRuntime.run_session()
    │       ├── 创建/恢复 SessionRecord
    │       ├── 创建 RunContext + CancellationToken
    │       ├── AgentFactory.create_agent()
    │       │   ├── 解析 AgentDefinition (YAML 或内置)
    │       │   ├── 确定 delegation 策略
    │       │   ├── 构建受限 ToolRegistry
    │       │   └── 注入 approval/stream callbacks
    │       └── agent.run(task, event_log)
    │           └── ReActAgent.run() [见 §4.1]
    │
    └── [WS 事件推送] EventBus → WebSocket subscribers
        ├── thought_delta     # 实时思考流
        ├── thought           # 完整思考
        ├── tool_call         # 工具调用
        ├── observation       # 工具输出
        ├── approval_required # 工具审批
        ├── plan_ready        # 计划待审批
        ├── subagent_start    # 子代理启动
        ├── subagent_stop     # 子代理停止
        ├── worktree_resolved # Worktree 操作结果
        └── status: completed/failed
```

### 5.2 WebSocket 事件协议

```
客户端                             服务端
  │                                  │
  │──── WS connect /api/ws/sessions/  │
  │     {id}                        │
  │                                  │
  │◀─── {"type":"thought_delta",     │
  │       "text":"..."}              │  实时流式输出
  │                                  │
  │◀─── {"type":"tool_call",         │
  │       "name":"Read","params":{}} │  工具调用
  │                                  │
  │◀─── {"type":"observation",       │
  │       "output":"..."}            │  工具结果
  │                                  │
  │◀─── {"type":"approval_required", │
  │       "request_id":"...",        │  需审批 (仅在 headless Web 模式)
  │       "tool_name":"Write"}       │
  │                                  │
  │──── POST /api/approvals/resolve  │
  │     {"decision":"allow"}        │
  │                                  │
  │◀─── {"type":"status",            │
  │       "status":"completed"}      │  运行完成
```

### 5.3 流式渲染管线

```
LLM stream (SSE/WebSocket)
    │
    ▼
StreamingToolExecutor
    │  TOOL_USE → enqueue → 立即执行 (parallel-safe 检查)
    │  TEXT_DELTA → stream_callback → WS thought_delta → chatStore → ChatView
    │  FINISH → build Action
    │
    ▼
chatStore.handleWsEvent()
    │  thought_delta → 累积 streamingThought (不清洗 timeline)
    │  thought → 清洗 streamingThought → 加入 timeline
    │  tool_call / observation → 加入 timeline
    │
    ▼
ChatView.tsx 渲染
    ├── MessageBubble (user messages)
    ├── ToolCallCard (工具调用展示)
    ├── ToolApprovalCard (审批卡片)
    ├── SubagentProgress (子代理状态)
    └── 流式思考区 (实时显示 streamingThought)
```

---

## 6. API 契约

### 6.1 REST API 一览

| Method | Path | 用途 |
|--------|------|------|
| `GET` | `/` | React SPA (生产构建) 或静态 HTML |
| `GET` | `/docs` | Swagger UI |
| `POST` | `/api/sessions` | 创建 Session |
| `GET` | `/api/sessions` | 列出 Sessions |
| `GET` | `/api/sessions/{id}` | 获取 Session 详情 |
| `PATCH` | `/api/sessions/{id}` | 更新 Session (model/agent_name) |
| `DELETE` | `/api/sessions/{id}` | 删除 Session |
| `GET` | `/api/sessions/{id}/messages` | 获取消息列表 |
| `GET` | `/api/sessions/{id}/events` | 获取事件追踪 |
| `POST` | `/api/sessions/{id}/chat` | **执行 Agent 循环** (返回 202) |
| `POST` | `/api/sessions/{id}/cancel` | 取消运行 |
| `POST` | `/api/sessions/{id}/compact` | 触发上下文压缩 |
| `POST` | `/api/sessions/{id}/approve` | 审批计划 |
| `POST` | `/api/sessions/{id}/reject` | 拒绝计划 |
| `POST` | `/api/sessions/{id}/save-plan` | 保存计划 |
| `POST` | `/api/sessions/{id}/abort-plan` | 中止计划 |
| `POST` | `/api/approvals/resolve` | 解决工具审批 |
| `GET` | `/api/sessions/{id}/plan-revisions` | 获取计划修订历史 |
| `GET` | `/api/sessions/{id}/diffs` | 获取 Diff |
| `GET` | `/api/sessions/{id}/stats` | Session 统计 |
| `GET` | `/api/stats/overview` | 全局统计概览 |
| `GET` | `/api/config` | 当前配置快照 |
| `GET` | `/api/skills` | 已注册 Skill 列表 |
| `GET` | `/api/memory/{id}` | 获取记忆 |
| `POST` | `/api/memory/{id}/save` | 保存记忆 |
| `POST` | `/api/memory/{id}/delete` | 删除记忆 |
| `POST` | `/api/attachments` | 上传附件 |
| `WS` | `/api/ws/sessions/{id}` | WebSocket 事件流 |

### 6.2 核心 Chat API

```http
POST /api/sessions/{id}/chat?prompt=你的任务描述&intent=edit&mode=build
Response: 202 Accepted
```

- `agent_name` 通过 Session 的 agent_name 字段控制
- `intent`: `edit` | `analysis` | `plan` (默认 edit)
- `mode`: `build` | `plan` (前端 UI 切换)
- 执行结果通过 WebSocket 异步推送，非 HTTP 响应

---

## 7. 状态管理 (前端)

### 7.1 Zustand Store 架构

```
┌─────────────────────────────────────────────────────┐
│                   Zustand Stores                     │
│                                                     │
│  chatStore (useChatStore)                           │
│  ├── sessionStateById: Record<string, SessionUiState>│
│  │   ├── timeline: TimelineItem[]    # 消息+事件    │
│  │   ├── events: WsMessage[]         # 原始 WS 事件 │
│  │   ├── isRunning: boolean                         │
│  │   ├── steps, tokens: number                      │
│  │   ├── error: string | null                       │
│  │   ├── planApproval: PlanApproval | null           │
│  │   ├── toolApprovals: Record<string, ToolApproval> │
│  │   ├── backgroundAgents: Record<string,            │
│  │   │       BackgroundAgentState>                   │
│  │   ├── draft: string              # 用户草稿持久化 │
│  │   └── streamingThought: string   # 实时流缓冲    │
│  ├── ws: WebSocket | null                          │
│  ├── wsConnected: boolean                           │
│  └── _wsSessionId: string | null                    │
│                                                     │
│  sessionStore (useSessionStore)                     │
│  ├── sessions: SessionSummary[]                     │
│  ├── children: Record<string, SessionTreeItem[]>     │
│  ├── activeId: string | null                        │
│  └── sessionDetail: SessionDetail | null             │
└─────────────────────────────────────────────────────┘
```

### 7.2 状态流转

```
sendChat(prompt)
  → isRunning = true
  → streamingThought = ""
  → WS events arrive...
  → status:completed → isRunning = false
```

---

## 8. 鉴权与权限链路

### 8.1 权限管线 (5 层)

```
ToolRegistry.execute_tool(name, params)
  │
  ├── Layer 1: ToolAvailabilityGuard 检查
  │   工具是否物理可用? (MCP 离线 → block)
  │
  ├── Layer 2: Tool.permission_denial_reason()
  │   工具自身安全检查
  │
  ├── Layer 3: PermissionPipeline.check()
  │   ├── 风险分级: NONE → LOW → MEDIUM → HIGH
  │   ├── PermissionRule 匹配 (deny/ask/allow)
  │   ├── Circuit Breaker (3 次连续拒绝 → 终止 session)
  │   ├── PermissionMode (default/acceptEdits/plan/bypass)
  │   └── requiresUserInteraction (强制确认)
  │
  ├── Layer 4: Web Confirm Callback (Web 模式)
  │   ├── WebSocket push approval_required
  │   ├── 阻塞等待 HTTP POST /api/approvals/resolve
  │   └── 超时回退 (默认 60s)
  │
  └── Layer 5: 路径安全 (file tools)
      ├── sanitize_path()    — ../ 清理
      ├── is_path_safe()     — 父目录边界检查
      └── safe_open_for_write() — TOCTOU 保护 (O_NOFOLLOW)
```

### 8.2 权限规则层级

```
优先级 (高→低):
  1. .forge-agent/settings.local.json   (本地, git-ignored)
  2. .forge-agent/settings.json         (项目级, 版本控制)
  3. ~/.forge-agent/settings.json       (用户级)
  4. Builtin defaults                   (只读允许, 破坏性阻止)
```

### 8.3 Policy 感知工具注册表

```
TaskPolicy → PolicyAwareToolRegistry
  |
  ├── ExecutionPhase.READ  → 只允许 read/discover 工具
  ├── ExecutionPhase.PLAN  → 允许 read + write (计划阶段)
  └── ExecutionPhase.EDIT  → 完整工具集
```

---

## 9. 核心业务逻辑

### 9.1 子代理系统 (V2 Delegation)

```
SessionRuntime.run_session()
  │
  ├── [检查] AgentDefinition.delegation
  │   └── DelegationScope
  │       ├── FULL   → 可用 Agent tool (fork + explicit)
  │       ├── LIMITED → 仅 explicit delegation
  │       └── NONE   → 不可委派
  │
  ├── [Agent tool call] → run_child_agent()
  │   ├── Fork 模式: git worktree 隔离
  │   │   ├── managed_worktree (项目 .grace/worktrees/)
  │   │   ├── agent_worktree   (系统 tmp)
  │   │   └── background 执行
  │   ├── Fresh Context: 完整上下文 (默认 100K tokens)
  │   └── Result: <task-notification> XML 注入父消息
  │
  └── [父代理] 接收 task-notification
      ├── _ChildTurnPhase.SYNTHESIS (需合成)
      ├── _ChildTurnPhase.RESOLUTION_PENDING (worktree 需处理)
      └── Agent 工具被暂时撤回 (防止连锁子代理)
```

### 9.2 上下文压缩管道 (5 层)

```
Pre-LLM (每步):
  ├── Layer 1: ToolResultBudget — 截断旧工具输出
  ├── Layer 2: SnipCompact      — 移除冗余 user/tool 消息
  └── Layer 3: MicroCompact     — 清除旧工具输出内容

Mid-context:
  └── Layer 4: ContextCollapse  — 折叠大文件内容为引用 (CollapseStore)

Budget-aware:
  └── Layer 5: AutoCompact      — LLM 驱动的完整对话压缩 (>100% budget)
```

### 9.3 记忆系统

```
长期记忆 (MEMORY.md):
  ├── 文件系统存储 (~/.grace/projects/<hash>/memory/)
  ├── Frontmatter: name, description, metadata
  ├── 支持: user | feedback | project | reference 类型
  └── 注入时机: 每轮系统 prompt 组装时

Session 记忆:
  ├── SessionMemoryTracker: 每 N 步自动提取
  ├── 基于最近文件 + 命令历史 + 上下文摘要
  └── 存储为 MEMORY.md 文件

外部记忆:
  └── ExternalMemoryStore (FastEmbed 语义搜索)
```

### 9.4 任务状态机 (TSM)

```
状态转换:
  CREATED → RUNNING → COMPLETING → COMPLETED
                    ↘ FAILED
                    ↘ CANCELLED

Guards (Runtime 强制):
  ├── circuit_breaker_guard     (熔断 → RUNNING_TO_FAILED)
  ├── consecutive_failures_guard(连续失败 → RUNNING_TO_FAILED)
  ├── git_diff_guard            (无变更 → COMPLETING_TO_COMPLETED)
  └── stop_hook_retry_guard     (retry 超限 → COMPLETING_TO_FAILED)
```

### 9.5 Plan 模式

```
用户: agent_name="plan" 或 intent="analysis"
  │
  ▼
ReActAgent 运行 (受限工具集: read + discover)
  │
  ├── ExitPlanMode tool → plan_contract
  │   ├── goal: 目标
  │   ├── steps: 步骤列表
  │   ├── target_files: 目标文件
  │   └── verification: 验证方式
  │
  ▼
plan_ready WS 事件 → 前端 PlanView
  │
  ├── [Approve] → 注入 PlanContext → run_session(agent_name="build")
  ├── [Reject] → 注入反馈 → 重新规划 (最多 5 次)
  └── [Save/Abort] → 持久化或丢弃计划
```

### 9.6 检查点 (Checkpoint) 系统

```
自愈流程:
  Crash/Loop/Timeout
    → Grace.orchestrator.check_and_recover()
    → Session 检查: DB 状态 vs 内存状态
    → Agent 重启: 重新注入上下文 + 继续
    → 错误分类: BENIGN / RETRY / FATAL
```

### 9.7 工具并发模型

```
StreamingToolExecutor:
  ├── partition_tool_calls() — 按并发安全性分区
  │   ├── SERIAL 工具: 独占一个 batch (如 Write, Bash destructive)
  │   ├── PARALLEL_SAFE: 可共存 batch (如 Read, Grep, ls)
  │   └── 串行 batch 间按顺序执行
  ├── Batch 内去重 (相同 name+params 只执行一次)
  └── 结果按原始顺序收集 (collect() preserves input order)
```

---

## 10. 子系统体系

| 子系统 | 核心模块 | 对标 CC | 成熟度 |
|--------|---------|---------|--------|
| **ReAct 引擎** | `agent/core.py` (2609 行) | ⭐⭐⭐⭐⭐ | 高 |
| **SessionRuntime V2** | `agent/session/runtime.py` | ⭐⭐⭐⭐ | 高 |
| **上下文管理** | `context/` (8 模块) | ⭐⭐⭐⭐ | 中高 |
| **权限管线** | `hitl/pipeline.py` | ⭐⭐⭐⭐⭐ | 高 |
| **MCP 集成** | `agent/mcp/` | ⭐⭐⭐⭐ | 中高 |
| **Hook 系统** | `hooks/` (10 事件) | ⭐⭐⭐⭐ | 中高 |
| **长期记忆** | `memory/` | ⭐⭐⭐ | 中 |
| **Plan 模式** | `agent/session/` + `PlanView` | ⭐⭐⭐ | 中 |
| **可观测性** | `observability/` | ⭐⭐⭐ | 中 |
| **Web 前端** | `web/` (React + Vite) | ⭐⭐⭐ | 中 |
| **WebSocket 事件** | `server/routers/websocket.py` | ⭐⭐⭐⭐ | 中高 |
| **Flow 引擎** | `agent/session/run_context.py` | ⭐⭐ | 低 |
| **工具注册/调度** | `core/base.py` + `streaming_executor.py` | ⭐⭐⭐⭐⭐ | 高 |

---

> **下一步**: Phase 2 将产生深度审计报告和 TODO.md 更新。Phase 3 将对标 Claude Code 最佳实践进行差距分析。

---

*本文档随项目演进实时更新。最后修订: 2026-07-21*
