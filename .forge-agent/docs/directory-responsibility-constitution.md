# Directory Responsibility Constitution

> **本宪法规定每个目录的允许职责、禁止职责、允许依赖方向。**
> 违反宪法的代码必须被重构。这是后续所有工作的基石。

---

## 1. `agent/` — 行为编排层

**一句话定位**: 主循环编排器。只管"什么时候做什么"，不管"怎么做"。

**拥有的职责**:
- 主循环（step 级状态推进）
- finish/give_up 的统一收束
- 调用协作者（不自己承包细节）

**禁止拥有的职责**:
- request context 怎么组装 → `context/`
- memory 怎么筛和注入 → `memory/`
- tool output 怎么 artifact 化 → `context/`
- 会话状态怎么持久化 → `agent/v2/`
- hooks/hitl/permission 的接入细节 → `hooks/` + `hitl/`
- 具体的 LLM retry/backoff 细节 → `llm/`

**允许依赖**: `context/`, `memory/` (仅接口), `tools/` (仅 registry 接口), `agent/v2/`, `llm/`, `hooks/`

**禁止依赖**: 无（这是顶层）

**验收标准**:
- `agent/core.py` 不再直接 `import memory.*`、大部分 `context.*`、大部分 `observability.*`
- `ReActAgent.run()` 读起来像 orchestration，不像系统总管

---

## 2. `context/` — 上下文生命周期

**一句话定位**: 请求上下文装配器。唯一负责拼装发给 LLM 的 messages。

**拥有的职责**:
- request context assembly
- token budget planning
- session/task state
- compaction policy
- artifact summary routing
- context trace / stats

**禁止拥有的职责**:
- tool 执行
- memory 存储实现
- agent 控制流
- 知道具体 agent/runtime 类型

**允许依赖**: `memory/` (仅接口), `llm/` (token counting), `tools/` (仅 artifact 引用)

**禁止依赖**: `agent/`, `agent/v2/`, `entry/`

**当前违规**:
- `memory/context.py` 居然 import `agent.v2.runtime` — 必须修复
- `agent/core.py` 中的 `_build_messages()` 拥有本应属于 context 的逻辑

**验收标准**:
- 除 `context/` 外，其他层不再自己拼完整 request messages
- `ContextManager` 是唯一 request builder

---

## 3. `memory/` — 长期知识与检索

**一句话定位**: 记忆存储与检索服务。输出数据和候选，不操纵主循环。

**拥有的职责**:
- memory store (CRUD)
- retrieval / ranking
- metadata / freshness / ttl
- consolidation / extraction
- 对 context 暴露可注入的结果

**禁止拥有的职责**:
- 知道具体 agent/runtime 类型
- 知道 CLI/chat 生命周期细节
- 直接控制 prompt 组装策略

**允许依赖**: 无内部依赖（纯服务层）

**禁止依赖**: `agent/`, `agent/v2/`, `entry/`

**当前违规**:
- `memory/context.py` import `agent.v2.runtime` — P0 修复

**验收标准**:
- `memory/*` 不再 `import agent.*`
- memory 只输出数据和候选

---

## 4. `tools/` — 能力单元

**一句话定位**: 工具定义与执行。每个工具只关心"做什么"。

**拥有的职责**:
- tool definition (name, schema, description)
- execute contract
- tool result model (ToolResult + ToolError)
- risk classification

**禁止拥有的职责**:
- 应用级 runtime orchestration
- session lifecycle
- 复杂权限策略编排（那是 `hitl/` 和 `hooks/` 的活）
- permission/hook/capability 的内部实现

**允许依赖**: 无（叶子层）

**禁止依赖**: `agent/`, `context/`, `memory/`, `entry/`

**验收标准**:
- 新增工具不需要碰 `agent/core.py` 和 `entry/cli.py`
- 工具层 API 稳定

---

## 5. `agent/v2/` — Session Runtime 与 Task Delegation

**一句话定位**: 基于 session 的编排。管理 parent-child session 语义和 task 委派。

**拥有的职责**:
- session-based orchestration (SessionRuntime)
- parent-child session semantics
- task delegation contract (AgentTool)
- compact child result boundary
- task ledger (idempotency guard)

**禁止拥有的职责**:
- repo/worktree 具体生命周期实现 → 应由独立模块处理
- 低层 runtime registry 细节 → `tools/`
- capability/circuit 的太多实现细节 → `agent/`

**允许依赖**: `agent/`, `tools/`, `context/`, `llm/`, `memory/`

**禁止依赖**: `entry/`

**当前违规**:
- `fork_session()` 硬编码 `repo_path="."` 而不是继承 session.repo_path
- Worktree create/merge/discard 混在 subagent execution 中

**验收标准**:
- SessionRuntime 只编排，不承载每个具体策略
- 子任务 repo scope 严格继承 session

---

## 6. `entry/` — 用户入口

**一句话定位**: 命令行解析 + 模式分发。绝对不做系统装配。

**拥有的职责**:
- 用户入口 (click commands)
- 命令解析
- mode dispatch
- 调用 ApplicationBuilder

**禁止拥有的职责**:
- 全系统装配细节 → `ApplicationBuilder`
- tool registry 拼装 → `RegistryFactory`
- memory/hook/permission 的内部接线
- 过多 session lifecycle 逻辑

**允许依赖**: 所有层（作为入口组装点），但只做"组装"不做"实现"

**禁止依赖**: 无

**当前违规**:
- `cli.py` 中 `_init_memory()`, `_build_registry()`, `_init_hook_dispatcher()` 等系统装配逻辑
- Plan 预算计算（已修复：现在委托给 `AgentConfig.get_plan_limits()`）
- `chat.py` 中的 session lifecycle 逻辑

**验收标准**:
- `entry/cli.py` 主要是 click commands + handoff
- 入口层改动不会牵一发而动全身

---

## 7. `hitl/` + `hooks/` — 横切控制

**一句话定位**: 外围控制层。所有 pre/post tool decision 的唯一入口。

**拥有的职责**:
- permission pipeline (5-layer)
- hook dispatch (PreToolUse, PostToolUse, Stop, etc.)
- pattern inference (Always Allow rules)

**禁止拥有的职责**:
- 不直接嵌入 agent 核心循环
- 不自己管理 tool registry

**允许依赖**: `tools/` (仅接口)

**禁止依赖**: `agent/`, `agent/v2/`, `entry/`

**验收标准**:
- 权限和 hooks 是真正的横切层，不是内嵌判断

---

## 8. `llm/` — 模型适配

**一句话定位**: SDK adapter。被动响应，不驱动流程。

**拥有的职责**:
- provider adapter
- request/response normalization
- streaming support
- token counting

**禁止拥有的职责**:
- 上层 `agent.task` 语义对象耦合

**允许依赖**: 无（最底层）

**禁止依赖**: 所有上层

---

## 9. `observability/` — 被动记录

**一句话定位**: 被动 observer。记录但不驱动业务。

**拥有的职责**:
- trace/model/log/dataset/validation

**禁止拥有的职责**:
- 驱动业务主流程

**允许依赖**: 所有层（被动读取）

**禁止依赖**:（被动层，无限制但不应驱动流程）

---

## 依赖方向图

```
entry/ ────────────────────────────────────────── 入口组装
  │
  ├── agent/ ──────────────────────────────────── 行为编排
  │     ├── context/ ──────────────────────────── 上下文装配
  │     │     ├── memory/ ─────────────────────── 记忆服务
  │     │     └── llm/ ────────────────────────── 模型适配
  │     ├── agent/v2/ ─────────────────────────── session 编排
  │     ├── tools/ ────────────────────────────── 能力单元
  │     └── hooks/ + hitl/ ────────────────────── 横切控制
  │
  └── observability/ ──────────────────────────── 被动记录 (所有层)
```

**规则**: 上层可依赖下层。下层绝不可反向依赖上层。同层之间通过接口解耦。
