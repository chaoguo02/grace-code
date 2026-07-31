# Grace Code 三模式 Agent 执行设计

> 状态：设计基线  
> 日期：2026-07-30  
> 范围：Workbench 的 Build、Plan、Multi-Agent 三种模式，以及它们与 Agent、Skill、MCP、Runtime、Hook、事件和前端展示的关系  
> 原则：保持产品模型简单；不引入 Agent Team；不支持子 Agent 嵌套

## 1. 设计结论

Grace Code 只向用户提供三种模式：

```text
Build | Plan | Multi-Agent
```

三种模式只决定顶层执行策略：

- `Plan`：只读搜索和分析，最终产出规划。
- `Build`：直接实施任务，可以调用子 Agent，但所有子 Agent 严格串行。
- `Multi-Agent`：由 Orchestrator 拆分任务，使用有界并行执行并统一汇总。

`explore`、`plan`、`general` 不是用户模式，而是 Runtime 内部选择的 Worker 类型。

```text
用户模式
  -> Primary Agent
    -> Runtime 执行策略
      -> explore / plan / general Worker
        -> Skill
          -> MCP / Tool
```

第一阶段明确不做：

- Worker 创建 Worker；
- Agent Team、P2P 通信、共享任务认领；
- Build 模式中的并行 Agent；
- Plan 模式中的写文件或实现；
- 由 Prompt 单独决定并发、深度、权限或完成状态；
- 为三个模式分别复制一套 Session、Tool、MCP、Skill 或事件链路。

## 2. 为什么需要收敛

当前两次测试暴露的问题，本质上都来自“模型知道一些规则，但 Runtime 没有形成可验证的执行契约”。

### 2.1 Skill/MCP 顺序和完成证据不可信

用户要求使用 `city-weather` Skill 查询三个城市并写报告，实际执行却先使用对话记忆生成文件，之后才加载 Skill，并最终宣称 MCP 查询已经完成。

错误链路：

```text
对话记忆
  -> 写文件
  -> 补加载 Skill
  -> 缺少完整 MCP 调用证据
  -> Completed
```

正确链路：

```text
加载 Skill
  -> 解析 Skill 依赖
  -> 激活并调用 MCP
  -> 验证结果
  -> 生成文件
  -> 验证文件
  -> Completed
```

对话中的旧结果只能作为参考，不能直接替代本轮明确要求的 Tool/MCP 执行。允许复用时，必须命中 Runtime 管理的结构化缓存，并记录 `cache_hit`、参数、版本指纹和新鲜度。

### 2.2 不支持的 Multi-Agent 拓扑被反复尝试

用户要求：

```text
Root
  -> Worker
    -> 北京 Agent
    -> 上海 Agent
```

这需要两层 spawn，但当前只允许一层。系统没有在执行前清楚拒绝，而是错误尝试 `AgentBatch`、遇到参数校验错误、继续试错直到预算耗尽，最终页面仍显示 `Completed`。

当前支持的唯一多 Agent 拓扑应当是：

```text
Orchestrator
  -> Worker A
  -> Worker B
  -> Worker C
```

Worker 永远不能再看到 `Agent` 或 `AgentBatch`。如果用户要求嵌套，应在调用 LLM 工具前返回结构化的 `nested_delegation_disabled`，并说明可用的同级拆分方案。

## 3. 核心概念

### 3.1 Product Mode

Product Mode 是用户在 Workbench 中选择的顶层执行方式。它决定：

- Primary Agent；
- 是否允许修改；
- 是否允许委派；
- 委派是串行还是并行；
- 可用 Worker 类型；
- 完成条件；
- 前端如何展示运行状态。

### 3.2 Primary Agent

每次顶层 Run 只有一个 Primary Agent：

| Product Mode | Primary Agent |
|---|---|
| Build | `build` |
| Plan | `plan` |
| Multi-Agent | `orchestrator` |

Primary Agent 对用户目标、最终状态和最终回答负责。Worker 不能宣布整个用户任务已经完成。

### 3.3 Worker Type

首期只定义三个核心 Worker 类型：

| Worker | 权限 | 用途 |
|---|---|---|
| `explore` | 只读 | 搜索文件、定位代码、收集事实 |
| `plan` | 只读 | 分析影响面、设计局部方案、提出验证路径 |
| `general` | 可写 | 完成边界明确的实现任务 |

后续可以增加 `verification` 等专业 Worker，但不改变三模式模型。

### 3.4 Skill、MCP 和 Tool

- Skill 是执行流程和领域规则。
- MCP 是外部能力或数据来源。
- Tool 是文件、搜索、命令等具体操作。
- Agent 是在独立上下文中完成有限任务的执行单元。

Skill 不是 Agent，MCP 也不是 Agent。它们在三个模式中共用同一套加载、调用、缓存和事件链路。

## 4. 三种模式的正式契约

### 4.1 Plan 模式

#### 用户承诺

系统搜索和理解现状，最后给出可以执行的规划，不修改项目。

#### Runtime 约束

```text
write_allowed = false
delegation_allowed = true
delegation_strategy = serial
allowed_workers = [explore, plan]
agent_batch_allowed = false
max_in_flight_workers = 1
max_spawn_depth = 1
```

Plan Primary 可以：

- 使用 Read、Grep、Glob 等只读工具；
- 加载只读 Skill；
- 调用只读 MCP；
- 串行委派 `explore` 或 `plan` Worker；
- 汇总为结构化计划。

Plan Primary 不可以：

- Write、Edit 或执行会修改项目的命令；
- 调用 `general`；
- 调用 `AgentBatch`；
- 自动切换到 Build；
- 在计划完成后继续实施。

#### 输出要求

Plan 最终至少包含：

- 目标与非目标；
- 现状和关键证据；
- 涉及模块或文件；
- 分步骤改造方案；
- 风险、兼容和回滚；
- 测试与验收方式；
- 尚需用户决定的问题。

完成规划后状态为 `completed`，但语义是“规划完成”，不是“功能实现完成”。

### 4.2 Build 模式

#### 用户承诺

系统直接实现并验证任务。Primary Agent 可以寻求子 Agent 帮助，但执行顺序清晰、同一时间只有一个 Worker。

#### Runtime 约束

```text
write_allowed = true
delegation_allowed = true
delegation_strategy = serial
allowed_workers = [explore, plan, general]
agent_batch_allowed = false
max_in_flight_workers = 1
max_spawn_depth = 1
```

Build Primary 可以：

- 自己搜索、修改和验证；
- 串行调用 `explore` 获取事实；
- 串行调用 `plan` 分析局部方案；
- 串行调用 `general` 完成一个边界明确的实现任务；
- 等待一个 Worker 完成并处理结果后，再调用下一个 Worker。

Build 模式的“串行”必须由 Runtime 保证：

```text
Agent A start
  -> Agent A terminal
    -> parent consumes/integrates result
      -> Agent B start
```

以下情况都属于违反串行契约：

- 同时存在两个 running Worker；
- 在前一个 Worker 仍运行时接受下一次 Agent 调用；
- 通过多个后台任务绕过 `max_in_flight_workers = 1`；
- 在 Build 中调用 AgentBatch；
- 同一个模型 turn 发出多个并行 Agent 调用。

`general` Worker 的写入继续使用现有隔离与集成机制。即使只有一个 Worker，也不能绕开 write set、worktree、冲突检查和父级验收。

Build Primary 始终负责：

- 决定是否采用 Worker 结果；
- 集成变更；
- 运行最终验证；
- 判断整个任务是否完成；
- 向用户输出唯一最终回答。

### 4.3 Multi-Agent 模式

#### 用户承诺

Orchestrator 将适合拆分的任务组织成有限 DAG，按依赖关系并行执行，在集成和验证后输出一个结果。

#### Runtime 约束

```text
write_allowed = task_and_permission_dependent
delegation_allowed = true
delegation_strategy = bounded_parallel
allowed_workers = [explore, plan, general]
agent_batch_allowed = true
max_in_flight_workers = configured_limit
max_spawn_depth = 1
nested_delegation_allowed = false
```

Orchestrator 负责：

1. 保留原始用户目标和完成条件。
2. 判断任务是否存在有效拆分。
3. 生成同级 Worker DAG。
4. 为每个任务选择 Worker 类型。
5. 为写任务声明 `write_files` 和隔离工作区。
6. 将独立任务并行，将有依赖的任务按 wave 推进。
7. 收集结构化 WorkerReport。
8. 集成写入结果并处理冲突。
9. 执行整体验证。
10. 输出一个最终回答和真实终态。

允许的拓扑：

```text
fan-out/fan-in:

Orchestrator
  +-- Explore A ----+
  +-- Explore B ----+--> Synthesis
  +-- General C ----+

dependency waves:

Wave 1: Explore A || Explore B
Wave 2: General C depends_on A
Wave 3: General D depends_on B
Wave 4: Integration and verification
```

禁止的拓扑：

```text
Orchestrator
  -> General A
    -> Explore B
    -> General C
```

如果任务不值得并行，Orchestrator 可以直接完成或使用一个 Worker，但必须记录：

```text
execution_topology = direct | single_worker
downgrade_reason = insufficient_parallelism | budget | conflict_risk
```

不得为了显示 Multi-Agent 而创建无意义 Worker。

## 5. 模式能力矩阵

| 能力 | Plan | Build | Multi-Agent |
|---|---:|---:|---:|
| 直接读取 | 是 | 是 | 是 |
| 修改项目 | 否 | 是 | 按任务和权限 |
| `explore` Worker | 串行 | 串行 | 有界并行 |
| `plan` Worker | 串行 | 串行 | 有界并行 |
| `general` Worker | 否 | 串行 | 有界并行 |
| Agent 工具 | 是 | 是 | 可用于单 Worker |
| AgentBatch | 否 | 否 | 是 |
| Worker 再委派 | 否 | 否 | 否 |
| 最终规划 | 是 | 可选中间产物 | 可作为子任务 |
| 最终实施 | 否 | 是 | 是 |

## 6. Runtime 设计

### 6.1 ModeExecutionPolicy

Runtime 在每个 Run 开始时创建一份不可变策略：

```python
@dataclass(frozen=True)
class ModeExecutionPolicy:
    mode: Literal["plan", "build", "multi-agent"]
    primary_agent: str
    write_allowed: bool
    delegation_strategy: Literal["serial", "bounded_parallel"]
    allowed_worker_types: frozenset[str]
    agent_batch_allowed: bool
    max_in_flight_workers: int
    max_spawn_depth: int
    nested_delegation_allowed: bool = False
```

策略只能从统一配置和当前模式构建一次。下列组件必须消费同一个 policy：

- AgentFactory；
- RegistryBuilder；
- Agent/AgentBatch；
- Runtime spawn；
- ResourceGovernor；
- PromptBuilder；
- Completion Guard；
- 事件投影和前端。

禁止各模块再次读取环境变量并自行推导模式限制。

### 6.2 工具暴露

工具注册应根据 policy 确定：

```text
Plan:
  Read tools + Skill + allowed MCP + Agent

Build:
  Build tools + Skill + allowed MCP + Agent

Multi-Agent:
  Build/read tools + Skill + allowed MCP + Agent + AgentBatch

Any Worker:
  its role tools + Skill + allowed MCP
  never Agent
  never AgentBatch
```

隐藏工具只是帮助模型正确决策，Runtime 仍必须在调用入口重复校验，不能把 registry 隐藏当作安全边界。

### 6.3 串行调度

Build 和 Plan 共用同一个 `SerialDelegationGate`：

```text
acquire parent delegation slot
  -> reserve budget
  -> spawn one child
  -> wait for terminal
  -> persist report
  -> release resources
  -> parent resumes
```

当已有 Worker 运行时，新的 Agent 调用返回结构化错误：

```json
{
  "code": "serial_delegation_busy",
  "active_worker_id": "...",
  "retryable": true
}
```

不创建排队线程，也不隐式转入并行执行。

### 6.4 并行调度

只有 Multi-Agent 的 AgentBatch 可以申请多个并发槽位。并行度由统一 ResourceGovernor 决定：

```text
requested fanout
  -> topology validation
  -> write-set validation
  -> atomic budget reservation
  -> bounded scheduler
  -> worker reports
  -> budget reconciliation
```

模型只能提出任务 DAG，不能自行决定突破并发、深度和 token 上限。

### 6.5 拓扑预检

在创建 Worker 前必须检查：

- 当前深度；
- 请求深度；
- Worker 是否试图委派；
- AgentBatch 是否只包含同级任务；
- 任务依赖是否无环；
- 并行写集合是否冲突；
- 当前模式是否允许 AgentBatch。

嵌套请求直接返回：

```json
{
  "code": "nested_delegation_disabled",
  "requested_depth": 2,
  "allowed_depth": 1,
  "supported_alternative": "Create sibling workers under the primary agent"
}
```

同一原因连续失败两次后终止模型重试，避免预算耗尽。

## 7. Skill、MCP 与执行证据

三种模式使用同一个 Evidence Ledger：

```text
Run
  +-- skill_loaded
  +-- mcp_activated
  +-- tool_call_started
  +-- tool_call_completed
  +-- cache_hit
  +-- artifact_written
  +-- artifact_verified
  +-- worker_started
  +-- worker_completed
  +-- validation_completed
```

当 Skill 声明 MCP 依赖时：

1. Skill 加载成功后才激活依赖。
2. MCP 结果必须关联调用参数、server fingerprint 和结果版本。
3. 下游写文件记录 `depends_on`。
4. Completion Guard 检查依赖是否真实完成。

例如天气报告：

```text
skill_loaded(city-weather)
  -> mcp_call(Beijing)
  -> mcp_call(Shanghai)
  -> mcp_call(Shenzhen)
  -> artifact_written(weather-report.md, depends_on=[three calls])
  -> artifact_verified(weather-report.md)
```

如果复用缓存，Ledger 记录 `cache_hit`；不重新伪造 Tool/MCP 调用。

## 8. 完成状态

统一 Run 终态：

```text
completed
failed
blocked
cancelled
gave_up
```

典型 reason code：

```text
nested_delegation_disabled
serial_delegation_busy
budget_exhausted
max_steps_reached
required_skill_not_loaded
required_mcp_evidence_missing
worker_failed
integration_failed
validation_failed
user_cancelled
```

最终文本不能覆盖 Runtime 状态。以下情况不得显示 `Completed`：

- 必需 Skill/MCP 没有执行或合法命中缓存；
- 用户要求的 Worker 没有创建；
- 预算耗尽或达到最大步骤；
- required Worker 失败；
- 写入结果没有集成；
- 最终验证失败；
- Completion Guard 发现回答和 Ledger 不一致。

## 9. Hook 边界

Hook 用于扩展和观测，不是执行真相来源。

合理用途：

- PreToolUse 提前解释模式拒绝原因；
- Skill/MCP 生命周期观测；
- Subagent start/stop 通知；
- 记录额外审计信息；
- Stop 时追加非权威提示。

不能只依赖 Hook 实现：

- 深度限制；
- 串行/并行限制；
- token 预算；
- 文件写权限；
- 最终完成状态；
- Worker 是否真实创建。

这些必须由 Runtime 和持久化事实保证。

## 10. 前端展示

### 10.1 Plan

展示：

```text
Plan · Searching
Plan · Synthesizing
Plan · Completed
```

子 Agent 以串行步骤显示，不展示 Multi-Agent DAG。

### 10.2 Build

展示主时间线和串行 Worker：

```text
Build
  -> Explore · Completed
  -> General · Completed
  -> Verification · Completed
```

任何时刻最多一个 Worker 为 Running。

### 10.3 Multi-Agent

展示 Orchestrator、DAG/wave 和资源状态：

```text
Multi-Agent · Wave 1 · 2 running
  Explore backend
  Explore frontend

Multi-Agent · Wave 2 · 1 running
  General implementation

Multi-Agent · Integrating
Multi-Agent · Verifying
Multi-Agent · Completed
```

前端只投影后端事件，不根据是否收到最终文本自行推断 `Completed`。

Skill 和 MCP 在所有模式中使用相同卡片；如果调用发生在 Worker 下，卡片归属对应 child session，但仍可在主时间线折叠展示。

## 11. 与现有代码的映射

可以直接复用：

- `web/src/modes.ts`：三模式 UI 到 Primary Agent 的映射；
- `agent/session/models.py`：`build`、`plan`、`orchestrator` 和 Worker definitions；
- `agent/session/task_tool.py`：单 Worker 委派；
- `agent/session/agent_batch_tool.py`：Multi-Agent DAG；
- `agent/session/runtime_spawn.py`：唯一 spawn 路径和深度校验；
- `agent/session/registry_builder.py`：按能力暴露 Agent 工具；
- `agent/session/runtime_prompt_builder.py`：模式和 Worker 列表提示；
- `server/services/multi_agent_service.py`：Multi-Agent 投影；
- `web/src/components/MultiAgentRunCard.tsx`：Multi-Agent 展示。

需要收紧的地方：

1. 增加统一 `ModeExecutionPolicy`。
2. Build/Plan 不注册 `AgentBatch`。
3. Build/Plan 的 Agent 调用接入串行 gate。
4. 所有 Worker registry 移除 Agent/AgentBatch。
5. Agent/AgentBatch schema 注入当前模式、剩余深度和并发能力。
6. Skill/MCP/Artifact 进入同一个 Evidence Ledger。
7. 修复 `gave_up`、预算耗尽和最大步骤到终态事件的映射。
8. 前端完全使用后端 `run_terminal.status` 和 `termination_reason`。

现有大型 Multi-Agent 文档仍可作为历史实现和资源治理参考，但本文件是三模式产品语义的收敛基线；冲突时应以本文件的模式边界为准。

## 12. 分阶段实施

### Phase 1：模式强约束

- 落地 `ModeExecutionPolicy`；
- Build/Plan 禁用 AgentBatch；
- Build/Plan 串行 gate；
- Worker 禁止嵌套；
- 修复不真实的 Completed。

### Phase 2：证据闭环

- Skill、MCP、cache、artifact 写入 Evidence Ledger；
- Completion Guard 检查必要依赖；
- 前端展示真实来源和 cache hit。

### Phase 3：Multi-Agent 收敛

- AgentBatch 只接受同级 DAG；
- 拓扑预检和重复错误熔断；
- Orchestrator 统一 integration/verification；
- UI 按 wave 展示并行执行。

本设计不要求重写 Session Runtime，也不新增第二套 Multi-Agent 执行器。

## 13. 验收场景

### 13.1 Plan

请求：

```text
分析认证模块并给出重构计划，不修改代码。
```

验收：

- 可以搜索；
- 可以串行调用 explore/plan；
- 没有文件修改；
- 没有 AgentBatch；
- 最终产出结构化规划。

### 13.2 Build 串行

请求：

```text
定位重复回答问题，完成修复并运行测试。
```

验收：

- 可以依次调用 Explore、General、验证 Worker；
- Worker 时间区间不重叠；
- 同时 Worker 数始终小于等于 1；
- 变更被集成；
- 验证后才 Completed。

### 13.3 Multi-Agent 并行

请求：

```text
分别检查前端展示和后端事件链，修复问题并统一验证。
```

验收：

- Orchestrator 创建同级 DAG；
- 独立调查任务可并行；
- 有依赖任务按 wave 执行；
- 写集合冲突会阻止并行；
- 最终只有一个综合回答。

### 13.4 嵌套拒绝

请求：

```text
创建一个 Worker，让它再创建北京和上海两个子 Agent。
```

验收：

- 在 spawn 前返回 `nested_delegation_disabled`；
- 不创建 Worker；
- 不进入长时间重试；
- 不显示 Completed；
- 提供同级北京、上海 Worker 的替代方案。

### 13.5 Skill/MCP 顺序

请求：

```text
使用 city-weather Skill 查询三个城市，并保存报告。
```

验收：

- Skill 先于其 MCP 依赖执行；
- 三个查询均有调用或合法 cache-hit 证据；
- 文件写入依赖三个结果；
- 缺少任何必要证据时不能 Completed。

## 14. 完成定义

三模式改造完成必须同时满足：

1. UI 只有 Build、Plan、Multi-Agent 三种产品模式。
2. Plan 只读搜索并输出规划。
3. Build 可以委派，但 Worker 严格串行。
4. Multi-Agent 只有 Orchestrator 可以进行有界并行。
5. Worker 永远不能再次委派。
6. Agent、AgentBatch 共用唯一 runtime spawn 和 ResourceGovernor。
7. Skill、MCP、Tool、文件写入具备可验证的先后关系。
8. Runtime 终态与前端展示一致。
9. 预算耗尽、拓扑不支持和证据缺失不会显示 Completed。
10. 不新增 Agent Team、嵌套 Agent 或第二套执行链。

