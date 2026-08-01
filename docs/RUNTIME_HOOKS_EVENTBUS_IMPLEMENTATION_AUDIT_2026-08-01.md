# Runtime / Hooks / EventBus 实现复审（2026-08-01）

## 结论

本轮完成 R0-R2：恢复测试基线、彻底移除 Agent Team 产品、落实 Hook Engine 的核心契约。Build、Plan、Multi-Agent 保留，Agent Team 不再存在于领域层、Runtime、工具、HTTP API 或 Web UI。

EventBus 与 Runtime Ports 尚未达到设计稿终态，应作为下一轮 R3-R4 实现，不能将当前状态标记为整体重构完成。

## 已完成范围与代码证据

| 范围 | 状态 | 代码证据 | 验收证据 |
|---|---|---|---|
| R0 测试基线 | ✅ | `agent/core.py`、`server/services/agent_service.py`、`core/process.py`、序列化与校验器修复 | 全量 pytest 通过 |
| Agent Team 删除 | ✅ | 删除 `agent/team/`、Team tools、Runtime Team 状态机、Team routes/projection/UI | `tests/test_agent_team_removed.py`；生产目录残留扫描为零 |
| 三种模式保留 | ✅ | `agent/session/registry_builder.py` 继续提供 Agent/AgentBatch/控制与 Worktree 工具 | Scenario、Batch、Multi-Agent、Runtime 回归 39 项通过 |
| Hook 四维契约 | ✅ | `hooks/registry.py`：Scheduling、DecisionAuthority、DataAuthority、FailurePolicy | `tests/test_hook_contract.py` |
| Hook 聚合语义 | ✅ | `hooks/dispatcher.py`：priority 排序、deny-overrides、总时限失败策略 | allow→deny、fail-closed 测试通过 |
| Internal Hook 返回值 | ✅ | `HookOutput`/`HookResult`/dict/None 统一归一化 | context/input/output transform 测试通过 |
| PostTool 输出变换 | ✅ | `core/tool_execution.py` 仅应用显式授权的 output/data/metadata | sanitizer 测试通过 |
| HITL 边界 | ✅ | `hitl/pipeline.py` 不再允许 Hook APPROVE 免除人工确认 | Permission Pipeline 回归通过 |
| SessionStart 注入 | ✅ | `entry/bootstrap/hook_bootstrap.py` 注册 `SessionContextInjector` | CLAUDE.md 注入测试通过 |
| Goal Stop 迁移 | ✅ | `entry/chat.py` 注册正式 STOP policy hook；删除 `_goal_stop_hook` 旁路 | `_goal_stop_hook` 生产扫描为零 |

## 本轮门禁结果

- `pytest -q`：全量通过；仅保留两个既有 warning（未注册 integration mark、Starlette httpx deprecation）。
- `npm run build`：TypeScript 与 Vite production build 通过；仅有既有 chunk-size warning。
- `python -m compileall -q agent server hooks entry core llm hitl`：通过。
- `git diff --check`：通过。
- Agent Team 生产符号扫描：零结果。
- `_goal_stop_hook` 生产符号扫描：零结果。

## 尚未达到设计要求

### R3 — EventBus 事务可靠性（P0）

当前仍有明确缺口：

1. `server/services/event_bus.py` 同时承担持久化、广播、队列与生命周期，边界仍然过宽。
2. `publish_raw()` 仍存在，并在 `agent_service.py`、`chat_pipeline.py`、`replay_service.py` 有调用。
3. 代码库没有 transactional outbox；状态事务成功后、事件发布前崩溃仍可能丢失事实事件。
4. DomainEvent 与 WebSocket DTO 尚未形成强制的 Mapper 边界。

建议实施顺序：

1. 新增 `event_outbox` 表与 `OutboxRecord`，业务状态和 outbox insert 使用同一 SQLite transaction。
2. 新增 `OutboxRelay`，按 `event_id` 幂等投递，定义 retry/backoff/dead-letter/shutdown drain。
3. 将剩余 `publish_raw()` 迁移到强类型 DomainEvent；CI 设置生产目录 grep gate。
4. 拆分 `DomainEventPublisher`、`ProjectionRunner`、`WsEventMapper`，EventBus 退化为传输设施。

机器验收：崩溃注入后重启可补投；同一 `event_id` 不产生重复投影；`publish_raw` 在生产代码为零；慢消费者不会无限占用内存。

### R4 — Runtime Ports 与 Application Coordinator（P1）

`server/services` 仍有 6 处直接访问 Runtime 私有字段：Web mode、stats recorder、terminal publisher、evidence store、memory callback、store。它们说明 Runtime 仍被当作可变 Service Locator。

建议新增并注入：`RuntimeModePort`、`RunTerminalPublisher`、`EvidencePort`、`MemoryEventPort`、`RunStorePort`；由 Application Coordinator 完成装配，随后设置 CI 门禁禁止 `runtime._*`。

机器验收：`rg "runtime\\._|_runtime\\._" server/services` 为零；Runtime 构造完成后不再由 AgentService 注入私有回调；跨层依赖通过 Protocol/ABC 可替换测试。

## 下一轮建议

只推进 R3（EventBus Outbox）作为独立批次，完成崩溃恢复和幂等测试后再进入 R4。不要在同一批次同时重写 Runtime Ports，以免事件可靠性与依赖反转两类故障相互掩盖。
