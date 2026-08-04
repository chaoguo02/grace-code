# Native Orchestration — 全阶段实现计划（含单路径合并）

> 文档版本：3.4.0（Phase 11 设计完成）
> 当前基线：Phase 0-10 完成，Batch A+B 完成，记忆系统设计就绪
> **v3.4 更新**：新增 [Phase 11 长生命周期 Session](./PHASE11_LONG_LIVED_SESSION_2026-08-04.md)——
> CC async generator 模式，系统提示词+GRACE.md+记忆目录只注入一次，消除每轮 8K token 浪费。

---

## 0. 总览

### 0.0 单路径合并策略（v2.0 新增）

**当前是双路径**：

```
CLI (entry/chat.py)              Web (agent_service.py)
      │                                  │
      ▼                                  ▼
SessionRuntime.run_session()      ChatPipeline
      │                           ├─ coordinator? → AgentRuntime.run()  ← 新
      │                           └─ else → SessionRuntime.run_session() ← 旧
      ▼                                  ▼
┌─────────────────────────────────────────────────┐
│              SessionRuntime (god object)         │
│  owns: store + registry + backend + MCP +       │
│        memory + hooks + governor                │
│                                                  │
│  spawn_agent() → run_child_agent()              │
│    → ReActAgent.run()  ← 旧 child               │
└─────────────────────────────────────────────────┘

═══════════════════════════════════════════════════
独立的另一条线 — 只能跑 parent，不知道 child 存在
═══════════════════════════════════════════════════
assemble() → AgentRuntime.run()
  → NativeStepLoop.execute()
    → NativeBackend.invoke()  ← 新（parent only）
```

**SessionRuntime 是问题根因**：它不是执行器，而是一个持有 9 个依赖的 god object。35 个文件 import 它。两条路径并存的本质是：AgentRuntime 只替换了"执行循环"这一小块，CLI 入口和所有子模块（MCP、memory、skills、governor、child spawn）仍然挂在 SessionRuntime 上。

**目标**：10 个 Phase 后，只剩一条路径：

```
CLI + Web
    │
    ▼
AgentRuntime.run()
    │
    ▼
NativeStepLoop.execute()
    ├─ Model invoke（NativeBackend）
    ├─ Tool execute（ScopedToolPort）
    ├─ Hook gate（_RealHooks）
    ├─ Child spawn（NativeAgentTool → NativeChildRunner）
    │    └─ AgentRuntime.run()  ← recursive, child own ports
    └─ Outcome → NativeChildResult → parent loop
```

### 0.1 CC 的真实模型（唯一基线）

CC 的一切都可以归结为两层：

```
┌─────────────────────────────────────────────┐
│ Layer 1: Agent Definition (.md YAML)         │
│   name, description, tools, disallowedTools, │
│   model, permissionMode, maxTurns,           │
│   skills, mcpServers, memory, hooks,         │
│   background, effort, isolation              │
├─────────────────────────────────────────────┤
│ Layer 2: Agent Tool (runtime tool call)      │
│   Input:  { description, prompt,             │
│             subagent_type, model?,           │
│             run_in_background?, isolation? } │
│   Output: { status, agentId, content,        │
│             totalToolUseCount,               │
│             totalDurationMs, totalTokens }   │
└─────────────────────────────────────────────┘
```

**没有** preset、profile、policy wrapper。Layer 1 声明一切，Layer 2 用最小参数触发，runtime 负责中间所有连接。

### 0.2 Grace Code 现状

```
已实现（Layer 1）：AgentDefinition 完整支持所有 CC 字段
已实现（Native 执行）：AgentRuntime → NativeStepLoop → NativeBackend
已实现（旧执行）：SessionRuntime → spawn_agent → run_child_agent → ReActAgent

断裂：Layer 2 的 Agent 工具在旧路径上运行（ReActAgent），
      NativeStepLoop 完全不知道 "spawn child" 这个操作。
```

### 0.3 全阶段架构目标（单路径）

```
                  CLI + Web (single entry)
                       │
                  assemble()
                       │
    ┌──────────────────┼──────────────────┐
    │ MCP Manager  MemoryContext  Governor │ ← Phase 0 提取
    │ Agent Registry   HookDispatcher     │
    └──────────────────┼──────────────────┘
                       │
                  RuntimePorts (parent)
                       │
              AgentRuntime.run()
                       │
         NativeStepLoop.execute()
              │
    ┌─────────┼──────────┐
    │ Model   │ Hook     │ Tool Exec ─── Agent Tool        (Phase 2)
    │ Invoke  │ Gate     │              │
    └─────────┼──────────┘              │
              │                  ┌──────┴──────┐
              │                  │ Child        │
              │                  │ Context      │ (Phase 3-4)
              │                  │ Skills/Memory│
              │                  │ MCP/Perm     │
              │                  └──────┬──────┘
              │                         │
              │         ┌───────────────┼───────────────┐
              │         │ Background    │ Worktree      │ (Phase 5-6)
              │         └───────────────┼───────────────┘
              │                         │
              │         ┌───────────────┴───────────────┐
              │         │   Fan-out / Chain / Orchestrator │ (Phase 7-9)
              │         └───────────────────────────────┘
              │
       RuntimeOutcome → NativeChildResult → parent loop continues
```

全部在 `RuntimePorts → NativeStepLoop` 一条链上叠加。没有 SessionRuntime，没有 ReActAgent，没有 LLMMessage。

### 0.4 四个维度

| 维度 | 覆盖内容 | 对应 Phase |
|---|---|---|
| **执行契约** (Execution Contract) | Request/Result 数据结构、Agent Tool 注册、工具隔离在 runtime 生效 | 1-2 |
| **上下文** (Context) | Child 初始 conversation、agent system prompt、project rules、model override、MCP 激活、permission mode | 3-4 |
| **运行时与执行方式** (Runtime & Execution Modes) | 同步/后台、worktree 隔离、取消传播、超时 | 5-6 |
| **拓扑与编排** (Topology & Orchestration) | 并行 ToolCallBatch、chain（StepLoop 自然行为）、Orchestrator AgentDefinition | 7-9 |

---

## Phase 0：父执行单路径合并（CLI + Web → AgentRuntime）

> 依赖：无（当前代码已具备条件）
> 状态：PLANNING
> **v2.0 新增**：用户要求只留一条执行路径

### 做什么
消除 SessionRuntime 在父执行路径中的所有使用。CLI 和 Web 的用户请求只经过 `AgentRuntime.run()`。

### 0.1 SessionRuntime 职责拆分

`SessionRuntime.__init__` 接受 9 个依赖。Phase 0 将这些依赖提升为 `assemble()` 能直接构造的独立组件：

| SessionRuntime 持有的 | Phase 0 后位置 | 说明 |
|---|---|---|
| `store` (SessionStore) | `assemble()` 独立构造 | 已存在 |
| `backend` (LLMBackend) | `NativeBackend` | 已接线 |
| `base_registry` (ToolRegistry) | `_RealTools` | 已接线 |
| `agent_registry` (AgentRegistryV2) | 独立构造，传入 NativeAgentTool | 需提取 |
| `root_agent_config` (AgentConfig) | 拆散为分散配置 | 需拆分 |
| `mcp_integration` | 独立 MCP Manager | 需提取 |
| `memory_context` | 独立 MemoryContext | 需提取 |
| `hook_dispatcher` | `_RealHooks` | 已接线 |
| `governor` | 独立 ResourceGovernor | 需提取 |

### 0.2 子任务

**0a：Web 单路径（最小可行，优先做）**

`ChatPipeline` 当前 gate：
```python
if self._ports.coordinator is not None:
    return self._execute_native(request, prepared)
# fall through to SessionRuntime.run_session()
```

改为：// 始终走 Native 路径。去除 `_runtime` 属性（返回 `SessionRuntime`）。

`agent_service.py` 不再构造 `SessionRuntime`，改为接收 `ApplicationComponents`（assemble 产物）。

**0b：CLI 单路径**

`entry/chat.py` 的 `ChatSession` 不再构造 `SessionRuntime`：
```python
# 旧
self._runtime = SessionRuntime(store=store, backend=backend, ...)
result = self._runtime.run_session(session_id=..., agent_name=..., ...)

# 新
self._runtime = AgentRuntime(ports)  # assemble() 产物
result = self._execute_turn(...)     # 构建 RuntimeExecution → runtime.run(ctx)
```

`entry/modes/v2_runner.py` 同理。

**0c：跨轮状态管理**

SessionRuntime 管理跨轮 history 注入、metadata、auto-compact。映射：

| SessionRuntime 功能 | 新路径 |
|---|---|
| 跨轮 messages 注入 | `ConversationStore.rebuild_conversation()` + `ConversationSnapshot` |
| Session metadata（round_count） | SessionService（已存在） |
| Auto-compact | 独立 CompactionTrigger |
| Plan 节流 | PlanRevisionStore（已存在） |
| Hook SESSION_START | HookDispatcher.dispatch()（已存在） |

### 0.3 分步策略

```text
Phase 0a（已完成 ✅ 2026-08-04）：
  Web 单路径 + ChatPipeline 去 gate + agent_service 去 gate
  → 验证通过：28 tests passed，run_session() 零命中

Phase 0b（→ 合并到 Phase 10 完成 ✅）：
  CLI 单路径 — entry/cli.py 已在 Phase 10 点 3 接线 AgentRuntime
```

Phase 0a 先做的理由：`ChatPipeline._execute_native()` 已经工作，风险最低。CLI 迁移涉及 `ChatSession` 的 500+ 行 `run_session()` 逻辑重写，等 child 路径稳定（Phase 3）后再做更安全。

### 0.4 文件范围（Phase 0a 实际改动）

| 文件 | 动作 | 实际改动 |
|---|---|---|
| `server/services/chat_pipeline.py` | 修改：去 gate，execute() 始终走 _execute_native() | -55 行（旧 run_session 路径），coordinator 字段上移 |
| `server/services/agent_service.py` | 修改：chat() + run_chat_async() 去 gate | -2 行（chat）+-3 行（run_chat_async 去 Optional） |
| `tests/runtime_core/test_native_persist_roundtrip.py` | 修改：传 mock coordinator 替代 None | +2 行 |

**保留不动**（Phase 0a 不改，后续 Phase 处理）：

| 不改 | 原因 |
|---|---|
| `self._runtime` property | `submit_user_prompt`（hook dispatch）、`apply_model_switch`、`_build_callbacks`、cleanup 仍使用 |
| `self._runtime.set_text_stream_callbacks()` | 仍在设置，native 路径暂不消费（Phase 3） |
| `self._runtime.try_acquire_session()` | SessionRuntime 仍在内存中（Phase 0b） |
| SessionRuntime 构造（`_build_session_runtime`） | callback/hook/review_service 依赖（Phase 0b） |
| CLI（`entry/chat.py`） | Phase 0b |

### 0.5 Phase 0a 验证策略

**BT-0a-1 和 BT-0a-2 移除**。原因：
- `test_chat_pipeline_native_conversation.py` 已覆盖 native conversation 路径
- `test_native_persist_roundtrip.py` 已覆盖跨轮 persist 路径
- "不走 SessionRuntime.run_session()" 通过 `rg` 静态检查验证——比测试更可靠

**BT-0a-3（跨轮 history 注入）移到 Phase 3**。原因：Phase 3 补齐 context/system prompt/skills/memory 后，多轮 conversation 才有完整测试场景。当前 native 路径的文本流式输出和 context 注入是已知 gap。

**Phase 0a 实际验证结果**：

```bash
# 全部通过
tests/integration/test_chat_pipeline_native_conversation.py  2 passed
tests/runtime_core/test_native_persist_roundtrip.py          3 passed
tests/composition/test_native_object_graph.py               23 passed

# 静态检查
rg "run_session\(" server/services/chat_pipeline.py   # 仅注释
rg "run_session\(" server/services/agent_service.py   # 仅注释  
rg "coordinator=None" --glob="*.py"                   # 零命中
```

### 0.6 不在此 Phase

- Child/spawn（Phase 1-2）
- MCP/skills/memory 新路径（Phase 3-4）：这些组件被**提取**为独立对象，但执行逻辑暂不改
- Background/worktree（Phase 5-6）

---

## Phase 1：Native Child Contract + Runner

> 状态：**COMPLETED** ✅ 2026-08-04
> 详细计划：[NATIVE_ORCHESTRATION_PHASE1_CC_ALIGNED_PLAN_2026-08-04.md](./NATIVE_ORCHESTRATION_PHASE1_CC_ALIGNED_PLAN_2026-08-04.md)

### 做什么
定义对标 CC Agent Tool 的最小数据契约，实现 native 路径上的 child 执行器，打通第一条 child 链路。

### 关键产出
- `NativeChildRequest`：7 字段，逐字段对 CC
- `NativeChildResult`：8 字段（6 CC + 2 Grace Code 扩展）
- `run_native_child()`：用 fake backend 完成一次 child 执行
- `filter_tool_schemas()`：parent schemas → child schemas
- `child_runtime_ports()`：parent ports → child ports
- `build_child_conversation()`：AgentDefinition + prompt → NativeConversation

### 与 CC 的对应关系
```
CC Agent Tool input       → NativeChildRequest
CC Agent Tool output      → NativeChildResult
CC named subagent (fresh) → run_native_child(fresh conversation)
CC tools/disallowedTools  → filter_tool_schemas()
```

### 不在此 Phase
- 真实的 Agent 工具注册（Phase 2）
- System prompt / skills / memory 构造（Phase 3）
- 后台执行（Phase 5）
- Worktree 隔离（Phase 6）
- Model override 运行时生效（Phase 3）

### 文件范围
| 文件 | 动作 |
|---|---|
| `runtime_core/native_child_contract.py` | 新增 |
| `runtime_core/native_child_runner.py` | 新增 |
| `tests/runtime_core/test_native_child.py` | 新增 |
| `application/coordinators/orchestration_contracts.py` | 删除 |
| `tests/application/test_orchestration_contracts.py` | 删除 |

### Before Tests
BT-1 ∼ BT-7（详见 Phase 1 计划文档）

---

## Phase 2：Agent Tool 注册 + 工具隔离强制执行

> 依赖：Phase 1
> 状态：**COMPLETED** ✅ 2026-08-04

### 做什么
让 NativeStepLoop 能够调用 Agent 工具。当一个 child 被 spawn，它的工具集必须严格遵循 `AgentDefinition.tools ∩ parent_tools − AgentDefinition.disallowedTools`。

### 2.1 Agent 工具在 Native 路径的注册

当前 `_RealTools` 持有所有工具的 registry（通过 `tool_registry` 参数传入 `assemble()`）。Phase 2 需要：

1. **新增 `NativeAgentTool`**：一个实现 native `execute()` 接口的工具类，对标 CC Agent Tool schema。不是继承旧的 `AgentTool(BaseTool)`，而是全新实现，依赖 `AgentRuntime` 而非 `SessionRuntime`。

```python
class NativeAgentTool:
    """Agent delegation tool for the Native execution path.
    
    输入对标 CC Agent Tool: description, prompt, subagent_type, model?, run_in_background?, isolation?
    输出对标 CC Agent Tool: status, agentId, content, totalToolUseCount, totalDurationMs, totalTokens
    
    不在 NativeAgentTool 中做：tool filtering（child runner 做）、
    context building（child runner 做）、permission check（前置 hook 做）。
    """
    
    def __init__(self, definition_registry, parent_ports, parent_backend):
        self._definitions = definition_registry      # name → AgentDefinition
        self._parent_ports = parent_ports
        self._parent_backend = parent_backend
    
    def execute(self, params: dict) -> ToolResult:
        """CC Agent Tool 的 Native 实现。
        
        1. 解析 params → NativeChildRequest
        2. resolve AgentDefinition
        3. child_runtime_ports(parent_ports, definition)
        4. filter_tool_schemas(...)
        5. NativeBackend.for_child(parent_backend, filtered_schemas, model)
        6. build_child_conversation(definition, prompt)  [Phase 1 实现]
        7. run_native_child(child_ports, ...)             [Phase 1 实现]
        8. → NativeChildResult → 格式化输出
        """
```

2. **在 `assemble()` 中注册**：构造 `_RealTools` 后，将 `NativeAgentTool` 注册进去。条件：agent definition 的 `delegation_policy` 允许。

### 2.2 工具隔离在 Runtime 强制执行

Phase 1 的 `filter_tool_schemas()` 确保 child backend 只认识 allowed 工具。但这不够——child 的 `_RealTools.execute()` 也需要强制执行隔离。

```python
# runtime_core/native_child_runner.py

class ScopedToolPort:
    """ToolPort 的 child 适配器——强制执行工具白名单。"""
    
    def __init__(self, parent_tool_port, allowed: frozenset[str]):
        self._parent = parent_tool_port
        self._allowed = allowed
    
    def execute(self, tool_name, params, invocation_id=""):
        if tool_name not in self._allowed:
            return ToolDenied(
                tool_name=tool_name,
                reason=f"Tool '{tool_name}' is not allowed for this subagent",
            )
        return self._parent.execute(tool_name, params, invocation_id)
```

`child_runtime_ports()` 将 parent 的 `tools` port 包装为 `ScopedToolPort`。

### 2.3 与 CC 的对应关系

```
CC Agent Tool schema         → NativeAgentTool.parameters_schema
CC tools (allowlist)         → ScopedToolPort.allowed
CC disallowedTools (denylist)→ filter_tool_schemas 在 backend 层移除
CC 工具由 Agent Tool 触发    → NativeAgentTool.execute() → run_native_child()
```

### 2.4 文件范围

| 文件 | 动作 |
|---|---|
| `runtime_core/native_agent_tool.py` | 新增：`NativeAgentTool` |
| `composition/runtime_composition.py` | 修改：注册 NativeAgentTool 到 _RealTools |
| `tests/runtime_core/test_native_agent_tool.py` | 新增 |

### 2.5 验证结果

**BT-8 (schema 对 CC)** ✅ — `NativeAgentTool.parameters_schema` 含 6 个 CC 字段，不含 v1.x 编造字段。  
**BT-9 (端到端 spawn)** ✅ — `NativeAgentTool.execute()` → resolve definition → `run_native_child()` → 返回 `NativeChildResult` JSON。  
**BT-10 (旧路径零引用)** ✅ — `native_agent_tool.py` 无 `SessionRuntime|ReActAgent|LLMMessage` import。

额外：`_ScopedToolPort` 已在 Phase 1 实现（`native_child_runner.py`），工具隔离强制执行在 Phase 2 无需额外代码。

### 2.6 文件范围（实际改动）

| 文件 | 动作 | 行数 |
|---|---|---|
| `runtime_core/native_agent_tool.py` | **新增** | ~215 |
| `runtime_core/native_backend.py` | **修改**：新增 `tool_schemas` 属性 | +4 |
| `composition/runtime_composition.py` | **修改**：注册 NativeAgentTool | +17 |
| `tests/runtime_core/test_native_child.py` | **修改**：BT-8/9/10 + BT-7 扩展 | ~100 |

### 2.7 与计划的差异

- 原 BT-9（`ScopedToolPort` deny 测试）已在 Phase 1 通过 BT-5 覆盖，未重复
- BT-8/9/10 编号与原计划一致，但具体内容调整为可执行的 fake backend 测试
- 原计划的 BT-9 实际变为 BT-8 (schema) + BT-9 (端到端)；BT-10 从原 "Agent 工具→child 执行" 改为 "旧路径零引用"

---

## Phase 3：Child Context — Project Rules + Model Override

> 依赖：Phase 1
> 状态：**COMPLETED** ✅ 2026-08-04

### 调研结论（v2.4）

1. **CC 的 Skills 不是预加载到 system prompt**。Skills 是 tool-invoked：启动时只注册 name+description 作为可用工具，模型调用 `Skill` 工具时 SKILL.md body 作为 `tool_result` 注入。Grace Code 已有 `Skill` 工具——无需额外实现。
2. **CC 的原生 Memory 只有两项**：(a) `CLAUDE.md` 作为 `<system-reminder>` 在每个 user message 中注入，自动传播到 subagent；(b) auto-memory (`MEMORY.md`) 前 ~25KB 在会话启动时加载。没有 "user/project/local memory scope" 概念——那是第三方插件。Grace Code 对齐：用 `GRACE.md` 替代 `CLAUDE.md`。
3. **CC Agent body 替换默认 system prompt**，不是叠加。Phase 1 的 `build_child_conversation` 已正确实现。
4. **Skills 预加载是 Grace Code 旧路径独有的概念**（`runtime_prompt_builder.py` 的 `_load_skills`），Native 路径不继承。

### 做什么

Phase 1 的 `build_child_conversation` 已有：agent system prompt + subagent protocol + task prompt。Phase 3 加两件事：

1. **项目规则注入**——对齐 CC 的 CLAUDE.md `<system-reminder>` 传播。Grace Code 用 `GRACE.md` 直接替代，读取 `<project>/.grace/GRACE.md`，注入到 child conversation。
2. **Model override 运行时生效**——`NativeChildRequest.model` → child 用不同 backend 执行。

### 3.1 项目规则注入

CC 行为：project 的 `CLAUDE.md` 作为 `<system-reminder>` block 注入到**每一条 user message**，包括 subagent 的。Grace Code 对齐：用 `GRACE.md` 替代，同样注入 child conversation。

```python
# runtime_core/native_child_runner.py — 扩展 build_child_conversation

def build_child_conversation(
    definition: AgentDefinition,
    prompt: str,
    description: str = "",
    *,
    project_dir: str = "",
) -> NativeConversation:
    """构造 child 的初始 NativeConversation — CC-aligned。

    CC 对齐的上下文构成：
    1. Agent system prompt (definition.body, 替换默认 system prompt)
    2. Project rules (.grace/GRACE.md, 对齐 CC CLAUDE.md <system-reminder>)
    3. Subagent protocol rules (运行时行为约束)
    4. User turn: description + prompt
    """
    messages: list[NativeMessage] = []

    # 1. System prompt — agent body REPLACES default (CC priority chain)
    if definition.system_prompt:
        messages.append(NativeMessage.system(definition.system_prompt))

    # 2. Project rules — CC CLAUDE.md <system-reminder> 机制, Grace Code 用 GRACE.md
    if project_dir:
        rules = _load_project_rules(project_dir)
        if rules:
            messages.append(NativeMessage.system(rules))

    # 3. Subagent protocol
    messages.append(NativeMessage.user(_SUBAGENT_PROTOCOL))

    # 4. User turn
    task = _format_task_message(description, prompt)
    messages.append(NativeMessage.user(task))

    return NativeConversation.from_messages(messages)
```

### 3.2 项目规则加载

```python
# runtime_core/native_child_context.py — 新增

def _load_project_rules(project_dir: str) -> str:
    """加载项目规则文件 `.grace/GRACE.md`，对齐 CC CLAUDE.md <system-reminder> 机制。

    Grace Code 用 GRACE.md 直接替代 CLAUDE.md——不做兼容回退。
    截断到 25KB。空文件或无文件 → 返回空字符串。
    """
    from pathlib import Path
    path = Path(project_dir) / ".grace" / "GRACE.md"
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")[:25_000]
        except OSError:
            pass
    return ""
```

### 3.3 Model Override 运行时生效

CC 的 `model` 参数（Agent Tool 调用参数 + Agent Definition frontmatter）控制 child 用哪个模型。

```python
# runtime_core/native_child_context.py — 新增

def resolve_child_model(
    request_model: str,
    definition_model: str,
    parent_backend,
) -> str:
    """CC model 参数优先级: request.model > definition.model > inherit parent."""
    if request_model:
        return request_model
    if definition_model and definition_model != "inherit":
        return definition_model
    # inherit: reuse parent model name
    if hasattr(parent_backend, "model_name"):
        return parent_backend.model_name
    return getattr(parent_backend, "_model", "")
```

在 `NativeAgentTool.execute()` 中，用 `resolve_child_model()` 的结果创建 child backend。

### 3.4 明确不做（从旧计划移除）

| 移除项 | 原因 |
|---|---|
| Skills 预加载 (`_load_skill_content`) | CC skills 是 tool-invoked，Grace Code 已有 `Skill` 工具 |
| Memory scope 加载 (`_load_memory_content`) | CC 无此概念。项目规则文件替代 |
| `AgentDefinition.memory` 字段的运行时消费 | 保留字段但不在此 Phase 消费。若需要跨 session 记忆，用 project rules 文件 |
| 跨轮 history 注入 (BT-0a-3) | 继续推迟，需要 NativeStepLoop stream 接口 |

### 3.5 不在此 Phase

- MCP server 激活（Phase 4）
- Permission mode 继承（Phase 4）
- `run_in_background` 运行时生效（Phase 5）
- `isolation` 运行时生效（Phase 6）
- Native 文本流式输出

### 3.6 文件范围

| 文件 | 动作 |
|---|---|
| `runtime_core/native_child_runner.py` | 修改：`build_child_conversation` 加 `project_dir` 参数 + project rules |
| `runtime_core/native_child_context.py` | 新增：`_load_project_rules` + `resolve_child_model` |
| `runtime_core/native_agent_tool.py` | 修改：传入 `project_dir` + model 生效 |
| `composition/runtime_composition.py` | 修改：传入 `project_dir` 给 NativeAgentTool |
| `tests/runtime_core/test_native_child.py` | 修改：BT-11~BT-14（替换旧 BT-11~BT-15） |

### 3.7 Before Tests

```python
# BT-11: Project rules 注入
def test_child_conversation_includes_project_rules():
    """Child conversation 包含项目规则文件内容。"""
    with _temp_project_dir(rules_content="Use pnpm, not npm.") as project_dir:
        conv = build_child_conversation(
            definition=make_explore_definition(),
            prompt="Search code",
            project_dir=project_dir,
        )
        has_rules = any("pnpm" in _first_text(m) for m in conv.messages)
        assert has_rules

# BT-12: 项目规则文件不存在时不报错
def test_child_conversation_without_project_rules():
    """若无 GRACE.md，不注入 project rules，其他消息照常。"""
    conv = build_child_conversation(
        definition=make_explore_definition(),
        prompt="Search",
        project_dir="/nonexistent",
    )
    assert len(conv.messages) >= 3  # system + protocol + task

# BT-13: Model 解析优先级
def test_resolve_child_model_hierarchy():
    """request.model > definition.model > inherit parent."""
    parent = _FakeBackend(model_name="claude-sonnet")
    assert resolve_child_model("haiku", "sonnet", parent) == "haiku"
    assert resolve_child_model("", "sonnet", parent) == "sonnet"
    assert resolve_child_model("", "inherit", parent) == "claude-sonnet"
    assert resolve_child_model("", "", parent) == "claude-sonnet"

# BT-14: Model override → child 用不同 backend（端到端，fake backend 验证）
def test_child_model_override_creates_different_backend():
    """model="haiku" → child backend.model_name == "haiku"."""
    # 用 spy backend 验证 NativeAgentTool.execute() 传入的 model
```

### 3.8 Skills / Memory 的正确理解（不实现，记录供后续）

CC 中 subagent 获得 skills 的途径是：parent 在调用 Agent 工具时，在 prompt 中**显式告诉 child**"use the X skill"。或者 child 自己的 `Skill` 工具可用，child 可以主动调用。

Memory 在 CC 中通过 CLAUDE.md 实现跨 session 持久化（Grace Code 用 GRACE.md 替代）。没有"按 agent type 自动加载 memory"的机制——memory 是按 project 组织的，所有 agent 共享。

Grace Code 旧路径的 `memory` scope 机制是项目自己的扩展。如果未来需要，可以作为 Grace Code 自己的增强（不标 CC-aligned），在独立 Phase 实现。

---

## Phase 4：MCP + Permission Mode

> 依赖：Phase 3
> 状态：**COMPLETED** ✅ 2026-08-04

### 做什么

补齐 `AgentDefinition` 中影响 child 执行环境的字段：MCP server 激活、permission mode 继承。

### 4.1 MCP Server 激活

CC 的 `mcpServers` 字段指定 subagent 可用的 MCP server（名称列表或 inline 定义）。

Grace Code 已有 MCP 基础设施（`agent/mcp/`）。Phase 4 需要：

```python
def activate_child_mcp_servers(
    definition: AgentDefinition,
    parent_mcp_manager,
) -> dict[str, Any]:
    """激活 child 的 MCP servers，返回 server_name → connection。

    CC 行为：子代理只加载 definition 明确声明的 MCP server，
    或父代理显式授予的交集。不能扩大父代理权限。
    """
```

激活后的 MCP tools 通过 `_RealTools.register()` 动态注册到 child 的 tool port。

### 4.2 Permission Mode 继承

CC 的 `permissionMode` 字段：`default` | `acceptEdits` | `dontAsk` | `plan` | `bypassPermissions`。

```python
def resolve_child_permission_mode(
    definition: AgentDefinition,
    parent_mode: str,
) -> str:
    """CC 权限模式继承。
    定义有 → 用定义的；定义无 → inherit parent。取更严格者。
    """
```

### 4.3 不在此 Phase

- MCP 连接失败处理（后续完善）
- 不同 provider（Anthropic ↔ OpenAI）的 child backend 切换：Phase 3 已解决 model override，provider 切换通过 `NativeBackend.for_child()` 的 `model` 参数自然支持

### 4.4 文件范围

| 文件 | 动作 |
|---|---|
| `runtime_core/native_child_context.py` | 修改：扩展 MCP + permission |
| `tests/runtime_core/test_native_child.py` | 修改：新增 MCP/permission 测试 |

---

## Phase 5：Background Execution（异步 Child）

> 依赖：Phase 2（Agent Tool 注册）
> 状态：**COMPLETED** ✅ 2026-08-04

### 做什么
实现 `run_in_background=True` 的异步 child 执行。对标 CC 的 background subagent 行为。

### 5.1 CC 的 Background 行为

```
CC Agent Tool 调用 (run_in_background=true)
  → spawns child in background thread/process
  → 立即返回 { status: "async_launched", agentId, outputFile }
  → Child 独立运行
  → 完成后写 outputFile
  → Parent 通过 TaskOutput 工具查询结果
  → 或通过 completion notification 被通知
```

### 5.2 实现设计

Phase 5 分两步：

**5.2.1 线程内 后台（Phase 5a）**

最简单的实现：Python thread + 共享 state。

```python
# runtime_core/native_child_runner.py

@dataclass
class BackgroundChildHandle:
    """对标 CC 的 async_launched 返回。"""
    agent_id: str
    status: str = "running"
    output_file: str = ""       # 结果写入路径
    started_at: str = ""

def run_native_child_background(
    ports: RuntimePorts,
    session_id: SessionId,
    run_id: RunId,
    conversation: NativeConversation,
    cancellation: CancellationHandle,
    max_steps: int,
    budget_tokens: int,
) -> BackgroundChildHandle:
    """在后台线程中启动 child 执行，立即返回 handle。
    
    对标 CC 的 run_in_background: true 行为。
    """
    import threading
    
    result_container: list[RuntimeOutcome] = []
    
    def _run():
        outcome = run_native_child(
            ports, session_id, run_id, conversation,
            cancellation, max_steps, budget_tokens,
        )
        result_container.append(outcome)
        _write_child_output_file(run_id, outcome)
    
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    
    return BackgroundChildHandle(
        agent_id=str(session_id),
        output_file=_child_output_path(run_id),
    )
```

**5.2.2 完成通知（Phase 5b）**

Child 完成后，通过 `live_events.publish()` 发送通知：

```python
ports.live_events.publish(
    event_type="child.completed.v1",
    payload=freeze_json({
        "child_session_id": str(session_id),
        "child_run_id": str(run_id),
        "parent_session_id": str(parent_session_id),
        "status": outcome.status.value,
        "output_file": output_path,
    }),
    scope=parent_scope,
)
```

Parent 端通过 `TaskOutput` 工具（对标 CC）或 event listener 获取通知。

### 5.3 超时与取消

```python
# Background child 超时控制
def run_native_child_with_timeout(
    timeout_seconds: float,
    **kwargs,
) -> RuntimeOutcome:
    """带超时的 child 执行。"""
    ...

# 取消传播：parent cancellation → child cancellation
def propagate_cancellation(
    parent_handle: CancellationHandle,
    child_handle: BackgroundChildHandle,
) -> None:
    parent_handle.cancel()  # → child loop 检查 context.cancellation.cancelled
```

### 5.4 文件范围

| 文件 | 动作 |
|---|---|
| `runtime_core/native_child_runner.py` | 修改：新增 `run_native_child_background()` |
| `runtime_core/native_child_handle.py` | 新增：`BackgroundChildHandle` |
| `runtime_core/native_agent_tool.py` | 修改：`run_in_background=True` 分支 |
| `tests/runtime_core/test_native_child_background.py` | 新增 |

### 5.5 Before Tests

```python
# BT-16: 后台启动立即返回
def test_background_child_returns_immediately():
    handle = run_native_child_background(...)
    assert handle.status == "running"
    assert handle.agent_id

# BT-17: 后台 child 最终完成
def test_background_child_completes():
    handle = run_native_child_background(...)
    outcome = wait_for_child(handle, timeout=10)
    assert outcome.status == RunStatus.COMPLETED

# BT-18: 取消传播
def test_cancellation_propagates_to_child():
    handle = run_native_child_background(...)
    cancel_parent()
    outcome = wait_for_child(handle, timeout=5)
    assert outcome.status == RunStatus.CANCELLED

# BT-19: 超时终止
def test_child_timeout():
    outcome = run_native_child_with_timeout(timeout_seconds=0.5, ...)
    assert "timeout" in outcome.error.lower()
```

---

## Phase 6：Worktree Isolation

> 依赖：Phase 2
> 状态：**COMPLETED** ✅ 2026-08-04

### 做什么
实现 `isolation="worktree"` 的 child 执行。对标 CC 的 `isolation: worktree` 行为。

### 6.1 CC 的 Worktree 行为

```
CC Agent Tool 调用 (isolation="worktree")
  → 检测 git repo
  → git worktree add --detach <tmp_path>   (创建隔离分支)
  → Child 在 worktree 目录中运行
  → 所有文件操作 (Read/Write/Edit/Bash) 在该 worktree 内
  → Child 完成：
    - 无变更 → auto-cleanup（删除 worktree）
    - 有变更 → 保留 worktree，返回 worktree path
  → Parent 检查变更 → apply/discard
```

### 6.2 实现设计

```python
# runtime_core/native_child_runner.py

@dataclass
class WorktreeContext:
    """隔离 worktree 的上下文。"""
    worktree_path: str
    branch_name: str
    original_repo_path: str

def create_child_worktree(
    repo_path: str,
    child_session_id: str,
) -> WorktreeContext:
    """创建 child 专用的 git worktree。
    
    对齐 CC:
    - git worktree add --detach <repo_path>/.grace/worktrees/<session_id>
    - branch name: agent-<hex>
    """

def cleanup_child_worktree(ctx: WorktreeContext, has_changes: bool) -> str:
    """CC-aligned worktree 清理。
    
    has_changes=False → git worktree remove + branch -D
    has_changes=True  → 保留，返回 worktree_disposition="preserved"
    """
```

### 6.3 与 NativeStepLoop 的集成

Child 的 `RuntimeExecution` 需要知道 workspace 路径。当前 `NativeStepLoop` 不关心文件系统路径（它通过 `ToolPort` 执行工具，工具自己知道路径）。所以 worktree 隔离实际上是：

1. 创建 worktree
2. 设置 child 的 tool registry 的 workspace root 为 worktree path
3. child 执行
4. 清理或保留

```python
def run_native_child_in_worktree(
    repo_path: str,
    definition: AgentDefinition,
    **kwargs,
) -> tuple[RuntimeOutcome, WorktreeContext]:
    worktree = create_child_worktree(repo_path, kwargs["session_id"])
    try:
        # 构造 worktree-scoped tool port
        child_ports = child_runtime_ports_with_worktree(
            kwargs["parent_ports"], definition, worktree,
        )
        outcome = run_native_child(ports=child_ports, **kwargs)
        has_changes = _detect_worktree_changes(worktree)
        disposition = cleanup_child_worktree(worktree, has_changes)
        return outcome, worktree
    except Exception as exc:
        cleanup_child_worktree(worktree, has_changes=False)
        raise
```

### 6.4 文件范围

| 文件 | 动作 |
|---|---|
| `runtime_core/native_child_runner.py` | 修改：新增 worktree 版本 |
| `runtime_core/native_child_worktree.py` | 新增：worktree 创建/清理 |
| `tests/runtime_core/test_native_child_worktree.py` | 新增 |

### 6.5 Before Tests

```python
# BT-20: Worktree 创建成功
def test_worktree_created_in_grace_dir():
    ctx = create_child_worktree(repo_path, "session-123")
    assert os.path.isdir(ctx.worktree_path)
    assert ".grace/worktrees" in ctx.worktree_path

# BT-21: 无变更 auto-cleanup
def test_worktree_auto_cleanup_no_changes():
    outcome, ctx = run_native_child_in_worktree(...)
    assert not os.path.exists(ctx.worktree_path)

# BT-22: 有变更保留
def test_worktree_preserved_with_changes():
    # child 执行 Write 工具
    outcome, ctx = run_native_child_in_worktree(...)
    assert os.path.exists(ctx.worktree_path)
    assert ctx in preserved_worktrees

# BT-23: 异常时清理
def test_worktree_cleaned_on_exception():
    try:
        run_native_child_in_worktree(broken_repo_path, ...)
    except Exception:
        pass
    assert not worktree_exists("session-fail")
```

---

## Phase 7：Fan-out / Fan-in（并行 Child）

> 依赖：Phase 2 + Phase 5（background）
> 状态：**COMPLETED** ✅ 2026-08-04

### 做什么
让 NativeStepLoop 能同时启动多个 child 并等待它们全部完成。对标 CC 的"Run agents in parallel"模式。

### 7.1 CC 的并行行为

CC 中并行是通过 **在一个 turn 中多次调用 Agent 工具** 实现的：

```json
// 同一 turn 的 tool_calls 数组
[
  {"name": "Agent", "params": {"subagent_type": "explore", "description": "..."}},
  {"name": "Agent", "params": {"subagent_type": "explore", "description": "..."}},
  {"name": "Agent", "params": {"subagent_type": "explore", "description": "..."}},
]
```

CC 的 AgentBatch（Workflow）在此基础上增加了 barrier 等待和结果聚合。

### 7.2 Grace Code 现状

Grace Code 已有 `AgentBatch` 工具（`agent/session/agent_batch_tool.py`），但它走的是旧 `SessionRuntime` 路径。Phase 7 需要：

1. **复用 `NativeAgentTool`**（Phase 2 产出）——每次 Agent 调用走同一个执行路径
2. **新增 `NativeAgentBatch` 工具**——轻量 wrapper：接受 tasks[]，逐项调 `NativeAgentTool`，并行等待
3. **新增 `ChildBarrier`**——等待 N 个 background child 全部完成

```python
class NativeAgentBatch:
    """对标 CC AgentBatch / Workflow 的并行 child 执行器。
    
    不复制 AgentTool 的逻辑——每个 task 都调 NativeAgentTool.execute()。
    只负责：并行启动 + barrier 等待 + 结果聚合。
    """
    
    def execute(self, params: dict) -> ToolResult:
        tasks = params["tasks"]
        required = params.get("required", True)
        
        handles = []
        for task in tasks:
            handle = run_native_child_background(
                ...,  # 复用 NativeAgentTool.execute() 的逻辑
            )
            handles.append((task, handle))
        
        # Barrier: 等待全部完成（或超时）
        results = _wait_all(handles, timeout=params.get("timeout", 300))
        
        return _aggregate_results(results, required)
```

### 7.3 并发控制

```python
# CC 的三类限制
MAX_CONCURRENT_SUBAGENTS = 4       # 同时运行的 child 数上限
MAX_FANOUT_PER_TURN = 3            # 单次 turn 的最大并行数
MAX_SUBAGENT_SPAWN_DEPTH = 1       # Phase 7 不做嵌套，保持 depth=1
```

### 7.4 结果聚合

```python
def _aggregate_results(
    results: list[NativeChildResult],
    required: bool,
) -> str:
    """聚合多个 child 结果，输出给 parent LLM。
    
    行为：
    - 全部成功 → 格式化的结果列表（每个 child 的 summary）
    - 部分失败 + required=False → 成功的聚合 + 失败的 warning
    - required worker 失败 → 标注 FAILED，指导 parent 降级
    """
```

### 7.5 文件范围

| 文件 | 动作 |
|---|---|
| `runtime_core/native_agent_batch.py` | 新增：`NativeAgentBatch` + `ChildBarrier` |
| `composition/runtime_composition.py` | 修改：注册 NativeAgentBatch |
| `tests/runtime_core/test_native_agent_batch.py` | 新增 |

### 7.6 Before Tests

```python
# BT-24: 两个 child 并行执行
def test_two_children_run_in_parallel():
    start = time.monotonic()
    batch = NativeAgentBatch(...)
    result = batch.execute({"tasks": [task_a, task_b]})
    elapsed = time.monotonic() - start
    # 并行意味着总时间 < sum(individual)，而 ≈ max(individual)
    assert elapsed < 3.0  # 每个 child ~1.5s fake delay

# BT-25: 全部 required child 完成
def test_batch_waits_for_required_children():
    results = batch.execute({"tasks": [task_a, task_b], "required": True})
    assert results  # 非空

# BT-26: Optional child 失败不阻塞
def test_optional_child_failure_non_blocking():
    results = batch.execute({
        "tasks": [good_task, broken_task],
        "required": False,
    })
    assert "FAILED" not in results  # optional failure → warning only

# BT-27: 并发数受控
def test_batch_respects_concurrency_limit():
    # 5 tasks, MAX_CONCURRENT=2 → 应分 3 波
    results = batch.execute({"tasks": 5_tasks})
    # 验证各波次时间不重叠（最多 2 个同时跑）

# BT-28: 超时
def test_batch_timeout():
    results = batch.execute({
        "tasks": [slow_task],
        "timeout": 1.0,
    })
    assert "timeout" in results.lower()
```

---

## Phase 8+9：Chain + Orchestrator（两阶段合一）

> 依赖：Phase 2 + 7
> 状态：**COMPLETED** ✅ 2026-08-04

### Phase 8 跳过理由

Chain = StepLoop 自然行为。模型在 turn N 调用 `Agent(debugger)` → 结果注入 conversation → 模型在 turn N+1 看到结果 → 调用 `Agent(general, "修复: {summary}")`。**零新代码**。CC 没有 `depends_on` 字段、没有依赖 DAG——模型自己决定串行顺序。

### Phase 9 做什么

CC Orchestrator 不是新 runtime mode——是 **AgentDefinition + 工具配置**：

```
Orchestrator = Primary agent (agent_name="orchestrator")
             + Agent 工具可用（Phase 2 NativeAgentTool）
             + 并行能力（Phase 7 ToolScheduler concurrency_safe）
             + system prompt（委托决策 + 综合策略）
```

Phase 9 只需要：创建 `.grace/agents/orchestrator.md`，其余全部已就绪。

### 9.1 Orchestrator Agent Definition

```yaml
# .grace/agents/orchestrator.md
---
name: orchestrator
description: >
  Coordinate complex multi-step tasks by decomposing, delegating to
  specialized subagents, and synthesizing results. Use when the task
  benefits from parallel investigation or specialized workers.
intent: edit
agent_kind: primary
tools: Read, Grep, Glob, Bash, Write, Edit, Agent, Skill, WebFetch
permissionMode: default
maxTurns: 100
background: false
---
# Orchestrator

You are an orchestrator agent. Your role is to coordinate complex work
by delegating to specialized subagents.

## When to delegate
- Task can be split into independent investigations or implementations
- Different skills or focus areas are needed
- Parallel execution will save time (use multiple Agent calls in one turn)
- Budget and tool availability allow

## When NOT to delegate
- Simple single-file tasks you can do yourself in 1-3 tool calls
- Tasks requiring heavy shared context from this conversation
- Multiple changes to the same file

## How to delegate
- Use the Agent tool with a clear 3-5 word description
- Provide full context in the prompt: objective, what you know, what to find
- For independent investigations: call Agent multiple times in one turn
- After receiving results: cross-check evidence, resolve conflicts, synthesize

## Synthesis rules
- Remove duplicate findings
- Flag conflicting conclusions
- State what you verified vs. what depends on agent reports
- You are responsible for the final answer to the user
```

### 9.2 验证测试

端到端：用 fake backend 模拟 Multi-Agent 流程。

```python
# BT-28: Orchestrator 并行调 Agent → child 完成 → 综合
def test_orchestrator_fan_out_and_synthesize():
    # 1. LLM 返回 ToolCallBatch(Agent×3)
    # 2. StepLoop 并行执行 3 个 child
    # 3. 每个 child 返回 NativeChildResult
    # 4. 结果注入 conversation
    # 5. 下一 turn LLM 综合 → 返回 final text
```

### 9.3 文件范围

| 文件 | 动作 |
|---|---|
| `.grace/agents/orchestrator.md` | **新增** |
| `tests/runtime_core/test_native_child.py` | **修改**：BT-28 端到端编排测试 |

### 9.4 CM 已完成无需改动的

- `NativeAgentTool`（Phase 2）——orchestrator 调用 Agent 工具
- `ToolScheduler`（Phase 7）——多个 Agent 并行执行
- `ChatPipeline._execute_native()`（Phase 0a）——通过 `agent_name="orchestrator"` 选取
- `RuntimePorts` + `NativeStepLoop`——自然串行 chain 行为

### 9.5 关于 AgentBatch 工具

不创建。CC 没有独立的 AgentBatch——并行就是模型在一个 turn 里多次调用 Agent 工具，Phase 7 已并行化。Orchestrator 的 system prompt 指导模型何时这样做。

---

## 全局：CC 字段映射总表（v2.4 — 调研修正）

这张表覆盖 CC Agent Definition 所有字段，标注每个字段在哪个 Phase 原生生效。
标注 `⇒` 的字段在调研后修正了理解。

| CC 字段 | AgentDefinition 字段 | 原生生效 Phase | 说明 |
|---|---|---|---|
| `name` | `name` | Phase 1 | Child runner 用 name 查找 definition |
| `description` | `description` | Phase 1 | 注入 child 的 task message |
| `tools` | `tools: frozenset[str]` | Phase 2 | `ScopedToolPort` 强制执行 |
| `disallowedTools` | `disallowed_tools: frozenset[str]` | Phase 1 | `filter_tool_schemas()` backend 层移除 |
| `model` | `model: str` | **Phase 3** ⇒ | `resolve_child_model()` → child backend 切换 |
| `permissionMode` | `permission_mode: str` | Phase 4 | 权限模式继承 + hook gate |
| `maxTurns` | `max_turns: int` | Phase 1 | → `RuntimeExecution.max_steps` |
| `skills` | `skills: tuple[str, ...]` | **—** ⇒ | **CC skills 是 tool-invoked，不预加载**。Grace Code 已有 `Skill` 工具 |
| `mcpServers` | `mcp_servers: tuple` | Phase 4 | Per-child MCP 激活 |
| `memory` | `memory: str` | **—** ⇒ | **CC 无此概念**。项目 rules 文件（CLAUDE.md → Grace Code 用 GRACE.md）是 CC 的持久化机制，Phase 3 实现 |
| `background` | `background: bool` | Phase 5 | 默认后台执行 |
| `effort` | `effort: str` | — | 当前 Claude API 不支持 reasoning effort |
| `isolation` | `workspace_mode` | Phase 6 | worktree 创建/清理 |
| `hooks` | `hooks: tuple[dict, ...]` | — | 后续 Phase（hook 系统已有但未接入 child） |
| `initialPrompt` | `initial_prompt: str` | Phase 1 | 注入 child conversation 首位 |

### 调研修正摘要

| 旧理解（v2.0-2.3） | 新理解（v2.4） | 来源 |
|---|---|---|
| Skills 预加载到 child system prompt | Skills 是 tool-invoked，不在 system prompt | CC 源码逆向：`Skill` 工具 → `tool_result` 注入 |
| Memory scope（user/project/local）加载 | CC 原生只有 CLAUDE.md `<system-reminder>` + MEMORY.md 25KB | CC 文档 + Issue #4418 |
| Agent body 叠加 system prompt | Agent body **替换**默认 system prompt（CC priority chain） | `buildEffectiveSystemPrompt()` 分析 |
| CLAUDE.md 不传播到 subagent | **自动传播**——CC runtime 在每个 user message 注入 | CC 文档 confirmed |

---

## 明确不做（独立里程碑）

这些能力需要独立设计文档，不在本计划的 9 个 Phase 中：

| 能力 | 原因 |
|---|---|
| **Fork agent** (`ContextOrigin.PARENT_SNAPSHOT`) | 需要 parent conversation 序列化/反序列化 + tool schema digest 验证，复杂度高 |
| **Nested delegation** (`depth > 1`) | Child spawn child 需要递归 RuntimePorts 构造 + 三层限制 + allowlist 传递 |
| **Agent Team** | 共享 task board + mailbox + lease claim + P2P 通信，完全不同的执行模型 |
| **Auto-topology selection** | "何时用 fan-out / chain / single" 的自动判断，需要 TaskShape 分析 + LLM 决策 |
| **Resume / Follow-up** | terminal child 后发新 generation，需要恢复 child session 的 conversation state |
| **Parent-to-running-child steering** | live message injection，需要安全轮次语义 + at-most-once delivery |
| **ReportFindings 强制执行** | `required_tools` / `completion_requires` 的 runtime enforcement |
| **跨进程 child** | 所有 child 当前在同一进程（线程内）执行 |

---

## Phase 10：删除旧路径

> 依赖：Phase 0a + 1~9（全部完成）
> 状态：**COMPLETED** ✅ 2026-08-04
> **详细执行**：[PHASE10_REMAINING_4_POINTS_2026-08-04.md](./PHASE10_REMAINING_4_POINTS_2026-08-04.md)

### 做什么

分两线推进：

**A 线 — 新能力接线**：Model Switch / Stream / CLI Wiring（已完成 1+2，剩余 3）
**B 线 — 旧代码清理**：agent_service.py 死代码删除 + SessionRuntime 标记废弃

### 10.2 A 线：新能力接线（当前状态）

| 步骤 | 状态 | 内容 |
|---|---|---|
| Model Switch | ✅ 已完成 | `NativeBackend.invoke/stream_iter` 加 `model=""` per-request 参数 |
| Stream 接口 | ✅ 已完成 | `NativeStepLoop.execute(text_callback)` + `_stream_model_call()` |
| CLI Wiring | ⬜ 待执行 | `entry/cli.py` ChatSession 加 `agent_runtime` param |

### 10.3 B 线：旧代码清理（当前状态）

| 删除目标 | 状态 | 替代品 | 替代品 CC 对齐？ |
|---|---|---|---|
| `chat()` (~95 行) | ⬜ | `ChatPipeline._execute_native()` | ✅ 一条路径 |
| `_memory_maintenance_loop/do` (~50 行) | ⬜ | `MemoryMaintenanceJob` | ✅ 更优 |
| `_inject_session_context()` (~15 行) | ⬜ | `ChatPipeline.inject_session_context()` | ✅ 概念对齐 |
| `_build_recovery_context()` (~50 行) | ⬜ | `jobs/session_context.py`（修 GRACE.md） | ⚠️ Grace Code 扩展 |
| `SessionRuntime` 标记废弃 | ⬜ | — | — |

**替代品审核结论**：7 个替代品全部审核通过（详见[子文档](./PHASE10_REMAINING_4_POINTS_2026-08-04.md) §6），零个需重写。

| 删除目标 | 原因 |
|---|---|
| `agent/session/runtime.py` (SessionRuntime) | 父执行已全量迁移到 AgentRuntime |
| `agent/session/subagent.py` (run_child_agent) | Child 执行已全量迁移到 NativeChildRunner |
| `agent/session/runtime_spawn.py` | Spawn 逻辑已迁移到 NativeAgentTool |
| `agent/session/task_tool.py` (AgentTool) | 已被 NativeAgentTool 替代 |
| `agent/session/agent_batch_tool.py` (AgentBatch) | 已被 NativeAgentBatch 替代 |
| `agent/core.py` (ReActAgent) | 已被 AgentRuntime + NativeStepLoop 替代 |
| `llm/base.py` (LLMMessage) | 已被 NativeMessage 替代 |
| `llm/tool_call_validator.py` | 已被 SchemaValidator 替代 |
| `core/base.py` ToolExecutionPipeline 引用 | 已无调用方 |
| CLI/Web 中所有 SessionRuntime 引用 | 已在 Phase 0 移除 |

### 10.2 保留清单

以下旧路径组件在新路径中没有替代，需先迁移再删：

| 保留 | 说明 |
|---|---|
| `agent/session/agent_definition.py` | Layer 1 核心，新老路径共用 |
| `agent/session/models.py` | AgentDefinition + 枚举，新老路径共用 |
| `agent/session/session_store.py` | SQLite 持久化，新老路径共用 |
| `core/base.py` (BaseTool, ToolResult) | 工具基类，被 _RealTools 使用 |
| `hitl/pipeline.py` (PermissionPipeline) | 权限管线，_RealHooks 使用 |
| `hook_core/` | Hook 系统，_RealHooks 使用 |

### 10.3 验证

```bash
# 全量回归
python -m pytest tests/ -q --ignore=tests/test_smoke_e2e.py -x

# 零命中
rg -n "SessionRuntime|ReActAgent|LLMMessage\b" agent/ --glob='*.py'
rg -n "SessionRuntime|ReActAgent|LLMMessage\b" entry/ --glob='*.py'
rg -n "SessionRuntime|ReActAgent|LLMMessage\b" server/ --glob='*.py'

# 旧工具类不存在
rg -n "AgentTool\b|AgentBatch\b" runtime_core/ --glob='*.py'  # Native 前缀
```

---

## 实现依赖图（v2.9 — Phase 8 跳过 + 9 合并）

```
Phase 0a（Web 父执行单路径）← 已完成 ✅
  └─ Phase 1（Child Contract + Runner）           ← 已完成 ✅
       └─ Phase 2（Agent Tool + 工具隔离）          ← 已完成 ✅
            ├─ Phase 3（Project Rules + Model）      ← 已完成 ✅
            │    └─ Phase 4（MCP + Permission）        ← 已完成 ✅
            ├─ Phase 5（Background）                   ← 已完成 ✅
            ├─ Phase 6（Worktree）                      ← 已完成 ✅
            └─ Phase 7（Fan-out / 并行）                ← 已完成 ✅
                 └─ Phase 8+9（Chain/Orchestrator）       ← 已完成 ✅
                      └─ Phase 10（清理 + 接线）           ← 已完成 ✅
```

**Phase 10 实际出产（v3.2）**：
- A 线（能力接线）：Model Switch + Stream 接口 + CLI Wiring ✅
- B 线（旧代码清理）：agent_service 删 210 行死代码 ✅
- C 线（审计）：7 个替代品逐 CC 对照审核 ✅

---

## 每 Phase 验证总策略

```bash
# 每个 Phase 完成后的回归切片
python -m pytest tests/runtime_core/test_native_child*.py -v     # 新增测试
python -m pytest tests/runtime_core/ -q                           # runtime_core 回归
python -m pytest tests/composition/test_native_object_graph.py -q # composition 回归

# 静态检查
rg -n "SessionRuntime|ReActAgent|LLMMessage" runtime_core/native_child_*.py
# 必须零命中（每个 Phase 完成后）

# 全量回归（Phase 3 开始）
python -m pytest tests/ -q --ignore=tests/test_smoke_e2e.py -x
```

---

## 审批检查点

每个 Phase 完成后，生成一份检查报告：

1. **CC 对齐检查**：本 Phase 涉及的 CC 字段，是否在 Grace Code 中有 1:1 的生效路径
2. **抽象泄漏检查**：是否引入了 CC 中不存在的概念（preset/profile/policy wrapper）
3. **静态边界**：`rg` 零命中 `SessionRuntime|ReActAgent|LLMMessage`
4. **测试覆盖**：Before Tests → 实现 → Target Tests 全部通过
5. **新增文件数**：不超过 Phase 计划的文件范围

---

## 与旧路径的共存策略

```
Phase 1-4: 新 Native 路径与旧 SessionRuntime 路径共存
  - 旧的 AgentTool（agent/session/task_tool.py）继续服务 CLI/Web 旧路径
  - 新的 NativeAgentTool 服务 GRACE_RUNTIME_MODE=NATIVE

Phase 5-6: 新路径开始具备旧路径的核心能力（background + worktree）
  - 旧路径保持不动

Phase 7-9: 新路径能力超过旧路径（fan-out + chain + orchestrator）
  - 开始考虑 Web 入口从旧路径切换到新路径
  - 旧路径作为 fallback，逐步标记 DEPRECATED
```

**原则**：不删旧代码，直到新路径在 production 中稳定运行。

---

## 结论

本计划覆盖了从最小可用（Phase 1: 一个 child 在 native 路径上跑通）到完整编排（Phase 9: Multi-Agent Orchestrator）的全部 9 个阶段，按四个维度组织：

| 维度 | Phase | 核心交付 |
|---|---|---|
| 执行契约 | 1-2 | Request/Result 契约 + Agent 工具 + 工具隔离 |
| 上下文 | 3-4 | Project rules + model override + MCP + permission |
| 运行时与执行方式 | 5-6 | Background execution + worktree isolation |
| 拓扑与编排 | 7-9 | Fan-out/fan-in + chain + orchestrator |

每个 Phase 明确标注：做什么、不做什么、对 CC 哪个字段、文件范围、Before Tests。全部 Phase 在 `RuntimePorts → NativeStepLoop` 这一条执行链路之上叠加，不引入第二条链路。
