# Grace Code Workbench Multi-Agent 模式设计与实施计划

> 状态：实现完成、发布验收进行中（本地代码、非 manual 回归与真实 Git worktree 门禁已通过；外部 LLM/UI/运维门禁待完成）  
> 基线日期：2026-07-29  
> 目标：以 `Multi-Agent` 模式替换 Workbench 当前的 `Explore` 模式  
> 适用范围：Web Workbench、Agent Definition、Session Runtime、AgentBatch、Delegation 持久化、WebSocket 事件、Multi-Agent UI、兼容迁移与质量门禁  
> 相关设计：[`SCENARIO_DRIVEN_SUBAGENT_ARCHITECTURE_PLAN.md`](./SCENARIO_DRIVEN_SUBAGENT_ARCHITECTURE_PLAN.md)

## 1. 执行摘要

Workbench 当前提供 `Build / Plan / Explore` 三种模式，但 `Explore` 的实际行为是把 UI 模式映射到只读 `research` Primary Agent。它能够委派只读调查任务并汇总证据，却不能把复杂实现任务拆分给多个可写 Agent、集成结果并完成统一验证。

本计划将 Workbench 模式调整为：

```text
Build | Plan | Multi-Agent
```

其中：

- `Build` 继续负责以单个 Primary Agent 为中心的直接实现，可按需委派。
- `Plan` 继续负责只读研究、需求澄清和结构化实施计划。
- `Multi-Agent` 新增独立的 Orchestrator Primary Agent，负责拆解复杂任务、生成任务 DAG、动态选择专业 Subagent、执行 fan-out/fan-in、集成 worktree、完成统一验证并向用户输出一个最终结果。

Multi-Agent 模式不是现有实验性 Agent Team 的别名：

- Multi-Agent 是临时、主从式、由 Orchestrator 自动调度的执行拓扑。
- Agent Team 是固定成员、共享任务板、Mailbox、Lease 与用户审批组成的高级 P2P 协作能力。
- Multi-Agent 默认复用 `AgentBatch`、真实 child session、`WorkerReport`、delegation 持久化和 worktree 收敛能力。
- Agent Team 保持独立 feature flag 和显式审批，不进入本模式的默认执行主链。

本次改造不得退化为仅修改前端标签，也不得仅依赖 Prompt 要求模型“多调用几个 Agent”。模式语义、运行时状态、失败处理、持久化、恢复、UI 和测试必须一起闭环。

## 2. 当前实现基线

### 2.1 Explore 的实际调用链

当前链路为：

```text
ChatView 选择 explore
  -> chatStore.currentMode = "explore"
  -> agentNameForUiMode("explore") = "research"
  -> POST /api/sessions/{id}/messages { agent_name: "research" }
  -> AgentService / ChatPipeline
  -> SessionRuntime.run_session()
  -> AgentFactory.create(agent_name="research")
  -> research Primary Agent
```

关键代码：

- [`web/src/modes.ts`](../web/src/modes.ts)：`Explore -> research` 映射。
- [`web/src/components/ChatView.tsx`](../web/src/components/ChatView.tsx)：模式菜单、slash command、快捷键和请求 intent。
- [`web/src/stores/chatStore.ts`](../web/src/stores/chatStore.ts)：保存每个 session 的模式并发送 `agent_name`。
- [`web/src/api/sessions.ts`](../web/src/api/sessions.ts)：HTTP chat 请求。
- [`agent/session/runtime.py`](../agent/session/runtime.py)：Primary Agent 执行、历史注入、结果持久化和终态事件。
- [`agent/session/agent_factory.py`](../agent/session/agent_factory.py)：根据 Agent Definition 构建工具、策略和 TaskContract。
- [`.grace/agents/research.md`](../.grace/agents/research.md)：当前 Explore 产品模式实际使用的 agent 契约。

### 2.2 当前行为限制

`research` 当前具有以下约束：

- `intent: analysis`；
- `permissionMode: plan`；
- `delegationScope: read_only`；
- 禁止 `Write / Edit / Bash`；
- 只能委派 `explore / code-reviewer / security-reviewer`；
- 适合并行调查和证据汇总，不适合复杂实现。

`explore` 本身是只读叶子 Subagent，不能再次委派，也不能写文件。当前产品名称因此与用户期望的复杂任务协同执行不一致。

### 2.3 可直接复用的能力

[`agent/session/agent_batch_tool.py`](../agent/session/agent_batch_tool.py) 已经提供：

- `fan_out_fan_in` 和 `chain` 拓扑；
- 任务依赖检查和稳定波次执行；
- 并发安全检查；
- 真实 child session；
- Subagent Router；
- token、step、time、fan-out 和并发预算；
- required/optional worker 语义；
- `WorkerReport`；
- delegation run/task 持久化；
- partial failure；
- 返回所有报告供 Primary Agent 综合。

[`agent/session/session_store.py`](../agent/session/session_store.py) 已有：

- `delegation_runs`；
- `delegation_tasks`；
- child session；
- completion notification；
- trace event 持久化基础。

已有 Worker 角色：

| Agent | 用途 | Intent | 工作区 | 是否可写 |
|---|---|---|---|---:|
| `explore` | 文件定位、调用链和模块分析 | analysis | current | 否 |
| `general` | 边界明确的实现任务 | edit | worktree | 是 |
| `debugger` | 失败诊断和根因分析 | analysis | current | 否 |
| `test-runner` | 限定范围验证 | analysis/verification purpose | current | 否 |
| `code-reviewer` | 正确性和维护性审查 | analysis | current | 否 |
| `security-reviewer` | 安全边界审查 | analysis | current | 否 |

### 2.4 现有 Agent Team 的边界

[`agent/team/`](../agent/team/) 和 [`agent/session/agent_team_tool.py`](../agent/session/agent_team_tool.py) 实现了实验性 Agent Team：

- feature flag：`GRACE_AGENT_TEAMS_ENABLED`；
- 用户审批；
- 固定成员；
- Mailbox；
- shared TaskBoard；
- Lease claim；
- 控制面逐项执行；
- 进程内 live state；
- 重启后 `recovery_required`。

它适合确实需要成员直接通信和共享任务认领的场景，不适合作为 Workbench Multi-Agent 模式的默认执行器。两套概念必须在命名、API、UI 和文档中保持分离。

## 3. 产品语义和术语

### 3.1 UI 与 Runtime 命名

推荐命名：

| 层级 | 名称 | 说明 |
|---|---|---|
| Workbench 标签 | `Multi-Agent` | 用户看到的运行模式 |
| UI key | `multi-agent` | 前端持久化与切换 key |
| Backend agent name | `orchestrator` | Primary Agent 的职责名称 |
| 执行拓扑 | `single / one_to_one / fan_out_fan_in / chain` | Runtime 事实 |
| Worker | named subagent | 临时执行单元 |
| 高级协作能力 | `Agent Team` | 保留现有固定成员/P2P 语义 |

不建议把 Backend Agent 命名为 `team`，否则会与现有 `TeamRuntime`、`ProposeAgentTeam` 和控制面混淆。

### 3.2 Multi-Agent 模式承诺

用户选择 Multi-Agent 后，系统承诺：

1. Orchestrator 对用户目标和最终结果负责。
2. 对可有效拆分的任务生成结构化 DAG，而不是无条件创建 Agent。
3. 动态选择最合适的 Worker，而不是全部使用 `general` 或 `explore`。
4. 独立任务并行，有依赖任务按 wave 串行推进。
5. 写任务默认进入独立 worktree，禁止多个 Worker 并发写同一 current workspace。
6. Worker 结果必须经过完整性检查、集成和验证，不能直接拼接为最终答复。
7. 用户只收到一个由 Orchestrator 输出的最终结果。
8. UI 能显示为什么拆分、有哪些任务、当前阶段、失败和最终验证事实。

### 3.3 不适合拆分的任务

Multi-Agent 不代表所有请求都必须启动多个 Worker。以下情况允许直接执行或降级为单 Worker：

- 仅需读取一个已知文件；
- 修改范围极小且高度耦合；
- 子任务会修改同一核心文件，无法安全隔离；
- 启动 Worker 的成本高于任务本身；
- 剩余预算不足；
- 用户明确要求单 Agent；
- 高风险操作需要先等待用户决策。

每次降级必须记录 `reason_code` 和解释，以便 UI 和回放区分“合理单 Agent”与“编排失效”。

## 4. 目标架构

```text
User
  |
  v
Workbench Multi-Agent Mode
  |
  v
Orchestrator Primary Agent
  |
  +-- Task Shape Decision
  |      -> direct / one_to_one / fan_out_fan_in / chain
  |
  +-- Structured Work DAG
  |      -> scope / dependencies / files / acceptance / verification
  |
  +-- AgentBatch Scheduler
  |      +-- explore
  |      +-- general (isolated worktree)
  |      +-- debugger
  |      +-- test-runner
  |      +-- code-reviewer
  |      +-- security-reviewer
  |
  +-- Result Collection
  |      -> WorkerReport / findings / changed files / failures
  |
  +-- Integration Gate
  |      -> inspect / apply / discard / conflict handling
  |
  +-- Verification Gate
  |      -> tests / build / review / requirement checks
  |
  +-- Synthesis
  |      -> one final response
  v
Persisted trace + timeline + Multi-Agent visualization
```

### 4.1 Orchestrator 职责

Orchestrator 必须：

- 保留原始用户目标；
- 判断是否拆分及理由；
- 生成可验证的任务 DAG；
- 确保 scope 和 write set 尽量不重叠；
- 为任务选择 Agent；
- 等待所有 required task；
- 审查 WorkerReport 与真实 workspace/worktree 事实；
- 按依赖顺序集成变更；
- 处理冲突、失败、重试和取消；
- 完成最终验证；
- 综合一个最终答复。

不得委托给 Worker 的责任：

- 用户意图的最终解释；
- 高风险操作审批；
- 多个结果冲突时的最终决策；
- worktree 是否应用；
- 整体任务是否成功；
- 面向用户的最终答复。

### 4.2 Worker 契约

每个 Worker 只处理一个有限任务，并返回统一报告：

```text
status
summary
findings
changed_files
verification
unresolved
warnings
tokens_used
duration_ms
worktree evidence
```

Worker 不得：

- 自行扩大范围；
- 宣称整个用户任务完成；
- 在未授权时修改 current workspace；
- 把未读取或未验证的内容写成事实；
- 绕过父级 cancellation、budget 和 permission policy。

## 5. 模式与会话兼容迁移

### 5.1 前端模式模型

修改 [`web/src/modes.ts`](../web/src/modes.ts)：

```ts
export type UiMode = "build" | "plan" | "multi-agent";
```

映射：

```text
build       -> build
plan        -> plan
multi-agent -> orchestrator
```

历史兼容读取：

```text
research     -> legacy explore session
explore      -> legacy explore session
orchestrator -> multi-agent
```

推荐 UI 策略：

- 历史 `research/explore` 会话可以显示 `Multi-Agent` 标签和“Legacy read-only run”提示。
- 不批量改写数据库中历史 session 的 `agent_name`。
- 用户在旧会话中选择 Multi-Agent 并发送新消息时，请求明确携带 `orchestrator`。
- `/explore` 暂时保留为兼容 alias，显示迁移提示并切换到 `/multi-agent`。
- 至少经过一个兼容周期后再考虑移除 `/explore`。

### 5.2 Session Agent 切换

必须确认并测试：

- 当前请求的 `body.agent_name` 是否仅影响当前 run，还是会更新 session agent；
- 刷新页面后模式来源是 session 持久化字段还是前端内存；
- 一个 session 在 Build、Plan、Multi-Agent 之间切换时，历史消息和 TaskContract 不发生错误继承；
- 旧 `research` 运行中的 session 不被前端切换逻辑中途改为 `orchestrator`；
- mode 切换时不复用上一个 agent 的 permission mode、injected rules 或 backend 实例。

### 5.3 UI 文案和入口

修改：

- [`web/src/components/ChatView.tsx`](../web/src/components/ChatView.tsx)
- [`web/src/components/ModeTab.tsx`](../web/src/components/ModeTab.tsx)
- 快捷键帮助、slash commands、placeholder、欢迎页和设置描述

推荐文案：

```text
Multi-Agent
Break complex work into coordinated specialist tasks, then integrate and verify the result.
```

不能继续使用“Read the repo, inspect files, and report findings”作为描述。

## 6. Orchestrator Agent Definition

### 6.1 新增定义

新增项目级定义：

```text
.grace/agents/orchestrator.md
```

同时在 [`agent/session/models.py`](../agent/session/models.py) 增加内置 fallback，确保项目文件缺失时仍可创建该 Primary Agent。

建议契约：

```yaml
name: orchestrator
kind: primary
intent: edit
permissionMode: default
allowedSubagents:
  - explore
  - general
  - debugger
  - test-runner
  - code-reviewer
  - security-reviewer
```

工具建议：

- Read / Glob / Grep：主线程核对关键事实；
- Agent：单 Worker 委托；
- Runtime 动态附加的 AgentBatch；
- worktree inspect/apply/discard/retain；
- Edit/Write：只用于小型集成修复，不用于替代正常拆分；
- Bash/pytest：最终集成验证；
- git status/diff：验证 workspace 收敛。

### 6.2 权限要求

`orchestrator` 必须是 `intent: edit`，否则 Runtime 会把 delegation effect 限制为只读，不能合法委派 `general`。

同时必须保证：

- 子 Agent 不能获得超过父 Agent 的能力；
- hidden reviewer 只有显式 allowlist 才可使用；
- `general` 保持 `worktree` 隔离；
- Orchestrator 仍经过 PermissionPipeline；
- Multi-Agent 模式不自动降低危险命令的审批级别；
- 选择 Multi-Agent 仅表示同意自动拆分，不表示同意高风险工具操作。

### 6.3 Prompt 不是唯一约束

Prompt 用于指导拆解质量，但以下规则必须由 Runtime 验证：

- Agent 是否可委派；
- intent 和 delegation scope 是否允许；
- DAG 是否有环；
- task id 是否唯一；
- write set 是否冲突；
- 并发、深度和预算是否超限；
- worktree 是否真实存在并属于当前 root session；
- required task 是否全部达到可接受终态；
- 未解决 worktree 是否阻止整体完成。

## 7. 结构化编排契约

### 7.1 Work Item Schema

在现有 AgentBatch task 基础上补充：

```json
{
  "id": "frontend",
  "goal": "Implement the frontend integration",
  "prompt": "...",
  "purpose": "implementation",
  "agent": "general",
  "scope": ["web/src"],
  "depends_on": ["api-contract"],
  "expected_files": ["web/src/api/example.ts"],
  "write_files": ["web/src/api/example.ts"],
  "required": true,
  "acceptance_criteria": ["API errors are rendered"],
  "verification": ["npm test -- --run ..."],
  "isolation": "worktree"
}
```

新增字段必须保持后向兼容：旧 AgentBatch 调用没有这些字段时继续使用默认值。

### 7.2 Task Shape Decision

每次 Orchestrator 运行先形成一个持久化决策：

```text
topology
reason_code
explanation
estimated worker count
estimated budget
write conflict assessment
user-visible summary
```

推荐 reason code：

- `direct_small_scope`
- `single_specialist`
- `independent_domains`
- `dependency_chain`
- `parallel_read_only`
- `isolated_parallel_writes`
- `shared_write_conflict`
- `insufficient_budget`
- `user_requested_multi_agent`
- `high_coordination_cost`

### 7.3 拓扑选择

| 条件 | 拓扑 |
|---|---|
| 小任务或高度耦合 | direct |
| 一个专业领域 | one_to_one |
| 两个以上独立范围 | fan_out_fan_in |
| 后续任务依赖前序结果 | chain / wave DAG |
| 多个写任务且文件不重叠 | worktree fan-out + ordered integration |
| 写文件重叠 | 串行、重新划分或由 Orchestrator 直接集成 |
| 需要成员 P2P 通信 | 提示使用显式 Agent Team，不自动升级 |

## 8. AgentBatch 增强计划

### 8.1 保留单一执行路径

不得为 Multi-Agent 模式复制第二套 spawn 实现。所有 Worker 必须继续通过：

```text
AgentBatch
  -> SessionRuntime.spawn_agent()
  -> child session
  -> AgentFactory
  -> TaskContract / PhasePolicy / PermissionPipeline
```

这样 cancellation、budget、tool policy、worktree 和 completion notification 才不会产生双轨行为。

### 8.2 从固定 2～4 任务扩展到波次 DAG

当前单次 AgentBatch 限制 2～4 个 task。建议分两步：

第一阶段：

- 保持 2～4 限制；
- 跑通 Multi-Agent 产品闭环；
- 验证 UI、集成和终态。

第二阶段：

- 一个 delegation run 可保存更大的 DAG；
- 每个并发 wave 仍受 `GRACE_MAX_CONCURRENT_SUBAGENTS` 和 `GRACE_MAX_FANOUT_PER_TURN` 限制；
- 只调度 dependency 已完成的 ready tasks；
- 不一次性启动全部 task；
- 每个 wave 后重新检查 cancellation、预算和失败传播。

### 8.3 并发安全

并发判定至少检查：

- Worker 是否只读；
- `write_files` 是否重叠；
- workspace mode 是否为 worktree；
- 任务是否依赖同一未完成产物；
- 是否共享数据库、生成目录、锁文件或不可并发测试资源；
- 是否会执行高副作用命令。

`write_files` 为空不能自动解释为“安全并行写”。对可写 Agent 缺少 write set 时，应保守串行或要求重新规划。

### 8.4 失败传播

- required dependency 失败：下游 task 标记 blocked/failed，不启动。
- optional dependency 失败：由 task 契约决定是否继续。
- Worker 超时：保存已知 child id、generation 和错误分类。
- Worker 返回 partial：不等同 completed，由 Orchestrator 判断是否满足 acceptance criteria。
- Worker 报告成功但 session/worktree 事实不一致：标记 contract violation。
- 批次内部异常：必须终结 delegation run，不能永久停在 running。

## 9. 写任务、Worktree 与集成

### 9.1 写任务隔离

所有 `general` Worker 默认使用独立 worktree。禁止：

- 多个写 Worker 直接并发修改 parent current workspace；
- 仅凭 Worker summary 判断文件已进入 parent workspace；
- 自动应用 revision 已变化的 worktree；
- 未审阅 changed files 就整体完成。

### 9.2 Integration Gate

每个写任务完成后，Orchestrator 必须执行：

1. 读取 persisted `AgentRunResult`；
2. 检查 worktree disposition；
3. 重新 inspect worktree；
4. 比较 expected revision；
5. 对比 declared `write_files` 与实际 changed files；
6. 审阅 diff 和 acceptance criteria；
7. 按 DAG 依赖顺序 apply/discard/retain；
8. apply 后验证 parent workspace；
9. 记录 integration outcome。

推荐 integration outcome：

```text
pending_review
accepted
applied
no_changes
rejected
discarded
conflict
stale
retained
```

### 9.3 冲突处理

发生冲突时不得静默重试或覆盖：

- 标记相关 task 为 `integration_blocked`；
- 保存冲突文件和 base/revision；
- Orchestrator 可做小型手工整合；
- 复杂冲突重新创建一个 scoped `general` integration task；
- 重新运行受影响验证；
- UI 显示冲突和处理结果。

### 9.4 Completion Guard

现有 unresolved preserved worktree 会阻止 parent 完成，应继续保留。Multi-Agent 还需增加：

- required task 未终结时阻止完成；
- required write task 未有明确 disposition 时阻止完成；
- integration conflict 未解决时阻止成功；
- final verification 未执行或失败时，根据任务契约阻止成功；
- synthesis 尚未持久化时不能发送 completed terminal。

## 10. 验证与最终综合

### 10.1 Verification Gate

验证应分层：

1. Worker 局部验证：在自己的 scope/worktree 内执行。
2. 集成验证：所有 accepted worktree 应用后在 parent workspace 执行。
3. 契约验证：逐项检查用户要求和 acceptance criteria。
4. 可选独立审查：code-reviewer/security-reviewer。

最终验证结果必须记录：

```text
command/check
scope
status
exit code
decisive output
duration
failure category
source session/task
```

不得把 Worker worktree 中通过的测试当作 parent 集成后通过。

### 10.2 Synthesis 输入

最终综合至少包含：

- 原始用户目标；
- topology decision；
- task DAG；
- 每个 WorkerReport；
- required/optional 完成情况；
- changed files；
- worktree integration outcome；
- 最终验证；
- unresolved/warnings；
- budget/cancellation 信息。

### 10.3 最终状态

| 状态 | 条件 |
|---|---|
| completed | required tasks、integration 和 required verification 全部通过 |
| partial | 有可用结果，但至少一个 required 交付未完全满足 |
| failed | 无法交付核心目标或契约被破坏 |
| cancelled | 用户或上层 cancellation 中止 |
| blocked | 需要用户审批、外部依赖或人工冲突决策 |

最终答复不得隐藏 partial、failed、skipped verification 或 retained worktree。

## 11. 持久化与状态机

### 11.1 Delegation Run Phase

在现有 status 之外增加明确 phase，或用兼容字段表达：

```text
planning
queued
executing
integrating
verifying
synthesizing
completed
partial
failed
cancelled
blocked
recovery_required
```

状态转换必须由 Runtime 控制，不由 UI 推断。

### 11.2 Schema 迁移原则

修改 [`agent/session/session_store.py`](../agent/session/session_store.py) 时：

- 只做 additive migration；
- 新列必须有安全默认值；
- 老数据库打开后不要求重建；
- row projection 同时兼容字段存在和缺失；
- JSON 字段反序列化失败时返回可诊断错误，不让整个 session 列表崩溃；
- migration 必须幂等；
- 索引只针对真实查询路径增加。

建议补充：

Delegation run：

```text
phase
orchestrator_agent
synthesis_json
verification_json
interrupted_at
```

Delegation task：

```text
acceptance_criteria_json
verification_json
retry_count
max_retries
integration_status
integration_error
supersedes_task_id
```

字段最终形态应在实现前通过 schema review 确认，避免为了 UI 重复存储可计算数据。

### 11.3 幂等性

- chat POST 继续使用 idempotency key；
- 同一 AgentBatch tool call 不得重复创建相同 run；
- task start 使用 CAS 或等价检查；
- child completion 按 child/generation 去重；
- worktree apply 使用 expected revision；
- synthesis 只能对同一 run/version 生效一次；
- WebSocket replay 不得重复增加任务计数。

## 12. 取消、重试与恢复

### 12.1 取消

取消 root Multi-Agent run 时：

1. 取消 root token；
2. 阻止 queued task 启动；
3. 向 running child 传播取消；
4. 等待有限 grace period；
5. 保存已完成报告；
6. 不自动应用未审阅 worktree；
7. delegation run 进入 cancelled 或 partial；
8. 发出唯一 terminal event。

### 12.2 Retry

当前独立 retry endpoint 会创建新的 delegation run，无法自然回到原 run 的 integration/synthesis。Multi-Agent 需要：

- retry 关联原 task；
- 递增 generation/retry_count；
- 保留旧报告作为历史；
- 新报告 supersede 旧报告；
- 成功后重新计算依赖 ready 状态；
- 必要时重新进入 integration/verification/synthesis；
- 防止 retry 后出现两个有效 worktree。

### 12.3 进程恢复

第一版采用保守恢复，不伪装原线程仍存活：

- completed task 复用持久化报告；
- queued task 可重新调度；
- 进程退出时 running task 标记 interrupted/recovery_required；
- child session 已完成但报告未回填时执行 reconciliation；
- preserved worktree 重新 inspect revision；
- Mailbox/Lease 不参与普通 Multi-Agent 恢复；
- 恢复后必须重新执行 integration 和 verification 判断；
- 不自动重放高风险工具。

## 13. 事件、WebSocket 与 Timeline

### 13.1 事件契约

复用并完善：

```text
delegation_planned
delegation_task_queued
delegation_task_started
delegation_task_reported
delegation_task_failed
delegation_synthesis_started
delegation_completed
delegation_budget_exhausted
```

建议增加或明确：

```text
delegation_phase_changed
delegation_task_blocked
delegation_task_retrying
delegation_integration_started
delegation_integration_completed
delegation_verification_started
delegation_verification_completed
```

每个事件至少包含：

```text
session_id
run_id
turn_id
delegation_run_id
task_id（如适用）
sequence
timestamp
phase/status
child_session_id（如适用）
```

### 13.2 顺序和重放

- EventBus 继续作为持久化和广播的单一入口；
- 不增加只广播不持久化的旁路；
- sequence 必须单调；
- timeline replay 和 live WS 使用相同 reducer；
- task terminal 事件按 `(delegation_run_id, task_id, generation)` 去重；
- root `run_terminal` 只能在 delegation 进入终态后发出；
- WS 断线重连后必须恢复任务卡片，而不是只恢复 assistant 文本。

### 13.3 前端类型

修改：

- [`web/src/types/events.ts`](../web/src/types/events.ts)
- [`web/src/types/blocks.ts`](../web/src/types/blocks.ts)
- [`web/src/stores/chatStore.ts`](../web/src/stores/chatStore.ts)

不得继续让 delegation event 以未声明的任意对象穿过前端。需要 discriminated union 和统一 reducer。

## 14. Workbench UI 设计

### 14.1 主聊天内展示

新增 `MultiAgentRunCard` 或等价 ContentBlock：

```text
Multi-Agent · Executing
4 tasks · 2 running · 1 completed · 1 waiting

✓ Inspect backend flow       explore
● Implement backend          general
● Implement frontend         general
○ Integration verification   test-runner
```

支持：

- 展开 DAG；
- 显示依赖；
- 查看 child session；
- 查看 WorkerReport；
- 显示 changed files；
- 显示 integration/verification；
- 取消 task/run；
- 对可重试 task retry；
- 最终折叠为稳定摘要。

### 14.2 状态来源

聊天内卡片以 WS/timeline event reducer 为实时来源，以 MultiAgent snapshot 为校验/恢复来源。不能让两个来源独立维护互相冲突的状态。

推荐：

- live：事件驱动；
- reconnect/refresh：timeline 重建；
- 高级检查：`GET /api/multi-agent/{session_id}`；
- snapshot 发现不一致时触发 reconciliation 提示，不静默覆盖正在流式更新的数据。

### 14.3 Multi-Agent Control Plane

[`web/src/components/MultiAgentControlPlane.tsx`](../web/src/components/MultiAgentControlPlane.tsx) 保留为高级视图，并调整术语：

- 普通 Multi-Agent run 显示 Orchestrator、DAG、Worker 和 worktree；
- Agent Team 区域继续明确标注“Explicit opt-in capability”；
- 不把普通 Multi-Agent worker 称为 teammate；
- 不要求用户到控制面逐项启动普通 Multi-Agent task；
- routing decision、budget、失败、重试和恢复状态保持可审计。

### 14.4 可访问性与性能

- 任务状态不能只靠颜色；
- 按钮有 disabled reason；
- 大 DAG 默认折叠；
- 高频 token/thought 事件不触发整棵 DAG 重渲染；
- child 输出按需加载；
- 卡片在 summary/normal/verbose 三种 view mode 下有不同密度；
- 移动或窄窗口不横向溢出。

## 15. API 计划

### 15.1 继续复用 Session Chat API

用户请求仍通过：

```text
POST /api/sessions/{session_id}/messages
```

携带：

```json
{
  "prompt": "...",
  "agent_name": "orchestrator",
  "intent": "edit",
  "idempotency_key": "..."
}
```

不要新增另一套 Multi-Agent chat endpoint，否则会复制 run/turn/idempotency/WS 生命周期。

### 15.2 Multi-Agent 控制 API

现有 [`server/routers/multi_agent.py`](../server/routers/multi_agent.py) 继续承担：

- snapshot；
- task cancel；
- task retry；
- 高级 Agent Team 操作。

建议补充普通 delegation run 级操作：

```text
POST /api/multi-agent/{session_id}/runs/{run_id}/cancel
POST /api/multi-agent/{session_id}/runs/{run_id}/resume
POST /api/multi-agent/{session_id}/tasks/{task_id}/retry
GET  /api/multi-agent/{session_id}/runs/{run_id}
```

所有操作必须验证 run/task 确实属于该 root session，避免跨 session 控制。

## 16. 安全与权限边界

### 16.1 不因模式切换放宽权限

Multi-Agent 增加执行单元，意味着风险面更大，因此：

- 每个 child 独立经过 policy registry；
- 父级 allowlist 和 delegation scope 必须生效；
- 用户 deny 必须传播；
- dangerous tool 仍需正常审批；
- Worker 不能共享父 Agent 的一次性 approval，除非审批契约明确允许；
- 路径安全检查以实际 execution repo/worktree 为准；
- 不把 Agent 输出当作可信命令或路径直接执行。

### 16.2 资源滥用防护

继续并统一使用：

```text
GRACE_MAX_SUBAGENTS_PER_SESSION
GRACE_MAX_CONCURRENT_SUBAGENTS
GRACE_MAX_SUBAGENT_SPAWN_DEPTH
GRACE_MAX_FANOUT_PER_TURN
```

同时要求：

- run 级 token reserve；
- Orchestrator synthesis reserve；
- recovery reserve；
- 每个 Worker 上限；
- wall-clock timeout；
- circuit breaker；
- 同一任务重复失败后停止自动 retry。

### 16.3 Prompt Injection 与不可信 Worker 输出

Worker 返回内容视为不可信数据：

- 不执行 Worker 报告中的任意命令；
- 文件路径必须重新做 scope/path 验证；
- worktree facts 从 Runtime 获取，不从文本解析；
- tool approval 不能由 Worker summary 伪造；
- Web 内容中的指令不得改变 Orchestrator policy。

## 17. 可观测性与指标

每个 Multi-Agent run 应能回答：

- 为什么拆分或没有拆分；
- 使用了何种拓扑；
- 创建了哪些 Worker；
- 哪些任务并行；
- 每个任务耗时和 token；
- 哪些任务失败、重试或被阻塞；
- 哪些 worktree 被应用或丢弃；
- 最终验证结果；
- 最终状态为什么是 completed/partial/failed。

建议指标：

```text
multi_agent_runs_total
multi_agent_topology_total{topology}
multi_agent_workers_total{agent,status}
multi_agent_task_duration_ms
multi_agent_task_tokens
multi_agent_peak_parallelism
multi_agent_integration_conflicts_total
multi_agent_retries_total
multi_agent_recovery_required_total
multi_agent_synthesis_failures_total
```

日志和事件禁止记录 secrets、完整环境变量或未经截断的大型 tool output。

## 18. 分阶段实施批次

### Batch A：命名、兼容与 Agent Definition

范围：

- 新增 `orchestrator` builtin 和 `.grace/agents/orchestrator.md`；
- `UiMode` 增加 `multi-agent`，移除产品入口中的 `explore`；
- 保留 `research/explore` 历史读取；
- 更新 `/multi-agent`、旧 `/explore` alias、快捷键和文案；
- 更新模式映射测试和 Agent Definition 测试。

验收：

- 新 session 可发送 `agent_name=orchestrator`；
- Build/Plan 行为不变；
- 历史 research session 可打开；
- orchestrator 能委派 read-only 和 write Worker；
- 不启用 Agent Team feature flag 也能使用 Multi-Agent。

回滚：恢复 UI 入口映射即可，数据库没有破坏性迁移。

### Batch B：只读 Multi-Agent 闭环

范围：

- Orchestrator 使用现有 AgentBatch 执行 2～4 个只读 task；
- 持久化 topology decision；
- 完整 delegation event；
- 主 Agent 等待并综合；
- 失败和 cancellation 闭环。

验收：

- 前后端独立调查可真实并行；
- child session、run/task、WorkerReport 可从 snapshot 查看；
- required task 失败时不能返回 completed；
- 最终只出现一个面向用户的综合答复。

### Batch C：聊天内可视化

范围：

- 前端 typed delegation events；
- reducer；
- MultiAgentRunCard；
- refresh/reconnect replay；
- child detail、cancel 和 retry 入口。

验收：

- live、refresh 和 replay 三条路径显示一致；
- 不重复任务、不重复计数；
- run_terminal 后卡片进入稳定终态；
- 不影响 Build/Plan 的 streaming blocks。

### Batch D：写任务与 Worktree 集成

范围：

- `general` Worker；
- declared/actual write set 验证；
- integration phase；
- ordered apply/discard；
- conflict 和 stale revision；
- parent workspace 最终验证。

验收：

- 两个不重叠实现任务可并行；
- 修改不会在审阅前进入 parent workspace；
- 重叠写任务不会被错误并行；
- unresolved worktree 阻止成功；
- 集成后测试而不是只在 child 中测试。

### Batch E：大 DAG、Retry 与恢复

范围：

- wave scheduler；
- 更大 task graph；
- retry generation/supersede；
- interrupted run reconciliation；
- phase 恢复；
- budget reserve。

验收：

- 进程中断后不会假装 running Worker 仍存活；
- 已完成 task 不重复执行；
- retry 可以回到原 run 并重新触发 synthesis；
- 大 DAG 不突破并发限制。

### Batch F：清理和正式发布

范围：

- 移除 Explore 产品文案；
- 更新用户文档和帮助；
- 清理仅服务旧映射的代码，但保留数据兼容 reader；
- 指标和告警；
- feature rollout；
- 最终回归和性能检查。

验收：所有质量门禁通过，且 Agent Team 仍保持独立、显式启用和用户审批。

## 19. 测试矩阵

### 19.1 单元测试

模式和定义：

- `multi-agent -> orchestrator`；
- `orchestrator -> multi-agent`；
- legacy `research/explore` 可读取；
- Build/Plan 映射不变；
- orchestrator 是 Primary/edit；
- allowedSubagents 正确；
- general 使用 worktree。

DAG 和调度：

- unique id；
- unknown dependency；
- cycle；
- fan-out；
- chain；
- blocked propagation；
- required/optional；
- read/write routing；
- overlapping write set；
- missing write set；
- budget downgrade。

状态机：

- phase 合法转换；
- duplicate terminal；
- cancel；
- retry generation；
- supersede；
- recovery_required；
- synthesis once-only。

### 19.2 Runtime 集成测试

- 真实 child session 创建；
- parent/root/depth/generation 正确；
- child 使用正确 Agent Definition；
- cancellation 传播；
- WorkerReport 持久化；
- EventBus 顺序；
- delegation run 终态；
- worktree inspect/apply/discard；
- stale revision；
- apply 后 parent diff；
- completion guard。

### 19.3 API 测试

- chat 请求创建 orchestrator run；
- idempotency；
- snapshot projection；
- root ownership 验证；
- task cancel/retry；
- run cancel/resume；
- unknown/stale task；
- legacy database projection；
- Agent Team endpoint 不受影响。

### 19.4 前端测试

- ModeTab；
- slash alias；
- session reopen；
- event normalization；
- out-of-order sequence；
- replay dedup；
- MultiAgentRunCard 状态；
- child detail；
- cancel/retry；
- partial/failed 文案；
- view mode；
- Build/Plan streaming regression。

### 19.5 端到端场景

场景 1：并行只读调查

```text
分析前端模式选择、后端 Session 执行和持久化模型，并统一总结。
```

期望：至少两个独立 Worker，最终统一回答，无文件变更。

场景 2：跨前后端实现

```text
新增后端 API，并在前端调用和展示，完成相关验证。
```

期望：调查、后端实现、前端实现、集成验证形成 DAG；写任务进入 worktree；最终 parent workspace 包含完整变更。

场景 3：写冲突

两个 Worker 被规划修改同一文件。期望 Runtime 拒绝并行、重新规划或串行，不发生覆盖。

场景 4：required Worker 失败

期望下游 blocked，整体不能 completed，最终答复明确未完成项。

场景 5：取消

运行中取消。期望 queued 不启动、running 收到取消、未审阅 worktree 不应用、只有一个 terminal。

场景 6：刷新和 WS 重连

期望 Multi-Agent 卡片从 timeline 重建，计数和 child 状态不重复。

场景 7：进程重启

期望持久化任务可重建；running 标记 recovery_required；不伪造 Mailbox/Lease；可显式恢复。

场景 8：模式回归

Build 和 Plan 的计划审批、工具权限、streaming、run terminal、历史恢复均保持原行为。

## 20. 防回归质量门禁

每个 Batch 合并前必须通过：

1. 受影响 Python 单元和集成测试；
2. Web TypeScript typecheck；
3. Web 相关组件测试；
4. session/migration 测试；
5. Build/Plan 模式回归；
6. WS replay/terminal 回归；
7. worktree 安全测试（涉及写任务的 Batch）；
8. 取消和超时测试；
9. 至少一个真实 Multi-Agent smoke scenario。

禁止以“命令退出码为 0”作为唯一验收证据。必须检查：

- 实际 agent_name；
- child session 数量和角色；
- delegation run/task 状态；
- changed files 所在 workspace；
- worktree disposition；
- final verification；
- UI replay 后状态；
- 最终 response 与真实结果一致。

## 21. 发布与回滚策略

### 21.1 Feature Rollout

建议新增独立开关，而不是复用 Agent Team 开关：

```text
GRACE_MULTI_AGENT_MODE_ENABLED
```

阶段：

1. 开发环境：仅显式开关可见；
2. 内部默认：收集成功率、成本、冲突和取消数据；
3. 灰度用户：允许 UI 切换；
4. 默认启用：替换 Explore；
5. 清理兼容入口，但保留历史 reader。

### 21.2 回滚

必须支持：

- 关闭 UI Multi-Agent 入口；
- 停止创建新的 orchestrator run；
- 已持久化 run 仍可查看和取消；
- 不回滚 additive database columns；
- legacy Build/Plan session 继续可用；
- 不自动把 orchestrator session 改写成 build/research；
- preserved worktree 继续允许用户处理。

## 22. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 仅改 UI 名称 | 行为仍是 read-only research | 独立 orchestrator identity 和 runtime 验收 |
| Prompt 不稳定拆分 | 有时不委派或错误委派 | 结构化契约 + Runtime 校验 + reason code |
| 并行写覆盖 | 代码丢失或错误合并 | worktree + write set + integration gate |
| Child 成功但整体失败 | 用户看到虚假成功 | required task、integration、verification completion guard |
| WS 重放重复 | UI 任务重复和计数错误 | sequence + generation key + 单一 reducer |
| 进程重启丢状态 | 永久 running 或无法恢复 | durable phase/task/report + conservative reconciliation |
| Token 成本失控 | 响应慢、费用高 | topology downgrade + reserves + limits |
| Build/Plan 被污染 | 原有稳定模式回归 | 独立 agent、模式测试和 session-scoped policy |
| Multi-Agent 与 Agent Team 混淆 | API/UI/维护成本上升 | 术语和执行主链严格分离 |
| Retry 产生双 worktree | 集成错误 | generation/supersede + revision ownership |
| Schema 迁移破坏旧 DB | 无法启动或读取 session | additive/idempotent migration + fixture 测试 |
| 恶意 Worker/Web 输出 | 越权工具或路径操作 | 输出不可信、Runtime 重新验证 |

## 23. 明确禁止的实现方式

- 只把 `Explore` 文案改成 `Multi-Agent`。
- 继续把新模式映射到 `research`。
- 把 Multi-Agent 默认绑定到 `TeamRuntime`。
- 依赖用户在控制面逐项点击启动 Worker。
- 为 Multi-Agent 复制第二套 Session/Run/WS 执行路径。
- 让多个 `general` Worker 并发修改 parent current workspace。
- 仅凭 Worker 文本声明应用或验证结果。
- required task 失败后仍发 completed。
- 在 root terminal 之后继续修改 delegation 状态。
- 把内部协调日志直接拼进最终答复。
- 为了展示并行而强行拆分小任务。
- 通过关闭审批、路径检查或 completion guard 来简化实现。

## 24. 完成定义

Multi-Agent 模式只有在以下条件全部满足时才视为完成：

1. Workbench 正式显示 `Build / Plan / Multi-Agent`。
2. Multi-Agent 映射到独立 `orchestrator` Primary Agent。
3. 历史 `research/explore` session 可正常读取。
4. Orchestrator 可根据任务选择 direct、单 Worker、fan-out 或 chain。
5. read-only 和 write Worker 均使用真实 child session。
6. 写 Worker 默认 worktree 隔离，并通过显式 Integration Gate 收敛。
7. required task、integration 和 verification 共同决定最终状态。
8. delegation 状态可持久化、回放、取消、重试并保守恢复。
9. 聊天主时间线能展示 Multi-Agent DAG 和阶段，不依赖控制面手工推进。
10. 最终只由 Orchestrator 输出一个基于真实执行结果的答复。
11. Build 和 Plan 的既有行为及审批流程无回归。
12. Agent Team 仍是独立、显式启用、需要审批的高级能力。
13. 所有防回归质量门禁和端到端验收场景通过。

## 25. 建议的首个实施切片

首个切片应限制为“只读 Multi-Agent 闭环”，不要一次同时引入写任务、恢复和大 DAG：

1. 新增 `orchestrator`；
2. 前端增加 `Multi-Agent` 并保留 legacy reader；
3. Orchestrator 使用现有 AgentBatch 执行 2～4 个只读 Worker；
4. 持久化 topology 和 WorkerReport；
5. 聊天内显示任务卡片；
6. 完成 cancellation、partial failure 和 replay；
7. 验证 Build/Plan 无回归。

该切片通过后，再加入 `general + worktree + integration + final verification`。这样能够把产品入口、执行语义、事件链和 UI 先稳定下来，减少同时修改所有高风险边界造成的新 Bug。


## 26. 2026-07-29 Batch D–F 实施复核

本轮已完成 Batch D–F 的代码实现范围，并保持 Build/Plan 与实验性 Agent Team 的独立语义。由于完成定义第 13 项要求全部质量门禁和端到端场景通过，而真实 Multi-Agent LLM、浏览器 UI 和发布运维场景仍有外部前提，**本节不把整个计划标记为无条件发布完成**。

### 26.1 实施完成范围

- **产品与兼容**：Workbench 使用 `Build / Plan / Multi-Agent`，Multi-Agent 映射独立 `orchestrator` Primary Agent；历史 `research/explore` reader 与 `/explore` alias 保留；Agent Team 继续使用独立 feature flag、控制面和审批语义。
- **真实委派与 DAG**：Orchestrator 通过 `AgentBatch + child session + WorkerReport` 执行 read-only/write Worker；支持 direct、fan-out、chain 和大 DAG 分 wave 调度。`GRACE_MAX_MULTI_AGENT_TASKS` 控制 run 总任务数，fanout/concurrency 限制每 wave，不再把总 DAG 错误限制为 2～4 个 task。
- **写集成安全**：edit Worker 必须声明非空 `write_files`；Runtime 从 worktree facts 计算 actual changed files，越出 declared write set 时标记 `contract_violation`。`DelegationIntegrationCoordinator` 按持久化 DAG 顺序处理每个 task 的显式 reviewed `apply/discard/retain`，并用 revision 检查拒绝 stale decision；未审阅 worktree 不会自动进入 parent workspace。
- **最终验证**：集成后的 parent verification 是 required gate，只运行管理员配置的 JSON argv，并以 `shell=False` 执行；不执行 Worker/LLM 文本中的命令。未配置验证命令时保持 `awaiting_verification`，不会伪造 passed。
- **Retry 与恢复**：非叶 task retry 会原子 supersede 目标和 downstream，创建 replacement generation、重写依赖并保留历史报告；resume 对 interrupted 子图使用相同的保守 replacement 语义。durable scheduler 只调度 ready wave，传播 blocked，并避免把重启前线程伪装为 running。
- **状态与 exactly-once terminal**：phase、blocked、retrying、integration 和 verification 使用 typed lifecycle event。delegation terminal 通过 `BEGIN IMMEDIATE` 将状态 CAS 与 trace insert 放入同一事务；仅 CAS 胜者经 EventBus 广播已经持久化的事件，避免重复 terminal 和状态/trace 间隙。
- **API 与操作面**：已提供 run GET/cancel/resume/integrate/verify 和 task retry API，同时保留 legacy run cancel endpoint。关闭新建 feature 后，历史 run 仍可查看、取消和处置。
- **聊天卡片**：`MultiAgentRunCard` 展示 DAG 依赖、WorkerReport、changed files、unresolved/warnings、token/duration、integration/verification gate；支持 child detail、task cancel/retry、run cancel/resume、reviewed apply/discard/retain 和 verify。
- **Replay 与 reconciliation**：timeline、live WS 和 durable snapshot synthetic events 汇入同一 typed reducer，使用 sequence/generation 去重；refresh、reconnect 和 fallback trace 不建立第二套控制面状态源。
- **发布配置与观测投影**：新增独立 `GRACE_MULTI_AGENT_MODE_ENABLED`、task/fanout/concurrency 限额和 verification 配置，并记录于 `.env.template`。snapshot 提供 feature/limits 以及 run/task/integration/verification/token/duration 指标投影。
- **迁移与兼容**：delegation schema 采用 additive/idempotent migration；Build/Plan 映射、历史 session reader 和 Agent Team endpoint 未被替换。

### 26.2 验证证据

- 三项历史回归已修复并通过定向验证：`3 passed`。observation 翻译按行为字段及 EventEnvelope/`tool_call_id` 契约验证；`TASK_FAILED` 只根据 Runtime 生产的结构化 status/boolean 取消事实映射 `cancelled`，自由错误文本不会改变终态；hook 重写后的 schema 二次校验继续 fail-closed，并恢复稳定的类型错误文案。
- Multi-Agent/worktree 聚焦 Python 套件：`33 passed`，覆盖 write-set、ordered integration、stale revision、parent verification、non-leaf retry、downstream reschedule、resume、typed events、exactly-once terminal、feature/config、legacy additive migration、snapshot metrics、大 DAG/预算边界及 worktree resolution contract。
- 非 manual 回归已按外部依赖拆分并取得可核验退出码：纯本地 `python -m pytest tests --ignore tests/manual --ignore tests/test_e2e_smoke.py -q --tb=short` 为 `492 passed`、`0 failed`、真实 return code `0`、外层耗时 `31.709s`；依赖用户现有 localhost:8765 的 server smoke 单独运行，为 `5 passed`、`0 failed`、真实 return code `0`。合计 `497 passed`。review cancellation 并发测试另行重复 `20/20` 通过。
- 真实 Git/worktree 临时仓库验收通过：从同一 clean parent 创建两个真实 worktree，非重叠 changed files 与 revision 检查正确，ordered apply 两次均为 `APPLIED`，parent 内容完整且 clean；旧 revision 返回 `STALE` 并保留 worktree；真实同一行冲突返回 `CONFLICT`、merge 自动 abort、parent 保持 clean；最终只剩 parent worktree。
- 现有 localhost server 的 Build-mode HTTP/WS smoke 在全量套件中通过；它证明 server/session/chat/WS 基础链路可用，但不替代真实 Multi-Agent Orchestrator 或浏览器 E2E。
- Manual lifecycle quick self-check 通过，`test_server_lifecycle.py` 可收集 2 个测试；需要真实 LLM 或启动长期 server 的 abort、timeout 和完整 lifecycle 场景未在本轮执行。
- 前端 delegation reducer：通过 esbuild bundle 后由 Node 执行，乱序、去重、snapshot/live reconciliation 等断言无错误。
- Web production build：成功，`95 modules transformed`；仅有 bundle chunk 超过 500 kB 的非失败警告。
- `git diff --check` 未发现本轮源文件空白错误；用户已有 `README.md` 仍只报告 CRLF warning，该文件未被本轮修改、覆盖或清理。
- 当前环境仍有发布卫生告警：Requests 依赖组合 warning、2 个测试 mock coroutine 未 await warning，以及 websockets/uvicorn deprecation warning；均未造成测试失败，但应在正式发布环境清理。

### 26.3 完成定义逐项复核

| 完成定义 | 复核结果 |
|---|---|
| 1–3：正式入口、独立 orchestrator、legacy reader | 实现层满足 |
| 4–5：拓扑选择、真实 read/write child session | 实现层满足；真实供应商 LLM 场景仍待外部 smoke |
| 6：worktree 隔离与显式 Integration Gate | 实现层、聚焦测试和真实临时 Git 多 worktree apply/stale/conflict 验收满足 |
| 7：required task、integration、verification 决定终态 | 实现层与聚焦测试满足 |
| 8：持久化、回放、取消、重试、保守恢复 | 实现层与聚焦测试满足 |
| 9：聊天主时间线展示 DAG/阶段并可操作 | 实现层和 reducer/build 验证满足；真实浏览器 E2E 待验收 |
| 10：Orchestrator 单一综合答复 | 执行契约满足；真实 LLM 端到端输出待验收 |
| 11：Build/Plan 无回归 | 非 manual 全量 `497 passed`、Web build 和既有 Build-mode server smoke 满足本地回归门禁 |
| 12：Agent Team 独立、显式和需审批 | 实现层满足 |
| 13：全部门禁与端到端场景通过 | **未满足** |

### 26.4 外部与发布验收阻塞

以下项目不再是 Batch D–F 的核心代码缺口，但仍阻止完成定义第 13 项和 Batch F 的“所有质量门禁通过”：

1. 经用户明确允许外部 LLM 调用后，使用真实供应商凭据执行 Multi-Agent 只读调查、跨前后端写任务、required failure、cancel 和统一 synthesis smoke，并核对实际 `agent_name`、child 数量/角色、delegation 状态、最终 response；不得在未授权时把项目代码发送给第三方。
2. 启动隔离 server/browser 环境执行聊天卡片 live、refresh、WS reconnect、child detail、reviewed integration 和操作按钮 E2E。当前 Build-mode HTTP/WS smoke 已通过，但不覆盖 Multi-Agent UI。
3. 为 `tests/manual` 提供隔离且可重复的 server/LLM fixture，再执行 abort、LLM timeout 和完整双 server lifecycle；本轮仅通过不启动长期服务的 lifecycle quick self-check。
4. 将现有 snapshot metrics projection 接入正式 metrics exporter/alert sink；当前已具备基础投影，但尚未配置外部告警。
5. 在目标发布环境执行并发、时延、token 成本、大 DAG 和前端 bundle 的性能基准；单元回归与 Web build 不能替代性能验收。
6. 清理 Requests 依赖组合、测试 mock coroutine 未 await、websockets/uvicorn deprecation 和前端大 chunk 等发布告警。

已从阻塞清单移除：非 manual 全量的 3 个既有失败，以及真实 Git 多 worktree ordered apply/stale/conflict 验收；两项均已在本轮闭环。

**结论：Batch D–F implementation complete；local acceptance gates substantially complete；release acceptance pending external LLM/UI/operations gates。** 在上述阻塞清零前，不宣称整个 Multi-Agent 完成定义或 Batch F 发布质量门禁全部通过。
