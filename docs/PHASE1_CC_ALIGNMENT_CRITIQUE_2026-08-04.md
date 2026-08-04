# Phase 1 契约 vs Claude Code 真实行为——逐项批判

> 日期：2026-08-04  
> 目标：根据 CC 公开文档 + CC 源码逆向（社区资料） + 本项目已有 CC 对齐文档，逐项识别 [NATIVE_ORCHESTRATION_PHASE1_DATA_CONTRACT_PLAN_2026-08-04.md](./NATIVE_ORCHESTRATION_PHASE1_DATA_CONTRACT_PLAN_2026-08-04.md)
> 与 [orchestration_contracts.py](../application/coordinators/orchestration_contracts.py) 中**编造/过度设计/与 CC 冲突**的部分，指导重新生成新计划。  
> 结论：**当前 Phase 1 方案完全不是 CC-aligned。它是一套从零发明的抽象层，与 CC 的真实 subagent 模型几乎没有任何对应关系。**

---

## 0. CC 的真实模型（用作全部对比的基线）

Claude Code 的 subagent 模型极其简洁，只由两层构成：

### 第 1 层：Agent Definition（Markdown + YAML frontmatter）

文件位置：`.claude/agents/<name>.md`

```yaml
---
name: explore
description: "Fast read-only code search and understanding..."
tools: Read, Grep, Glob, Web            # allowlist; 省略 = 继承全部
disallowedTools: Write, Edit, Agent     # denylist
model: haiku                             # sonnet|opus|haiku|inherit
permissionMode: dontAsk                  # dontAsk|default|acceptEdits|plan|bypassPermissions
maxTurns: 25
skills: []
mcpServers: []
background: true
effort: low
isolation: worktree                      # "worktree" or omit
hooks: {}
---
```

关键事实：
- **`tools` 是 allowlist**。省略则该 subagent 继承 parent 的全部工具（包括 MCP tools）。
- **`disallowedTools` 是 denylist**。与 `tools` 同时存在时，先是 allowlist 做交集，再减去 denylist。
- **没有 preset 概念**。`explore` 是一个 built-in subagent 类型（其 `.md` 定义内置在 CC 中），不是 preset。
- **没有 `global_denylist`**。denylist 是每个 agent definition 独立声明的。
- **没有 `background_whitelist`**。background subagent 使用相同工具约束机制。
- **没有 `remove_delegate_tools` flag**。delegation 能力由 `tools` 中是否包含 `Agent` 决定。如果不授权 `Agent`，则不能 spawn。
- **没有 `allow_global_state_write` 或 `allow_background_task_registration`**。这些不是 CC 概念。

### 第 2 层：Agent Tool（工具调用参数）

Claude 调用 subagent 时使用的工具 schema：

```json
{
  "description": "Find relevant files",      // 3-5 词
  "prompt": "Inspect the routing layer",     // 完整任务
  "subagent_type": "explore",                // agent type name
  "model": "haiku",                          // 可选覆盖
  "run_in_background": false,                // async launch
  "isolation": "worktree"                    // "worktree" or omit
}
```

关键事实：
- **参数极少，全部扁平**。
- **没有 `invocation_id`**——agent ID 由 runtime 生成并返回。
- **没有 `budget_tokens` / `max_steps`**——这些来自 agent definition 的 `maxTurns`。
- **没有 `context_mode`**——context 来源由 agent type 决定（named → fresh, fork → parent snapshot）。
- **没有嵌套的 policy/profile 对象**。

### 返回（sync）

```json
{
  "status": "completed",
  "agentId": "agent-a1b2c3d4",
  "content": "...",                          // subagent 的 final message
  "totalToolUseCount": 12,
  "totalDurationMs": 4500,
  "totalTokens": 18432,
  "usage": { ... },
  "prompt": "..."
}
```

### 返回（async / background）

```json
{
  "status": "async_launched",
  "agentId": "agent-a1b2c3d4",
  "description": "Find relevant files",
  "prompt": "Inspect the routing layer",
  "outputFile": "/path/to/output"
}
```

---

## 1. 错误清单（按严重程度排序）

### ERROR 1 [CRITICAL]：`ToolIsolationProfile` 的 `preset` 概念完全编造

**当前代码**：
```python
class ToolIsolationProfile:
    preset: str = ""                         # 编造的
    global_denylist: tuple[str, ...] = ()    # 编造的
    visible_tools: tuple[str, ...] = ()      # CC 的 tools (allowlist)
    disallowed_tools: tuple[str, ...] = ()   # CC 的 disallowedTools
    background_whitelist: tuple[str, ...] = () # 编造的
    remove_delegate_tools: bool = True        # 编造的
    allow_global_state_write: bool = False    # 编造的
    allow_background_task_registration: bool = True # 编造的
```

**CC 真实行为**：只有两个字段：`tools`（allowlist, 逗号分隔字符串）和 `disallowedTools`（denylist），在 agent definition 的 YAML frontmatter 中声明。没有 preset。

**为什么错**："explore"、"code" 等是 subagent **类型**（即 agent definition），不是 tool isolation preset。将 agent type 降格为 tool preset 混淆了"是什么 agent"与"能做什么工具"两个独立维度。CC 的 `subagent_type` 参数引用的是一个完整 agent definition，其工具集是该 definition 的一部分——不是单独配置的 preset。

**影响**：整个 tool isolation 模型需要重做。

---

### ERROR 2 [CRITICAL]："三道门" 是编造的抽象

**当前设计**：
```
第一道门：global_denylist（系统/coordinator 填充）
第二道门：disallowed_tools（agent definition）
第三道门：background_whitelist（background child 收缩）
```

**CC 真实行为**：工具隔离由以下交集确定：
```
parent 可用工具 ∩ child 的 tools (allowlist) − child 的 disallowedTools
```
只有**一道交集 + 一道减法**。background 不创建新的门，它使用相同的机制。

**为什么错**：三道门的划分（尤其是 `global_denylist` vs `disallowed_tools` 的分离）在 CC 中不存在。CC 中对所有 subagent 通用的限制通过 **parent 层面的工具权限**自然传递（subagent 不能扩大 parent 权限）。Grace Code 项目文档 [SCENARIO_DRIVEN_SUBAGENT_ARCHITECTURE_PLAN.md](./SCENARIO_DRIVEN_SUBAGENT_ARCHITECTURE_PLAN.md) §7.3 已经正确描述了交集模型。

---

### ERROR 3 [CRITICAL]：`AgentInvocation` 是过度设计的、CC 中不存在的抽象

**当前代码**：`AgentInvocation`（~15 个字段，含嵌套对象）

**CC 真实行为**：Agent Tool 接受 6 个扁平参数，全部是简单类型。CC **没有** invocation 对象——runtime 内部当然有数据结构跟踪一次 spawn，但它不是"数据契约层"暴露给工具调用方的东西。

**为什么错**：
- CC 的 Agent 工具参数远简单于当前 `AgentInvocation`。
- `budget_tokens` / `max_steps` 在 CC 中是 agent definition 的 `maxTurns`，不是 per-invocation 参数。
- `workspace_mode` 映射到 CC 的 `isolation`（取值 "worktree" 或省略），语义更简单。
- `idempotency_key` 在 CC 中不存在。
- `metadata` 在 CC 中不存在（subagent 只有 prompt + description）。

**影响**：如果该模块被称为"CC-aligned contract"，它必须能直接映射到 CC 的 Agent Tool schema。当前做不到。

---

### ERROR 4 [CRITICAL]：`ContextIsolationPolicy` 完全是编造的

**当前代码**：
```python
class ContextIsolationPolicy:
    conversation: str = "fresh"
    read_cache: ReadCachePolicy = ReadCachePolicy.NONE
    global_state: str = "forbid_write"
    background_tasks: BackgroundTaskPolicy = ...
    depth: int = 0
    parent_depth: int = 0
```

**CC 真实行为**：CC 的上下文来源只有两种：
1. **Fresh**：named subagent（fresh context，只看自己的 prompt）
2. **Fork**：fork agent（继承 parent 的不可变快照）

没有 `ReadCachePolicy`、`BackgroundTaskPolicy`、`global_state` 写控制、`depth` 作为 policy 字段。这些是 runtime 内部实现细节，不应作为"数据契约"暴露。

**为什么错**：把 runtime 实现关注点（缓存策略、全局状态写入权限、后台任务策略、嵌套深度）包装成"策略对象"并放入执行契约，是典型的"抽象泄漏"。CC 把这些留在 agent definition 的 YAML 字段和 runtime 内部。

---

### ERROR 5 [HIGH]：`AgentHandle` 是 CC 中不存在的概念

**当前代码**：`AgentHandle` 作为持久引用，含 `child_session_id`, `child_run_id`, `invocation_id`, `state`, `agent_type`, `depth`, `placement`, `created_at`。

**CC 真实行为**：Agent Tool 返回 `{ status, agentId, content }`。`agentId` 就是引用。CC 没有 handle 对象——wait/cancel 直接使用 `agentId`。

**为什么错**：不是"错"在抽象本身，而是把它称为"CC-aligned data contract"是误导。CC 的接口比这简单得多。如果本项目的确有持久化需求需要更丰富的引用，应明确标注为 "Grace Code internal runtime handle，非 CC 接口"。

---

### ERROR 6 [HIGH]：`AgentResult` 字段不匹配 CC 输出

**当前代码**：
```python
class AgentResult:
    child_session_id: str
    child_run_id: str
    state: ChildRunState
    summary: str
    error: str
    clarification_needed: bool = False      # ← 关键错误
    structured_report: Mapping | None
    tokens_used: int
    steps_taken: int
    evidence_refs: tuple[str, ...]
    worktree_disposition: str
```

**CC 真实输出**：`{ status, agentId, content, totalToolUseCount, totalDurationMs, totalTokens, usage, prompt }`。

具体不匹配：
- `clarification_needed`——**CC subagent 不能向用户请求澄清**（subagent 没有 UI）。
- `structured_report`——CC 返回 `content`（plain text），Grace Code 的 `ReportFindings` 是项目自己的结构化扩展。
- `evidence_refs`——CC 没有独立 evidence ref 字段。
- `worktree_disposition`——CC 没有这个字段；worktree 状态在 isolation result 中管理。

---

### ERROR 7 [HIGH]：`clarification_needed` 直接违反 CC 行为

**CC 明确约束**：Sub-agents have **no UI** and **cannot prompt the user for clarification**。

**当前设计**：`AgentResult.clarification_needed: bool = False` 以及在计划文档 §3.6 中："child 需要澄清时返回 `clarification_needed=True`，parent 决定是否发起新 invocation"。

**为什么错**：这暗示 subagent 有能力判断自己需要澄清。在 CC 中，subagent 要么完成，要么报告失败/不完整。如果需要更多信息，subagent 在其 `content` 中说明，parent（不是 subagent）决定下一步。`clarification_needed` 作为结构化 flag 是编造的语义。

**正确替代**：Parent（Orchestrator/主代理）根据 `AgentResult` 的 content 自行判断是否需要追问，而不是依赖一个 boolean flag。

---

### ERROR 8 [MEDIUM]：`ChildRunState` 的 7 态机含 CC 中不存在的状态

**当前代码**：`CREATED → QUEUED → RUNNING → COMPLETED / FAILED / CANCELLED → CLOSED`

**CC 真实行为**：CC 对 parent 暴露的状态只有：
- `completed`（sync 返回）
- `async_launched`（background 返回）
- 后续通过 `/tasks` 或 `TaskOutput` 查询可知 `running` / `completed` / `failed` / `cancelled`

**为什么错**：`QUEUED` 和 `CLOSED` 是内部调度/GC 状态，CC 不暴露给工具层。7 态机作为内部实现没问题，但放入"数据契约"并标榜 CC-aligned 是过度暴露。

---

### ERROR 9 [MEDIUM]：`OrchestrationToolResult` 是 CC 中不存在的包装

**当前代码**：`OrchestrationToolResult(ok, action, handle, result, state, error, retryable)`

**CC 真实行为**：CC 工具直接返回结果。Agent Tool 直接返回 `{ status, agentId, content }`。没有 wrapper。

**为什么错**：这可能是合理的内部抽象（统一工具结果格式），但应与 CC 模型区分。标为"orchestration tool result"暗示它是 orchestration 工具链的标准输出，在 CC 中并不存在。

---

### ERROR 10 [MEDIUM]：`AgentWaitRequest` 和 `AgentCloseRequest` 偏离 CC 模式

**CC 真实行为**：
- Wait 是隐式的：`run_in_background=false` 的同步调用 block 直到 subagent 完成。
- Background subagent 通过 notification（`outputFile` 路径）通信完成状态。
- CC 有 `TaskStop` 工具用于 cancel/kill。

**当前设计**：`AgentWaitRequest(parent_session_id, child_session_id, timeout_seconds)` 和 `AgentCloseRequest(parent_session_id, child_session_id, reason)` 作为显式契约。

**为什么错**：Wait 在 CC 中不是显式 API（它是同步调用的默认行为）。Close 在 CC 中是 TaskStop 工具。命名和契约不匹配。

---

### ERROR 11 [MEDIUM]：`ChildExecutionPlacement` 的命名不匹配 CC

**当前代码**：`FOREGROUND` / `BACKGROUND`

**CC 真实行为**：`run_in_background: true/false`（工具参数）+ `background: true`（agent definition 中的默认值）。

**为什么错**：`ChildExecutionPlacement` 这个 enum 名称在 CC 中不存在。`run_in_background`（boolean）比 `FOREGROUND / BACKGROUND`（enum）更简单、更接近 CC。此外，项目已有 `ExecutionPlacement` enum（`models.py` 中的 `AUTO / FOREGROUND / BACKGROUND`），与 contracts 中的重复定义不一致。

---

### ERROR 12 [LOW]："三个正交机制"整体框架是编造的

**当前设计文档**：
```
机制一：Tool Isolation Mechanism
机制二：Context Isolation Mechanism
机制三：Mode Intent Metadata Mechanism
```

**CC 真实行为**：CC 没有这个框架。工具隔离是 agent definition 中的 `tools` + `disallowedTools`。上下文隔离是 agent type（named → fresh, fork → snapshot）。意图元数据是 agent definition 的 `description` + 调用方的 `description` / `prompt`。三者在 CC 中不是对称的"机制"——它们是不同层面、不同粒度的配置。

**为什么错**："正交机制"为追求架构美观而硬造了三个对等概念，实际上 CC 的三个维度有本质不同的形态和粒度。

---

## 2. 当前方案中保留有效的部分

并非全部错误。以下部分是对 CC 行为的合理映射或项目正当扩展：

| 元素 | 判断 | 原因 |
|---|---|---|
| `ChildContextMode.FRESH`（Phase 1 仅 fresh） | ✓ 正确 | CC named subagent 就是 fresh context |
| 拒绝 `OrchestrationMode` enum | ✓ 正确 | CC 没有 Orchestrator mode，它只是工具配置策略 |
| 拒绝 `WAITING_INPUT` + `AgentResumeRequest` | ✓ 正确 | CC subagent 不能等待用户输入 |
| 拒绝 `fork` / `parent_snapshot`（Phase 1） | ✓ 正确 | Phase 1 明确不做 fork |
| 拒绝 `Agent Team`（Phase 1） | ✓ 正确 | User 要求不做 team |
| 拒绝 `generation` 字段 | ✓ 正确 | CC 不暴露 generation 概念 |
| `metadata` 必须是 JSON-safe | ✓ 正确 | 持久化要求 |
| JSON roundtrip 测试 | ✓ 正确 | 验收要求 |

---

## 3. 指导：正确的新 Phase 1 应该是什么样

### 3.1 不要发明新的抽象层——直接对齐 CC 的两层模型

CC 的模型就是两层：
1. **Agent Definition**：Markdown + YAML frontmatter
2. **Agent Tool**：简单参数 + 简单返回值

Grace Code 已有这两层：
- Agent Definition → [agent/session/agent_definition.py](../agent/session/agent_definition.py) + `.grace/agents/*.md`
- Agent Tool → [agent/session/task_tool.py](../agent/session/task_tool.py)

Phase 1 的新契约应该做的是：**确保这两层的接口与 CC 行为对齐**，而不是在它们之外再发明第三套契约。

### 3.2 Phase 1 的正确范围

Phase 1 应该是：

```text
目标：确保 NativeChildRunner 可以消费一个与 CC 一致的输入，
     产生与 CC 一致的输出。
范围：定义清晰的输入 dataclass 和输出 dataclass，
     只包含 CC 工具 schema 中存在的字段。
明确不包含：执行引擎、调度器、持久化策略、preset 解析。
```

### 3.3 正确的输入契约（对标 CC Agent Tool 参数）

```python
@dataclass(frozen=True, slots=True)
class NativeChildRequest:
    """与 CC Agent 工具 schema 逐字段对应。"""
    description: str          # 3-5 词，CC: "description"
    prompt: str               # 完整任务，CC: "prompt"
    subagent_type: str        # CC: "subagent_type"
    model: str = ""           # CC: "model"（空 = inherit）
    run_in_background: bool = False  # CC: "run_in_background"
    isolation: str = ""       # CC: "isolation"（空 = current，"worktree"）
```

**仅此而已**。不需要 `invocation_id`（runtime 内部生成），不需要 `budget_tokens` / `max_steps`（来自 agent definition），不需要 `context_mode` / `context_policy` / `tool_profile`（都来自 agent definition 解析）。

### 3.4 正确的输出契约（对标 CC Agent 工具返回）

```python
@dataclass(frozen=True, slots=True)
class NativeChildResult:
    """与 CC Agent 工具返回的 discriminated union 对应。"""
    status: str               # "completed" | "failed" | "cancelled"
    agent_id: str             # CC: "agentId"
    content: str              # CC: "content"（subagent final message）
    total_tool_use_count: int = 0      # CC: "totalToolUseCount"
    total_duration_ms: float = 0.0     # CC: "totalDurationMs"
    total_tokens: int = 0              # CC: "totalTokens"
```

**Stop here**。Grace Code 自己的扩展（`structured_report`、`evidence_refs`、`worktree_disposition`）是合法的项目增强，但必须明确标记为 **Grace Code extension**，不与 CC 字段混淆。

### 3.5 Tool Isolation 的正确模型

不需要 `ToolIsolationProfile`。Agent definition 的 YAML frontmatter 中已经有：

```yaml
tools: Read, Grep, Glob       # allowlist
disallowedTools: Write, Edit  # denylist
```

Phase 1 只需要确保 native child runner 在启动 subagent 时，使用对应的 agent definition 的 `tools` / `disallowedTools`。Preset 映射（如 `explore` → 内置 agent definition）是 agent registry 的职责，不应暴露为契约。

### 3.6 Context Isolation 的正确模型

不需要 `ContextIsolationPolicy`。只有两种：

```python
class ContextOrigin(StrEnum):       # 项目已有此 enum!
    FRESH = "fresh"                 # named subagent
    PARENT_SNAPSHOT = "parent_snapshot"  # fork（Phase 1 不做）
```

这就是全部。`read_cache`、`background_tasks`、`global_state` 都是 runtime 实现细节，不是契约。

### 3.7 正确的新文件范围

| 文件 | 动作 |
|---|---|
| `runtime_core/native_child_contract.py` | 新增：`NativeChildRequest` + `NativeChildResult` |
| `tests/runtime_core/test_native_child_contract.py` | 新增：对比测试 (CC schema ↔ Grace Code) |
| `docs/NATIVE_ORCHESTRATION_PHASE1_CC_ALIGNED_PLAN.md` | 新增：基于 CC 对齐的新计划 |

不要在 `application/coordinators/` 下放置——native child 的执行契约属于 runtime_core（执行层），不属于 application 层。

---

## 4. 现有 `orchestration_contracts.py` 中哪些可以保留/迁移

| 当前元素 | 处置 |
|---|---|
| `ChildContextMode.FRESH` | 迁移到新契约；项目已有 `ContextOrigin.FRESH`，应复用而非重复定义 |
| `ChildRunState` 7 态机 | 移到 `agent/session/` 或 `runtime_core/` 作为内部状态跟踪，不暴露为 "CC 对齐契约" |
| `ToolIsolationProfile` | **删除**。用 agent definition 的 `tools`/`disallowedTools` 取代 |
| `ContextIsolationPolicy` | **删除**。Context 只有 fresh/fork 两种 |
| `AgentInvocation` | **删除**。用更简单的 `NativeChildRequest` 取代 |
| `AgentHandle` | **删除**。用 `agent_id` string 取代 |
| `AgentResult` | **删除**。用 `NativeChildResult` 取代 |
| `AgentWaitRequest` / `AgentCloseRequest` | 移到 `agent/session/` 作为内部工具参数，不标 "CC 对齐" |
| `OrchestrationToolResult` | **删除**。工具直接返回 `NativeChildResult` |
| `ReadCachePolicy` / `BackgroundTaskPolicy` | **删除**。不暴露为契约 |
| `metadata` 字段 | 可保留为 Grace Code extension，但必须明确标注 |
| JSON 序列化基础 | 保留，迁移到新契约 |

---

## 5. 结论

当前 Phase 1 方案的根本问题不是某个字段命名不当，而是**整体架构思路错误**：

- CC 的模型是 **两层简单结构**（Agent Definition + Agent Tool），强调"agent definition 文件定义一切 → 工具调用传最少参数 → 返回简单结构"。
- 当前方案是 **三层抽象**（契约层 → Profile/Policy 层 → 执行层），包含大量 CC 中不存在的概念。

正确做法：**先对齐 CC 的两层简单模型，再在其上加 Grace Code 自己的结构化扩展（如 ReportFindings）**。不要在 CC 的简单接口外再包一层复杂抽象然后还是叫 "CC-aligned"。

新 Phase 1 计划应：
1. 定义 `NativeChildRequest` / `NativeChildResult`（对标 CC Agent 工具 schema）
2. 复用/对齐项目已有的 `ContextOrigin`、`AgentKind`、`ExecutionPlacement`
3. 确保 agent definition（`.grace/agents/*.md` 的 YAML frontmatter）正确解析 `tools`/`disallowedTools`
4. Grace Code 扩展（`structured_report`、`evidence_refs` 等）明确标注
5. 不在契约层发明 preset/policy/profile 等 CC 不存在的概念
