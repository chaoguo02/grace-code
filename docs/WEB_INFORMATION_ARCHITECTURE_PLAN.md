# Grace Code Web 信息架构重组方案

> 状态：设计方案，尚未实施  
> 范围：Web 导航、模块职责、跨模块关联与迁移计划  
> 原则：保留能力，减少一级入口；按用户任务分组，不按后端子系统罗列

## 1. 当前问题

当前代码包含 14 个顶部标签：

`Overview / Chat / Runs / Context / Eval / System / Agents / Health / Safety / Replay / Review / Plans / Memory / Trace`

除此之外，Session Sidebar、Session Tree 和 Chat 右侧 Event Rail 也承担独立信息区，因此用户感知上接近 15 个以上页面。

问题不在于能力太多，而在于所有能力都以相同权重出现在一级导航：

- Chat 和 Trace 的使用频率完全不同，但当前拥有相同导航权重。
- Runs、Context、Replay、Trace 都是在解释同一次执行，却被拆成四个平级产品模块。
- System、Agents、Safety 描述的是同一个控制面，只是分别展示配置、运行拓扑和权限。
- Health 与 Eval 都在回答“系统是否可靠”，区别只是线上历史与离线场景。
- Plans、Review、Memory 都服务于开发工作流，却远离 Chat。
- 页面作用域不明显：有的面向项目，有的依赖选中 session，有的是混合作用域。

## 2. 设计目标

重组后只保留 5 个一级模块：

1. `Overview`
2. `Workbench`
3. `Inspect`
4. `Control`
5. `Quality`

每个旧页面仍然保留独立组件、API 和错误边界，只降为模块内部的二级视图。

目标不是把十四个页面拼成五个巨大页面，而是建立两层信息架构：

```text
一级模块：用户现在要做什么
    └── 二级视图：当前任务需要哪类信息
```

验收目标：

- 一级导航从 14 个减少到 5 个。
- Chat 仍然一键可达。
- 任意旧页面最多两次点击可达。
- 页面刷新后能够恢复模块、二级视图和选中 session。
- 页面标题明确显示当前是 Project scope、Session scope 还是 Hybrid scope。
- 低频页面保留能力，但不再与 Chat 竞争视觉注意力。

## 3. 最终模块设计

### 3.1 Overview

定位：项目入口、能力地图和演示导航。

二级视图：无。

包含现有页面：

- `Overview`

主要回答：

- Grace Code 是什么？
- 当前项目有哪些已配置能力？
- 哪些能力已有实际运行证据？
- 面试或演示应该从哪里开始？

作用域：Project + selected-session overlay。

重要性：入口级，但不是日常工作区。

设计要求：

- 保持当前 capability evidence、demo journeys 和 recent sessions。
- 不继续增加详细表格。
- 所有卡片只负责摘要和导航，详细证据留在对应模块。
- Overview 不能成为其他服务的单点故障，继续保持分区降级。

### 3.2 Workbench

定位：用户实际完成开发工作的地方。

默认二级视图：`Chat`

二级视图：

| 新名称 | 现有页面 | 作用 | 作用域 | 权重 |
|---|---|---|---|---|
| Chat | Chat | 提交任务、查看回答、处理 HITL、观察实时执行 | Session | 核心 |
| Plans | Plans | 查看保存的计划、版本和 revision diff | Project / linked session | 次要 |
| Changes | Review | 查看 diff、人工接受/拒绝、发起多 Agent review | Hybrid | 核心 |
| Memory | Memory | 管理持久知识、查看 recall 和 generated memory | Project + session overlay | 高级 |

为什么放在一起：

- Plans 是工作开始前的结构化意图。
- Chat 是任务执行的主要交互面。
- Changes 是任务执行后的代码决策面。
- Memory 是影响后续工作的长期输入。

它们形成完整工作闭环：

```text
Memory / Plan
      ↓
     Chat
      ↓
   Changes
```

设计要求：

- Chat 保持模块默认页和最高视觉权重。
- Chat 组件即使切换到同模块其他视图，也应谨慎处理卸载，避免中断流式状态。
- Plans 使用“计划库”定位，不与 Chat 内的 plan approval 重复：
  - Chat 中审批当前计划；
  - Plans 中检索和比较持久化计划。
- Changes 取代导航中的 Review 名称，因为它同时包含 diff decision 和 review orchestration。
- Memory 标记为 Advanced，不放在首个视觉焦点。

### 3.3 Inspect

定位：解释一条选中 session 或 run 到底发生了什么。

默认二级视图：`Run Summary`

二级视图：

| 新名称 | 现有页面 | 作用 | 作用域 | 权重 |
|---|---|---|---|---|
| Run Summary | Runs | 运行结果、步骤、工具、验证和 workspace delta | Session / run | 核心证据 |
| Context | Context | provider request 的 token、能力和压缩边界 | Session / request | 高级 |
| Replay | Replay | replay contract、终止边界和失败复盘 | Session / run | 失败分析 |
| Event Trace | Trace | 原始有序事件和实时事件过滤 | Session | 专家 |

为什么放在一起：

四个页面都是同一运行事实的不同抽象层：

```text
Run Summary：发生了什么
Context：模型当时看见了什么
Replay：能否完整复盘，失败边界在哪里
Event Trace：最底层事件证据是什么
```

设计要求：

- Run Summary 是默认页，承担主要解释责任。
- Context、Replay、Trace 不应重复展示 Run Summary 已有的 headline metrics。
- Run Summary 中提供明确的 `Open context / Open replay / Open trace` 深链。
- Context 的选择应能关联到 run_id、turn_id 或 step_number。
- Replay 仅在需要验证契约或排查失败时突出。
- Event Trace 使用 `Expert` 标识，默认不主动加载大量事件。
- Trace 与 Chat 右侧 Event Rail 的区别必须写清：
  - Event Rail：当前对话的轻量实时辅助；
  - Event Trace：完整历史、筛选和底层审计。

### 3.4 Control

定位：解释系统如何配置、如何调度、允许做什么。

默认二级视图：`Architecture`

二级视图：

| 新名称 | 现有页面 | 作用 | 作用域 | 权重 |
|---|---|---|---|---|
| Architecture | System | Agent、Tool、Skill、MCP、Memory、Runtime 的配置拓扑 | Project + session overlay | 概览 |
| Agents | Agents | 实际委派、上下文隔离、调度、回执和 worktree 收敛 | Selected session tree | 核心证据 |
| Safety | Safety | 权限层、规则、审批记录和继承边界 | Project + session overlay | 核心证据 |

为什么放在一起：

这三个页面分别表示控制面的三个层次：

```text
Architecture：系统被配置成什么样
Agents：这次任务实际如何被调度
Safety：这些执行主体被允许做什么
```

设计要求：

- Architecture 不再使用含糊的 `System` 导航名。
- Architecture 保持 configured topology，不能把配置存在描述成运行成功。
- Agents 需要选中 session；未选择时显示最近含子 Agent 的 session 建议，而不是空白页。
- Safety 继续同时展示静态规则和 selected-session approval overlay。
- Agents 中的 worktree consistency 可以深链到 Workbench / Changes。
- Safety 的 approval item 可以深链到 Inspect / Event Trace 的对应 sequence。

### 3.5 Quality

定位：判断系统整体是否稳定，以及改动是否造成能力回归。

默认二级视图：`Health`

二级视图：

| 新名称 | 现有页面 | 作用 | 作用域 | 权重 |
|---|---|---|---|---|
| Health | Health | 跨 session 成功率、延迟、token、工具错误和失败分类 | Project / time window | 日常质量 |
| Evaluations | Eval | 固定场景、baseline、artifact 和 regression comparison | Project / evaluation run | 发布质量 |

为什么放在一起：

两者都在回答质量问题，但证据来源不同：

```text
Health：真实历史运行得怎么样
Evaluations：受控场景是否仍然满足预期
```

设计要求：

- Health 默认展示 30 天真实运行证据。
- Evaluations 必须继续强调 chat completion 不等于 evaluation pass。
- Health 中的 reference objectives 不能叫 SLA。
- Eval regression 可以深链到 Inspect，但只有关联到真实 session/run 时才显示链接。
- 两个页面不合并数据口径：
  - Health 使用 persisted run / step log；
  - Eval 使用 validation artifact / baseline。

## 4. 完整页面映射

| 当前标签 | 新一级模块 | 新二级视图 | 优先级 |
|---|---|---|---|
| Overview | Overview | Overview | 入口 |
| Chat | Workbench | Chat | P0 |
| Plans | Workbench | Plans | P1 |
| Review | Workbench | Changes | P0/P1 |
| Memory | Workbench | Memory | P2 |
| Runs | Inspect | Run Summary | P1 |
| Context | Inspect | Context | P2 |
| Replay | Inspect | Replay | P2 |
| Trace | Inspect | Event Trace | P3 Expert |
| System | Control | Architecture | P2 |
| Agents | Control | Agents | P1 |
| Safety | Control | Safety | P1 |
| Health | Quality | Health | P1 |
| Eval | Quality | Evaluations | P2 |

优先级含义：

- `P0`：日常核心工作，不应被折叠或隐藏。
- `P1`：关键证据和决策页面，应在模块内直接可见。
- `P2`：高级分析或低频管理页面。
- `P3 Expert`：原始证据，仅在深入排查时使用。

## 5. 页面之间的真实关联

### 5.1 主数据流

```text
                           ┌──────────────┐
                           │ Architecture │
                           └──────┬───────┘
                                  │ 配置能力
Memory ──→ Plan ──→ Chat ──→ Run Summary ──→ Changes
                    │             │              ↑
                    │             ├──→ Context   │
                    │             ├──→ Replay    │
                    │             └──→ Trace     │
                    │
                    ├──→ Agents ──→ worktree ───┘
                    │       │
                    └──→ Safety

所有 persisted runs ──→ Health
validation artifacts ──→ Evaluations
所有模块摘要 ──────────→ Overview
```

### 5.2 关键关联规则

#### Chat → Inspect

- Chat 完成后显示 `Inspect run`。
- 导航时携带 session_id、run_id、turn_id。
- Inspect 默认定位到刚完成的 run，而不是最新列表中的任意项。

#### Chat / Agents → Changes

- workspace delta 存在时显示 `Review changes`。
- preserved worktree 显示 `Resolve in Changes`。
- 没有 diff 或 worktree 时不显示无效入口。

#### Run Summary → Context / Replay / Trace

- 三个链接使用同一个 run identity。
- 目标页必须显示来源 breadcrumb，支持回到 Run Summary。

#### Safety → Trace

- approval 记录携带 sequence 时，可定位到对应事件。
- 不能只跳到 Trace 首页后让用户重新搜索。

#### Health / Eval → Inspect

- 只有存在 session_id / run_id 关联时才提供深链。
- 聚合数据没有具体运行身份时，只显示筛选说明，不制造关联。

#### Memory → Context

- selected-session recall 可以跳到对应 context snapshot。
- Project memory 本身不应声称已经被某次模型请求使用。

## 6. 导航与布局规范

### 6.1 一级导航

顶部只显示：

```text
Overview | Workbench | Inspect | Control | Quality
```

规则：

- 一级导航稳定，不随 session 改变。
- 不使用更多下拉菜单隐藏旧标签。
- 支持键盘快捷键 `Alt+1` 到 `Alt+5`，但输入框聚焦时禁用。
- 小屏幕下可以变为五项图标导航，名称通过 tooltip 和 active title 保留。

### 6.2 二级导航

进入模块后，在页面标题下显示模块内 tab：

```text
Workbench  [Chat] [Plans] [Changes] [Memory]
Inspect    [Run Summary] [Context] [Replay] [Event Trace]
Control    [Architecture] [Agents] [Safety]
Quality    [Health] [Evaluations]
```

规则：

- 二级 tab 不与一级导航使用相同视觉重量。
- 默认页始终排第一。
- Advanced / Expert 页面可带安静的文字标签，不能使用警告色。
- 有数量意义时才显示 badge，例如 pending changes、failed runs。
- 不显示重复状态 badge，例如顶部已经显示 active session 时，二级导航不再重复。

### 6.3 标准页面头

所有二级视图统一使用：

```text
模块 / 视图                         Scope: Session abc123
页面标题                             [关联操作]
一句话说明
```

Scope 类型：

- `Project`
- `Session`
- `Run`
- `Hybrid`

如果页面需要 session 而当前未选择：

- 显示选择建议和最近 session；
- 不发起必然失败的 API；
- 不把“没有选择”显示成系统错误。

### 6.4 Sidebar 与 Event Rail

Session Sidebar 和 Session Tree 不是一级页面，而是全局 session navigator。

建议：

- Overview 可以默认折叠 Sidebar，保留展开入口。
- Workbench 和 Inspect 默认展开 Sidebar。
- Control / Agents 需要 session tree 时展开，其余 Control 页面可保持用户上次状态。
- Quality 默认折叠 Sidebar，因为它主要是项目作用域。
- Chat Event Rail 仅在 Workbench / Chat 出现。
- Event Trace 仍是 Inspect 内独立专家视图，不能与 Event Rail 合并。

## 7. 状态与路由模型

当前单一 `activeView` 应演进为：

```ts
interface NavigationState {
  module: "overview" | "workbench" | "inspect" | "control" | "quality";
  view: string;
  sessionId?: string;
  runId?: string;
  turnId?: string;
  sequence?: number;
}
```

建议 URL：

```text
/overview
/workbench/chat?session=s1
/workbench/plans
/workbench/changes?session=s1
/inspect/runs?session=s1&run=r1
/inspect/context?session=s1&run=r1
/inspect/replay?session=s1&run=r1
/inspect/trace?session=s1&sequence=42
/control/architecture
/control/agents?session=s1
/control/safety?session=s1
/quality/health?days=30
/quality/evaluations?evaluation=e1
```

如果暂时不引入路由库，至少使用 `history.pushState` 和统一 parser，不能继续只存在 React 内存中，否则刷新会丢失演示位置。

每个一级模块记住上次二级视图：

```text
Workbench → Chat
Inspect   → Replay
Control   → Agents
Quality   → Health
```

再次点击一级模块时回到该模块上次位置，而不是强制跳默认页。

## 8. 组件边界

建议新增：

```text
AppShell
├── PrimaryNavigation
├── SessionNavigator
├── ModuleHeader
├── SecondaryNavigation
├── WorkspaceOutlet
└── ContextualRail
```

旧页面组件继续作为 view component：

```text
WorkbenchModule
├── ChatView
├── PlanLibrary
├── DiffReviewView
└── MemoryView
```

禁止：

- 把四个 Inspect 页面复制进一个新大组件。
- 为新导航重写各页面 API。
- 同时 mount 所有 Dashboard 并让其后台请求。
- 因为分组而改变 SessionRuntime 或 WebSocket 协议。

需要特别保留：

- Chat 的流式状态和 active turn。
- Tool Approval 的同步交互。
- Session switch 的历史恢复。
- EventBus / WebSocket 每 session 隔离。

## 9. 空状态与重要性表达

### 核心页面

Chat、Changes：

- 使用明确主操作。
- 空状态直接告诉用户下一步能做什么。
- 不使用“Coming soon”。

### 证据页面

Runs、Agents、Safety、Health：

- 显示数据来源。
- 区分 configured、observed、legacy、unavailable。
- 缺少证据不是错误。

### 高级页面

Context、Replay、Architecture、Memory、Evaluations：

- 页面说明强调何时使用。
- 通过核心页面的深链进入时保留来源上下文。

### Expert 页面

Event Trace：

- 默认使用摘要筛选。
- 大量事件延迟加载。
- 原始 payload 按需展开。
- 不在一级导航显示。

## 10. 迁移计划

### 阶段 A：导航模型

目标：建立新的模块/view 映射，不改变页面组件。

修改：

- 新增 NavigationState 和映射表。
- 新增 PrimaryNavigation、SecondaryNavigation。
- 为 14 个旧 view 建立兼容映射。
- 添加刷新恢复和 invalid route fallback。

验收：

- 五个一级模块可切换。
- 所有旧页面仍可达。
- 刷新保持当前位置。

### 阶段 B：Workbench 与 Inspect

目标：先收敛最常用工作流和最混乱的证据页面。

修改：

- Workbench 接入 Chat / Plans / Changes / Memory。
- Inspect 接入 Runs / Context / Replay / Trace。
- 建立 run identity 深链。
- 仅 Chat 显示 Event Rail。

验收：

- Chat 行为、流式状态和审批不回归。
- 从完成 run 两次点击内可达全部执行证据。
- Trace 不再占据一级导航。

### 阶段 C：Control 与 Quality

目标：合并控制面和质量面。

修改：

- Control 接入 Architecture / Agents / Safety。
- Quality 接入 Health / Evaluations。
- 建立 Agents → Changes、Safety → Trace 的深链。
- 为 session-required 页面补选择建议。

验收：

- 配置事实与运行事实不混淆。
- Health 与 Eval 数据口径保持独立。
- Agents worktree 决策入口清晰。

### 阶段 D：视觉与可访问性收口

目标：统一层级，不重写业务页面。

修改：

- 统一 ModuleHeader、Scope badge、secondary tabs。
- 统一 empty / loading / partial / error 状态。
- 加入键盘和 focus-safe navigation。
- 小屏幕导航适配。
- 删除旧平级 tab 样式和死映射。

验收：

- 一级导航恒定为五项。
- 键盘导航不会劫持输入框。
- 所有模块可通过键盘访问。
- 页面标题和 scope 无歧义。

## 11. 测试计划

### 导航契约

- 14 个旧 view 均映射到正确 module/view。
- 无效 view 回退到模块默认页。
- 刷新恢复 module/view/session/run。
- 一级模块记住上次二级页。

### 工作流回归

- Chat streaming 在模块切换后不产生 zombie run。
- Tool Approval 在切换回来后仍可处理。
- Session switch 后 Inspect 使用新的 session。
- Overview recent session 能进入 Chat 和 Runs。
- Agents preserved worktree 能进入 Changes。

### 请求隔离

- 只加载当前二级视图的数据。
- 隐藏 Dashboard 不继续轮询。
- Event Trace 不在未打开时加载完整 trace。
- Quality 页面不依赖 active session 才能工作。

### 可访问性

- Primary 和 Secondary navigation 使用正确 tab/aria-current 语义。
- 键盘焦点顺序稳定。
- 输入框聚焦时快捷键禁用。
- 小屏幕仍能访问全部二级视图。

## 12. 最终建议

推荐采用五模块方案：

```text
Overview
Workbench  → Chat / Plans / Changes / Memory
Inspect    → Run Summary / Context / Replay / Event Trace
Control    → Architecture / Agents / Safety
Quality    → Health / Evaluations
```

不推荐以下方案：

1. 保留 14 个顶部标签，仅缩小字号：只缓解空间，不解决认知层级。
2. 把低频标签全部放进 `More`：只是隐藏，没有建立模块关系。
3. 把 Runs、Context、Replay、Trace 合成一个长页面：信息密度过高，加载成本和状态耦合都会恶化。
4. 单独增加 Knowledge 一级模块只容纳 Memory：一级模块仍然过多，且 Memory 与工作上下文的关系被削弱。
5. 删除 Trace、Replay 或 Eval：会损失项目最有面试价值的证据链。

最终产品叙事应当是：

> 在 Workbench 完成任务，在 Inspect 解释一次执行，在 Control 理解调度与权限，在 Quality 判断长期可靠性，再由 Overview 把所有证据串成完整故事。
