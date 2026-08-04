# Native Orchestration Phase 1 — Native Child Execution Contract 设计计划

> 文档版本：1.2.0  
> 创建日期：2026-08-04  
> 当前基线：`master` 最新远端（`070b9ae Final cleanup: delete old StepLoop, message_builder, and Legacy tests.`）  
> 适用范围：Native fresh child 的执行事实契约 + 工具隔离/上下文隔离/意图元数据三个正交机制  
> 明确不包含：fork agent、session-based child、Agent Team、worker 间通信、实际执行实现、UI 实现  
> 状态：PLANNING ONLY — 本文只定义契约与验收

---

# 0. 总结

Phase 1 的目标不是定义“subagent 模式”或“orchestrator 模式”。底层 runtime 只需要一种实体：

```text
Native Child Execution Contract
```

它表达 **一次 fresh child 执行** 的完整事实：创建意图、稳定引用、状态机、终态结果。所有上层差异（subagent、orchestrator、plan）都通过三个正交机制参数化，而不是让主体契约出现不同变体：

1. **Tool Isolation Mechanism**：child 能做什么。
2. **Context Isolation Mechanism**：child 能看到什么、能改什么。
3. **Mode Intent Metadata Mechanism**：人类/UI/合成层如何解释这次执行。

核心原则：

```text
主体是“动词”（一次执行），不是“名词”（角色）。
模式差异是对执行的参数化配置，不是执行契约本身的分叉。
```

---

# 1. 当前代码基线

| 能力 | 当前位置 | Phase 1 处理方式 |
|---|---|---|
| 单 run runtime | [`runtime_core/runtime.py`](../runtime_core/runtime.py) | 后续 child execution 内核；本阶段不改 |
| Native loop | [`runtime_core/native_step_loop.py`](../runtime_core/native_step_loop.py) | 后续调用；本阶段不改 |
| Native message | [`runtime_core/native_message.py`](../runtime_core/native_message.py) | result/trace JSON 契约保持兼容 |
| Conversation store | [`runtime_core/conversation_store.py`](../runtime_core/conversation_store.py) | 后续 child transcript 存储基础 |
| RuntimeExecution | [`runtime_core/execution.py`](../runtime_core/execution.py) | 后续 invocation → execution 的映射目标 |
| Legacy spawn | [`agent/session/runtime_spawn.py`](../agent/session/runtime_spawn.py) | 只提取语义，不依赖实现 |
| Legacy child runner | [`agent/session/subagent.py`](../agent/session/subagent.py) | 不复用执行路径 |
| Legacy Agent/AgentBatch tools | [`agent/session/task_tool.py`](../agent/session/task_tool.py), [`agent/session/agent_batch_tool.py`](../agent/session/agent_batch_tool.py) | 参考行为，不继承类 |

建议契约模块：

```text
application/coordinators/orchestration_contracts.py
```

原因：application 层是 coordinator 命令边界；runtime_core 不应反向了解 session/delegation 业务；后续 tools/server/runtime composition 都能稳定引用。

---

# 2. 绝对边界

## 2.1 MUST

1. **只定义数据契约**：dataclass / enum / 状态机语义 / JSON 序列化格式。
2. **Native-first**：契约必须能服务 `AgentRuntime + NativeStepLoop`，但本阶段不接线。
3. **Fresh child only**：child 不继承 parent history，也不等待输入。
4. **单一主体契约**：无论上层意图是 subagent、orchestrator、plan，底层都是同一个 `AgentInvocation/AgentHandle/AgentResult`。
5. **正交机制表达差异**：运行时差异只能通过 Tool Isolation 与 Context Isolation 表达。
6. **Intent 只做元数据**：metadata 仅用于审计/UI/prompt/合成策略，不参与 runtime 分支。
7. **可持久化**：public contract 必须 JSON-safe。
8. **可测试**：每个关键契约有 Before Test 和 Target Test。

## 2.2 MUST NOT

1. **不做 fork agent**：不设计 `parent_snapshot`、prefix replay、tool schema digest、fork resume。
2. **不做 session-based child**：不设计 `send_input`、`resume_agent`、`WAITING_INPUT`、multi-generation handle。
3. **不做 Agent Team**：没有 teammate、mailbox、shared task board、worker-worker channel。
4. **不定义 OrchestratorMode**：Orchestrator 是工具配置 + system prompt 策略，不是 runtime mode。
5. **不改现有执行链路**：不接 `NativeStepLoop`，不改 `SessionRuntime`，不改工具注册。
6. **不实现调度/UI**：只定义执行事实和隔离配置。
7. **不让 worker 具备 delegation 能力**：契约层必须能表达 worker delegation tools 被移除。

---

# 3. 主体：Native Child Execution Contract

这是唯一运行时实体。它只表达“一次 fresh child 执行”，不表达“我是 orchestrator / plan / code reviewer”。

包含契约：

```text
AgentInvocation
AgentHandle
AgentResult
ChildRunState
ChildContextMode
ChildExecutionPlacement
OrchestrationToolResult
```

## 3.1 ChildContextMode

```python
class ChildContextMode(StrEnum):
    FRESH = "fresh"
```

Phase 1 只允许 `fresh`。

禁止字段/枚举：

```text
fork
parent_snapshot
prefix_digest
tool_schema_digest
```

## 3.2 ChildExecutionPlacement

```python
class ChildExecutionPlacement(StrEnum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"
```

`AUTO` 不进入事实层。工具 UX 可以接受 `auto`，但必须在提交 coordinator 前解析成 foreground/background。

## 3.3 ChildRunState

```python
class ChildRunState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLOSED = "closed"
```

状态机：

```text
CREATED -> QUEUED -> RUNNING -> COMPLETED
                         |        -> FAILED
                         |        -> CANCELLED
COMPLETED/FAILED/CANCELLED -> CLOSED
```

规则：

- 无 `WAITING_INPUT`。
- `CLOSED` 是控制面收尾，不删除事实。
- `COMPLETED -> RUNNING` 非法；需要新 invocation。

## 3.4 AgentInvocation

```python
@dataclass(frozen=True, slots=True)
class AgentInvocation:
    invocation_id: str
    parent_session_id: str
    parent_run_id: str
    agent_type: str
    description: str
    prompt: str
    context_mode: ChildContextMode = ChildContextMode.FRESH
    placement: ChildExecutionPlacement = ChildExecutionPlacement.FOREGROUND
    workspace_mode: str = "current"
    tool_profile: ToolIsolationProfile = field(default_factory=ToolIsolationProfile)
    context_policy: ContextIsolationPolicy = field(default_factory=ContextIsolationPolicy)
    budget_tokens: int = 50_000
    max_steps: int = 10
    idempotency_key: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
```

不变量：

- `invocation_id` 非空。
- `parent_session_id` / `parent_run_id` 非空。
- `agent_type` 非空。
- `description` / `prompt` 非空。
- `context_mode` 只能是 `fresh`。
- `budget_tokens > 0`。
- `max_steps > 0`。
- `metadata` 必须 JSON-safe。
- 主体契约不含任何 orchestrator/plan mode 字段。

## 3.5 AgentHandle

```python
@dataclass(frozen=True, slots=True)
class AgentHandle:
    child_session_id: str
    child_run_id: str
    invocation_id: str
    parent_session_id: str
    state: ChildRunState = ChildRunState.CREATED
    agent_type: str = ""
    depth: int = 1
    placement: ChildExecutionPlacement = ChildExecutionPlacement.FOREGROUND
    created_at: str = ""
```

规则：

- 不含 `generation`。
- `child_session_id + child_run_id + invocation_id` 稳定引用一次 fresh execution。
- `child_run_id` 唯一标识一次执行。

## 3.6 AgentResult

```python
@dataclass(frozen=True, slots=True)
class AgentResult:
    child_session_id: str
    child_run_id: str
    state: ChildRunState
    summary: str = ""
    error: str = ""
    clarification_needed: bool = False
    structured_report: Mapping[str, JsonValue] | None = None
    tokens_used: int = 0
    steps_taken: int = 0
    evidence_refs: tuple[str, ...] = ()
    worktree_disposition: str = "not_applicable"
```

规则：

- parent/orchestrator 合成 result，不转发 transcript。
- child 需要澄清时，返回 `clarification_needed=True`，parent 决定是否发起新 invocation。
- `structured_report` 必须 JSON-safe。
- `evidence_refs` 引用持久化证据，不内嵌大对象。

## 3.7 OrchestrationToolResult

```python
@dataclass(frozen=True, slots=True)
class OrchestrationToolResult:
    ok: bool
    action: str
    handle: AgentHandle | None = None
    result: AgentResult | None = None
    state: ChildRunState | None = None
    error: str = ""
    retryable: bool = False
```

规则：

- 工具结果可直接映射为 Native `tool_result` content。
- 失败必须结构化。
- `retryable` 由 coordinator 判断，不让模型猜。

---

# 4. 机制一：Tool Isolation Mechanism

这是控制 “child 能做什么” 的正交维度。它是安全边界，不是功能开关。

包含契约：

```text
ToolIsolationProfile
```

## 4.1 ToolIsolationProfile

```python
@dataclass(frozen=True, slots=True)
class ToolIsolationProfile:
    preset: str = ""
    global_denylist: tuple[str, ...] = ()
    visible_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    background_whitelist: tuple[str, ...] = ()
    remove_delegate_tools: bool = True
    allow_global_state_write: bool = False
    allow_background_task_registration: bool = True
```

三道门显式分离：

| 门 | 字段 | 说明 |
|---|---|---|
| 全 subagent 通用黑名单 | `global_denylist` | 由系统/coordinator 填充，所有 child 适用 |
| 自定义 agent 黑名单 | `disallowed_tools` | 来自 agent definition，resolver 后名称 |
| background 白名单 | `background_whitelist` | background child 额外收缩 |

Preset 规则：

- `preset` 非空时，`visible_tools/disallowed_tools/background_whitelist` 必须为空。
- `global_denylist` 仍可与 preset 同时存在；它是安全底线，不是 preset 展开结果。
- `preset` 为空时，允许显式工具字段。
- Phase 1 不定义 preset 映射表；只承载 preset 名称与展开后的结果表达。

模式差异如何表达：

| 上层意图 | ToolIsolationProfile 表达 |
|---|---|
| Subagent | `preset="explore"` / `preset="code"` 或显式工具字段 |
| Orchestrator | 解析为只含 orchestration/control/inspection 工具；业务工具不进入 visible set |
| Plan | 解析为只读工具 profile；写入/执行/委托工具进入 deny/exclusion |

关键原则：工具隔离只回答“允许/禁止”，不回答“为什么”。“为什么”由 prompt 和 metadata intent 解释。

---

# 5. 机制二：Context Isolation Mechanism

这是控制 “child 能看到什么、能改什么” 的正交维度。它是数据边界，不是性能优化。

包含契约：

```text
ContextIsolationPolicy
ReadCachePolicy
BackgroundTaskPolicy
```

## 5.1 ReadCachePolicy

```python
class ReadCachePolicy(StrEnum):
    NONE = "none"
    SHARED_READONLY = "shared_readonly"
```

Phase 1 只允许 `NONE`。`SHARED_READONLY` 是未来优化保留值，Phase 1 创建 policy 时不得使用。

明确移除：

```text
copy
```

## 5.2 BackgroundTaskPolicy

```python
class BackgroundTaskPolicy(StrEnum):
    ALLOW_CHILD_OWNED = "allow_child_owned"
    FORBID = "forbid"
```

语义：

- `ALLOW_CHILD_OWNED`：child 可注册后台任务，但 owner 必须是 child session。
- `FORBID`：禁止该 execution 注册后台任务。

Orchestrator 自身后续可使用 `FORBID`，避免协调者生成额外后台副作用。Phase 1 只定义策略，不接工具注册。

## 5.3 ContextIsolationPolicy

```python
@dataclass(frozen=True, slots=True)
class ContextIsolationPolicy:
    conversation: str = "fresh"
    read_cache: ReadCachePolicy = ReadCachePolicy.NONE
    global_state: str = "forbid_write"
    background_tasks: BackgroundTaskPolicy = BackgroundTaskPolicy.ALLOW_CHILD_OWNED
    depth: int = 0
    parent_depth: int = 0
```

规则：

- `conversation` Phase 1 只允许 `fresh`。
- `read_cache` Phase 1 只允许 `none`。
- `global_state` Phase 1 只允许 `forbid_write`。
- `background_tasks` 可为 `allow_child_owned` 或 `forbid`。
- `depth` 由 coordinator 创建时计算并填充；child 不可自行修改。

模式差异如何表达：

| 上层意图 | ContextIsolationPolicy 表达 |
|---|---|
| Subagent | `fresh / none / forbid_write / allow_child_owned / depth=parent+1` |
| Orchestrator | 同上，但可设置 `background_tasks=forbid` |
| Plan | 同上；如需 ephemeral，只放 metadata，不改变 runtime behavior |

---

# 6. 机制三：Mode Intent Metadata Mechanism

这是控制 “上层如何解释这次执行” 的正交维度。它不参与 runtime 决策，仅用于审计、UI、prompt 注入、后续合成策略。

包含契约：

```text
AgentInvocation.metadata
```

约定 key（文档 schema，不在 Phase 1 代码强校验）：

| key | 示例 | 用途 |
|---|---|---|
| `intent` | `delegate` / `orchestrate` / `plan` | UI/审计/合成解释 |
| `agent_role` | `code_reviewer` | 展示 worker 角色 |
| `synthesis_strategy` | `merge_summaries` | 合成策略提示 |
| `output_format` | `structured_steps` | 下游展示/合成期望 |
| `ephemeral` | `true` | 提示 coordinator 后续可丢弃 transcript（Phase 1 不实现） |

关键约束：

- metadata 不影响运行时行为。
- coordinator/tool resolver 不根据 metadata 做权限、上下文、调度分支。
- 所有运行时差异必须通过 ToolIsolationProfile 与 ContextIsolationPolicy 表达。
- metadata 必须 JSON-safe。

设计原则：Intent 是“注释”，不是“指令”。

---

# 7. JSON 序列化契约

所有契约类型必须支持 deterministic JSON：

```text
Dataclass -> dict -> json.dumps(sort_keys=True, ensure_ascii=False)
```

字段限制：

- 不允许 Python object、Path、Enum 对象直接入 JSON；必须转 str/value。
- timestamp 使用 ISO-8601 UTC 字符串。
- id 使用 string。
- tuple 序列化为 list，反序列化回 tuple。
- metadata 必须是 JSON object，不能含任意 class。

建议提供：

```python
def to_dict(self) -> dict[str, JsonValue]
@classmethod
def from_dict(cls, data: Mapping[str, JsonValue]) -> Self
```

验收：同一对象 `to_dict -> json -> from_dict` 后相等。

---

# 8. Before Test 计划

测试文件：

```text
tests/application/test_orchestration_contracts.py
```

## BT-1：主体执行契约存在

当前应 FAIL：模块不存在。

实现后断言：

- 创建 fresh invocation 成功。
- context_mode 只能是 `fresh`。
- prompt/agent_type/parent ids 为空时报 ValueError。

## BT-2：契约 JSON roundtrip

实现后断言：

- `AgentInvocation -> dict -> json -> from_dict` 相等。
- `AgentHandle` roundtrip 相等。
- `AgentResult` roundtrip 相等。

## BT-3：主体契约不含上层模式/会话态/fork

实现后断言：

- 不存在 `OrchestrationMode`。
- 不存在 `AgentMessage` / `AgentResumeRequest`。
- `ChildRunState` 不含 `waiting_input`。
- `AgentInvocation` 不含 `mode/parent_snapshot/prefix_digest/tool_schema_digest`。
- `AgentHandle` 不含 `generation`。

## BT-4：ToolIsolationProfile 三道门 + preset

实现后断言：

- `preset="explore"` 合法。
- preset 非空时显式 tool lists 必须为空。
- `global_denylist` 可与 preset 共存。
- `global_denylist/disallowed_tools/background_whitelist` 分别 roundtrip。
- 默认 `remove_delegate_tools=True`。
- 默认 `allow_global_state_write=False`。

## BT-5：ContextIsolationPolicy 严格隔离

实现后断言：

- 默认 `conversation=fresh`。
- 默认 `read_cache=none`。
- `read_cache=shared_readonly` Phase 1 失败。
- 不存在 `copy` 策略。
- `background_tasks=forbid` 合法。

## BT-6：fresh child 状态机

实现后断言：

- `CREATED -> QUEUED -> RUNNING -> COMPLETED -> CLOSED` 合法。
- `RUNNING -> FAILED/CANCELLED` 合法。
- `COMPLETED -> RUNNING` 非法。
- `CLOSED -> RUNNING` 非法。
- 不存在 `RUNNING -> WAITING_INPUT -> RUNNING`。

## BT-7：Mode intent metadata 只透传

实现后断言：

- `metadata={"intent":"orchestrate", ...}` 可 JSON roundtrip。
- `metadata` 不生成 runtime mode 字段。
- 非 JSON-safe metadata 失败。

## BT-8：clarification result

实现后断言：

- `AgentResult(clarification_needed=True)` 可 JSON roundtrip。
- clarification 不引入 resume/send 契约。

---

# 9. Target Tests

Phase 1 完成后必须通过：

```bash
python -m pytest tests/application/test_orchestration_contracts.py -q
python -m pytest tests/runtime_core/test_native_message.py tests/runtime_core/test_conversation_store.py -q
python -m pytest tests/composition/test_native_object_graph.py -q
```

---

# 10. 文件范围

Phase 1 允许文件不超过 3 个：

| 文件 | 动作 |
|---|---|
| `application/coordinators/orchestration_contracts.py` | 新增/修正契约类型 |
| `tests/application/test_orchestration_contracts.py` | 新增/修正契约测试 |
| `docs/NATIVE_ORCHESTRATION_PHASE1_DATA_CONTRACT_PLAN_2026-08-04.md` | 本文档 |

禁止修改：

- `runtime_core/native_step_loop.py`
- `runtime_core/runtime.py`
- `agent/session/runtime.py`
- `agent/session/runtime_spawn.py`
- `agent/session/subagent.py`
- `agent/session/agent_batch_tool.py`
- `composition/runtime_composition.py`
- UI 文件

---

# 11. 验收标准

1. 新契约模块存在，且不 import legacy execution path。
2. 静态禁止 legacy import：

```bash
rg -n "SessionRuntime|ReActAgent|LLMMessage|AgentSpawnContext|AgentSpawnRequest" application/coordinators/orchestration_contracts.py
# 必须零命中
```

3. 主体契约不过早复杂化：

```bash
rg -n "OrchestrationMode|WAITING_INPUT|AgentMessage|AgentResumeRequest|generation|parent_snapshot|prefix_digest|tool_schema_digest|read_cache.*copy" application/coordinators/orchestration_contracts.py
# 必须零命中
```

4. JSON roundtrip 测试全绿。
5. 状态机测试全绿。
6. Native runtime/message/store 回归切片全绿。
7. `git diff --check` clean。

---

# 12. 后续阶段输入

Phase 1 完成后，后续阶段只能消费这些契约，不应重新定义 command/result shape。

后续可单独设计：

- Phase 2：Native fresh child runner
- Phase 3：tool isolation preset 解析与应用
- Phase 4：`spawn_agent / wait_agent / close_agent` native tools
- Phase 5：Orchestrator 工具配置策略与 system prompt
- Phase 6：persistence/recovery/eventing

明确单独立项：

- fork agent
- session-based child（send/resume/waiting input/multi-generation）
- Agent Team

---

# 13. 结论

Phase 1 的正确边界是：

```text
主体：Native Child Execution Contract
机制一：Tool Isolation Mechanism
机制二：Context Isolation Mechanism
机制三：Mode Intent Metadata Mechanism
```

这样可以让 subagent、orchestrator、plan 都映射到同一个 fresh child 执行动词，同时保持工具权限、上下文边界和上层解释语义彼此正交。