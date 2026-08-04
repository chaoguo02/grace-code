# Native Orchestration Phase 1 — CC 对齐版实现计划

> 文档版本：2.2.0（Phase 1 + 2 已完成 ✅）  
> 当前基线：`master`（Phase 0a + 1 + 2 已完成）  
> **v2.2 更新**：Phase 2 完成——`NativeAgentTool` 注册到 assemble()，12/12 tests pass。  

---

## 0. 为什么废弃 v1.x

[v1.x 计划](./NATIVE_ORCHESTRATION_PHASE1_DATA_CONTRACT_PLAN_2026-08-04.md) 和 [orchestration_contracts.py](../application/coordinators/orchestration_contracts.py) 的根本问题：

| 问题 | v1.x 做了 | CC 真实行为 |
|---|---|---|
| `ToolIsolationProfile`（含 preset/三道门） | 编造了 8 字段策略对象 | CC 只有 `tools`(allowlist) + `disallowedTools`(denylist)，在 agent definition YAML 中 |
| `ContextIsolationPolicy` | 编造了 ReadCache/BackgroundTask/global_state | CC 上下文只有 fresh/fork 两种 |
| `AgentInvocation`（15 字段含嵌套） | 过度设计 | CC Agent Tool 只有 6 个扁平参数 |
| `AgentHandle` | 编造了引用对象 | CC 只用 `agentId` string |
| `clarification_needed` | 编造了交互协议 | CC subagent 无 UI，不能请求澄清 |
|  放在 `application/coordinators/` | 位置错误 | 执行契约属于 `runtime_core` |

**新计划的核心原则**：CC 已经定义好了 subagent 模型，Grace Code 不需要重新发明它。Phase 1 的任务是**接线**——让现有的 NativeStepLoop 能启动 child。

---

## 1. CC 的两层简单模型（我们的目标基线）

```
Layer 1: Agent Definition (.grace/agents/*.md YAML frontmatter)
  → name, description, tools, disallowedTools, model, maxTurns, ...
  
Layer 2: Agent Tool (NativeStepLoop 注册的 delegation 工具)
  → 输入: { description, prompt, subagent_type, model?, run_in_background?, isolation? }
  → 输出: { status, agent_id, content, totalToolUseCount, totalDurationMs, totalTokens }
```

**Grace Code 已有 Layer 1**：[AgentDefinition](agent/session/agent_definition.py) + `.grace/agents/*.md` 已完整实现所有 CC 字段。不需要改。

**Grace Code 缺少 Layer 2 的 Native 路径实现**。当前 Agent Tool 走的是旧 `SessionRuntime` 路径（`ReActAgent` + `LLMBackend`），与 `AgentRuntime` + `NativeStepLoop` 完全不连通。

---

## 2. 当前接线状态

```
                          compose/assemble()
                               |
                          RuntimePorts (global)
                               |
                          AgentRuntime
                               |
                   NativeStepLoop.execute(RuntimeExecution)
                               |
                   NativeBackend.invoke(NativeConversation)     ← 能跑
                  ═════════════════════════════════════════════════
                         FRACTURE — no child path
                  ═════════════════════════════════════════════════
                          spawn_agent()
                               |
                   _execute_child_session()
                               |
                   run_child_agent()
                               |
            ReActAgent(LLMBackend, ToolRegistry).run()           ← 旧路径
```

两条链路用**完全不同的基础设施**。Phase 1 的目标是造桥——让 Native 路径能跑 child。

---

## 3. 断裂点 × 修复方案

### Gap 1：NativeStepLoop 没有 Agent 工具

**现状**：`NativeStepLoop` 注册了什么工具？没有。整个 loop 只知道 model → tool_execute → model，没有任何 delegation 工具。

**修复**：在 `assemble()` 中，为 Native 路径构造 `_RealTools` 时注册 `Agent` 工具。工具名称和 CC 保持一致：`Agent`。

```python
# 在 composition/runtime_composition.py 中
# 给 _RealTools 注册 native_agent_tool
```

### Gap 2：Child RuntimePorts 构造

**现状**：`assemble()` 产生单个全局 `RuntimePorts`，所有工具全集可用。Child 需要**过滤后的**工具集。

**修复**：新增 `child_runtime_ports(parent_ports, agent_definition)` 工厂函数。输入 parent 的 `RuntimePorts` + child 的 `AgentDefinition`，输出过滤后的 `RuntimePorts`：

```python
def child_runtime_ports(
    parent_ports: RuntimePorts,
    definition: AgentDefinition,
) -> RuntimePorts:
    """从 parent ports 构造 child 专用 ports。
    
    工具过滤：parent 可用工具 ∩ definition.tools − definition.disallowedTools
    """
```

### Gap 3：Tool Name → NativeToolSchema 转换

**现状**：`AgentDefinition.tools` 是 `frozenset[str]`（只有名称）。`NativeBackend` 构造时需要 `NativeToolSchema(name, description, input_schema: dict)`。

**修复**：从 parent `NativeBackend` 已缓存的 `tool_schemas` 中按名称过滤：

```python
def filter_tool_schemas(
    parent_schemas: tuple[NativeToolSchema, ...],
    allowed: frozenset[str],
    disallowed: frozenset[str],
) -> tuple[NativeToolSchema, ...]:
    """从 parent schemas 过滤出 child 可用子集。"""
    return tuple(
        s for s in parent_schemas
        if s.name in allowed and s.name not in disallowed
    )
```

### Gap 4：Child NativeBackend 构造

**现状**：`NativeBackend` 的 tool_schemas 在 `__init__` 绑定。Child 可能需要不同 model、不同 API key。

**修复**：`NativeBackend` 已支持 `from_backend()` 工厂方法。新建 `NativeBackend.for_child()` 方法：

```python
@classmethod
def for_child(cls, parent: "NativeBackend", tool_schemas, model="") -> "NativeBackend":
    """从 parent backend 构造 child backend，复用 client，覆盖 tools/model。"""
```

### Gap 5：Child Conversation 初始化（NativeMessage 格式）

**现状**：`_build_system_messages()` 在 `subagent.py` 中构造 `list[LLMMessage]`。Native 路径需要 `list[NativeMessage]`。

**修复**：新增 `build_child_conversation(definition, prompt)` → `NativeConversation`：

```python
def build_child_conversation(
    definition: AgentDefinition,
    prompt: str,
    project_dir: str = "",
) -> NativeConversation:
    """构造 child 的初始 conversation（system prompt + skill + memory + user prompt）。
    
    等价于 subagent.py _build_system_messages()，但输出 NativeMessage 而非 LLMMessage。
    """
```

### Gap 6：RuntimeOutcome → AgentRunResult 转换

**现状**：`NativeStepLoop.execute()` 返回 `RuntimeOutcome`。调用方（spawn 层）期望 `AgentRunResult`。

**修复**：转换函数：

```python
def outcome_to_agent_result(outcome: RuntimeOutcome, child_session_id: str) -> "AgentRunResult":
    """Native RuntimeOutcome → legacy AgentRunResult（兼容层，后续移除）。"""
```

### Gap 7：Child 执行入口

**现状**：Child 执行走 `run_child_agent()` → `ReActAgent.run()`（旧路径）。

**修复**：新增 `run_native_child()` 函数，直接调 `AgentRuntime.run()`：

```python
def run_native_child(
    ports: RuntimePorts,
    session_id: SessionId,
    run_id: RunId,
    conversation: NativeConversation,
    cancellation: CancellationHandle,
    max_steps: int,
    budget_tokens: int,
) -> RuntimeOutcome:
    """在 Native 路径上执行一次 child run。
    
    = 现有 AgentRuntime.run(RuntimeExecution(...)) 的 thin wrapper。
    与 legacy run_child_agent() 功能对等，但使用 NativeStepLoop。
    """
    runtime = AgentRuntime(ports)
    ctx = RuntimeExecution(
        session_id=session_id,
        run_id=run_id,
        cancellation=cancellation,
        max_steps=max_steps,
        budget_tokens=budget_tokens,
        conversation=ConversationSnapshot(messages=conversation.to_api_messages()),
    )
    return runtime.run(ctx)
```

---

## 4. Phase 1 数据契约（对齐 CC，仅此而已）

### 4.1 输入：对标 CC Agent Tool 参数

```python
# runtime_core/native_child_contract.py

@dataclass(frozen=True, slots=True)
class NativeChildRequest:
    """对标 CC Agent 工具 schema 的 child 执行请求。
    
    CC 真实参数: description, prompt, subagent_type, model, run_in_background, isolation.
    Grace Code 在 CC 基础上仅增加 idempotency_key（幂等保障，非 CC 字段）。
    """
    description: str       # 3-5 词
    prompt: str            # 完整任务
    subagent_type: str     # agent definition 名称
    model: str = ""        # 空 = inherit parent
    run_in_background: bool = False
    isolation: str = ""    # 空 = current, "worktree"
    idempotency_key: str = ""  # Grace Code 扩展
```

### 4.2 输出：对标 CC Agent 工具返回

```python
@dataclass(frozen=True, slots=True)
class NativeChildResult:
    """对标 CC Agent 工具返回结构。
    
    CC 返回: status, agentId, content, totalToolUseCount, totalDurationMs, totalTokens.
    Grace Code 扩展: error, structured_report, evidence_refs, worktree_disposition.
    """
    status: str            # "completed" | "failed" | "cancelled"
    agent_id: str          # child session id (CC: agentId)
    content: str           # subagent final message (CC: content)
    total_tool_use_count: int = 0
    total_duration_ms: float = 0.0
    total_tokens: int = 0
    
    # ── Grace Code 扩展（明确标注，不与 CC 字段混淆）──
    error: str = ""
    structured_report: dict | None = None
    evidence_refs: tuple[str, ...] = ()
    worktree_disposition: str = "not_applicable"
```

### 4.3 不在 Phase 1 出现的

| 不在 Phase 1 | 原因 |
|---|---|
| `ToolIsolationProfile` / `ContextIsolationPolicy` | CC 不存在。工具隔离 ∈ AgentDefinition，上下文只有 fresh/fork |
| `AgentInvocation` / `AgentHandle` / `AgentWaitRequest` / `AgentCloseRequest` | 过度抽象。CC 只有 `NativeChildRequest` + `agent_id` + 同步/异步 |
| `OrchestrationToolResult` wrapper | CC 工具直接返回结果 |
| `ChildRunState` 7 态机 | runtime 内部实现，不暴露为契约 |
| `preset` 概念 | CC agent type 就是 agent definition，不是 tool preset |
| `clarification_needed` | CC subagent 无 UI，不能请求澄清 |
| `ReadCachePolicy` / `BackgroundTaskPolicy` | runtime 实现细节 |
| `metadata` 字段 | 保留作为 Grace Code 扩展，但在 Phase 1 不做 |

---

## 5. 文件范围

| 文件 | 动作 |
|---|---|
| `runtime_core/native_child_contract.py` | **新增**：`NativeChildRequest` + `NativeChildResult` |
| `runtime_core/native_child_runner.py` | **新增**：`run_native_child()` + 平台函数 |
| `composition/runtime_composition.py` | **修改**：注册 Agent 工具、child ports 工厂 |
| `tests/runtime_core/test_native_child.py` | **新增**：Before Tests + Target Tests |
| `docs/NATIVE_ORCHESTRATION_PHASE1_CC_ALIGNED_PLAN_2026-08-04.md` | 本文档 |
| `docs/PHASE1_CC_ALIGNMENT_CRITIQUE_2026-08-04.md` | 批判报告（已完成） |

**删除或标记废弃**：
- `application/coordinators/orchestration_contracts.py` — 删除（完全被新方案取代）
- `tests/application/test_orchestration_contracts.py` — 删除
- `docs/NATIVE_ORCHESTRATION_PHASE1_DATA_CONTRACT_PLAN_2026-08-04.md` — 标记废弃

**禁止修改**：
- `agent/session/subagent.py`（旧路径，保留不动）
- `agent/session/runtime_spawn.py`（旧路径，保留不动）
- `runtime_core/native_step_loop.py`（已能跑，不改）

---

## 6. Before Tests（先写，必须 fail）

### BT-1：`NativeChildRequest` 字段匹配 CC

```python
def test_child_request_matches_cc_agent_tool_schema():
    """NativeChildRequest 的字段 = CC Agent 工具的字段（+ Grace Code 扩展）。"""
    from runtime_core.native_child_contract import NativeChildRequest
    
    req = NativeChildRequest(
        description="Find relevant files",
        prompt="Inspect the routing layer",
        subagent_type="explore",
    )
    # CC 字段
    assert req.description == "Find relevant files"
    assert req.prompt == "Inspect the routing layer"
    assert req.subagent_type == "explore"
    assert req.model == ""           # inherit
    assert req.run_in_background is False
    assert req.isolation == ""       # current
    # 无 preset/profile/policy/mode
    assert not hasattr(req, "tool_profile")
    assert not hasattr(req, "context_policy")
    assert not hasattr(req, "context_mode")
    assert not hasattr(req, "metadata")
    assert not hasattr(req, "budget_tokens")
    assert not hasattr(req, "max_steps")
```

### BT-2：`NativeChildResult` 字段匹配 CC

```python
def test_child_result_matches_cc_agent_tool_output():
    """NativeChildResult 的核心字段 = CC Agent 输出字段。"""
    from runtime_core.native_child_contract import NativeChildResult
    
    result = NativeChildResult(
        status="completed",
        agent_id="agent-abc123",
        content="Found 3 files: ...",
        total_tool_use_count=5,
        total_duration_ms=1200.0,
        total_tokens=3500,
    )
    assert result.status == "completed"
    assert result.agent_id == "agent-abc123"
    assert result.content == "Found 3 files: ..."
    # Grace Code 扩展默认值
    assert result.error == ""
    assert result.structured_report is None
    assert result.evidence_refs == ()
    # 无 clarification_needed
    assert not hasattr(result, "clarification_needed")
```

### BT-3：JSON roundtrip

```python
def test_child_contract_json_roundtrip():
    """NativeChildRequest / NativeChildResult 可 JSON 往返。"""
    req = NativeChildRequest(
        description="Search codebase",
        prompt="Find all API endpoints",
        subagent_type="explore",
    )
    result = NativeChildResult(
        status="completed",
        agent_id="agent-xyz",
        content="Found 12 endpoints",
        total_tool_use_count=8,
        total_duration_ms=3400.0,
        total_tokens=8200,
    )
    for obj in (req, result):
        d = obj.to_dict()
        restored = type(obj).from_dict(d)
        assert restored == obj
```

### BT-4：Tool filter 正确过滤

```python
def test_filter_tool_schemas_respects_allowlist_and_denylist():
    """Child 工具 = parent ∩ allowed − disallowed。"""
    from runtime_core.native_backend import NativeToolSchema
    
    schemas = (
        NativeToolSchema("Read", "Read file", {}),
        NativeToolSchema("Write", "Write file", {}),
        NativeToolSchema("Grep", "Search code", {}),
        NativeToolSchema("Bash", "Shell command", {}),
    )
    filtered = filter_tool_schemas(
        schemas,
        allowed=frozenset({"Read", "Grep", "Bash"}),
        disallowed=frozenset({"Bash"}),
    )
    names = {s.name for s in filtered}
    assert names == {"Read", "Grep"}
```

### BT-5：Child RuntimePorts 工具受限

```python
def test_child_ports_has_restricted_tools():
    """从 parent ports 构造的 child ports 只暴露 allowed 工具。"""
    # 使用 mock RuntimePorts，验证 child ports 的工具集受限
```

### BT-6：`run_native_child` 能完成一次 fresh child 执行

```python
def test_run_native_child_completes():
    """用 fake backend，child 应能完成一次执行并返回 RuntimeOutcome。"""
```

### BT-7：禁止的导入边界

```python
def test_native_child_has_no_legacy_imports():
    """native_child_contract.py / native_child_runner.py 不导入旧路径。"""
    import ast, os
    for module_name in ("native_child_contract", "native_child_runner"):
        path = f"runtime_core/{module_name}.py"
        # 不存在则 skip（BT 先于实现）
        ...
    # SessionRuntime, LLMMessage, ReActAgent 零命中
```

---

## 7. 实现步骤（实际执行）

### Step 1 ✅：新建 `native_child_contract.py` + 测试

**内容**：`NativeChildRequest` + `NativeChildResult` + `to_dict/from_dict` + JSON roundtrip。

**验收**：BT-1, BT-2, BT-3 pass → 3/3。

### Step 2 ✅：新建 `native_child_runner.py` + 平台函数

**内容**：
- `filter_tool_schemas(parent_schemas, allowed, disallowed)` → tuple[NativeToolSchema, ...]
- `build_child_conversation(definition, prompt, ...)` → NativeConversation
- `child_runtime_ports(parent_ports, definition)` → RuntimePorts
- `run_native_child(ports, session_id, run_id, conversation, ...)` → RuntimeOutcome

**验收**：BT-4, BT-5, BT-6 pass → 3/3。

### Step 4 ✅：静态边界检查 + 旧 Contract 清理

**内容**：
- `application/coordinators/orchestration_contracts.py` — **删除**
- `tests/application/test_orchestration_contracts.py` — **删除**
- BT-7 pass → `SessionRuntime|ReActAgent|LLMMessage` 零命中

### Step 3 → 移入 Phase 2：在 `assemble()` 中注册 Agent 工具

**移入原因**：`NativeAgentTool` 需要 definition registry + parent backend 注入 + 真实 tool schema 过滤链，属于 Phase 2 能力范围。Phase 1 的 `run_native_child` 已能在 fake backend 下完成一次执行——Phase 2 只需补真实 backend 和 Agent 工具注册。

---

## 8. 验证总策略

```bash
# 每一步完成后
python -m pytest tests/runtime_core/test_native_child.py -v

# 全部完成后
python -m pytest tests/runtime_core/ tests/composition/ -q

# 静态边界
rg -n "SessionRuntime|ReActAgent|LLMMessage" runtime_core/native_child_runner.py
# 必须零命中
```

---

## 9. 明确不做（后续 Phase）

- Fork agent（`ContextOrigin.PARENT_SNAPSHOT`）
- 嵌套 child 执行（`max_subagent_spawn_depth > 1`）
- Background child（`run_in_background=True` 的异步路径）
- Worktree isolation（`isolation="worktree"`）
- Agent Team
- `structured_report` 的 ReportFindings 实际执行
- `NativeChildRequest` 的 `model` / `run_in_background` / `isolation` 字段的运行时生效

---

## 10. 与 v1.x 的差异对比

| 维度 | v1.x（废弃） | v2.0（本计划） |
|---|---|---|
| 位置 | `application/coordinators/` | `runtime_core/` |
| 输入契约 | `AgentInvocation`（15 字段） | `NativeChildRequest`（7 字段，逐一对 CC） |
| 输出契约 | `AgentResult` + `AgentHandle` | `NativeChildResult`（逐一对 CC） |
| 工具隔离 | `ToolIsolationProfile`（8 字段） | `AgentDefinition.tools/disallowedTools`（已有） |
| 上下文隔离 | `ContextIsolationPolicy`（6 字段） | 只有 fresh（不用 policy 对象） |
| 状态机 | `ChildRunState`（7 态） | 内部实现，不暴露 |
| Preset | `preset="explore"` 概念 | 不存在（agent type = agent definition） |
| Clarification | `clarification_needed` flag | 不存在（CC subagent 无 UI） |
| 接线 | 不接线 | **核心工作就是接线** |
| 文件数 | 3 | 6 |
