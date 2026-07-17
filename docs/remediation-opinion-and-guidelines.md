# 架构整改意见与分批执行计划（施工版）

最后更新：2026-07-17  
适用仓库：`forge-agent` 当前工作区  
本版特点：基于官方 Claude Code 文档重新对齐，并把计划展开到“函数/文件级修改方案”

## 0. 本次复核结论

先给结论，避免我们又落回“只讲流程、不讲落点”的状态。

当前仓库最值得优先处理的，不再是 `agent.v2` 本身，而是下面 5 条主线：

1. 主入口仍残留 `agent.v2` 兼容引用，需要彻底切到 `agent.session`
2. Skill `allowed-tools` 语义仍然写偏了：现在是“过滤可见工具”，而不是“本轮免确认授权”
3. Plan mode 虽然已经接进主循环，但 `entry/modes/v2_runner.py` 里仍保留独立 orchestration
4. Subagent 过于依赖 `_SUBAGENT_PROTOCOL` 超长提示词，运行时契约还没有真正下沉
5. MCP / Shell / 内部进程调用 仍有 bridge / legacy / 双语义问题，尚未完全收口

`agent.v2` 的状态我重新检查过：

- `agent/v2/` 目录现在只剩 [`agent/v2/__init__.py`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/v2/__init__.py:1>)
- 当前 `agent.v2` 引用只剩 14 处，已经从“多 shim 并存”收束成“单兼容入口 + 少量遗留引用”

因此：

- `Point 2` 可以从“主问题”降为“收尾项”
- 后续第一优先级应该转到主入口迁移、Skill/Plan/Subagent/MCP 主线纠偏

---

## 1. 本计划采用的 Claude Code 官方基线

下面这些结论，不是我自己拍脑袋设计的，都是按 Claude Code 官方文档抽出来的约束。

### 1.1 Agent loop 应该是干净主循环，外围机制下沉

来源：

- Claude Code Agent SDK / Agent loop  
  https://code.claude.com/docs/en/agent-sdk/agent-loop

对我们代码的含义：

- 主循环应尽量只做：取消息、拿工具 schema、执行 tool use、写回结果、继续或结束
- Plan / skill modifier / reflection / completion / child continuation 这些都应是外围组件，不应该继续堆进单个大文件

### 1.2 Plan mode 的正确语义是“模式切换”，不是第二套工作流

来源：

- Claude Code Permission modes  
  https://code.claude.com/docs/en/permission-modes

对我们代码的含义：

- `plan` 首先是权限模式，不是另一套 runner 体系
- 用户批准计划后，正确方向是退出 plan mode 并进入后续 mode
- 允许 UI 层存在 approval adapter，但不应该长期维持第二套“plan 专用编排语义”

### 1.3 Subagents 是原生能力；fresh context 与 fork context 要分清

来源：

- Claude Code Sub-agents  
  https://code.claude.com/docs/en/sub-agents

对我们代码的含义：

- 命名 subagent 与 fork subagent 的上下文来源不同
- 子代理可以继续调用自己的工具并继续派生子代理
- 约束应主要由 runtime / tool schema / permission / context 决定，而不是压在一个超长 prompt 上

### 1.4 Skills 的 `allowed-tools` 是“预授权”，不是“可见工具白名单”

来源：

- Claude Code Skills  
  https://code.claude.com/docs/en/skills

对我们代码的含义：

- skill 生效后，`allowed-tools` 的正确含义是：这些工具在当前 skill 作用窗口内无需再询问
- 它不是缩小工具显示集合
- `disallowed-tools` 才更接近“从当前作用窗口里移除”

### 1.5 MCP 是一等公民，且支持 deferred / on-demand 加载

来源：

- Claude Code MCP  
  https://code.claude.com/docs/en/mcp

对我们代码的含义：

- MCP 不应该长期停留在 “runtime tool → legacy proxy → 再注册” 的桥接态
- deferred schema、tool discovery、resources/roots 协议能力，应该让核心 registry 理解
- bridge 可以保留一段时间，但不能成为长期事实源

---

## 2. 当前代码与官方基线的主要差距

### 2.1 `agent.v2` 已收束，但主入口还没完全转正

定位：

- [`entry/cli.py:513`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/cli.py:513>)
- [`entry/cli.py:564`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/cli.py:564>)
- [`entry/worktree_admin.py:18`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/worktree_admin.py:18>)
- [`entry/modes/v2_runner.py:142`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/modes/v2_runner.py:142>)
- [`entry/modes/v2_runner.py:221`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/modes/v2_runner.py:221>)

判断：

- 实现层已经基本迁到 `agent.session`
- 但入口层还在消费 `agent.v2`
- 这会让兼容层长期留在主路径上，继续污染架构心智

### 2.2 Skill modifier 的“声明式接口”有了，但“运行语义”还没对齐

定位：

- [`skills/tool.py:28`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/skills/tool.py:28>)
- [`skills/tool.py:128`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/skills/tool.py:128>)
- [`core/policy_registry.py:67`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy_registry.py:67>)
- [`core/policy_registry.py:192`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy_registry.py:192>)
- [`core/policy_registry.py:210`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy_registry.py:210>)
- [`core/policy.py:299`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy.py:299>)

判断：

- 好消息：你们已经把 skill modifier 做成 typed dataclass 了
- 真实问题：`_apply_skill_modifier()` 里，`allowed_tools` 现在走的是 `with_allowed_tools(...)`
- 这会把“免审批”做成“过滤工具可见性”，语义错位

### 2.3 Plan mode 已接入主循环，但 runner 仍有第二套 orchestration

定位：

- [`tools/plan_mode_tool.py:22`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/plan_mode_tool.py:22>)
- [`agent/core.py:1218`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/core.py:1218>)
- [`agent/core.py:1608`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/core.py:1608>)
- [`entry/modes/v2_runner.py:390`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/modes/v2_runner.py:390>)
- [`entry/modes/v2_runner.py:457`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/modes/v2_runner.py:457>)
- [`entry/modes/v2_runner.py:496`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/modes/v2_runner.py:496>)

判断：

- 模式切换语义已经接到 agent 主循环，这是正确方向
- 但 `run_v2_mode()` 仍然自己管理 plan 保存、审批、replan、递归 build
- 所以 Point 4 / 5 不能写成“已完成”，更准确是“权限模式层已接通，编排层未收口”

### 2.4 Subagent 仍然被超长 prompt 强耦合

定位：

- [`agent/session/task_tool.py:52`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/task_tool.py:52>)
- [`agent/session/task_tool.py:121`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/task_tool.py:121>)
- [`agent/session/task_tool.py:227`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/task_tool.py:227>)
- [`agent/session/task_tool.py:267`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/task_tool.py:267>)
- [`agent/session/task_tool.py:666`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/task_tool.py:666>)
- [`agent/session/subagent.py:147`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/subagent.py:147>)

判断：

- 你们已经有一些正确方向：比如 typed result、runtime 约束、dynamic available subagent list
- 但 `_SUBAGENT_PROTOCOL` 仍然承载了过多的执行纪律、验证规范、输出要求
- 这在 Claude Code 思路里属于“提示层过重，运行时契约偏弱”

### 2.5 MCP 还是 bridge-first，不是 registry-first

定位：

- [`agent/session/mcp_integration.py:17`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/mcp_integration.py:17>)
- [`agent/session/mcp_integration.py:93`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/mcp_integration.py:93>)
- [`agent/session/mcp_integration.py:163`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/mcp_integration.py:163>)
- [`executor/mcp/registry.py:46`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/executor/mcp/registry.py:46>)
- [`executor/mcp/registry.py:58`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/executor/mcp/registry.py:58>)
- [`core/policy_registry.py:171`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy_registry.py:171>)

判断：

- 现在 MCP 先被包装成 `MCPRuntimeToolProxy(BaseTool)`，再喂给现有 registry
- 这对过渡期是有价值的
- 但 deferred schema 仍停在 executor 边缘，没有成为核心 registry 的一等能力

### 2.6 Shell 与内部进程执行还有双语义

定位：

- [`tools/shell_tool.py:86`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/shell_tool.py:86>)
- [`tools/shell_tool.py:165`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/shell_tool.py:165>)
- [`tools/shell_tool.py:192`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/shell_tool.py:192>)
- [`tools/shell_tool.py:230`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/shell_tool.py:230>)
- [`executor/project_environment.py:132`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/executor/project_environment.py:132>)
- [`executor/workspace_facts.py:144`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/executor/workspace_facts.py:144>)

判断：

- `command + args` 已经是主方向
- 但 `cmd` 兼容路径仍然保留在主 schema
- 内部事实采集仍散落 `subprocess.run(...)`
- 这让“用户态执行”和“内部探测”的底层保证还没有统一

---

## 3. 更新后的详细执行计划

下面这部分才是施工图。每一批都写目标、涉及代码、修改方案、风险边界、验收方式。

## Batch A：主入口彻底切到 `agent.session`

优先级：P0  
文件数：4~6  
目标：让 `agent.session` 成为唯一主实现入口，`agent.v2` 只留作兼容层

### A.1 涉及代码

- [`entry/cli.py:513`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/cli.py:513>)
- [`entry/cli.py:564`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/cli.py:564>)
- [`entry/worktree_admin.py:18`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/worktree_admin.py:18>)
- [`entry/modes/v2_runner.py:142`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/modes/v2_runner.py:142>)
- [`entry/modes/v2_runner.py:221`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/modes/v2_runner.py:221>)
- 相关测试：
  - [`tests/test_chat.py:55`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tests/test_chat.py:55>)
  - [`tests/test_v2_e2e_behavioral.py:34`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tests/test_v2_e2e_behavioral.py:34>)
  - [`tests/test_v2_runtime.py:33`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tests/test_v2_runtime.py:33>)

### A.2 修改方案

1. 把所有主入口 import 从 `agent.v2` 改成 `agent.session`
2. `agent/v2/__init__.py` 保留，但仅作为兼容导出层，不再被 entry 层直接使用
3. 测试分层：
   - 行为测试：改用 `agent.session`
   - 兼容测试：保留少量 `agent.v2` 断言

### A.3 非目标

- 本批不删除 `agent/v2/__init__.py`
- 本批不顺手改 Plan/Skill/Subagent 行为

### A.4 风险

- 低风险，主要是 import 迁移与测试断言调整
- 真正要小心的是别把“兼容层仍需要的测试”一起误删

### A.5 验收

- `entry/` 范围不再直接 import `agent.v2`
- `agent.v2` 仅出现在兼容性测试与兼容层自身

### A.6 来源

- Claude Code Overview / Sub-agents / Agent loop  
  https://code.claude.com/docs/en/overview  
  https://code.claude.com/docs/en/sub-agents  
  https://code.claude.com/docs/en/agent-sdk/agent-loop

---

## Batch B：修正 Skill modifier 语义

优先级：P0  
文件数：4~6  
目标：让 `allowed-tools` 真正变成“当前 skill 作用窗口下的预授权”

### B.1 涉及代码

- [`skills/tool.py:28`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/skills/tool.py:28>)
- [`skills/tool.py:128`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/skills/tool.py:128>)
- [`core/policy_registry.py:67`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy_registry.py:67>)
- [`core/policy_registry.py:192`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy_registry.py:192>)
- [`core/policy_registry.py:210`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy_registry.py:210>)
- [`core/policy.py:284`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy.py:284>)
- [`core/policy.py:299`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy.py:299>)
- [`skills/registry.py:286`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/skills/registry.py:286>)

### B.2 当前问题

`with_skill_restrictions()` 已经是对的：

- `allowed_tools -> with_pre_approved_tools(...)`
- `disallowed_tools -> denied_tools`

但 `_apply_skill_modifier()` 仍然是错的：

- `allowed_tools -> with_allowed_tools(...)`

也就是说，同一个系统里现在同时存在两套 skill 语义。

### B.3 修改方案

1. 在 [`core/policy_registry.py:198`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy_registry.py:198>) 附近，把 `_apply_skill_modifier()` 改成：
   - `allowed_tools` 走 `with_pre_approved_tools(...)`
   - `disallowed_tools` 走 `with_denied_tools(...)`
2. 明确 skill modifier 的生命周期：
   - 生效范围：当前 turn / 当前 skill 激活窗口
   - 清除时机：下一轮常规用户输入，或 skill 作用域结束
3. 如果当前 registry 结构不方便表达“临时 grant”，增加一个小型 typed scope 对象，不要再用字符串/隐式约定
4. 为 `with_skill_restrictions()` 与 `_apply_skill_modifier()` 建立同一套测试契约，禁止两处语义分叉

### B.4 非目标

- 不扩大 skill 功能
- 不在这批里重构 skill registry 的所有解析逻辑

### B.5 验收

- `allowed-tools` 不再缩小工具可见集
- `disallowed-tools` 仍然能按预期移除工具
- 同一 skill 无论是“静态 frontmatter 限制”还是“tool result 注入 modifier”，语义一致

### B.6 来源

- Claude Code Skills  
  https://code.claude.com/docs/en/skills

---

## Batch C：Plan mode 收口到“模式 + 审批适配”，缩减独立 orchestration

优先级：P1  
文件数：4~7  
目标：保留现有可用性，但开始减少 `v2_runner.py` 里的第二套 plan 工作流

### C.1 涉及代码

- [`tools/plan_mode_tool.py:22`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/plan_mode_tool.py:22>)
- [`agent/core.py:1218`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/core.py:1218>)
- [`agent/core.py:1608`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/core.py:1608>)
- [`entry/modes/v2_runner.py:197`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/modes/v2_runner.py:197>)
- [`entry/modes/v2_runner.py:390`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/modes/v2_runner.py:390>)
- [`entry/modes/v2_runner.py:457`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/modes/v2_runner.py:457>)
- [`entry/modes/v2_runner.py:496`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/modes/v2_runner.py:496>)

### C.2 当前问题

当前已经有一半是对的：

- tool 可以设置 `_pending_mode_switch`
- 主循环可以消费这个模式切换

但仍有一半不对：

- plan 保存逻辑、审批循环、replan、递归 build，仍集中在 `run_v2_mode()`

### C.3 修改方案

1. 先把 `run_v2_mode()` 中与 Plan 相关的逻辑拆出为两个层：
   - `PlanReviewAdapter`：纯 UI / 文件保存 / 用户交互
   - `PlanExecutionCoordinator`：纯状态推进
2. 保持现有行为不变，但把“Plan mode 状态”和“Plan approval 编排”从一个函数中拆开
3. 在第二阶段再把 `PlanAction.TRIGGER_BUILD` 的递归 `run_v2_mode(agent_name="build", ...)` 改造成更接近“同 session mode transition”的形态
4. 计划文件命名规则保留 hash 稳定命名，但要在文档里明确：这是状态产物，不是模式事实源

### C.4 非目标

- 本批不要求一步到位消灭 `plan-action`
- 本批不强行重写为单 session 全闭环

### C.5 验收

- `run_v2_mode()` 长度明显下降
- Plan 的保存、展示、审批、replan 不再全部混在一个函数里
- 后续若要切到更接近 Claude Code 的 session-mode transition，不需要再推倒重来

### C.6 来源

- Claude Code Permission modes  
  https://code.claude.com/docs/en/permission-modes

---

## Batch D：Subagent 契约下沉，削弱 `_SUBAGENT_PROTOCOL`

优先级：P1  
文件数：5~8  
目标：把“稳定性”从超长 prompt 转移到 typed contract + runtime enforcement

### D.1 涉及代码

- [`agent/session/task_tool.py:52`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/task_tool.py:52>)
- [`agent/session/task_tool.py:138`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/task_tool.py:138>)
- [`agent/session/task_tool.py:227`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/task_tool.py:227>)
- [`agent/session/task_tool.py:267`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/task_tool.py:267>)
- [`agent/session/task_tool.py:666`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/task_tool.py:666>)
- [`agent/session/subagent.py:147`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/subagent.py:147>)
- [`agent/session/agent_factory.py:51`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/agent_factory.py:51>)

### D.2 当前问题

现在的 prompt 里同时承担了：

- 证据要求
- 输出格式
- 不得偷懒
- 何时视为 verified / unverified
- 必须调用 `submit_findings`

这会导致两个问题：

1. 协议一长，模型偏移风险就上升
2. 真正的可靠性约束没有沉到 runtime / tool result / contract

### D.3 修改方案

1. 保留 `_SUBAGENT_PROTOCOL`，但只保留最小角色说明：
   - 子代理身份
   - fresh context vs fork context 提醒
   - 高层交付目标
2. 把下列内容从 prompt 下沉到运行时：
   - “必须提交结构化发现” → typed tool contract
   - “confirmed vs unverified” → schema / validator
   - “至少交叉读取依赖和调用者” → findings validator 或 review gate
3. 将 `submit_findings` 视为真正的 completion boundary，而不是附属建议
4. 若需要保留“反偷懒”规则，改为更短的 invariant 列表，不继续扩张成 200+ 行协议

### D.4 非目标

- 本批不改变子代理能力范围
- 本批不改多代理调度模型

### D.5 验收

- `_SUBAGENT_PROTOCOL` 明显缩短
- 结构化输出质量不下降
- 子代理正确性更多来自 runtime contract，而不是 prompt 长度

### D.6 来源

- Claude Code Sub-agents  
  https://code.claude.com/docs/en/sub-agents

---

## Batch E：拆 `agent/core.py`，恢复干净 loop 骨架

优先级：P1  
文件数：1 个大文件 + 3~6 个 supporting module  
目标：把 agent 主循环从“大一统”恢复成“骨架 + 外围组件”

### E.1 涉及代码

- [`agent/core.py:177`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/core.py:177>)
- [`agent/core.py:1215`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/core.py:1215>)
- [`agent/core.py:1608`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/core.py:1608>)

### E.2 修改方案

建议拆成 4 类支持模块：

1. `loop_driver`
   - 只保留 LLM 调用、tool execution、history append、termination
2. `mode_switching`
   - `_pending_mode_switch`、plan/build mode 过渡
3. `completion_and_reflection`
   - completion policy、review / reflection、child continuation
4. `history_materialization`
   - runtime messages、tool result materialization、history compaction 辅助

### E.3 顺序

第一轮只“搬函数”，不改行为。  
第二轮才清理 legacy 分支。

### E.4 验收

- `agent/core.py` 显著瘦身
- 主 loop 更接近 Claude Code 官方 loop 心智

### E.5 来源

- Claude Code Agent loop  
  https://code.claude.com/docs/en/agent-sdk/agent-loop

---

## Batch F：统一 Shell 与内部进程调用适配层

优先级：P1  
文件数：4~6  
目标：统一编码、cwd 校验、timeout、错误归类，同时继续保留项目级隔离

### F.1 涉及代码

- [`tools/shell_tool.py:86`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/shell_tool.py:86>)
- [`tools/shell_tool.py:165`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/shell_tool.py:165>)
- [`tools/shell_tool.py:192`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/shell_tool.py:192>)
- [`tools/shell_tool.py:230`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/shell_tool.py:230>)
- [`executor/process.py`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/executor/process.py>)
- [`executor/project_environment.py:132`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/executor/project_environment.py:132>)
- [`executor/workspace_facts.py:144`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/executor/workspace_facts.py:144>)

### F.2 修改方案

1. 抽一个共享的内部 `ProcessInvoker`
   - 统一 UTF-8/bytes decode
   - 统一 timeout
   - 统一 cwd must stay within project root / state root 的校验
   - 统一错误分类
2. `ShellTool` 继续保留 `cmd`，但：
   - 标记为仅兼容路径
   - 内部逐步迁空
3. `_BLOCKED_PATTERNS` 保留为 L0，但明确冻结边界：
   - 只保留绝对不可协商硬阻断
   - 不再继续把更多主权限语义堆进去

### F.3 非目标

- 本批不删除所有 `subprocess.run(...)`
- 本批不重写 Runtime 整体

### F.4 验收

- 用户态 shell 与内部探测共享底层保证
- 项目级隔离不退化
- `cmd` 不再是推荐路径

### F.5 来源

- Claude Code Permission modes  
  https://code.claude.com/docs/en/permission-modes

---

## Batch G：MCP 从 bridge-first 过渡到 registry-first

优先级：P2  
文件数：3~6  
目标：让 deferred schema / metadata 进入核心 registry，而不是停在 executor 边缘

### G.1 涉及代码

- [`agent/session/mcp_integration.py:17`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/mcp_integration.py:17>)
- [`agent/session/mcp_integration.py:93`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/mcp_integration.py:93>)
- [`agent/session/mcp_integration.py:163`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/session/mcp_integration.py:163>)
- [`executor/mcp/registry.py:46`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/executor/mcp/registry.py:46>)
- [`executor/mcp/registry.py:58`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/executor/mcp/registry.py:58>)
- [`core/policy_registry.py:171`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/core/policy_registry.py:171>)
- [`tools/workflow_tool.py:83`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/workflow_tool.py:83>)

### G.2 修改方案

1. `get_schemas()` 层面显式支持：
   - built-in schema
   - loaded MCP schema
   - deferred MCP schema
2. 将 `defer_loading` 的判定逻辑从 executor helper 提升到 registry 可理解的能力
3. 保留 `MCPRuntimeToolProxy` 作为短期桥接层，但把它降级为适配细节，不再作为 MCP 架构中心
4. 后续如果要补 `roots/list`、`tools/list_changed` 等协议行为，也应该挂在 runtime / registry 层，而不是塞进 proxy

### G.3 非目标

- 本批不要求彻底删 bridge
- 本批不要求一次实现全部 MCP 高级协议

### G.4 验收

- registry 理解 deferred schema
- MCP 不再主要依赖“包装成 legacy BaseTool”才能存在

### G.5 来源

- Claude Code MCP  
  https://code.claude.com/docs/en/mcp

---

## Batch H：legacy/fallback inventory + examples 清理

优先级：P2  
文件数：4~8  
目标：把历史残留从“默认永久保留”改成“可审计、可删除”

### H.1 涉及代码

- [`entry/chat.py`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/entry/chat.py>)
- [`agent/core.py`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/agent/core.py>)
- [`tools/shell_tool.py`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/tools/shell_tool.py>)
- [`executor/examples.py`](</abs/path/D:/StudyProjects/ProjectBench/forge-agent/executor/examples.py>)

### H.2 修改方案

给每个 fallback 建一张最小卡片：

- 它解决什么历史兼容问题
- 现在谁还在调用
- 何时可以删

没有真实消费者的，优先删。  
仍有兼容价值的，明确边界，不再自然扩张。

### H.3 来源

- Claude Code 整体产品心智更接近“兼容层可存在，但不应反过来主导架构”
- 参考：
  - https://code.claude.com/docs/en/overview
  - https://code.claude.com/docs/en/agent-sdk/agent-loop

---

## 4. 推荐执行顺序

按“先收主入口、再修语义、再减重、最后清边角”的顺序，我建议：

1. Batch A：主入口迁移到 `agent.session`
2. Batch B：Skill modifier 语义纠偏
3. Batch C：Plan orchestration 收口
4. Batch D：Subagent contract 下沉
5. Batch E：`agent/core.py` 拆职责
6. Batch F：Shell + Process adapter 统一
7. Batch G：MCP registry-first
8. Batch H：legacy/fallback + examples 清理

---

## 5. 当前最适合立即开工的批次

如果我们要避免“改了一处，后面别处又炸”，那最合理的起手顺序是：

- 先做 Batch A
- 再做 Batch B
- 然后做 Batch C

原因很简单：

- Batch A 先把 canonical namespace 定住
- Batch B 修正 skill 权限语义这个硬偏差
- Batch C 再处理 plan 这条用户最敏感、最容易暴露问题的主链

这三批做完，再进 Subagent / Core / MCP，会稳很多。

---

## 6. 备注

PowerShell 控制台里如果仍看到中文乱码，那是终端显示编码问题；这份文件本身已按 UTF-8 重写。
