# Trace 模块 — 后端数据接口设计

## 概述

Trace 模块负责将 agent 的 ReAct 执行过程结构化地暴露给前端。当前已通过 WebSocket 推送实时事件流，但**缺少 RESTful 的历史查询、统计聚合、结构化分组接口**。

本文档从"前端要展示什么数据"出发，倒推后端需要提供哪些接口和数据格式。

---

## 核心数据模型

### Step（执行步骤）

一次 ReAct 循环 = 一个 Step，包含：

```
Step N
├── thought: string          # 模型思考内容
├── tool_calls: ToolCall[]   # 本次调用的工具（可能多个并行）
│   └── ToolCall
│       ├── name: string
│       ├── params: object
│       ├── id: string
│       └── result: ToolResult | null  # 对应的工具执行结果
├── reflection: string | null  # 模型反思（可选）
├── duration_ms: number         # 这一步耗时
└── step_tokens: number         # 这一步消耗的 token
```

### ToolCall 与 ToolResult 的配对关系

```
ToolCall (id="call_abc")  ──→  Observation (tool_call_id="call_abc")
  name: "Read"                    tool_name: "Read"
  params: {path: "..."}           output: "file content..."
                                  status: "success" | "error"
```

当前 WS 流中两者独立推送，前端自行配对。后端应提供**已配对**的结构化数据。

---

## 现有接口 vs 缺口

| 接口 | 现状 | 问题 |
|------|------|------|
| `GET /api/sessions/{id}/events` | ✅ 已存在 | 返回原始 EventLog JSONL，事件是扁平的、未配对的，前端需自行解析和分组 |
| 按 step 分组的事件 | ❌ 无 | 前端需要自己遍历 events 按 step 分组 |
| tool_call + observation 配对 | ❌ 无 | 前端需要自己用 id/tool_call_id 配对 |
| 执行统计汇总 | ❌ 无 | 没有总步骤数、各工具调用次数、token 分布 |
| 子 session trace | ❌ 无 | subagent 的 trace 需要额外查 `GET /events` |
| 步骤级耗时/token | ❌ 无 | EventLog 不记录每步耗时 |

---

## 需要新增/改造的接口

### 1. `GET /api/sessions/{id}/trace` — 结构化执行轨迹（核心）

返回按 step 分组的完整执行轨迹，tool_call 与 observation 已配对。

**Response 200:**

```json
{
  "session_id": "abc123",
  "summary": {
    "total_steps": 7,
    "total_tokens": 15234,
    "total_duration_ms": 45200,
    "status": "completed",
    "tools_used": {
      "Read": 3,
      "Edit": 2,
      "Bash": 1,
      "Glob": 1
    },
    "termination_reason": "max_steps"
  },
  "steps": [
    {
      "step": 1,
      "thought": "I need to read the file first...",
      "tool_calls": [
        {
          "id": "call_001",
          "name": "Read",
          "params": { "path": "src/main.py" },
          "result": {
            "status": "success",
            "output": "def main():\n    print('hello')\n"
          }
        }
      ],
      "reflection": null,
      "duration_ms": 3200,
      "step_tokens": 1250
    },
    {
      "step": 2,
      "thought": "Now I need to edit the file...",
      "tool_calls": [
        {
          "id": "call_002",
          "name": "Edit",
          "params": { "path": "src/main.py", "old_str": "print('hello')", "new_str": "print('world')" },
          "result": {
            "status": "success",
            "output": "Applied edit"
          }
        }
      ],
      "reflection": "The edit was applied successfully.",
      "duration_ms": 5100,
      "step_tokens": 2340
    }
  ],
  "subagents": [
    {
      "child_session_id": "def456",
      "agent_name": "explore",
      "status": "completed",
      "summary": "Found the relevant files"
    }
  ]
}
```

**实现方式**：

从 `EventLog` JSONL 文件读取事件，按 `step` 字段分组：

```
读取 JSONL → 按 step 分组 → 同一 step 内配对 tool_call ↔ observation
          → 计算 duration_ms（相邻 step 的 timestamp 差）
          → 统计 tools_used
```

**文件**：
- `server/services/trace_service.py`（新建）— 核心逻辑
- `server/routers/sessions.py`（修改）— 注册 `/trace` 端点

---

### 2. `GET /api/sessions/{id}/trace/steps` — 仅步骤列表（轻量）

返回精简的步骤列表，不含完整 tool result，适合侧栏导航。

**Response 200:**

```json
{
  "steps": [
    { "step": 1, "tool_names": ["Read"], "status": "success", "duration_ms": 3200, "tokens": 1250 },
    { "step": 2, "tool_names": ["Edit"], "status": "success", "duration_ms": 5100, "tokens": 2340 },
    { "step": 3, "tool_names": ["Bash", "Glob"], "status": "partial", "duration_ms": 8900, "tokens": 4100 }
  ],
  "total_steps": 3,
  "total_duration_ms": 17200,
  "total_tokens": 7690
}
```

---

### 3. `GET /api/sessions/{id}/trace/stats` — 执行统计

**Response 200:**

```json
{
  "total_steps": 7,
  "total_tokens": 15234,
  "total_duration_ms": 45200,
  "status": "completed",
  "tools": {
    "Read": { "calls": 3, "success": 3, "error": 0, "total_tokens": 4500 },
    "Edit": { "calls": 2, "success": 1, "error": 1, "total_tokens": 3800 },
    "Bash": { "calls": 1, "success": 1, "error": 0, "total_tokens": 2100 },
    "Glob": { "calls": 1, "success": 1, "error": 0, "total_tokens": 850 }
  },
  "token_breakdown": {
    "reasoning": 8200,
    "tool_outputs": 5034,
    "system": 2000
  },
  "termination_reason": "task_complete"
}
```

---

### 4. `GET /api/sessions/{id}/trace/events` — 增强版事件流

替代当前的 `GET /api/sessions/{id}/events`，返回已翻译、结构化的 WS 格式事件，而非原始 EventLog payload。

**Response 200:**

```json
{
  "events": [
    { "type": "thought", "step": 1, "content": "I need to...", "timestamp": "..." },
    { "type": "tool_call", "step": 1, "name": "Read", "params": {...}, "id": "call_001", "timestamp": "..." },
    { "type": "observation", "step": 1, "tool_name": "Read", "status": "success", "output": "...", "timestamp": "..." }
  ],
  "total": 42,
  "has_more": false
}
```

**与 WS 流的关系**：返回的格式与 WS 推送的格式一致，前端可直接用同一套渲染逻辑。

---

### 5. `GET /api/sessions/{id}/tree` — Session 树

返回当前 session 及其所有子 session（subagent）的层级结构。

**Response 200:**

```json
{
  "session_id": "abc123",
  "agent_name": "build",
  "status": "completed",
  "children": [
    {
      "session_id": "def456",
      "agent_name": "explore",
      "status": "completed",
      "summary": "Found relevant files in src/",
      "created_at": "...",
      "completed_at": "...",
      "children": []
    },
    {
      "session_id": "ghi789",
      "agent_name": "general",
      "status": "running",
      "summary": "",
      "created_at": "...",
      "completed_at": null,
      "children": []
    }
  ]
}
```

**实现**：从 SessionStore 查询 `parent_id = session_id` 的所有子 session，递归构造树。

---

### 6. `GET /api/sessions/{id}/trace/export?format=json|md` — 导出

将执行轨迹导出为 JSON 或 Markdown 格式。

**Markdown 格式示例**：

```markdown
# Execution Trace: abc123

**Agent**: build · **Status**: completed · **Steps**: 7 · **Tokens**: 15,234

## Step 1
**Thought**: I need to read the file first...
**Tool**: Read → ✅
```json
{ "path": "src/main.py" }
```
**Result**: def main():\n    print('hello')\n

## Step 2
...
```

---

## 数据来源分析

### EventLog JSONL 文件

位置：`{state_dir}/logs/{task_id}.jsonl`

每行一个 JSON 事件，包含 `event_type`, `event_id`, `task_id`, `timestamp`, `payload`。

**可用于 trace 的关键 payload 字段**：

```python
# task_start
payload = {"task": description}

# action
payload = {
    "action": {
        "type": "action",
        "thought": "I need to...",
        "tool_calls": [
            {"id": "call_001", "name": "Read", "params": {"path": "..."}}
        ],
        "action_type": "action" | "finish" | "give_up"
    }
}

# observation
payload = {
    "observation": {
        "tool_name": "Read",
        "status": "success" | "error",
        "output": "...",
        "error": "..."
    },
    "tool_call_id": "call_001"
}

# reflection
payload = {"reason": "The approach seems correct..."}

# subagent_start
payload = {"child_session_id": "def456", "agent_name": "explore"}

# subagent_stop / subagent_complete
payload = {"child_session_id": "def456", "status": "completed", "summary": "..."}

# task_complete
payload = {"summary": "...", "steps": 7, "result": {...}}

# task_failed
payload = {"error": "...", "reason": "..."}
```

### 配对算法

同一 step 内的 tool_call 和 observation 通过 `tool_call_id` 配对：

```python
def pair_tool_calls(events: list[dict]) -> list[Step]:
    """
    1. 按 step 字段分组
    2. 每个 step 内：
       - 找 thought 事件 → step.thought
       - 找 tool_call 事件 → step.tool_calls[]
       - 找 observation 事件，按 tool_call_id 配对到对应的 tool_call
       - 找 reflection 事件 → step.reflection
    3. 计算 duration_ms: 当前 step 的 timestamp - 上个 step 的 timestamp
    4. 统计 step_tokens: 从 LLM 响应的 usage 中提取
    """
```

### token 统计来源

当前 EventLog 不记录每步 token 消耗。有两个方案：

**方案 A**：在 EventLog 中增加 `token_usage` 事件（推荐）
- 每次 LLM 调用后在 EventLog 中写入一条 `llm_call` 类型事件
- 包含 `prompt_tokens`, `completion_tokens`, `total_tokens`, `step`
- 不影响现有事件流

**方案 B**：从 `SessionRecord.summary` 或 `AgentRunResult` 获取总量
- 只能拿到总量，拿不到每步和按工具的分布

---

## 文件改动清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `server/services/trace_service.py` | **新建** | TraceService — 从 EventLog 读取、分组、配对、统计 |
| `server/routers/trace.py` | **新建** | 注册 `/api/sessions/{id}/trace/**` 路由 |
| `server/main.py` | 修改 | 注册 trace router |
| `server/services/agent_service.py` | 修改 | 暴露出 EventLog 目录路径和 session runtime 引用 |
| `server/schemas/trace.py` | **新建** | Pydantic 模型：TraceResponse, StepDetail, ToolCallWithResult, TraceStats 等 |
| `agent/event_log.py` | 修改 | 可选：增加 `llm_call` 事件类型记录每步 token |

---

## API 端点汇总

| 方法 | 路径 | 说明 | 优先级 |
|------|------|------|--------|
| `GET` | `/api/sessions/{id}/trace` | 完整结构化轨迹（steps + tools + subagents） | P0 |
| `GET` | `/api/sessions/{id}/trace/steps` | 精简步骤列表 | P1 |
| `GET` | `/api/sessions/{id}/trace/stats` | 执行统计汇总 | P1 |
| `GET` | `/api/sessions/{id}/trace/events` | 增强版事件流（替代旧 events） | P2 |
| `GET` | `/api/sessions/{id}/tree` | Session 父子树 | P2 |
| `GET` | `/api/sessions/{id}/trace/export` | 导出 JSON/Markdown | P3 |

---

## 验证

```bash
# P0: 完整轨迹
curl -s http://127.0.0.1:8765/api/sessions/{id}/trace | python -m json.tool

# P1: 步骤列表
curl -s http://127.0.0.1:8765/api/sessions/{id}/trace/steps | python -m json.tool

# P1: 统计
curl -s http://127.0.0.1:8765/api/sessions/{id}/trace/stats | python -m json.tool

# P2: 增强事件流
curl -s http://127.0.0.1:8765/api/sessions/{id}/trace/events | python -m json.tool

# P2: Session 树
curl -s http://127.0.0.1:8765/api/sessions/{id}/tree | python -m json.tool

# P3: 导出 Markdown
curl -s "http://127.0.0.1:8765/api/sessions/{id}/trace/export?format=md"
```
