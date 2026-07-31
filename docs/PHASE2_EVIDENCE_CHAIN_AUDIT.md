# Phase 2 Evidence Chain 实施验收与重构建议

> 审计日期：2026-07-30  
> 审计范围：Phase 2 计划、当前工作区中的 Phase 2 实现、主 Agent/子 Agent 运行时、Skill、MCP、Tool、Artifact、Verification、Completion Guard、持久化、压缩恢复、WebSocket 与前端展示  
> 审计原则：本轮只审计，不修改生产代码。结论以真实生产调用链为准，不以“类已经创建”“字段已经添加”或孤立单元测试通过作为完成依据。

## 1. 结论

Phase 2 目前不能按“完成”验收，也不建议在现状上继续做零碎补丁。

代码已经搭出了 Evidence Chain 的主要名词和若干局部组件，但真正的运行时闭环没有成立。最严重的问题不是少展示一个字段，而是证据归属、生命周期、并发隔离、需求生成、子 Agent 传播和持久化顺序这几个基础语义都不可靠。

综合判断：

- 代码结构完成度约为 **55%**：核心类型、Store、Recorder、Guard、SQLite 表等大部分已经出现。
- 端到端功能完成度约为 **25%～30%**：主 Agent 的普通 Tool 调用能进入 Recorder，但 Skill/MCP/Artifact/Verification/Worker/Compaction/UI 之间没有形成可信闭环。
- 生产可用度为 **不通过**：当前实现可能把上一次对话的证据用于本次完成判定，也可能在并发会话间串 Skill 或 MCP 证据。

必须优先推翻的三项设计：

1. 用 `session.root_id or session.id` 充当 `root_run_id`。
2. 由 `SessionRuntime`、`MCPToolIntegration` 和全局 pending 列表保存可变的“当前证据对象”。
3. 先在内存追加，再在锁外 best-effort 持久化，并把内存 Store 当作事实源。

正确方向应当是：

```text
真实 RunIdentity
  -> Runtime-owned EvidenceStoreManager
    -> Run-scoped EvidenceSink/Lease
      -> Primary + Workers + Skill + MCP + Tool 统一写入
        -> SQLite 原子提交并返回 canonical EvidenceEntry
          -> Completion Guard / Compaction Projection / WS / Replay 都只读同一事实源
```

## 2. 审计基准

Phase 2 原计划要求建立以下完整链路：

```text
Skill
  -> MCP activation/exposure
    -> Tool call
      -> Cache provenance
        -> Artifact write
          -> Artifact integrity
            -> Verification
              -> Completion Guard
                -> Persistence / Replay / Frontend
```

验收时采用四个标准：

1. **生产路径接通**：不是测试手工创建对象，而是用户请求真实经过该链路。
2. **单一事实源**：同一语义不能存在两套并行状态。
3. **并发与恢复正确**：多会话、多 Worker、重试、刷新、压缩和进程恢复后仍保持归属及顺序。
4. **失败闭环**：失败不能被吞掉、误判为缺失或被旧证据掩盖。

## 3. 逐阶段完成度

| 阶段 | 计划目标 | 当前状态 | 端到端完成度 | 验收 |
|---|---|---:|---:|---|
| 2A | 消除共享引用，建立每个 root run 的 Store | Store 已有，但 run 身份错误；MCP 和 Skill 又引入共享引用；Worker 未继承 Tool evidence | 25% | 不通过 |
| 2B | typed Evidence 与 Requirements | 类型已建立，但缺真实 `run_id`、强校验、深不可变和可靠顺序 | 60% | 部分 |
| 2C | 统一 Tool 拦截点 | 主 Agent 普通 Tool 路径已接 Recorder；子 Agent、Artifact 分类、参数关联不完整 | 55% | 部分 |
| 2D | Skill 与 MCP 接入 | HTTP/Tool/Preload 各走不同路径；CLI 未接；MCP 使用共享当前 Store | 20% | 不通过 |
| 2E | Artifact 因果关系与 Verification | Write/Edit 只附 metadata，没有生成 Artifact evidence；read-back 未实现 | 10% | 不通过 |
| 2F | Completion Guard | Guard 已调用 `evaluate()`，但 requirements 与 evidence 都可能错误，failed 分支处理有缺陷 | 50% | 不通过 |
| 2G | 持久化、幂等、恢复 | 表和 DAO 已有，但不持久化 sequence、写入非原子、删除不级联、身份错误 | 40% | 不通过 |
| 2H | WS 与前端 | Python 字段已声明，但 EventBus 不赋值，TS 不接收，UI 不展示，terminal 无 summary | 5% | 不通过 |

这里的比例表示“真实能力闭环”，不是代码行数。

## 4. P0：必须先处理的根本问题

### P0-1. `root_run_id` 实际是 root session id

#### 证据

`agent/session/runtime.py` 创建 Store 时使用：

```python
_root_run_id = session.root_id or session.id
```

但本次请求的真实 run id 已经存在于 `run_context.run_id`，它由 `server/routers/sessions.py` 的 `submit_run_turn()` 创建，并通过 `ChatPipeline` 传入 `run_session()`。

#### 后果

同一个会话的多次用户请求共享一个 evidence namespace：

```text
Session S
  Run R1: 查询北京天气 -> 产生 Skill/MCP evidence
  Run R2: 保存报告       -> 加载 R1 + R2 的全部 evidence
```

R2 可能被 R1 的 Skill、MCP、Tool 或 Verification 证据满足。刷新和恢复时，`list_evidence(_root_run_id)` 又会把整个会话历史重新加载，使错误长期存在。

这不是命名问题，而是 Completion Guard 的可信边界已经失效。

#### 正确做法

建立不可混淆的身份模型：

```python
@dataclass(frozen=True)
class EvidenceRunIdentity:
    run_id: str
    root_run_id: str
    root_session_id: str
    session_id: str
    producer_session_id: str
    turn_id: str
```

- `root_run_id` 必须来自当前顶层请求的真实 `run_id`。
- 所有 Worker 显式继承同一个 `root_run_id`。
- `root_session_id` 只用于会话树、资源治理和 UI 导航，不能参与证据满足判定。
- 新一轮用户请求必须创建新的 evidence namespace。
- resume 同一个 run 才加载旧 evidence；新 run 不加载上一轮 evidence。

### P0-2. Store 所有权与生命周期错误

#### 当前问题

`run_session()` 每次新建一个 `RunEvidenceStore`。不同实例即使 root id 相同，也只有 Lock 共享，`_entries` 并不共享。

Store 在主 `run_session()` 的 `finally` 中关闭，但：

- 后台 Worker 可能仍在运行；
- MCP 集成还持有这个 Store；
- `close()` 文档声称之后 `record()` 会抛错，实际 `record()` 没检查 `_closed`；
- 多 Store 实例会各自做内存幂等检查，无法形成进程内单一事实源。

#### 正确做法

由 Runtime 创建唯一的 `EvidenceStoreManager`：

```text
EvidenceStoreManager
  acquire(run_id) -> EvidenceRunLease
  acquire_child(parent_lease, child_session_id) -> EvidenceProducerSink
  release(lease)
  close only when root terminal AND all producers released
```

- Primary、Worker、MCP、Skill 都只拿到窄接口 `EvidenceSink`，不能替换全局 Store。
- StoreManager 按真实 `run_id` 注册唯一实例。
- 生命周期采用引用计数或结构化并发 TaskGroup；不能由 Primary 的 `finally` 提前关闭。
- terminal 之后拒绝新业务 evidence；迟到事件只能进入单独的 late-event 处理策略。

### P0-3. MCP 再次引入“共享当前 Store”

`MCPToolIntegration.set_evidence_store()` 把一次 run 的 Store 写入长生命周期服务字段 `_evidence_store`。

并发场景：

```text
Run A: set_evidence_store(store_A)
Run B: set_evidence_store(store_B)
Run A: MCP connect callback -> 写入 store_B
```

这与 Phase 2 原本要删除的共享可变 `EvidenceLedgerRef` 是同一种缺陷。

#### 正确做法

- MCP manager 只管理连接，不保存“当前 run”。
- 每次 MCP exposure/call 必须携带 `EvidenceSink` 或 `RunIdentity`。
- 连接级事实与运行级事实分开：
  - `MCP_SERVER_CONNECTED` 可以是进程/连接级 telemetry；
  - `MCP_TOOLS_EXPOSED_TO_RUN` 必须是 run-scoped evidence；
  - `MCP_TOOL_CALLED` 必须来自实际 invocation context。

### P0-4. Skill pending 队列会跨会话串数据

`SessionRuntime._pending_skill_activations` 是一个全局 list，不按 session/run 分区。`run_session()` 会 flush 整个列表后 `clear()`。

任何两个并发请求都可能互相消费 Skill activation。

更严重的是 preload 顺序：

1. `run_session()` 先 flush pending activation；
2. 后面 `_build_runtime_messages()` 才发现 preloaded Skill；
3. preload activation 留在全局 list；
4. 下一次不相关 run 把它 flush 走。

#### 正确做法

- 不允许全局 pending list。
- HTTP 请求在创建 run 后立刻获得 run-scoped sink，再记录 Skill。
- preload 必须发生在 Store 创建之后，并直接写当前 run。
- Tool 调用型 Skill 由统一 Tool pipeline 在成功结果后记录。
- CLI 入口也必须先创建/解析 run context，再调用同一个 `SkillActivationService`。
- pending 状态如果无法完全取消，至少必须是 `dict[run_id, deque]`，但这只能作为迁移方案。

### P0-5. 子 Agent 没有完整继承 evidence context

父工具把 `evidence_store` 传给 `runtime_spawn`，因此 Worker started/completed 生命周期可能写入父 Store；但 `run_child_agent()` 创建的 `AgentConfig` 没有显式接收本次 run 的 Store 和 Scope。

结果是：

- 父 Store 能看到“Worker 开始/结束”；
- Worker 内部 Read、Grep、MCP、Write、Test 等 Tool evidence 不一定写入父 Store；
- UI 和 Completion Guard 得到的是 Worker 外壳，而非 Worker 的真实工作证据。

#### 正确做法

`ChildRunContext` 必须显式携带：

```python
root_run_id
producer_session_id
evidence_sink
evidence_scope
mode_policy
budget_lease
cancellation
```

禁止通过复制 root `AgentConfig` 猜测或间接继承这些运行期资源。

## 5. 2A/2B：Store 和数据模型问题

### 5.1 内存先写、锁外持久化不是原子写

当前流程：

1. 锁内检查幂等；
2. `_entries.append(entry)`；
3. 锁外调用 SQLite；
4. 失败时再回滚内存。

竞争窗口内，另一个线程可能读到临时 entry 或把它当作已存在返回；随后首线程持久化失败并删除它。调用者得到一个实际上不存在的 evidence。

`record()` 即使持久化失败仍返回原 entry，也没有把失败暴露给 Completion Guard。

#### 正确做法

SQLite 应当是 canonical authority：

```text
BEGIN
  INSERT ... ON CONFLICT(root_run_id, idempotency_key) DO NOTHING
  SELECT canonical row
COMMIT
return canonical EvidenceEntry
```

之后再更新内存 projection 和发布事件。required evidence 写入失败必须 fail closed，不能静默降级。

### 5.2 `INSERT OR IGNORE` 会造成内存/数据库对象分裂

当幂等键冲突时，数据库保留旧 `evidence_id`，但 `create_evidence()` 不返回数据库中的 canonical row。内存 Store 可能保留新生成的 `evidence_id`。

同一逻辑事件因此在内存和数据库中拥有不同 ID，`depends_on` 会失效。

### 5.3 sequence 没有持久化

`EvidenceEntry` 有 `sequence`，但 `run_evidence` 表没有该列。恢复后所有 entry 的 sequence 变为 0，Store 仅把 `_seq` 设置为条目数量。

这会直接破坏：

- Artifact 必须晚于依赖；
- Verification 后发生写入的 stale 检测；
- 并发 Worker 的确定性时间顺序；
- replay 排序。

#### 正确做法

- 数据库保存 `sequence INTEGER NOT NULL`。
- sequence 由持久化层在每个 run 内原子分配，或使用数据库自增 `id` 作为稳定 ordering token。
- 不允许运行时代码直接改 `_entries` / `_seq`。
- 提供正式的 `load(run_id)` API。

### 5.4 “frozen” 不等于不可变

`EvidenceEntry` 是 frozen dataclass，但 `metadata` 和 requirement 的 `arguments_match` 仍是可变 dict。

应在边界处做深冻结或 canonical JSON value 转换，至少保证：

- 只允许 JSON-safe 类型；
- key 为字符串；
- 大小有限制；
- 记录后不能被调用者原地修改。

### 5.5 缺少模型校验

当前未充分校验：

- 空 `run_id` / `root_run_id`；
- `minimum_count <= 0`；
- 重复 requirement；
- 非法 verification requirement；
- 未规范化或逃逸 workspace 的 artifact path；
- `depends_on` 引用不存在、失败、其他 run 的 evidence；
- `evidence_id` 唯一性。

### 5.6 旧 `EvidenceLedger` 仍保留双重真相

`context/evidence.py` 在有 Store 时把 `add_observation()` 委托给 Store，但旧 `_records` 不更新；其他读取和 phase summary 仍读取 `_records`。

这会出现：

```text
Store 有数据
EvidenceLedger.records 为空
phase summary 仍认为没有 evidence
```

#### 正确做法

不要继续维护双向兼容状态：

- 要么删除 legacy Ledger；
- 要么把它改成纯 projection adapter，所有 read API 都从 `RunEvidenceStore` 查询；
- 迁移完成后删除 `_records` 生产路径。

### 5.7 Evidence 工具未绑定 Store

`EvidenceListTool` / `EvidenceGetTool` 定义了 `bind_store()`，但生产代码没有调用它们，也没有覆盖 `with_run_context()`。

因此 Tool 本身虽然已注册，执行时仍会返回 “No evidence store is attached”。

这属于典型的“接口已经改造，但调用链没有连接”。

## 6. 2C：统一 Tool 拦截点

### 6.1 已完成的部分

主 Agent 的普通 Tool 路径基本形成：

```text
ReActAgent
  -> RunContext
    -> ToolRegistry.with_run_context()
      -> ToolRegistry.execute_tool()
        -> ToolExecutionPipeline
          -> ToolEvidenceRecorder
```

PolicyAware 层也尝试在前置拒绝时记录 blocked evidence。这部分方向正确。

### 6.2 completed evidence 丢失调用参数

`record_started()` 接收 params，但只存 digest；`record_completed()` 的 params 为 `None`。Requirements evaluator 却从 completed entry 的 `metadata` 匹配 `arguments_match`。

真实调用因此无法证明：

```text
weather_get_current(city="北京")
weather_get_current(city="上海")
```

测试之所以能通过，是因为测试手工把城市放进 completed metadata；生产 Recorder 不会这样做。

#### 正确做法

为一次逻辑 invocation 建立稳定关联：

```python
ToolInvocationEvidence:
    invocation_id
    canonical_tool_id
    arguments_digest
    arguments_projection  # 只含 requirement 允许匹配的安全字段
    started_sequence
    terminal_sequence
```

Requirement 应预先 canonicalize，并用同一算法比较，而不是读取任意 result metadata。

### 6.3 Cache hit 被错误替代为另一种终态

当前 cache hit 使用 `EvidenceKind.CACHE_HIT`，不再生成 `TOOL_CALL_COMPLETED`。Evaluator 只匹配 completed，所以缓存命中无法满足 required tool call。

正确语义：

- 调用仍然完成：`TOOL_CALL_COMPLETED(cached=True)`；
- 可选增加独立 `CACHE_HIT` provenance；
- cache evidence 需要 `cache_key`、source fingerprint、生成时的 tool/version/config fingerprint；
- stale cache 不能满足要求。

### 6.4 Tool 分类没有真正实现

原计划要求根据 `ToolEffect` 生成 Artifact/Validation/Worker evidence。目前 Recorder 基本只生成 Tool terminal evidence，少量靠 result metadata 特判 Skill 和 Verification。

应建立唯一的 typed projector：

```text
ToolExecutionResult
  -> ToolEvidenceProjector
       always: TOOL_CALL_COMPLETED
       WRITE_WORKSPACE: ARTIFACT_WRITTEN
       TEST: VALIDATION_COMPLETED
       MCP: MCP_TOOL_CALLED provenance
       Skill: SKILL_LOADED
```

不能把分类逻辑散落到各 Tool、PolicyAware registry 和 MCP integration。

### 6.5 机密信息处理不足

当前仅对顶层参数 key 做简单字符串匹配，result metadata 原样持久化，summary 还会截取原始 output/error 前 500 字符。

风险包括：

- 嵌套 `headers.authorization` 未脱敏；
- URL query token；
- MCP command/env；
- Tool 输出中的密钥；
- 大对象或不可序列化对象被 `default=str` 静默写入。

正确做法是统一 EvidenceSanitizer：

- 递归脱敏；
- schema allowlist；
- 严格大小上限；
- output 只保存 digest 和安全摘要；
- 原始大输出进入已有 ArtifactStore，并按权限引用。

## 7. 2D：Skill 与 MCP

### 7.1 `SkillActivationService` 是未接入的空壳

`skills/activation.py` 声称四种入口汇聚，但生产代码没有实例化或调用它。

当前实际路径：

- HTTP：`agent_service.resolve_user_skill()` 直接计算 fingerprint 并写 pending list；
- Tool：`SkillTool` 把 evidence metadata 塞进 `ToolResult`；
- Preload：runtime prompt builder callback 写 pending list；
- CLI：未找到接入 `record_skill_activation()` 的生产代码。

所以“四入口统一”并未实现。

### 7.2 Skill fingerprint 不含 Skill 内容

fingerprint 主要由 name、source、依赖、allowed tools、file path 构成。`SKILL.md` 在同一路径原地修改时，fingerprint 可能完全不变。

应基于 canonical manifest + 实际内容 hash + loader/schema version 计算版本。

### 7.3 Requirement Factory 混入 weather 专用逻辑

runtime 为任何带 MCP dependency 的 Skill 构造：

```python
mcp:{first_server}:weather_get_current
```

这是把测试 fixture 的领域知识硬编码进通用运行时。非天气 Skill 会得到错误 requirement；多 MCP、多工具和不同参数 schema 都无法表达。

正确做法：

- Skill manifest 显式声明 typed evidence contract；
- 或 buildTool 后的 tool schema 提供 canonical tool id 和 requirement bindings；
- Runtime 不推测 Skill 会调用哪个工具。

### 7.4 Skill arguments 没传到 Runtime

HTTP schema 有 `skill_arguments`，渲染 Skill 时也使用了它，但 `ChatRequest` 和 `run_session()` 只传 `skill_name`。runtime 中 `_skill_args = ""`。

因此 `RequiredToolCall` 的 per-argument requirement 永远不会从真实 Web 请求产生。

### 7.5 MCP canonical identity 不稳定

当前 Recorder 根据 `is_mcp` 和 `mcp_props.server_name` 拼接显示名，其他地方又使用 `mcp:{server}:{tool}`，实际 Tool name 可能已经带 MCP namespace。

必须定义唯一的 canonical id，例如：

```text
mcp://weather_mock/weather_get_current@schema_fingerprint
```

显示名、协议名和 requirement key 不能混用。

### 7.6 MCP fingerprint 语义不正确

当前 fingerprint 基于 server name、transport、command、URL，未可靠包含：

- initialize 返回的 server name/version；
- tool schema；
- MCP 配置版本；
- watchdog 已发现的版本 fingerprint。

同时 command/URL 可能泄露敏感信息。

应复用 MCP Watchdog 的规范化 fingerprint，Evidence 层不要再实现第二套 hash。

### 7.7 exposure 与 connection 证据覆盖不完整

只在 agent-scoped 新连接时记录 connect/expose。已经全局连接或预加载的 MCP server 对当前 run 可见时，不一定产生 run-scoped exposure evidence。

正确模型应在组装当前 run 的 tool pool 时记录“哪些 canonical MCP tools 被暴露给该 run”，而不是只在物理连接发生时记录。

## 8. 2E：Artifact 与 Verification

### 8.1 Artifact evidence 实际没有生产

Write/Edit Tool 只返回：

```python
metadata["evidence"] = {"path": ..., "content_hash": ...}
```

Recorder 最终仍创建 `TOOL_CALL_COMPLETED`，没有创建 `ARTIFACT_WRITTEN`。因此：

- `entries_by_kind(ARTIFACT_WRITTEN)` 在真实写文件流程中为空；
- Artifact requirement 无法被真实写入满足；
- stale verification 检测看不到后续写入；
- runtime evidence summary 看不到 artifact。

这是 Phase 2E 最关键的断链。

### 8.2 `EvidenceScope` 从未被填充

Runtime 创建的是空 `EvidenceScope()`；没有代码把 required call evidence id、parent evidence 或 active Skill id 填进去。

`resolved_dependency_ids()` 还忽略传入的 `store_entries`，不验证 ID 是否：

- 存在；
- 成功；
- 属于同一 run；
- 时间早于 Artifact；
- 被当前任务显式消费。

正确做法不是“把此前所有成功 Tool 都设为依赖”，而是记录实际消费关系：

- 任务输入 evidence；
- Skill activation；
- 读取的 Artifact/Tool result；
- 父 Agent 显式传给 Worker 的 evidence；
- 生成 Artifact 时使用的 requirement satisfaction set。

### 8.3 evaluator 把所有成功 Tool 当 required dependencies

当 `must_depend_on_required_calls=True` 时，当前代码收集 run 中所有成功 `TOOL_CALL_COMPLETED`，而不是仅收集 `requirements.required_tool_calls` 对应的 evidence。

一次无关 Read/Grep 都可能变成 Artifact 的强制依赖，导致要求不可满足。

### 8.4 Read-back integrity 没有实现

虽然枚举中有 `ARTIFACT_OBSERVED` 和 `ARTIFACT_INTEGRITY_CHECKED`，生产代码没有生成它们。

正确流程：

```text
Write/Edit success
  -> workspace path canonicalization
  -> read file from disk
  -> calculate actual hash
  -> ARTIFACT_WRITTEN
  -> optional explicit Read later
  -> ARTIFACT_OBSERVED
  -> compare expected/current hash
  -> ARTIFACT_INTEGRITY_CHECKED
```

不能只 hash Tool 入参中的 content，因为 Tool 返回成功后磁盘内容仍可能不同。

### 8.5 Verification receipt “第一次优先”是错误策略

`agent/core.py` 只在 `verification_receipt is None` 时保存 receipt。

后果：

- 第一次失败、第二次修复后测试通过，Guard 仍看到第一次失败；
- 第一次通过、后来改文件，若没有 artifact evidence，旧通过仍可能被接受；
- 多个 test target 无法聚合。

正确做法：

- 保存每次 validation evidence；
- Completion Guard 根据 requirement 选择“适用于当前 workspace revision 的最新完整验证集合”；
- receipt 必须绑定 `checked_revision` 或受影响文件的 hash set；
- 后续写入自动使相关 receipt stale。

### 8.6 当前 pytest receipt 无法证明覆盖当前改动

Pytest Tool 的 `affected_files=()`，通常也没有可用 `checked_revision`。因此 Guard 无法证明该测试对应当前 workspace。

还需覆盖：

- Bash 中执行 pytest/npm test 等验证命令；
- formatter/typecheck/build；
- Worker worktree 内验证与应用到父 workspace 后的验证不能混用。

## 9. 2F：Completion Guard

### 9.1 Guard 调用位置正确，但输入不可信

Guard 已在 FINISH 路径进入 `evidence_store.evaluate(requirements)`，方向正确。

但由于：

- run scope 错误；
- requirement 构造不完整；
- Artifact evidence 不生产；
- Worker evidence 不完整；
- cache hit 不算 completed；
-参数匹配读取错误字段；

Guard 当前可能同时出现误放行和误阻塞。

### 9.2 failed evidence 分支没有正确反馈

Evaluator 的 matching 列表预先过滤为 succeeded，后面的 `elif matching` “调用过但失败”分支实际上不可达。

另外当 `evaluation.failed` 非空而 `missing` 为空时，Guard 只格式化 missing，可能返回：

```text
required_evidence_missing: 0 items
```

失败原因被丢失。

应区分：

- missing：尚未执行，可 retry；
- failed：执行失败，按错误类型决定 retry/abort；
- blocked：权限阻止，需要用户或策略处理；
- stale：执行过但已被后续状态失效；
- unavailable：环境不支持。

### 9.3 Skill 缺失被标成不可重试

required Skill 未加载当前设为 `retryable=False`，Guard 会直接 abort；但很多情况下可以在下一轮调用 Skill 修复。

retryability 不能由 evidence kind 固定决定，应由 capability availability、policy 和剩余预算共同判断。

### 9.4 Verification 存在两套判定来源

`RunEvidenceRequirements.verification_requirement` 存在，但 evaluator 不使用；Guard 另外读取 `mode_policy.verification_requirement` 和 `CompletionContext.verification_receipt`。

这形成两套验证真相。

正确做法：

- Mode policy 只负责生成 typed requirement；
- Guard 只评估 `RunEvidenceRequirements`；
- Verification receipt 只通过 evidence repository 查询；
- `CompletionContext` 不再保存另一份特殊 receipt。

### 9.5 Completion result 没有保存 evaluation snapshot

最终 run 记录和 `run_terminal` 没有保存：

- 满足了哪些 requirement；
- 使用了哪些 evidence ids；
- 哪些 requirement 被豁免；
- 最终 validation revision；
- completion evaluation version。

没有 snapshot 就无法审计“为什么允许完成”，也无法稳定 replay。

## 10. 2G：持久化与恢复

### 10.1 删除不是级联

`delete_run_evidence()` 的注释称会随 session 删除，但没有找到调用方，表也没有 foreign key。

删除 session 后可能留下 orphan evidence。

### 10.2 恢复条件错误

runtime 先检查 session 是否存在 persisted messages，才加载 evidence。Evidence 恢复不应依赖“是否有消息”。

恢复应以 run record 状态为准：

- 新 run：空 evidence；
- resume running/interrupted run：加载该 run；
- replay：只读加载；
- completed run：不可继续写，除非建立新 run。

### 10.3 Event log 与 evidence persistence 是两条未连接路径

已有 `EventType.EVIDENCE_RECORD` 和 `EventLog.log_evidence_record()`，但 Store 没有接 event callback；runtime 创建 Store 时也没传 event publisher。

结果是 SQLite 可能有 evidence，但 trace/WS 没有；或者 observation 有 Tool 输出但没有 evidence id。

应采用 outbox 或同事务事件：

```text
persist evidence
persist trace/outbox event
commit
publish WS
```

刷新时前端再从 persisted trace/evidence 恢复同一 projection。

### 10.4 缺少查询接口和 replay contract

即使数据库有 evidence，前端刷新后没有清晰的 run evidence API 来重建证据视图。

至少需要：

- `GET /runs/{run_id}/evidence`
- cursor/sequence 分页；
- sanitized summary；
- requirement evaluation snapshot；
- evidence schema version。

## 11. Compaction 感知审计

当前 runtime 只在 run 开始时把 `_build_evidence_summary_text(store)` 追加到 injected messages。

问题：

- 新 run 开始时几乎还没有本轮 Tool evidence；
- 因 run id 错误，它反而可能注入旧轮次 evidence；
- active run 内发生 compaction 后，recovery 只恢复 file cache、skill buffer、项目说明和 memory；
- evidence summary 不会从 Store 重新投影；
- summary 被放成普通 user message，容易与真实用户指令混淆；
- summary 只取最后若干项，不能保证 requirements 所依赖的 evidence 被保留。

#### 正确做法

Evidence 不应该依赖 LLM 上下文存活。它首先是 Runtime state。

Compaction 后按当前 requirement 动态生成受保护 projection：

```text
[RUNTIME EVIDENCE STATE]
run_id
open requirements
satisfied requirements -> evidence ids
failed/stale evidence
active artifact dependencies
worker terminal states
```

- 每次 context materialization 可重建；
- 只注入当前 run；
- 优先保留 completion-critical evidence，而非简单取“最后 8 条”；
- 使用 runtime/system 类型消息，不伪装成用户消息；
- Completion Guard 永远直接读 repository，不读 summary。

## 12. 2H：WebSocket 与前端

### 12.1 后端字段只声明，没有赋值

`WsObservation` 增加了 `evidence`，但 `EventBus` 从 observation 转换时没有传入该字段。

Tool Recorder 也没有把 canonical evidence id 回填到 observation metadata。

### 12.2 TypeScript 类型未接入

`web/src/types/events.ts` 的 `WsObservationEvent` 没有 `evidence` 字段。Tool block 类型和 reducer 也没有保存它。

### 12.3 UI 没有 evidence 展示

未找到：

- cached badge；
- MCP server/fingerprint；
- Artifact “based on N evidence”；
- evidence detail/tooltip；
- completion evidence summary。

### 12.4 `run_terminal` 没有 evidence summary

原计划要求 `_finalize_run()` 增加 `evidence_summary`，当前 payload 没有。TS 的 terminal event 也没有相应字段。

此外 backend 新增了 `partial`、`gave_up`、`blocked` 等 terminal status，而 TS 仍只声明 `completed | failed | cancelled`，需要统一终态模型，否则 UI 可能错误归档。

#### 正确前端流

```text
tool_call event
  -> pending tool block
observation event with evidence refs
  -> close tool block + attach evidence badges
evidence_record event
  -> update run evidence index
run_terminal with completion evaluation
  -> archive turn only after terminal projection committed
refresh
  -> replay same persisted events/evidence without产生第二套 UI 状态
```

前端只做投影，不重新推断证据关系。

## 13. 测试审计

### 13.1 本轮执行结果

- `python -m compileall -q agent context core server skills tools`：通过。
- `pytest tests/test_evidence_chain.py tests/test_e2e_core.py -q --basetemp=...`：50 个测试通过。
- `npm run build`：通过，有现存 bundle size warning。
- 前端 Vitest：16 个文件、44 个测试通过。

第一次 pytest 使用系统默认临时目录时因权限失败；改用 workspace 内 `--basetemp` 后通过。这不是 Phase 2 代码失败。

### 13.2 为什么绿测仍不能验收

`tests/test_evidence_chain.py` 大多直接构造 `EvidenceEntry` 并调用 Store，没有经过真实生产链。

典型例子：

- “四种 Skill 入口”测试手工构造四个 source 字符串，没有调用 HTTP、CLI、Preload、SkillTool。
- “并行 Worker 不串证据”只往同一个内存 Store 顺序写两条记录，没有并行线程和真实 Worker。
- “后台 Worker 不提前完成”只手工写一条 started，再断言没有 completed。
- “级联删除”没有 SQLite、session delete 或断言删除结果。
- “持久化恢复”用 list/lambda 模拟数据库，没有验证 `INSERT OR IGNORE`、sequence 或 canonical row。
- “stale verification”在测试里手写 sequence 比较，没有调用 Completion Guard。
- 参数匹配测试把 city 人工放入 completed metadata，掩盖生产 Recorder 不保存调用参数的问题。
- 没有测试 WebSocket evidence 字段和前端卡片。

### 13.3 必须新增的验收测试

#### Identity / isolation

1. 同一 session 连续两个 run，R1 evidence 不能满足 R2。
2. 两个并发 session 同时激活不同 Skill，不能串 evidence。
3. 两个并发 run 使用同一个 MCP manager，MCP evidence 归属正确。
4. Primary 结束但 background Worker 未结束时，Store 不关闭。

#### Full Tool chain

5. 真实 `ToolRegistry.execute_tool(Read)` 产生 started + completed。
6. 同一次 invocation 重试只有一个逻辑 terminal evidence。
7. cache hit 同时满足 completed requirement，并有 cache provenance。
8. blocked/failed/cancelled 分别产生正确 typed evidence。

#### Skill / MCP

9. HTTP、CLI、Preload、SkillTool 四条生产入口各走统一 ActivationService。
10. Skill 内容变化导致 fingerprint 变化。
11. MCP 已预连接时，当前 run 仍产生 exposure evidence。
12. required MCP 调用按 canonical tool id 和参数正确匹配。

#### Artifact / Verification

13. Write/Edit 真实落盘后产生 `ARTIFACT_WRITTEN` 和实际磁盘 hash。
14. Read-back 产生 observed/integrity evidence。
15. Artifact 只依赖显式消费的 evidence。
16. 测试通过后改文件，旧验证变 stale。
17. 修复后第二次测试通过能替代第一次失败。
18. Worker worktree 验证不能直接满足父 workspace 验证。

#### Persistence / recovery

19. SQLite 并发重复写返回同一个 canonical evidence id。
20. 重启恢复后 sequence、depends_on 和 stale 判断不变。
21. session/run 删除确实清理 evidence。
22. persist 失败时 required evidence 明确阻止完成。

#### Completion / Compaction / UI

23. Completion evaluation 记录 requirement -> evidence id 映射。
24. 压缩后当前 requirement/evidence projection 被重新注入。
25. `WsObservation.evidence` 经 EventBus、TS reducer 到 ToolCard 完整保留。
26. run terminal 包含 evidence summary，刷新 replay 后 UI 一致且不重复。

## 14. 建议的目标架构

### 14.1 核心对象

```text
RunCoordinator
  owns RunIdentity, ModePolicy, BudgetLease, Cancellation, EvidenceRunLease

EvidenceRepository
  owns persistence, atomic idempotency, ordering, queries

EvidenceStoreManager
  owns active run projections and producer leases

EvidenceSink
  narrow run/producer-bound write interface

ToolExecutionObserver
  one interception point for started/terminal/domain projections

EvidenceRequirementCompiler
  compiles trusted contracts to typed requirements

CompletionEvaluator
  pure evaluation over repository snapshot + workspace revision

EvidenceProjector
  produces compaction state, WS events, replay and frontend DTOs
```

### 14.2 单向数据流

```text
HTTP/CLI request
  -> create RunIdentity
  -> compile Requirements
  -> acquire EvidenceRunLease
  -> assemble ToolPool with run-bound EvidenceSink
  -> execute Primary/Workers
  -> persist canonical EvidenceEntries
  -> evaluate completion
  -> persist CompletionEvaluation
  -> publish committed events
  -> frontend projects events
```

不得再出现：

- `set_current_evidence_store(...)`
- runtime 全局 pending activation list
- Tool 自己直接写 Store
- CompletionContext 另存 verification 真相
- UI 根据 Tool 名称猜 Skill/MCP/evidence

### 14.3 Requirement contract

不要从自然语言、acceptance 文本或第一个 MCP server 猜 requirement。

建议定义：

```python
@dataclass(frozen=True)
class EvidenceContract:
    skills: tuple[SkillRequirement, ...]
    tool_calls: tuple[ToolCallRequirement, ...]
    artifacts: tuple[ArtifactRequirement, ...]
    validations: tuple[ValidationRequirement, ...]
```

来源只允许：

- 结构化 HTTP/CLI 请求；
- Skill manifest 的 typed contract；
- PlanContract 的结构化 deliverables/verification；
- DelegationTask 的 typed inputs/outputs；
- ModePolicy 产生的 validation requirement。

每个 requirement 要有稳定 id，CompletionEvaluation 保存满足它的 evidence ids。

## 15. 推荐重构顺序

### Step 0：冻结 Phase 2 增量功能

在 P0 修复完成前，不继续增加 evidence kind 或 UI 卡片。否则会在错误 identity 和 ownership 上堆更多依赖。

### Step 1：重建身份、Repository 和生命周期

- 引入真实 `RunIdentity`。
- 表增加 `run_id/root_run_id/root_session_id/sequence/schema_version`。
- 实现 atomic insert-or-get。
- 建立 StoreManager + Lease。
- Primary/Worker 使用同一个 root run，独立 producer。
- 删除 MCP 当前 Store 和全局 Skill pending list。

验收门槛：跨 run、跨 session、并发 Worker 隔离测试全部通过。

### Step 2：统一 Tool observer

- 保留一个拦截点。
- 统一 started/terminal。
- 引入 canonical tool id。
- 正确关联 params、result、retry、cache。
- 中央 Sanitizer。

验收门槛：所有 Tool outcome 的真实 registry integration tests 通过。

### Step 3：重做 Skill/MCP 接入

- 所有入口调用一个 ActivationService。
- Skill contract 显式声明 MCP/tool requirements。
- MCP exposure 在 assembleToolPool 时按 run 记录。
- fingerprint 复用 Watchdog/Skill loader 的唯一版本算法。

验收门槛：四入口、预连接 MCP、live reload、并发 run 测试通过。

### Step 4：实现真实 Artifact/Verification 链

- ToolEffect projector 生成 Artifact。
- 落盘回读 hash。
- 显式 dependency scope。
- validation 绑定 revision/hash。
- stale invalidation。

验收门槛：write -> verify -> rewrite -> reverify 的真实文件测试通过。

### Step 5：收敛 Completion Guard

- 删除 CompletionContext 中的平行 verification truth。
- Guard 只读取 Repository snapshot + typed requirements。
- 持久化 CompletionEvaluation。
- 明确 missing/failed/blocked/stale/unavailable。

验收门槛：不能被旧 run、旧 revision、失败 evidence 或 cache 污染。

### Step 6：Compaction、WS、Replay、Frontend

- runtime evidence projection 在每次 compaction 后重建。
- evidence 与 trace 使用事务/outbox。
- Observation 携带 evidence refs。
- terminal 携带 evaluation summary。
- 前端类型、store、card、refresh replay 全链路接通。

验收门槛：刷新和重连前后卡片及 completion evidence 完全一致。

### Step 7：删除兼容双路

- 删除 legacy EvidenceLedger 生产状态。
- 删除无调用的 `SkillActivationService` 旧壳后，用正式服务替代。
- 删除 pending list、`set_evidence_store()`、特殊 weather requirement。
- 删除 UI 侧基于名称的推断。

## 16. 可保留与应推翻的部分

### 可以保留

- `EvidenceKind` / `EvidenceStatus` 的总体方向。
- Requirements 与 Evaluation 分离的思路。
- ToolRegistry 统一执行点。
- Completion Guard 在 FINISH 时执行 runtime check 的位置。
- SQLite 独立 evidence 表的方向。
- `WsObservation` 携带 optional evidence refs 的方向。

### 需要重写

- Store identity 与生命周期。
- 持久化幂等和 sequence。
- Skill activation 汇聚。
- MCP evidence 绑定方式。
- Requirement Factory。
- Artifact/depends_on/verification 生成逻辑。
- Compaction evidence projection。
- WS 到前端的完整 evidence 数据流。

### 应直接删除

- weather-specific `primary_tool` 推断。
- 全局 `_pending_skill_activations`。
- `MCPToolIntegration._evidence_store` / `set_evidence_store()`。
- “第一次 verification receipt 优先”。
- `EvidenceScope()` 空对象作为已完成接入的做法。
- 测试中不触达生产入口却声称验证完整链路的用例名称和断言方式。

## 17. 最终验收定义

Phase 2 只有同时满足以下条件才算完成：

1. 一次用户请求对应唯一真实 run evidence namespace。
2. Primary 和所有 Worker 使用同一 root run、不同 producer，且并发不串。
3. Skill、MCP、Tool、Cache、Artifact、Verification 都由真实生产入口产生 typed evidence。
4. Artifact 能证明依赖来源、落盘 hash 和验证 revision。
5. Completion Guard 能给出 requirement -> evidence 的确定映射。
6. evidence 写入幂等、原子、可恢复，重启后顺序和判定不变。
7. compaction 不影响 Runtime 判定，LLM 能恢复当前关键 evidence state。
8. WebSocket、前端卡片、terminal、刷新 replay 展示同一份持久化事实。
9. 不存在第二套 Ledger、当前 Store、verification receipt 或 UI 推断逻辑。
10. 上述能力由真实端到端测试覆盖，而不是手工构造 Store 数据。

在这些条件达成前，应把当前 Phase 2 状态定义为“架构原型已搭建，生产链路未验收”，而不是“Phase 2 已完成”。
