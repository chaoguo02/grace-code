# Multi-Agent 统一资源治理改造文档计划

> **SSOT 设计文档**: [`MULTI_AGENT_RESOURCE_GOVERNANCE_REFACTOR_PLAN.md`](./MULTI_AGENT_RESOURCE_GOVERNANCE_REFACTOR_PLAN.md) — 目标架构、接口契约、状态机、持久化、测试方案  
> **关联文档**: [`MULTI_AGENT_MODE_IMPLEMENTATION_PLAN.md`](./MULTI_AGENT_MODE_IMPLEMENTATION_PLAN.md) — Multi-Agent 模式实现计划

## 摘要

主设计文档已创建：

[`docs/MULTI_AGENT_RESOURCE_GOVERNANCE_REFACTOR_PLAN.md`](./MULTI_AGENT_RESOURCE_GOVERNANCE_REFACTOR_PLAN.md)

该文档作为资源治理改造的 SSOT，包含完整的现状审计、目标架构、接口契约、状态机、持久化设计、分阶段路线和测试方案。文档覆盖现状证据、完整根因、目标架构、接口契约、配置迁移、状态机、持久化、前端、测试、发布和回滚。

采用“接口全服务化、首期只接入 Multi-Agent”的策略：

1. 首期统一治理所有 Multi-Agent worker。
2. 随后治理 LLM、工具、事件、数据库和工作树。
3. 最后接入普通根 Agent 与 Agent Team。
4. 按 observe → soft-enforce → enforce 分阶段上线；完成迁移后删除旧容量兼容入口。

资源不足时不改变任务拓扑：进入有界公平队列，不自动降级。队列全局最多 64 项，单项最多等待 120 秒，超时返回 `capacity_timeout`。

## 文档内容与目标架构

### 1. 现状审计与根因树

文档记录并区分已确认事实、历史运行证据和待压测验证项，完整覆盖：

- 子代理预算非原子分配，可能并发超卖。
- YAML、环境变量、运行时默认值多源分叉。
- 每 wave、每 agent、每 LLM、每工具池的线程乘法。
- 超时后 daemon thread 和底层连接无法保证退出。
- 取消只传播信号，不能终止阻塞 provider/tool。
- 子代理重复加载系统提示、工具 schema 和项目上下文。
- 子代理禁用 history compaction。
- provider 侧缺少共享 RPM、TPM、并发和 429 协调。
- EventBus 使用无界队列，高频 delta 缺少合并和背压。
- SQLite 并发写、trace/event 放大和 WAL 压力。
- modified/unknown worktree 长期保留，缺少容量预检和 orphan 清理。
- `RESOURCE_EXHAUSTED` 混合逻辑预算、主机资源和 provider 容量。
- 关闭流程只清理引用，没有完整 stop/cancel/drain/close/join。
- 测试只覆盖功能边界，没有覆盖资源稳定性。

附录提供完整影响面索引，按调度、LLM、工具、事件、存储、工作树、配置、API、UI、可观测性和测试分组列出相关模块。

### 2. 统一资源治理核心

新增服务级 `ResourceGovernor`，设计为全服务接口，但首期只由 Multi-Agent 使用。

核心类型固定为：

- `ResourceKind`：`worker_slot`、`llm_slot`、`provider_rpm`、`provider_tpm`、`tool_slot`、`token_budget`、`event_capacity`、`worktree_slot`、`disk_bytes`、`db_write_capacity`。
- `ResourceRequest`：请求所属 root/session/run/task、资源向量、优先级、排队截止时间和取消令牌。
- `ResourceLease`：不可重复释放的租约，支持实际用量结算、部分退款和上下文管理。
- `AdmissionResult`：`granted`、`queued`、`cancelled`、`capacity_timeout`、`impossible`、`shutdown`。
- `ResourceSnapshot`：容量、已用、已预留、排队数、等待时间、压力等级和拒绝原因。
- `ResourcePressure`：`normal`、`warning`、`critical`、`draining`。

统一不变量：

- 所有资源先申请、后执行、finally 释放。
- token 启动前原子预留，完成后按 provider 实际 usage 结算并退款。
- 超出预留必须二次申请，不允许出现负余额。
- `available + reserved + consumed = configured_limit`。
- 取消排队请求必须立即从队列移除。
- 租约释放必须幂等。
- shutdown 后拒绝新申请，并等待现有租约排空。
- 队列采用跨 root FIFO，并限制单 root 连续获批，防止单会话垄断。

首期默认值：

- Multi-Agent 全服务并发 worker：2。
- 单 root 并发 worker：2。
- 等待队列：64。
- 等待超时：120 秒。
- 原有 task 总数和 spawn 深度限制继续作为安全上限，不作为容量治理器。
- 不因拥塞修改用户已生成的 DAG；超时后任务进入明确的容量失败状态。

### 3. 分阶段实施方案

#### Phase 0：配置统一与观测模式

- 在统一配置 schema 中新增 `resource_governance`，包含 mode、worker、provider、queue、event、worktree、disk 和 shutdown 配置。
- 生产容量配置由 `resource_governance` 单路提供；旧 `GRACE_*` 容量映射已删除。
- spawn 深度和单 session 终身数量属于安全边界，由统一 helper 读取，不覆盖 governor 容量。
- `observe` 模式只计算申请、排队和超卖事实，不阻止运行。
- 增加资源快照和指标，不修改现有 API 行为。

#### Phase 1：Multi-Agent 强制接入

- AgentBatch、durable resume scheduler 和 runtime spawn 共用同一个 governor。
- 删除各模块独立判断容量的职责；旧检查仅保留 DAG、权限、深度和硬安全边界。
- worker 启动前同时申请 worker slot 和 token reservation。
- 重试任务必须重新排队和申请资源，不能继承已释放租约。
- token 估算包含系统提示、工具 schema、任务提示、历史、输出预留和恢复预留。
- 若估算成本超过整个可用预算，直接返回 `impossible`，不进入队列。
- delegation run/task 持久化排队开始时间、等待时长、申请资源、获批资源、实际消费和终止原因。
- 重启时不恢复旧进程租约；所有 running/queued 记录先转为 recovery 状态，再重新申请。

#### Phase 2：可终止生命周期和共享执行器

- 替换“超时后遗弃 daemon thread”的调用方式，provider stream 必须暴露 `close/cancel`。
- OpenAI 流式链路只保留一层受控 producer，不再嵌套创建不可管理线程。
- speculative tool executor 改为服务级共享有界池，增加 `shutdown(cancel_futures=True)`。
- AgentBatch 不再按 wave 创建临时线程池，改为提交给共享调度器。
- 统一关闭顺序：停止 admission → 取消排队 → 取消运行 → 关闭流和工具 → 等待 drain → 关闭 executor/MCP/DB。
- 对无法合作取消的 subprocess 执行进程树终止；对无法关闭的第三方 SDK 请求记录 `leaked_operation` 并触发 provider 隔离。

#### Phase 3：Provider、事件、数据库和工作树治理

- 按 provider/model 建立共享 LLM semaphore、RPM 滑动窗口和 TPM token bucket。
- 429 遵循 `Retry-After`，使用共享退避和熔断，禁止各 agent 独立形成重试风暴。
- EventBus 改为有界队列；text/thought delta 合并，控制事件和终态事件不可丢弃。
- 数据库高频事件进入单写者有界队列，按时间或数量批量提交；session/delegation 状态更新仍保持事务性强一致。
- worktree 创建前检查全局/单 root 配额、预计 checkout 大小和剩余磁盘。
- modified/preserved worktree 永不自动删除；容量不足时阻止新 writer 并要求用户处理。
- 启动和定时 janitor 只清理已确认 clean 的 orphan worktree，并执行 Git prune。
- 子代理开启 context compaction，并使用任务相关 repo/context 切片，避免复制完整上下文。

#### Phase 4：扩展到全服务

- 普通根 Agent、Agent Team、后台恢复、review worker 和 MCP 工具全部接入同一 governor。
- 去除旧的 per-session 独立并发实现和重复配置入口。
- 根据 observe 数据确定最终全服务容量，不直接沿用首期 Multi-Agent 默认值。
- 已移除旧变量到 governor 容量的兼容覆盖。

### 4. API、事件、持久化与 UI 契约

- Multi-Agent snapshot 和 run detail 增加 `resource` 字段，返回状态、排队位置、等待时长、预留/实际 token、压力等级和原因码。
- 新增事件：
  - `delegation_resource_queued`
  - `delegation_resource_granted`
  - `delegation_resource_reconciled`
  - `delegation_resource_released`
  - `delegation_resource_timeout`
  - `resource_pressure_changed`
- 事件必须携带稳定的 session/run/task 身份和单调序列号，支持重放去重。
- delegation 表采用可向后兼容的附加 JSON 字段保存 admission/resource 快照；旧记录读取为空对象，不批量改写历史数据。
- UI 卡片显示：
  - Waiting for capacity / Running / Capacity timeout
  - 排队位置和已等待时间
  - 并发槽和 token 预留摘要
  - 明确原因及重试入口
- API 旧字段保持不变；新字段均为附加字段，旧前端可以忽略。
- 失败分类拆分为 `budget_exhausted`、`capacity_timeout`、`provider_throttled`、`host_memory_pressure`、`thread_capacity`、`disk_capacity`、`event_backpressure` 和 `db_backpressure`。

## 测试与验收

### 自动化测试

- 原子预算：并发申请不能超卖；实际结算正确退款；重复释放无副作用。
- 公平排队：跨 root FIFO，同一 root 不得长期占满全局槽位。
- 边界：第 65 个排队请求立即失败；等待 120 秒进入 `capacity_timeout`。
- 取消：排队和运行中的取消都释放 reservation、slot、队列项和回调引用。
- 重试：每次重试重新申请，旧租约不可复用。
- 重启：租约不跨进程恢复，durable task 能进入 recovery 并重新排队。
- provider：429/Retry-After、TPM/RPM、熔断和恢复。
- 生命周期：hung stream、hung tool、关闭中 spawn、interpreter shutdown。
- EventBus：慢消费者下队列有界，delta 可合并，终态事件不丢。
- 数据库：并发事件写、WAL 压力、批量写失败和最终状态一致性。
- worktree：低磁盘、配额耗尽、clean orphan、modified preserved。
- 配置：新旧配置优先级、弃用告警和 observe/enforce 行为。
- 前端：排队、获批、超时、刷新重建、事件乱序和重复事件。

### 压力与验收门槛

- 100 个模拟 Multi-Agent 批次后，线程数在预热后保持稳定，不随批次数线性增长。
- 任意时刻活跃 worker 不超过全局 2、单 root 2。
- token 账本在每次运行结束后满足守恒不变量。
- 取消或超时后，所有可控 provider/tool 在限定 drain 时间内退出。
- 事件队列、DB writer 队列和 admission 队列都不能无限增长。
- 慢 WebSocket 不得推高 agent 执行线程或阻塞终态持久化。
- 进程关闭期间不能再出现 `cannot schedule new futures after interpreter shutdown`。
- 现有 Multi-Agent、delegation recovery、worktree integration、chat streaming 和前端回放测试全部通过。
- observe 阶段至少采集一轮真实工作负载，再从 soft-enforce 切换为 enforce。

## 文档交付要求与假设

- 文档使用中文，代码符号、事件名和配置名保留英文。
- 文档包含现状/目标架构图、资源申请时序图、取消与 shutdown 时序图、状态机和配置示例。
- 文档明确列出“首期接入”和“后续全服务接入”，避免实现者误把接口范围等同于首期范围。
- 不自动降级 DAG，不自动删除有修改的 worktree。
- 首期默认使用 64 项队列、120 秒等待、全局 2 worker、单 root 2 worker。
- 现有功能设计文档不重写，只增加本资源治理文档的入口和边界说明。
- 编写文档时不顺带修改运行时代码；文档评审完成后再按 Phase 0–4 分批实现。
