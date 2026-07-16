# Agent Responsibility Matrix

> **Core Philosophy**: LLM负责概率性的智能（理解、规划、推理、策略选择），Runtime负责确定性的控制（状态管理、权限、预算、熔断、防失控）。

## 职责矩阵

| 能力 | LLM 决定 | Runtime 强制执行 | 实现位置 | 说明 |
|------|:--------:|:----------------:|----------|------|
| **选择工具** | ✅ | — | LLM通过function calling选择 | Runtime通过`get_schemas()`过滤可用工具列表 |
| **执行工具** | — | ✅ | `ToolRegistry.execute_tool()` | 代码执行，LLM无法绕过 |
| **规划步骤** | ✅ | — | LLM推理 | LLM决定任务分解策略 |
| **限制步数** | — | ✅ | `ExecutionBudget` + `max_steps` | 达到上限时强制剥离工具 |
| **限制Token** | — | ✅ | `ExecutionBudget` | WARNING→CRITICAL→EXHAUSTED三级升级 |
| **限制时间** | — | ✅ | `CircuitBreaker.elapsed_seconds` | 超时强制终止 |
| **声明"完成"** | 提议 | ✅ | `CompletionGuard` | LLM调用FINISH只是提议，Runtime验证后才接受 |
| **任务状态转换** | — | ✅ | `TaskStateMachine` | PENDING→RUNNING→COMPLETING→COMPLETED，非法转换抛ValueError |
| **创建子Agent** | 提议(via task tool) | ✅ | `CircuitBreaker` + `CapabilityRegistry` | 限制数量、类型，循环检测后物理阻止 |
| **子Agent工具集** | — | ✅ | `AgentDefinition.tools` + `disallowed_tools` | 代码层过滤，子Agent看不到无权使用的工具 |
| **重试失败** | — | ✅ | LLM重试+指数退避（`_call_with_retry`）/ 工具级熔断（`CircuitBreaker`） | 代码决定重试策略 |
| **循环检测** | — | ✅ | `_is_looping()` + `MacroLoopDetector` | 检测到循环立即终止，不注入提示 |
| **熔断** | — | ✅ | `CircuitBreaker` | CLOSED→HALF_OPEN→OPEN，每一步检查 |
| **工具可用性** | — | ✅ | `CapabilityRegistry` | ACTIVE→CIRCUIT_OPEN→HALF_OPEN→UNAVAILABLE |
| **权限检查** | — | ✅ | `PermissionPipeline` (5层) | 安全黑名单不可覆盖 |
| **上下文窗口** | — | ✅ | `ContextManager` + Compaction | Token预算约束，自动压缩 |
| **记忆分类** | ✅ (内容提取) | ✅ (scope/TTL管理) | `memory/store.py` + `extractor.py` | LLM提取内容，Runtime管理生命周期 |
| **记忆注入策略** | — | ✅ | `MemoryContext` | user/feedback always-inject; project/reference on-demand |
| **总结结果** | ✅ | — | LLM生成summary | 模型负责表达 |
| **保存状态** | — | ✅ | `SessionStore` + `TaskLedger` | 持久化到SQLite，任务幂等 |
| **分析阶段控制** | — | ✅ | `AnalysisPhaseState` | plan_reads→discover→inspect→synthesize→verify→answer |
| **文件读写门控** | — | ✅ | `ReadPlan` + `PhasePolicy` | 计划阶段阻止源文件读取，执行阶段限制写入范围 |
| **证据追踪** | — | ✅ | `EvidenceLedger` | 代码记录证据ID，模型必须引用[ev_xxx] |
| **结构化输出验证** | — | ✅ | `SubmitFindingsTool` | JSON Schema验证子Agent输出 |

## 关键设计原则

### 1. 模型不能单方面声明"完成"
LLM调用FINISH只是一个**提议**。Runtime通过`CompletionGuard`验证：
- 是否读了必要的文件？
- 是否做了必要的修改？
- 是否满足了CompletionPolicy要求？

只有所有条件满足，Runtime才将状态转为COMPLETED。

### 2. 模型不能绕过工具限制
即使LLM"认为"它需要某个工具，如果该工具：
- 不在Agent定义的允许列表中
- 被CapabilityRegistry标记为不可用
- 被PermissionPipeline拒绝

则Runtime会阻止执行，模型只能看到错误反馈。

### 3. 循环检测是代码层行为，不是提示
检测到循环时，Runtime**立即终止**Agent，不注入"请不要再循环了"的提示。提示意味着多消耗一轮token，且模型可能忽略。

### 4. 预算耗尽时剥离工具
达到EXHAUSTED级别时，Runtime**移除所有工具schema**，只允许模型输出一段纯文本总结。模型无法调用任何工具。

### 5. 子Agent是受控的隔离执行单元
子Agent不是"另一个完整Agent"，而是：
- 隔离上下文（不继承父对话历史）
- 受限工具集（由AgentDefinition定义）
- 独立熔断器（父熔断器不受影响）
- 共享Token预算（子Agent消耗计入父Agent）
- 可被物理阻止（循环检测后CapabilityRegistry移除该类型）

## 反模式（应该避免）

| 反模式 | 为什么错误 | 正确做法 |
|--------|-----------|----------|
| 在提示中写"请不要重复调用同一个工具" | 模型可能忽略提示 | 代码层循环检测+终止 |
| 让模型自己决定是否停止 | 模型倾向于继续探索 | CompletionGuard验证后才能停止 |
| 信任模型的"我已完成"声明 | 模型可能过早声明完成 | Runtime验证所有完成条件 |
| 让模型管理自己的预算 | 模型不理解token概念 | ExecutionBudget代码层强制执行 |
| 用提示限制子Agent行为 | 提示不可靠 | AgentDefinition工具限制+熔断器 |
| 在提示中处理工具不可用 | 模型会反复尝试 | CapabilityRegistry物理移除工具 |
