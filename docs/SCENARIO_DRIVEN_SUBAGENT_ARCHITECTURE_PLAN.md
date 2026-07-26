# Grace Code 场景化 Subagent 架构设计与实施计划

> 状态：核心设计已一次性实现，Agent Team 保持显式 feature flag 与用户审批  
> 基线日期：2026-07-26  
> 对标对象：Claude Code 当前公开的 Subagents、Agent Teams、并行 Agent 行为  
> 适用范围：ReAct / Plan / Explore 三种交互模式、Agent Runtime、MCP、Skills、Worktree、Web 可观测界面

## 1. 结论

Grace Code 有必要提供“根据任务场景选择不同 subagent”的能力，但不能把它实现成“复杂问题就多开几个 agent”的提示词技巧。

目标应当是建立一套由 Runtime 约束、可解释、可观测、可回放的场景化委托系统：

1. 主代理先判断任务是否值得委托，而不是默认委托。
2. 需要委托时，根据任务意图选择专业 subagent，而不是始终使用 `explore` 或 `general`。
3. 根据依赖、文件重叠、沟通需求和预算选择单代理、一对一、扇出汇总、链式委托或 Agent Team。
4. 每个 subagent 使用独立上下文、明确工具权限、预算和交付协议。
5. 普通 subagent 默认只与直接父代理通信；Agent Team 才提供成员间直接通信和共享任务表。
6. 主代理始终对最终判断、变更合并、验证和用户回答负责。
7. Web 页面展示委托原因、拓扑、进度、结果可信度和成本，但不把内部状态污染到回答正文。

这与 Claude Code 的产品边界保持一致，同时利用 Grace Code 已有的持久化 Session、结构化 Finding 和显式 Worktree 收敛能力，形成适合本项目的落地方案。

## 2. 为什么现在必须设计，而不是继续补提示词

Grace Code 已经具备不少底层能力：

- `AgentDefinition` 已支持工具、禁用工具、模型、权限模式、MCP、Skills、Memory、Hooks、后台执行和工作区隔离，见 [`agent/session/models.py`](../agent/session/models.py) 与 [`agent/session/agent_definition.py`](../agent/session/agent_definition.py)。
- `AgentTool` 已支持 named subagent、fork、前后台执行、模型覆盖与 worktree 参数，见 [`agent/session/task_tool.py`](../agent/session/task_tool.py)。
- 子代理会话、结果、异常、预算和 worktree 生命周期已有独立执行路径，见 [`agent/session/subagent.py`](../agent/session/subagent.py)。
- 父代理已拥有 `SendMessage`、`WaitForAgent`、`CancelAgent` 和 worktree apply/discard/retain 控制面，见 [`agent/session/agent_control_tool.py`](../agent/session/agent_control_tool.py) 与 [`agent/session/worktree_tool.py`](../agent/session/worktree_tool.py)。
- `SubagentReport` 已能验证文件路径、行号与 finding，见 [`agent/session/result_contract.py`](../agent/session/result_contract.py)。
- Web 已能显示 subagent 生命周期、详情和拓扑，见 [`web/src/components/SubagentProgress.tsx`](../web/src/components/SubagentProgress.tsx)、[`web/src/components/SubagentDetail.tsx`](../web/src/components/SubagentDetail.tsx) 与 [`web/src/components/MultiAgentControlPlane.tsx`](../web/src/components/MultiAgentControlPlane.tsx)。

但现状仍缺少统一决策层：

- UI 的 Explore 模式直接把 `explore` 当作顶层 agent 使用，而项目内 `explore` 定义实际是不能委托的叶子 subagent。
- Build 和 Plan 的委托行为主要依靠 system prompt 中的自然语言规则，Runtime 不知道“为什么选择这个 agent”。
- 当前专业角色很少，主要只有 `explore`、`general`、`code-reviewer`，无法准确匹配调试、测试、安全审查等不同任务。
- [`tools/workflow_tool.py`](../tools/workflow_tool.py) 的 `WorkflowTool.execute()` 只返回 “Workflow dispatched”，没有真正创建任务、启动 agent、等待和汇总。
- 当前消息能力的实现和工具说明不一致：Runtime 已能把父消息追加给 `RUNNING/QUEUED` 的直接子会话并在下一轮 claim，但 `SendMessage` / `agent_control` 的描述仍声称只能续跑 terminal child。这会误导模型，也缺少系统性测试。
- [`server/services/multi_agent_service.py`](../server/services/multi_agent_service.py) 明确声明 `arbitrary_agent_message_bus: false`，目前不是 Agent Team。
- 现有 prompt 固定建议 “2-3 个并行只读任务”，但没有根据任务收益、文件冲突或剩余预算动态收缩。
- 当前注册表允许配置了 Agent 的非 Primary agent 看到公开子代理；它虽受深度和权限限制，但缺少显式的全局嵌套开关、层数预算与每层 allowlist 语义。
- 当前 `AgentDepth` 将最大层数硬编码为 5，没有分别管理累计 spawn、同时运行数和嵌套深度。
- 真正的 spawn 主路径已经拆到 [`agent/session/runtime_spawn.py`](../agent/session/runtime_spawn.py)，但 [`agent/session/runtime.py`](../agent/session/runtime.py) 仍保留一份随后被 monkey patch 覆盖的重复实现。后续修改 spawn 语义时必须先收敛所有权，避免改到未生效代码。

值得复用的现成范例是 Review：[`server/services/review_service.py`](../server/services/review_service.py) 已将 correctness、concurrency/security、tests/contracts 三个 lens 并行分发给 `code-reviewer`，并基于冻结 Git snapshot 做证据复验、去重、corroboration 和 stale 判断。它是目前最完整的 fan-out/fan-in 场景，但仍是 Review 专用协调器，尚未抽象成通用拓扑能力。

因此，下一步应增加“任务形状分析 + 拓扑决策 + 场景路由 + 结构化汇总”，而不是单纯继续扩写 prompt。

## 3. 对齐 Claude Code 的准确边界

### 3.1 普通 Subagent

按照 Claude Code 当前公开文档，普通 subagent 的关键行为是：

- 每个 subagent 有独立上下文窗口、自定义 system prompt、工具和权限。
- Claude 根据 subagent 的 `description` 自动判断何时委托，也允许用户明确指定。
- Explore 用于快速、只读的代码搜索与理解；Plan 用于 Plan Mode 中的研究；general-purpose 用于复杂、多步骤、需要行动的任务。
- 独立调查适合并行启动多个 subagent，结果返回主代理后统一综合。
- 有依赖的任务适合链式执行，由主代理把前一个结果中的必要部分传给下一个。
- 默认情况下 subagent 不能继续 spawn；配置最大 spawn depth 后才允许嵌套。
- 并发数、会话累计 spawn 数和嵌套深度是三个独立限制。
- named subagent 从 fresh context 开始；fork 才继承父会话上下文。

官方参考：

- [Create custom subagents](https://code.claude.com/docs/en/sub-agents)
- [Run agents in parallel](https://code.claude.com/docs/en/agents)

### 3.2 Agent Teams

Agent Team 与普通 subagent 不是同一个层级：

| 维度 | 普通 Subagent | Agent Team |
|---|---|---|
| 上下文 | 独立上下文，结果返回调用者 | 每个 teammate 是完整独立实例 |
| 通信 | 默认只向直接调用者回报 | teammate 可直接相互发消息 |
| 调度 | 主代理分配和汇总 | 共享任务表，可认领、依赖和协作 |
| 成本 | 较低 | 显著更高 |
| 适合 | 聚焦调查、独立实现、结果汇总 | 需要成员讨论、挑战、动态认领的复杂协作 |
| 默认状态 | 正常能力 | Claude Code 中仍是实验性、默认关闭 |

官方参考：[Orchestrate teams of Claude Code sessions](https://code.claude.com/docs/en/agent-teams)。

Grace Code 应先完整实现普通 subagent，再把 Agent Team 作为显式启用的高级能力。不能为了展示多代理而默认创建 Team。

### 3.3 “一致”的定义

本项目追求行为和心智模型一致，不要求内部类名、存储方式和 UI 完全复制：

- 用户看到的 agent 类型、自动委托原则、上下文隔离、工具约束、并行/链式行为与 Claude Code 一致。
- Grace Code 保留自己的结构化证据、Session 持久化和显式 worktree 收敛。
- 对 Claude Code 当前没有公开保证的内部算法，不假装精确复刻。
- 文档和界面应标注“aligned with documented behavior”，避免声称内部实现完全相同。

## 4. 术语和职责边界

### 4.1 Primary Agent

用户当前直接对话的代理，也是最终责任人。

职责：

- 理解用户目标和当前模式。
- 判断是否委托、委托给谁、采用何种拓扑。
- 构造完整、窄化、可验收的子任务。
- 对子代理结果做证据检查、去重、冲突处理和综合。
- 决定是否应用 worktree 变更。
- 完成最终验证并向用户回答。

不得下放的职责：

- 对用户意图的最终解释。
- 高风险操作的用户审批。
- 多个结果冲突时的最终决策。
- 最终回答的真实性和完整性。

### 4.2 Named Subagent

一个可复用的专业角色，使用 fresh context 和固定能力边界。

职责：

- 只完成一个清晰、有限的任务。
- 严格遵守分配的文件、模块和权限范围。
- 返回结构化结果，不代替 Primary 回答用户。
- 遇到超范围内容时报告，不自行扩大目标。

### 4.3 Fork

Fork 是上下文来源，不是专业角色。

- named subagent：加载自己的定义和任务摘要，不继承完整父会话。
- fork：继承父会话快照、模型与工具契约，适合需要大量既有上下文的延伸任务。

选择 fork 的门槛应高于 named subagent，因为它复制更多上下文、成本更高，也更容易继承无关信息。

### 4.4 Skill

Skill 不是 subagent。

| Skill | Subagent |
|---|---|
| 在当前 agent 上下文中加载工作方法或领域知识 | 在独立上下文中执行任务 |
| 不新增并发执行单元 | 新增独立执行单元 |
| 适合复用流程、规范、模板 | 适合隔离高容量搜索、并行调查或专业执行 |
| 共享当前 agent 的工具与权限边界 | 可声明更窄的工具、权限、模型和 MCP |

原则：

- “如何做”优先用 Skill。
- “由谁在独立上下文中做”才用 Subagent。
- 专业 subagent 可以预加载 Skill，但不能把 Skill 当作另一个 agent。

### 4.5 Agent Team

由 Lead、Teammates、共享任务表和 Mailbox 组成的独立协作形态。只有任务确实需要成员间直接沟通时才使用。

## 5. 目标架构

```text
User
  |
  v
Primary Agent
  |
  +-- TaskShapeAnalyzer --------> 结构化任务形状
  |
  +-- TopologyPlanner ----------> single / one_to_one / fan_out / chain / nested / team
  |
  +-- SubagentRouter -----------> 选择专业 agent + model + tools + skills + MCP
  |
  +-- DelegationScheduler
  |      |
  |      +-- Named Subagent (fresh context)
  |      +-- Fork (parent snapshot)
  |      +-- Worktree worker (isolated workspace)
  |
  +-- ResultAggregator ---------> 去重、证据校验、冲突标注、完整性检查
  |
  +-- VerificationGate --------> 测试 / review / safety / worktree 收敛
  |
  v
Final response + persisted trace + Web visualization
```

这里的 Analyzer、Planner、Router 不应先做成额外 LLM 调用。第一阶段使用 Runtime 可验证的数据结构和主模型的一次结构化决策，避免为“是否多开 agent”额外消耗一次模型调用。

## 6. 场景化 Subagent 目录

### 6.1 第一批必须具备的角色

| Agent | 核心场景 | Intent | 默认工具 | 默认隔离 | 是否可写 | 默认可嵌套 |
|---|---|---|---|---|---:|---:|
| `explore` | 定位文件、理解模块、追踪调用链 | analysis | Read / Grep / Glob / Web | current | 否 | 否 |
| `plan-researcher` | Plan Mode 的约束、影响面、验证路径调查 | analysis | Read / Grep / Glob | current | 否 | 否 |
| `code-reviewer` | 正确性、回归、可维护性审查 | analysis | Read / Grep / Git diff | current | 否 | 否 |
| `debugger` | 错误日志、失败测试、竞争假设、根因定位 | analysis | Read / Grep / Bash / tests | current | 否 | 否 |
| `test-runner` | 执行指定测试、归类失败、返回原始证据 | verify | Bash / pytest / Read | current | 否 | 否 |
| `security-reviewer` | 权限、注入、路径、敏感信息和依赖风险 | analysis | Read / Grep / dependency audit | current | 否 | 否 |
| `general` | 有明确边界的实现任务 | edit | Read / Edit / Write / Bash | worktree 优先 | 是 | 否 |

`verify` 如果当前 `TaskIntent` 尚无该枚举，可在第一版映射为 `analysis`，但文档和事件层应保留 `purpose=verification`，后续再扩展 typed intent，不能通过名称猜测。

### 6.2 每个角色的使用和禁用条件

#### `explore`

使用：

- “这个模块在哪里？”
- “前端每个模块有什么功能？”
- “这条请求从 router 到 runtime 怎么走？”
- 需要隔离大量搜索结果、避免污染主上下文。

禁用：

- 已知文件和行号，只需读一个小片段。
- 任务需要修改、运行危险命令或审批。
- 结果强依赖父会话中大量未结构化讨论，此时考虑 fork。

#### `plan-researcher`

使用：

- Plan Mode 需要调查现状、约束、影响面和验证路径。
- 计划跨多个相互独立的模块，可分区并行调查。

禁用：

- 不得保存、批准、拒绝计划。
- 不得进入或退出 Plan Mode。
- 不得修改代码。

这样能避免把 Primary `plan` 和 leaf `explore` 混在一起，也与 Claude Code 的 Plan research worker 心智模型对应。

#### `code-reviewer`

使用：

- 变更完成后的独立审查。
- 检查 diff 是否满足需求、是否有回归。
- 多视角 review 中负责 correctness / maintainability。

禁用：

- 不直接修改发现的问题。
- 不以“没有发现问题”代替验证；必须说明检查范围。
- 不审查自己未读取的 diff。

#### `debugger`

使用：

- 错误有多个可能根因。
- 需要读取日志、运行窄化测试、建立假设并证伪。
- 可与另一个 debugger 并行测试不同假设，但范围不得重叠。

禁用：

- 第一阶段只诊断，不直接编辑。
- 运行命令必须受只读/验证型 PhasePolicy 限制。
- 不允许借 Bash 绕过专用 Read/Grep 工具。

#### `test-runner`

使用：

- 父代理已明确测试命令或测试范围。
- 需要把验证过程从主上下文隔离。
- 多个互不影响的测试套件并行运行。

禁用：

- 不自行扩大到全仓库高成本测试。
- 不把测试失败自动解释为代码错误；区分产品失败、环境失败、超时和缺少依赖。
- 不修改 snapshot 或测试期待值来“让测试通过”。

#### `security-reviewer`

使用：

- 认证、权限、文件路径、命令执行、MCP、Secrets、输入验证变更。
- 发布前的高风险审查。

禁用：

- 不负责普通风格 review。
- 没有直接证据时以风险假设返回，不能宣称存在漏洞。

#### `general`

使用：

- 任务边界明确、预计修改文件互不重叠。
- 可在独立 worktree 中实现并返回 revision。

禁用：

- 同一文件需要频繁协同修改。
- 用户尚未授权修改。
- 任务需要主代理持续结合对话做产品决策。

### 6.3 后续按项目需要增加，不做第一批

- `docs-researcher`：外部官方文档和标准核对。
- `frontend-reviewer`：可访问截图、视觉规范和前端代码的 UI 专项审查。
- `database-reviewer`：迁移、事务、索引和兼容性审查。
- `performance-profiler`：基准、profile 和性能回归。

新增角色必须满足：高频复用、能力边界稳定、与已有角色明显不同。不能为每个临时任务创建一个 agent 类型。

## 7. Agent Definition 规范

### 7.1 描述必须能支撑自动路由

`description` 不是宣传文案，必须同时写清：

1. 正向触发场景。
2. 不应触发的场景。
3. 是否只读。
4. 交付物类型。

示例：

```yaml
description: >
  Diagnose test failures and runtime errors by inspecting code, logs, and
  running narrowly scoped verification commands. Use when the root cause is
  uncertain or multiple hypotheses should be tested. Read-only: report the
  root cause and evidence; do not edit files.
```

### 7.2 推荐 frontmatter

```yaml
---
name: debugger
description: "..."
intent: analysis
kind: named_subagent
tools: Read, Grep, Glob, Bash, pytest, submit_findings
disallowedTools: Write, Edit, Agent
model: inherit
permissionMode: dontAsk
maxTurns: 40
maxTokens: 24000
background: true
isolation: current
skills:
  - debug-triage
color: orange
---
```

当前 parser 已支持上述大部分字段。实施时需要补齐并验证：

- `requiredTools` / `completionRequires` 的 frontmatter 解析。`AgentDefinition` 有字段，但当前 [`agent/session/agent_definition.py`](../agent/session/agent_definition.py) 尚未把 YAML 值传入。
- `purpose` 或扩展后的 `TaskIntent.VERIFY`。
- `defaultThoroughness`，用于 `explore` 的 quick / medium / very_thorough。
- `maxConcurrencyPerType`，防止同一专业 worker 被无限 fan-out。

### 7.3 权限是交集，不是覆盖

有效能力应按以下交集计算：

```text
有效工具
= 父会话可用工具
∩ 子代理 tools
- 子代理 disallowedTools
∩ 当前 PhasePolicy
∩ PermissionPipeline
∩ MCP/Skill 可见性
∩ Workspace 安全边界
```

子代理定义不能扩大父代理权限。模型传入不存在或越权工具时，Runtime 应返回不可重试的 policy error。

## 8. 任务形状 TaskShape

### 8.1 为什么需要结构化任务形状

“复杂”“帮我调查一下”“多个模块”之类关键词不足以决定是否启用多代理。Runtime 至少需要知道任务是否可拆、是否依赖、是否写同一文件、是否需要相互沟通以及预算是否足够。

建议新增：

```python
@dataclass(frozen=True)
class TaskShape:
    intent: TaskIntent
    purpose: TaskPurpose
    domains: tuple[str, ...]
    work_items: tuple[WorkItem, ...]
    dependency_edges: tuple[DependencyEdge, ...]
    expected_files: tuple[str, ...]
    write_files: tuple[str, ...]
    context_volume: ContextVolume
    evidence_requirement: EvidenceLevel
    coordination_need: CoordinationNeed
    risk: RiskLevel
    user_requested_topology: str | None
```

`WorkItem` 至少包含：

```python
@dataclass(frozen=True)
class WorkItem:
    id: str
    goal: str
    domain: str
    candidate_agent: str
    depends_on: tuple[str, ...]
    expected_files: tuple[str, ...]
    write_files: tuple[str, ...]
    deliverable: str
```

### 8.2 TaskShape 的来源

优先级：

1. 用户显式要求：例如“用 3 个 subagent 并行 review”。
2. 主模型在同一次工具调用中提交结构化 `delegation_reason` 和 work items。
3. Runtime 根据 agent definition、工具 effect、文件集合和依赖做验证与降级。

第一阶段不单独调用一个“router LLM”。模型负责语义分解，Runtime 负责拒绝不安全或不划算的拓扑。

### 8.3 Runtime 必须验证的事实

- work item 数量是否与实际 spawn 数一致。
- 依赖未完成的任务不能并行启动。
- 多个写任务的 `write_files` 是否重叠。
- analysis parent 不能委托 edit worker。
- requested agent 是否在父代理 allowlist。
- 预计 token 是否超过剩余 delegation budget。
- spawn depth、session spawn count、concurrent count 是否允许。
- Team 是否已由用户显式请求或批准。

## 9. 拓扑选择

### 9.1 支持的拓扑

```text
single
  Primary

one_to_one
  Primary -> Worker -> Primary

fan_out_fan_in
  Primary -> Worker A --\
          -> Worker B ----> Primary synthesis
          -> Worker C --/

chain
  Primary -> Worker A -> Primary -> Worker B -> Primary

nested
  Primary -> Coordinator subagent -> leaf workers -> Coordinator -> Primary

team
  Lead <-> Teammates
       shared task list + mailbox
```

“多对一”不表示同一个 worker 有多个 parent。普通 subagent 必须只有一个直接 parent。多对一是多个 worker 的结果最终汇入同一个 Primary。

### 9.2 决策规则

#### 使用 `single`

满足任一条件：

- 任务窄，主代理 1-3 次工具调用即可完成。
- 子任务强依赖同一段上下文。
- 多个步骤频繁修改同一文件。
- 委托成本预计大于上下文隔离或并行收益。
- delegation budget 不足。

#### 使用 `one_to_one`

- 有一个边界清楚的高容量旁路任务。
- 需要专业工具或专业 prompt。
- 结果只需回传一次，不需持续沟通。

#### 使用 `fan_out_fan_in`

- 有 2-4 个相互独立的 work item。
- 读取范围或写入文件互不重叠。
- 每项都有独立 deliverable。
- 主代理能在结果返回后一次性综合。

默认上限建议为 3，最大 4。上限不是越大越好，应由配置、模型能力和预算共同决定。

#### 使用 `chain`

- B 依赖 A 的发现。
- Reviewer 需要先看到 Implementer 的 revision。
- Debugger 找到根因后，General 才能实现。

链式信息必须由 Primary 精简后传递，不能把 A 的全部 transcript 直接注入 B。

#### 使用 `nested`

- 被委托任务本身稳定地包含多个并行叶子任务。
- 中间 coordinator 能在自己的上下文中汇总，避免大量中间信息进入 Primary。
- 已显式启用 spawn depth，并有独立的深度、数量和 token 配额。

默认关闭。第一阶段不启用。后续建议默认 `max_subagent_spawn_depth=1`，用户或项目配置最多提升到 2；更深层通常难以解释、调试和控制成本。

#### 使用 `team`

只有在成员必须相互交流时：

- 多个 review 角色需要相互挑战结论。
- 多个跨层实现者需要动态同步接口变化。
- 调试需要竞争假设并共享新证据。
- 任务需要成员自助认领和依赖解锁。

以下情况不使用 Team：

- 纯并行搜索。
- 顺序任务。
- 同一文件修改。
- 预算敏感。
- 用户只需要最终结果。

第一版 Team 必须显式 opt-in，并在启动前展示预计成员数、预算和工作区策略。

### 9.3 收益判断

定义一个可记录但不必暴露给用户的决策量：

```text
delegation_value
= parallel_time_saved
 + context_pollution_avoided
 + specialization_gain
 - context_copy_cost
 - coordination_cost
 - synthesis_cost
 - conflict_risk
 - extra_token_cost
```

只有 value 为正时才自动委托。第一阶段可用离散规则，不需要伪造精确分数。

## 10. 不同 Grace Code 模式下的行为

### 10.1 ReAct / Build

- Primary：`build`。
- 窄任务由 Primary 直接完成。
- 大范围代码调查使用 `explore`。
- 根因不明使用 `debugger`。
- 独立实现使用 `general` + worktree。
- 实现后按风险使用 `test-runner`、`code-reviewer` 或 `security-reviewer`。
- 有依赖的“诊断 → 实现 → 验证”使用 chain，不并行。

### 10.2 Plan

- Primary：`plan`。
- 只允许 analysis / verify 类型子代理，不允许 edit worker。
- 调查分区使用 `plan-researcher` 或 `explore`。
- 计划质量审查使用 `code-reviewer`。
- Save、Approve、Reject、ExitPlanMode 始终由 Primary 和 HITL 流程拥有。
- 刷新后必须从持久化 plan state 恢复，而不是等待已丢失的内存 future。

### 10.3 Explore

这是现有设计中最需要先修正的地方。

UI 的 Explore 是用户直接对话的 Primary 模式，而 `.grace/agents/explore.md` 是一个叶子 named subagent。两者重名导致：

- Web 把 `currentMode=explore` 直接作为 `agent_name` 发送。
- Runtime 得到的是 `agent_kind=named_subagent`、delegation disabled 的 leaf。
- 用户以为在使用可编排的研究主代理，实际运行的是不能 spawn 的单个 worker。

目标设计：

- UI 仍显示 “Explore”。
- 后端 entrypoint 改为 `research` Primary。
- `research` 可以委托 `explore`、`code-reviewer`、`security-reviewer` 等只读 worker。
- leaf `explore` 保留原名，继续作为快速代码搜索 worker。
- Session 持久化存 `agent_name=research`，Web 映射回 Explore tab。

用户此前提出的“调查前端每个模块、定位代码并合并总结”应产生：

```text
research Primary
  ├─ explore A: Overview + Workbench
  ├─ explore B: Inspect
  └─ explore C: Control + Quality
Primary: 检查模块边界、去重、补充跨模块关系、统一回答
```

如果项目只有三个文件且主代理已经知道位置，则应降级为 single，而不是机械 spawn。

## 11. 调度与生命周期

### 11.1 状态机

建议新增统一任务状态：

```text
proposed
  -> queued
  -> running
  -> waiting_dependency
  -> awaiting_parent_review
  -> completed

terminal alternatives:
  failed | cancelled | budget_exhausted | rejected | superseded
```

Subagent Session 的运行状态和 Delegation Task 的业务状态必须分开：

- Session 表示 agent 执行载体。
- Task 表示要完成的工作及依赖。
- 一个 Task 可因重试产生多个 generation。
- 一个已完成 Session 可通过 follow-up 恢复，但必须记录新 generation。

### 11.2 并发

- 只读任务：默认可并行。
- 当前工作区写任务：默认串行。
- worktree 写任务：目标文件不重叠时可并行。
- 不确定写集合：按可能冲突处理，串行或要求 worktree。
- 工具本身的 `ToolConcurrency` 仍是最后一道执行约束。

### 11.3 三类硬限制

与 Claude Code 当前边界对齐，分别配置：

```yaml
agents:
  max_spawn_per_session: 64
  max_concurrent_subagents: 4
  max_subagent_spawn_depth: 1
  max_fanout_per_turn: 3
```

Grace Code 的默认值可以比 Claude Code 更保守，因为本项目当前的 Web、SQLite 和本地模型环境更容易受到并发影响。

不能用同一个 `max_agents` 同时表示累计数、活跃数和层数。

### 11.4 Background 与等待

- named subagent 默认 background，但 Primary 在确实依赖结果时必须使用 barrier 等待。
- fan-out 必须等待所有 required work item 进入 terminal，或达到统一 deadline。
- optional worker 失败不应阻塞全部综合。
- required worker 失败时，Primary 可重试一次、降级为直接执行，或明确报告缺口。
- 不允许 busy polling；等待应由 completion notification 唤醒。

## 12. 通信协议

### 12.1 普通 Subagent

只允许：

```text
Parent -> Child: DelegationTask
Child -> Parent: WorkerReport / completion notification
Parent -> terminal Child: FollowUpTask (new generation)
Parent -> running Child: bounded steering message / cancel
```

第一阶段不提供 child-to-child 消息，也不提供任意 session message bus。父代理对运行中 child 的 steering 消息只在 child 的下一个安全轮次注入，不承诺中断正在执行的单个工具。

实施时需要先统一 [`agent/session/agent_control_tool.py`](../agent/session/agent_control_tool.py) 的工具描述与 [`agent/session/runtime.py`](../agent/session/runtime.py) 的真实 live-message 行为，并补齐 direct-child、状态、generation、at-most-once claim 和取消竞态测试。

### 12.2 DelegationTask

```python
@dataclass(frozen=True)
class DelegationTask:
    task_id: str
    parent_session_id: str
    agent_type: str
    goal: str
    scope: tuple[str, ...]
    constraints: tuple[str, ...]
    deliverable: str
    known_context: tuple[ContextFact, ...]
    dependencies: tuple[str, ...]
    expected_files: tuple[str, ...]
    write_files: tuple[str, ...]
    evidence_level: EvidenceLevel
    budget: DelegationBudget
```

`known_context` 只包含已验证、与任务直接相关的事实，不复制父对话全文。

### 12.3 WorkerReport

现有 `SubagentReport` 只适合 finding，需要扩展为统一 envelope：

```python
@dataclass(frozen=True)
class WorkerReport:
    task_id: str
    session_id: str
    generation: int
    agent_type: str
    status: completed | partial | failed | no_findings
    summary: str
    findings: tuple[Finding, ...]
    changed_files: tuple[ChangedFile, ...]
    verification: tuple[VerificationResult, ...]
    unresolved: tuple[str, ...]
    warnings: tuple[str, ...]
    worktree: WorktreeEvidence | None
    tokens_used: int
    duration_ms: int
```

规则：

- `summary` 是给 Primary 消费的，不直接显示为最终回答。
- analysis finding 有文件定位时必须包含 file/line/evidence。
- edit worker 必须返回 changed files 和 worktree evidence。
- test worker 必须区分 pass / product_failure / environment_failure / timeout。
- `partial` 必须写 `unresolved`。
- Runtime 验证结构，不依赖“请务必返回”提示词。

### 12.4 ResultAggregator

聚合器负责：

1. 按 task id 检查是否漏项。
2. 校验 finding 证据。
3. 识别重复 finding。
4. 标记相互冲突的结论。
5. 按来源保留 provenance。
6. 生成给 Primary 的紧凑 synthesis context。

它不替代 Primary 做产品判断。

## 13. 上下文隔离

### 13.1 Named Subagent 的启动上下文

只应包含：

- 子代理 system prompt。
- 工作目录和必要环境事实。
- DelegationTask。
- 显式预加载的 Skills。
- 允许范围的 agent memory。
- 项目规则中与该任务相关的最小片段。

不包含：

- 父会话全部聊天记录。
- 父代理未完成的 thought。
- 其他 worker 的 transcript。
- 无关 Skills 内容。
- 未声明的 MCP schema。

### 13.2 Fork 的启动上下文

- 继承不可变 parent snapshot。
- 记录 snapshot id/hash，确保可回放。
- 后续父会话消息不会自动流入 fork。
- fork 的结果仍返回调用者，不与父会话共享可变 history。

### 13.3 Resume

- resume 使用原 child session 的持久化消息。
- follow-up 作为新 generation 追加。
- 必须重新验证父子关系、模型、workspace 和权限。
- 终止状态、通知 claim 和 generation 需保持幂等，避免刷新后重复交付。

### 13.4 Memory

- 默认 subagent 不读取 Primary 的会话记忆。
- 项目级专业记忆仅在 definition 显式声明时加载。
- 子代理 memory 不能写入用户级空间，除非定义和权限同时允许。
- 一次性搜索结果不进入长期 memory。

## 14. Tools、MCP 与 Skills

### 14.1 Tool

- Agent definition 决定可见工具。
- Runtime Policy 决定实际能否执行。
- 不要把工具说明、权限说明和 agent 角色说明重复注入多次。
- `Agent`、HITL、Plan 状态切换等控制工具默认不对 leaf worker 开放。

### 14.2 MCP

- 子代理只加载 definition 明确声明的 MCP server，或父代理显式授予的交集。
- deferred MCP tools 应在 worker 真正需要时通过 ToolSearch 暴露。
- MCP 连接失败应归类为 external_dependency_failure，不能伪装成“没有找到结果”。
- 使用外部 MCP 的结果必须带来源和时间信息。

### 14.3 Skills

- definition 的 `skills` 表示启动时预加载。
- 运行时 Skill 工具只暴露允许的 skill catalog。
- Skill 内容属于方法，不自动授予 Skill 中提到的工具权限。
- 同一 Skill 不应在 system prompt、task prompt 和 runtime prompt 三次重复注入。

## 15. 写入冲突与一致性

### 15.1 文件所有权

每个写任务在启动前声明：

```text
read_set: 可能读取的文件/模块
write_set: 允许修改的文件
workspace: current | worktree
```

规则：

- write_set 重叠：禁止并行。
- write_set 未知：视为重叠。
- current workspace 中只允许一个活动写 worker。
- worktree 中允许不重叠的多个写 worker。

### 15.2 Worktree 收敛

Grace Code 不应自动静默 merge。

流程：

1. Worker 在 worktree 修改并验证。
2. Runtime 保留 revision 和 changed files。
3. Primary inspect。
4. Primary 选择 apply / retain / discard。
5. apply 后在父工作区再次验证。
6. 只有父工作区验证通过才能宣称完成。

这应继续保留为 Grace Code 相对普通共享工作区 agent 的优势。

### 15.3 跨层接口

如果 frontend/backend/tests 分给不同 worker：

- 先由 Primary 固化接口 contract。
- 每个 worker 的任务中携带相同 contract version/hash。
- 发现 contract 需要变化时，普通 subagent 先返回 Primary，由 Primary 更新并重新分配。
- 只有 Agent Team 才允许成员直接协商，但最终 contract 仍需 Lead 确认。

## 16. 预算、失败与取消

### 16.1 预算层级

```text
Session Budget
  -> Delegation Pool
      -> Workflow Budget
          -> Worker Budget
```

父代理不能把全部剩余 token 分给 worker。建议至少保留：

- 25% 用于父代理综合和最终回答。
- 10% 用于失败恢复和必要验证。

### 16.2 预算耗尽

`Execution budget exhausted` 不应只成为回答正文中的一行。

Runtime 应：

1. 终止新的 spawn。
2. 允许已完成结果被聚合。
3. 取消 optional worker 或等待 bounded grace period。
4. 给 Primary 一个受控的 finalization allowance。
5. 产生结构化 `budget_exhausted` 事件。
6. Web 在 Run outcome / Agents 面板显示预算原因。
7. 最终回答只说明对用户有意义的未完成范围，不附加内部模板字符串。

### 16.3 重试

- invalid input / policy denied：不重试。
- provider transient error：同 generation 内按策略重试。
- worker 逻辑失败：Primary 最多重新分配一次，新 generation。
- 相同失败连续出现时触发 circuit breaker。
- 重试必须计入 spawn、token 和时间预算。

### 16.4 取消

- 用户取消 Primary 时向全部 descendant 传播 cancellation token。
- background worker 到达安全点后终止。
- 已产生 worktree 变更时保留并标记 unresolved，不能自动删除。
- 刷新页面不等于取消；状态必须由服务端持久化。

## 17. Web 交互设计

### 17.1 Chat 内的轻量展示

回答正文只显示用户需要的内容。内部状态使用独立 UI：

- 输入框附近显示 “Using 3 agents” 或 “Single agent” 状态。
- 展开后显示委托原因，例如“3 个独立模块，可并行调查”。
- 每个 worker 显示角色、任务、状态、耗时和预算。
- 完成后折叠为一条 “3 agents completed · 2.4k tokens”。
- 不把 `[UNVERIFIED ...]`、budget warning、tool warning 直接拼到 assistant 正文。

### 17.2 Agents / Control 页面

现有 Multi-Agent Control Plane 应增加：

- 拓扑类型：single / fan-out / chain / nested / team。
- Primary 与 worker 的角色，而不只显示 session parent/child。
- 每个 delegation 的 `reason`。
- task dependency DAG。
- required / optional 标记。
- worker report 完整性和 evidence 状态。
- token、时间、重试和失败分类。
- worktree disposition。
- “为什么没有使用 subagent”的决策说明，便于调试自动路由。

### 17.3 用户控制

建议提供三档，不把复杂参数全部暴露给普通用户：

- Auto：系统按任务形状选择。
- Single agent：本轮禁止自动委托。
- Team / explicit agents：用户明确要求高级协作。

开发者详情页再展示：

- max fan-out。
- agent allowlist。
- nesting depth。
- model routing。
- worktree policy。

### 17.4 刷新恢复

刷新后必须从持久化数据恢复：

- delegation task。
- child session 与 generation。
- topology 和依赖。
- completion report。
- worktree disposition。
- notification 是否已由 parent claim。

不能只依赖 WebSocket 的临时 `subagent_start/stop` 事件。

## 18. 数据、API 与事件

### 18.1 新增持久化实体

建议引入：

```text
delegation_runs
  id
  parent_session_id
  parent_run_id
  topology
  reason
  status
  required_count
  completed_count
  budget_json
  created_at / completed_at

delegation_tasks
  id
  delegation_run_id
  child_session_id
  generation
  agent_type
  purpose
  goal
  scope_json
  dependencies_json
  expected_files_json
  write_files_json
  required
  status
  report_json
```

不能只从 session 时间区间反推并行和依赖。

### 18.2 API

建议：

```text
GET  /api/sessions/{id}/delegations
GET  /api/delegations/{id}
POST /api/delegations/{id}/cancel
POST /api/delegation-tasks/{id}/retry
POST /api/delegation-tasks/{id}/message
GET  /api/agents/definitions
GET  /api/agents/routing-decision?run_id=...
```

普通聊天无需让 Web 客户端自己调度。所有决策和状态迁移发生在服务端 Runtime。

### 18.3 事件

在已有 `subagent_start` / `subagent_stop` 基础上增加：

```text
delegation_planned
delegation_task_queued
delegation_task_started
delegation_task_waiting
delegation_task_reported
delegation_task_failed
delegation_synthesis_started
delegation_completed
delegation_budget_exhausted
agent_message_sent
```

事件字段至少包括：

- parent session/run/turn id。
- delegation run id。
- task id。
- child session id + generation。
- agent type。
- topology。
- reason code。
- status 和 failure category。
- token/time budget snapshot。

所有事件必须能幂等重放。

## 19. 具体代码落点

### 19.1 先修正 Explore 身份

1. 在 [`agent/session/models.py`](../agent/session/models.py) 的 built-in definitions 中新增 `research` Primary。
2. 新增 `.grace/agents/research.md`，声明 `kind: primary`、`intent: analysis`、只读工具和允许的 analysis workers。
3. 保留 `.grace/agents/explore.md` 为 named leaf，并显式 `disallowedTools: Agent`。
4. 在 [`web/src/stores/chatStore.ts`](../web/src/stores/chatStore.ts) 增加 UI mode 到 backend agent 的双向映射：
   - Explore tab -> `research`
   - persisted `research` -> Explore tab
5. [`server/routers/sessions.py`](../server/routers/sessions.py) 只接收真实 agent name，不再把 UI label 当 Runtime identity。
6. 加测试保证 `research` 可作为 Primary 创建 session，而 `explore` 不能作为 Primary entrypoint。

### 19.2 增加专业 Definitions

1. 在 `.grace/agents/` 增加 `plan-researcher.md`、`debugger.md`、`test-runner.md`、`security-reviewer.md`。
2. 将内置 definitions 与项目覆盖 definitions 的优先级写成测试。
3. 在 [`agent/session/agent_definition.py`](../agent/session/agent_definition.py) 补全 runtime contract 字段解析。
4. 在 [`agent/session/models.py`](../agent/session/models.py) 校验每类 intent、workspace、tools 的合法组合。
5. 增加 agent doctor：重复 name、空 description、无效工具、越权 allowlist、可写 agent 未使用 worktree 等应产生诊断。

### 19.3 引入 TaskShape 与 RoutingDecision

新增建议：

- `agent/session/task_shape.py`
- `agent/session/topology_planner.py`
- `agent/session/subagent_router.py`

`RoutingDecision`：

```python
@dataclass(frozen=True)
class RoutingDecision:
    topology: AgentTopology
    work_items: tuple[WorkItem, ...]
    reason_code: str
    explanation: str
    estimated_budget: DelegationBudget
    downgraded_from: AgentTopology | None = None
```

Runtime 验证后将 decision 持久化并发出 `delegation_planned`。

### 19.4 扩展 AgentTool 输入

在 [`agent/session/task_tool.py`](../agent/session/task_tool.py) 中增加：

```json
{
  "task_id": "inspect-control",
  "subagent_type": "explore",
  "description": "Inspect Control module",
  "prompt": "...",
  "scope": ["web/src/components/SafetyCenter.tsx"],
  "constraints": ["read-only"],
  "deliverable": "module purpose + routes + dependencies with file/line evidence",
  "expected_files": ["..."],
  "write_files": [],
  "required": true,
  "delegation_reason": "independent module investigation"
}
```

保留现有参数兼容性，旧调用自动转换成单个 `DelegationTask`。

### 19.5 实现真正的 AgentBatch / Workflow

不要直接让现有 stub 承担全部逻辑。建议：

1. 增加 `AgentBatchTool` 作为 Runtime-bound 工具。
2. 输入 `tasks[]` 和 topology，只支持已验证的 fan-out。
3. 逐项复用 `AgentTool` 的授权与 spawn path，不能复制另一套执行逻辑。
4. 创建 delegation run 和 task records。
5. 并发启动安全任务。
6. 使用 completion notification/barrier 等待。
7. 返回 `WorkerReport[]` 与 partial failure 信息。
8. `WorkflowTool` 后续作为更高层 declarative wrapper 调用 AgentBatch，而不是返回假成功。

涉及：

- [`tools/workflow_tool.py`](../tools/workflow_tool.py)
- [`agent/session/task_tool.py`](../agent/session/task_tool.py)
- [`agent/session/runtime.py`](../agent/session/runtime.py)
- [`agent/session/runtime_spawn.py`](../agent/session/runtime_spawn.py)
- [`agent/loop/turns.py`](../agent/loop/turns.py)
- [`agent/session/session_store.py`](../agent/session/session_store.py)

在实现前先完成一次执行所有权清理：保留 `runtime_spawn.py` 作为唯一 spawn 实现，让 `runtime.py` 只做显式绑定或薄委托，删除/迁移被 monkey patch 覆盖的重复代码。否则 AgentBatch 很容易调用到与单次 AgentTool 不一致的路径。

### 19.6 统一报告

1. 在 [`agent/session/result_contract.py`](../agent/session/result_contract.py) 增加 `WorkerReport` envelope。
2. 保留 `SubagentReport` 作为 analysis findings payload。
3. 更新 [`tools/submit_findings_tool.py`](../tools/submit_findings_tool.py) 支持 task id 和 verification。
4. 在 Runtime 构建结果时强制生成合法 report；异常也返回 typed failed report。
5. Primary prompt 只接收紧凑 report，不接收完整 child transcript。

Review 的聚合逻辑不应被丢弃。应先抽取它已验证有效的能力：

- frozen snapshot identity；
- finding path/line/snippet 复验；
- duplicate grouping；
- corroboration source；
- stale evidence；
- partial/failure 分类。

通用 `ResultAggregator` 使用这些基础能力，`ReviewService` 再在其上保留 review lens 和 review-specific scoring，避免反过来把通用调度硬塞进 Review 的业务模型。

### 19.7 嵌套委托

第二阶段之后再做：

1. 增加三个独立配置项：累计 spawn、并发 spawn、spawn depth。
2. 默认 depth=1，即只有 Primary 可 spawn。
3. [`agent/session/agent_registry.py`](../agent/session/agent_registry.py) 对每一层都尊重当前 parent 的显式 allowlist，避免“非 Primary 自动看到全部 public children”的宽泛行为。
4. [`agent/session/registry_builder.py`](../agent/session/registry_builder.py) 只有在深度和 allowlist 均允许时才挂载 Agent。
5. 每层从父级剩余 delegation budget 中继续切分，不能重新获得完整预算。
6. Web topology 显示真实层级。

### 19.8 Agent Team

独立里程碑，不与普通 subagent 一起偷偷上线：

新增：

- `agent/team/team_runtime.py`
- `agent/team/task_board.py`
- `agent/team/mailbox.py`
- `agent/team/lease_manager.py`

必须具备：

- shared task list 与依赖。
- task claim lease，避免双重认领。
- direct message mailbox。
- Lead 最终综合。
- 文件 ownership / worktree。
- shutdown protocol。
- 单独预算。
- 显式 feature flag 和用户确认。

在这些能力完成前，Control Plane 必须继续显示 `arbitrary_agent_message_bus=false`，不能把父子 Session Tree 标成 Agent Team。

## 20. 分阶段实施

### Stage 1：身份和 Definitions

目标：让不同模式拥有正确 Primary，并拥有一组真实可选的专业 worker。

- 修正 Explore -> research Primary。
- 新增第一批专业 definitions。
- 规范 description、tools、permissions、skills。
- 默认禁止嵌套。
- 增加 registry/parser/entrypoint 测试。

验收：

- Explore 模式能委托 leaf `explore`。
- leaf `explore` 不能直接作为 Primary session。
- Plan 不能委托可写 worker。
- Build 可按 allowlist 使用不同专业 worker。

### Stage 2：TaskShape 与可解释路由

目标：系统知道为什么委托或为什么不委托。

- 实现 `TaskShape`、`WorkItem`、`RoutingDecision`。
- 增加 Runtime 验证和降级。
- 持久化 decision。
- Web 显示 reason。

验收：

- 小任务保持 single。
- 多个独立模块选择 fan-out。
- 同文件写任务降级为 serial。
- 预算不足时减少 worker 数。

### Stage 3：真实 Fan-out / Fan-in

目标：替换 Workflow 假成功。

- 实现 AgentBatch。
- barrier、partial failure、deadline。
- WorkerReport 聚合。
- 持久化 delegation run/task。
- 抽取 Review 现有冻结快照、证据复验和去重能力。
- 收敛 `runtime.py` / `runtime_spawn.py` 的 spawn 实现所有权。

验收：

- 2-3 个只读任务真实并行。
- 刷新后能恢复各 task 状态。
- 一个 optional worker 失败仍能综合。
- required worker 缺失时不能假装完整。

### Stage 4：Chain 与专业验证

目标：支持“调查 → 实现 → 验证 / review”。

- dependency DAG。
- 精简上下文传递。
- debugger/general/test-runner/reviewer 链。
- provenance 与最终验证 gate。

验收：

- B 在 A 完成前不能启动。
- B 只收到需要的 verified facts。
- edit 结果未在 parent workspace 验证前不能完成。

### Stage 5：并行写入与一致性

目标：安全支持多个独立实现 worker。

- read/write set。
- worktree ownership。
- 冲突预测和 apply 流程。
- parent verification。

验收：

- write_set 重叠拒绝并行。
- disjoint worktree 可并行。
- apply/discard/retain 全部可回放。

### Stage 6：受控嵌套

目标：与 Claude Code 当前可配置嵌套行为对齐。

- 三类 limit。
- per-level allowlist。
- nested budget。
- nested topology UI。

验收：

- 默认 leaf 无 Agent。
- 开启 depth=2 后只有第一层可继续 spawn。
- 超深度错误不可重试并能降级为自己完成。

### Stage 7：Agent Team 实验能力

目标：支持真正成员通信，不冒充普通 subagent。

- shared task list。
- mailbox。
- task claim / dependency。
- direct user-to-teammate interaction。
- opt-in 与成本提示。

验收：

- teammate 可直接通信。
- 同一 task 只有一个有效 lease。
- Lead 可看到 idle/failure 通知。
- Team shutdown 不遗留 running task。

## 21. 测试计划

### 21.1 单元测试

- Agent definition 字段解析和优先级。
- description/intent/tools/workspace 合法组合。
- TaskShape 校验。
- topology 选择和降级。
- allowlist 与 delegation scope。
- 三类并发/深度限制。
- WorkerReport schema 和 evidence。
- write_set 冲突。
- budget 分配。

建议新增：

- `tests/test_agent_definition_catalog.py`
- `tests/test_task_shape.py`
- `tests/test_topology_planner.py`
- `tests/test_subagent_router.py`
- `tests/test_agent_batch_tool.py`
- `tests/test_worker_report.py`
- `tests/test_nested_delegation_limits.py`

### 21.2 集成测试

- Explore UI -> research Primary -> explore children -> synthesis。
- Plan -> parallel research -> plan approval，不发生代码写入。
- Build -> debugger -> general worktree -> test-runner -> reviewer。
- background completion notification at-most-once。
- refresh / restart 后恢复。
- budget exhausted 后受控 finalization。
- cancellation 传播。
- worktree preserved 后 apply。

### 21.3 Prompt / Model 行为测试

不能只断言 prompt 包含一句文字，应使用 fake backend 验证：

- 小任务模型不调用 Agent。
- 多模块请求调用 2-3 个不同 scope 的 worker。
- 请求 edit 时不选择 read-only worker 完成修改。
- worker 报告缺证据时 Primary 标记缺口。
- 不把内部 warning 拼进最终正文。

### 21.4 Web 测试

- topology、状态、reason、token 正确显示。
- normal/summary 模式下内部事件不污染正文。
- 刷新后 worker 和 final output 都存在。
- mobile 下 agent 卡片可折叠。
- 键盘可访问、焦点顺序、aria-live 不重复播报流式事件。

## 22. 可观测指标

上线后至少记录：

- delegation rate：多少 turn 使用了 subagent。
- avoidable delegation rate：委托后无有效产出或主代理重复完成。
- specialization accuracy：所选 agent 是否匹配 task purpose。
- fan-out utilization：并行时间节省。
- report completeness：structured report 合法率。
- evidence validity：file/line 验证通过率。
- conflict rate：write_set 冲突与 apply 冲突。
- recovery rate：worker 失败后成功降级比例。
- token amplification：多代理 token / 单代理基线。
- final answer latency。
- refresh recovery correctness。

目标不是提高 delegation rate，而是提高有效委托率。

## 23. 关键验收场景

### 场景 A：单文件解释

请求：“解释 `chatStore.ts` 里 sendMessage 的流程。”

预期：Primary 直接读取并回答，topology=`single`。

### 场景 B：多模块调查

请求：“调查前端各模块的功能、定位代码并合并总结。”

预期：research Primary 按模块边界 fan-out 2-3 个 explore worker，最后统一汇总。

### 场景 C：错误诊断

请求：“Plan Save 后卡住，刷新 Reject 返回 500，定位根因。”

预期：可将服务端状态恢复、前端交互、持久化/HITL 分给独立 debugger/explore；Primary 交叉验证，不修改代码。

### 场景 D：独立实现

请求：“同时补后端 endpoint 和独立前端展示。”

预期：接口先固定；文件集合不重叠时使用两个 general worktree；Primary inspect/apply 后跑集成验证。

### 场景 E：同文件修改

请求包含三个都修改 `chatStore.ts` 的步骤。

预期：拒绝 fan-out，Primary 串行执行或只委托一个 worker。

### 场景 F：Plan Mode

请求：“生成实现计划。”

预期：只读 research workers；Save 之后 Primary 继续进入审批状态；刷新恢复；Reject 不触发 500；没有 edit worker。

### 场景 G：Review

请求：“全面 review 这次权限系统改动。”

预期：correctness、security、test coverage 可并行；Primary 去重和处理冲突。普通 subagent 足够，不默认创建 Team。

### 场景 H：需要讨论的架构评审

请求明确要求多个角色互相挑战方案。

预期：若 Team feature 已启用，先展示成本和成员并获得确认；否则用普通并行 reviewer，并明确其结果由 Lead 汇总、成员不直接通信。

## 24. 风险和防护

| 风险 | 后果 | 防护 |
|---|---|---|
| 过度委托 | token 和延迟增加 | single-first、收益判断、预算保留 |
| 角色描述模糊 | 选错 worker | description 规范、路由测试、decision trace |
| 上下文不足 | worker 重复搜索或误判 | typed known_context、scope、deliverable |
| 权限扩大 | 安全边界破坏 | 权限交集、Runtime policy、不可重试拒绝 |
| 并行写冲突 | 代码覆盖 | write_set、worktree、显式 apply |
| 假并行 | UI 显示并行但实际未执行 | 持久化 task interval、真实 scheduler event |
| 假 Workflow 成功 | 主代理误以为完成 | 移除 stub 成功语义、真实 AgentBatch |
| 结果污染正文 | 用户体验不规范 | structured event 独立渲染 |
| 刷新丢状态 | 卡住或 500 | 服务端持久化状态机、幂等 generation |
| 嵌套爆炸 | 成本和调试失控 | 默认关闭、深度/并发/累计三限制 |
| 把普通 subagent 叫 Team | 能力承诺失真 | feature flag、能力披露、独立模型 |

## 25. 最终设计原则

1. 专业化优先于数量。
2. Single agent 是默认正确路径，多代理必须证明收益。
3. Primary 始终负责最终结果。
4. 普通 subagent 是星型父子通信；Team 才是共享任务和直接消息。
5. 独立任务并行，依赖任务链式，同文件写入串行。
6. Context、Workspace、Permission、Model 是四个独立维度，不得混用。
7. Skill 提供方法，Subagent 提供隔离执行。
8. Prompt 负责引导，Runtime 负责约束。
9. 所有委托都应可解释、可持久化、可回放。
10. 内部运行状态属于 UI 和 trace，不属于回答正文。

## 26. 推荐的下一步

下一次实施只做 Stage 1，不同时展开 Workflow、嵌套和 Team：

1. 修复 Explore / research 身份冲突。
2. 建立第一批专业 agent definitions。
3. 补全 definition parser 和合法性校验。
4. 写 registry、entrypoint 和模式映射测试。
5. 用“前端模块调查”作为第一个端到端验收场景。

完成 Stage 1 后，再实现 TaskShape 和真实 fan-out。这个顺序能先修正“谁在运行、谁可以委托”的根边界，再增加自动调度，避免在错误身份模型上继续堆功能。

## 24. 实施结果（2026-07-26）

上面的分阶段顺序是设计阶段的风险控制方案。本轮已按最终设计一次性完成核心链路，不再以“下一次只做 Stage 1”作为当前实施状态。

| 设计能力 | 实现位置 | 当前状态 |
|---|---|---|
| Primary / leaf / specialist 身份 | `.grace/agents/*.md`、`agent/session/models.py`、`agent/session/agent_definition.py` | 已实现 |
| Explore UI 到 research Primary 映射 | `web/src/modes.ts`、`web/src/stores/chatStore.ts`、`web/src/components/ChatView.tsx` | 已实现 |
| TaskShape、DAG 与拓扑决策 | `agent/session/task_shape.py`、`agent/session/topology_planner.py`、`agent/session/subagent_router.py` | 已实现 |
| 一对一持久化委托 | `agent/session/task_tool.py`、`agent/session/session_store.py` | 已实现 |
| fan-out/fan-in 与 chain | `agent/session/agent_batch_tool.py` | 已实现；2–4 个任务，按依赖波次执行 |
| 嵌套深度、累计 spawn、并发限制 | `agent/session/runtime_spawn.py`、`agent/session/registry_builder.py` | 已实现；默认深度 1 |
| 统一 WorkerReport | `agent/session/result_contract.py` | 已实现 |
| live parent steering | `agent/session/agent_control_tool.py`、`agent/session/runtime.py` | 已实现；下一安全轮次注入 |
| delegation run/task 持久化 | `agent/session/session_store.py` | 已实现；刷新后可恢复 |
| 取消、失败重试与预算展示 | `server/routers/multi_agent.py`、`server/services/multi_agent_service.py` | 已实现 |
| Agent Team 提案、审批、拒绝 | `agent/session/agent_team_tool.py`、`agent/session/runtime.py` | 已实现；显式 feature flag，Agent 无权自批 |
| Team Mailbox、共享任务表与租约 | `agent/team/*`、`agent/session/team_coordination_tool.py` | 已实现；teammate 可直接发信、收件和读取 board |
| Team worktree review gate | `agent/session/runtime.py`、`agent/team/task_board.py` | 已实现 |
| Multi-Agent Control Plane | `web/src/components/MultiAgentControlPlane.tsx`、`web/src/api/multiAgent.ts` | 已实现 |
| Workflow 假成功移除 | `tools/workflow_tool.py` | 已实现；别名路由到真实 AgentBatch |

### 24.1 运行开关与默认限制

```text
GRACE_MAX_SUBAGENTS_PER_SESSION=64
GRACE_MAX_CONCURRENT_SUBAGENTS=4
GRACE_MAX_SUBAGENT_SPAWN_DEPTH=1
GRACE_MAX_FANOUT_PER_TURN=3

GRACE_AGENT_TEAMS_ENABLED=0
GRACE_AGENT_TEAM_MAX_MEMBERS=4
GRACE_AGENT_TEAM_MAX_TASKS=32
GRACE_AGENT_TEAM_LEASE_TTL_SECONDS=120
```

Agent Team 默认关闭。启用后，Agent 只能调用 `ProposeAgentTeam` 保存提案；用户必须在 Multi-Agent Control Plane 批准，之后才会创建 durable team run/task 并启动真实 teammate child session。拒绝提案不会创建 delegation run。

### 24.2 恢复与一致性边界

- 普通 subagent 的 session、delegation run/task、WorkerReport 和 worktree evidence 均持久化。
- Team 的 durable task board 会在刷新或进程重启后保留。
- Team 的 live mailbox 和 lease 不伪装成可跨进程恢复；重启后 Control Plane 显示 `recovery_required`，需要显式重新激活。
- Worktree 结果处于 `preserved` 时，Team task 进入 `awaiting_review`；只有 apply/discard 完成后，Lead 才能收敛任务状态。
- Agent 回答正文只接收父代理综合后的结果；路由、预算、审批、失败和恢复状态在 Control Plane 展示。

### 24.3 验证覆盖

新增或扩展的核心测试包括：

- `tests/test_scenario_agent_definitions.py`
- `tests/test_task_shape_domain.py`
- `tests/test_topology_planner_domain.py`
- `tests/test_subagent_router_domain.py`
- `tests/test_agent_batch_runtime.py`
- `tests/test_agent_team_domain.py`
- `tests/test_agent_team_runtime_adapter.py`
- `tests/test_multi_agent_service.py`

这些测试覆盖 definition 权限、拓扑降级、DAG 校验、并发冲突、真实 delegation 持久化、WorkerReport、Team 审批/拒绝、依赖排序和环境限制。
