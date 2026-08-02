# Runtime / Hooks / EventBus P1–P28 完成度审计与下一轮重建方案

> 文档版本：1.0.0  
> 审计日期：2026-08-02  
> 审计基准：`RUNTIME_HOOKS_EVENTBUS_CC_NATIVE_REBUILD_V2_2026-08-02.md`  
> 审计对象：提交 `0fcad45` 至 `e95d7e2` 所声称完成的 P1–P28  
> 结论状态：**REJECTED — 不得宣称 28/28 完成，不得开启 NATIVE 生产模式**

---

# 0. Executive Summary

## 0.1 最终结论

当前实现没有达到 V2.0 规划书的完成定义。它完成了若干新包、值对象、协议和孤立单元测试，但没有完成生产链路切换，也没有删除旧 Runtime、旧 Hooks、旧 EventBus。更严重的是，若手动设置 `GRACE_RUNTIME_MODE=NATIVE`，当前代码会进入包含空实现的路径，可能返回“提交成功”却没有写入权威 Run、Message 或 Outbox 数据。

因此，本次审计给出：

- **整体成熟度：27 / 100**。
- **阶段验收：2 / 28 通过，11 / 28 部分完成，15 / 28 失败或未实现**。
- **AC 验收：13 / 60 通过，6 / 60 部分满足，41 / 60 失败**。
- **生产切换状态：0%**。默认模式仍是 `LEGACY`，服务启动仍实例化旧 `server.services.event_bus.EventBus`。
- **旧链路删除状态：0%**。P25–P28 对应旧文件全部仍存在，迁移日志自己也全部标记为 `PENDING`。
- **专项测试状态：114 项通过**，但 composition 覆盖率为 0%，没有测试执行 `NATIVE` 模式。
- **全量回归状态：通过**，耗时约 261.5 秒；该结果主要证明旧生产链路没有被破坏，不能证明新架构已经接管。
- **strict type check：失败，71 个错误**；常规 strict 命令还会因 `runtime_core/__init__.py` 缺失先行失败。
- **部署打包：失败风险**。`runtime_core`、`eventing`、`hook_core`、`listeners`、`infrastructure`、`composition` 未全部进入 setuptools package include。

## 0.2 三项 P0 阻断

### P0-1：NATIVE 提交路径会静默丢失权威写入

代码证据：

- `server/services/run_submission.py:43`：只有环境变量精确等于 `NATIVE` 才走新链路。
- `server/services/run_submission.py:173-174`：`_StorageTx.append_fact()` 是 `pass`。
- `_StorageUoW.execute()` 虽然开启事务，但回调没有插入 Run、Message 或 Outbox。
- `RunCoordinator.submit()` 只创建 `RunSubmitted` envelope 并调用 `append_fact`；它没有创建 Run 状态。
- `_submit_via_coordinator()` 最终仍返回 `created=True`，构成假成功。

影响：一旦运维或开发环境启用 `GRACE_RUNTIME_MODE=NATIVE`，API 可以向调用方返回 run_id，但数据库中没有对应权威记录，后续执行、取消、恢复和投影全部无法可靠工作。

处置：在真正完成 Native 原子写入之前，必须禁止任何环境启用 NATIVE。不能通过给 `pass` 补一条 INSERT 修复；必须重新定义 Run UoW 的 state mutation + fact append 契约并写真实故障矩阵。

### P0-2：EventBus 没有实现作用域订阅，测试反而把泄漏写成成功

代码证据：

- `eventing/scoped_bus.py:59-69`：`subscribe()` 不接收 ScopeToken，只按 `event_type` 保存订阅者。
- `eventing/scoped_bus.py:97`：publish 会调用该 event type 的全部订阅者。
- `tests/eventing/test_scope_isolation.py:65`：Session A 发布事件后，明确断言 Session B 也收到一次：`assert len(received_b) == 1`。
- 测试注释声称“no cross-session leak”，实际断言正好相反。

影响：Session/Task 隔离这一核心安全边界完全不成立。不同用户会话的事件可能进入错误的 Listener、WebSocket 或投影。

处置：该测试必须首先改成失败测试；随后整体重写 subscription key、scope close、generation identity 和 registry cleanup。禁止只在 handler 外包一层 session_id 判断。

### P0-3：P25–P28 实际没有执行

代码证据：

- `server/services/event_bus.py`、`hooks/events.py`、`hooks/protocol.py`、`hooks/dispatcher.py`、`hooks/registry.py`、`agent/session/runtime.py`、`server/ws/event_mapper.py` 全部仍存在。
- `server/main.py:54,358` 和 `run_server.py:6,26` 仍 import/实例化旧 EventBus。
- `hooks/dispatcher.py:251-254` 仍创建 `daemon=True` 的 detached Hook thread。
- `composition/runtime_composition.py:22` 默认值仍是 `LEGACY`。
- `composition/deprecation_log.py:20,29,38,47` 对 P25–P28 全部写着 `PENDING`。
- P28 要求的 `tests/benchmarks/` 和 `tests/faults/` 不存在。

影响：架构处于双套代码共存、旧套权威、新套未接管的状态；提交名称“P25-P28 complete”与代码事实冲突。

处置：不得继续在 migration marker 或 deprecation list 上标记进度。删除阶段只能以“旧 import 失败 + 生产入口唯一 + CI gate”为完成证据。

## 0.3 评分模型

| 维度 | 权重 | 得分 | 说明 |
|---|---:|---:|---|
| Schema/类型契约 | 15 | 5 | 有 frozen dataclass，但存在 Any、裸 dict、无真实反序列化 |
| EventBus/Scope | 20 | 3 | 有 ScopeToken/ScopeTree，但订阅完全不按 scope 路由 |
| Hook | 15 | 6 | 有 typed decision 和 registry snapshot，但超时是假超时、任意返回值可通过 |
| Runtime/Coordinator | 20 | 4 | 新 Runtime 是固定 `"ok"` 骨架，Coordinator 不写 state |
| Outbox/Projection | 10 | 5 | 孤立 adapter 有部分正确行为，但 production delivery 是 `pass` |
| Production Cutover/Deletion | 15 | 0 | 默认旧链路，旧模块全部仍可 import |
| Verification/CI | 5 | 4 | pytest/build 通过，但 strict typing、import contract、性能/泄漏测试缺失 |
| **总计** | **100** | **27** | **不可发布** |

---

# 1. 审计方法与可复现证据

## 1.1 执行过的命令

```powershell
python -m pytest tests/application tests/eventing tests/hook_core tests/runtime_core tests/listeners -q
python -m pytest tests/application tests/eventing tests/hook_core tests/runtime_core tests/listeners -q `
  --cov=application --cov=eventing --cov=hook_core --cov=runtime_core `
  --cov=listeners --cov=composition --cov-report=term-missing
python -m pytest -q
python -m compileall -q application runtime_core hook_core eventing listeners infrastructure composition
python -m mypy --strict --explicit-package-bases `
  application runtime_core hook_core eventing listeners infrastructure composition
cd web
npm run build
```

## 1.2 实际结果

| 检查 | 结果 | 解释 |
|---|---|---|
| 新架构专项 pytest | PASS，114 tests | 只证明孤立模块测试通过 |
| 专项 coverage | 89% | `composition/runtime_composition.py` 为 0% |
| 全量 pytest | PASS | 默认仍是旧架构，不能作为 Native 证据 |
| compileall | PASS | 仅证明语法可编译 |
| Web build | PASS | 有 bundle >500KB warning，与本重构无直接阻断关系 |
| mypy strict | FAIL，71 errors/24 files | AC-1 直接失败 |
| importlinter | NOT INSTALLED | AC-60 无法执行 |
| benchmarks/fault tests | NOT IMPLEMENTED | P28、AC-29、49–53 失败 |

## 1.3 测试可信度问题

通过率不能单独作为完成证据，原因如下：

1. `test_no_cross_session_leak` 明确断言跨 Session 泄漏发生。
2. `test_canonical_json_roundtrip` 只执行 `json.loads()` 并检查三个字段，没有从 JSON 重建 typed envelope，更没有验证对象等值。
3. Coordinator 原子性测试的 FakeTransaction 只有 `append_fact()`，不存在 state mutation，因此无法证明 state + outbox 原子性。
4. Relay stop 测试只断言 `remaining >= 0`，任何非负返回值都能通过，无法验证精确 undelivered count。
5. Shadow 测试只比较两个 lambda 返回值，没有测试真实 tool 隔离、权威存储零写入或 99.5% digest 一致率。
6. 没有测试引用 `RuntimeComposition`、`GRACE_RUNTIME_MODE` 或 `_submit_via_coordinator`。

---

# 2. P1–P28 阶段完成度矩阵

等级定义：

- **PASS**：实现和阶段 AC 均有可信证据。
- **PARTIAL**：存在代码骨架或部分行为，但关键契约缺失。
- **FAIL**：实现与契约冲突，或存在会导致错误结果的实现。
- **NOT IMPLEMENTED**：只有注释、marker、计划或文件不存在。

| 阶段 | 结论 | 代码证据 | 必须修正 |
|---|---|---|---|
| P1 Core values/scope | PASS | ID 与 ScopeToken 存在，基础 invariant 测试通过 | 后续补 global_id identity 测试 |
| P2 Run Fact Schema | PARTIAL | typed payload 存在 | 无 decode/typed round-trip；codec 接受 list/dict |
| P3 Tool/Delegation/Registry | PARTIAL | schema 已登记 | registry 使用 Any；重复相同 class 不失败；key 没真正以 `(type, version)` 建模 |
| P4 Eventing Ports | PARTIAL | Protocol 文件存在 | 泛型 variance 错误；mypy 失败；bus 直接 import business envelope |
| P5 Scoped Bus | FAIL | 有 ScopeTree | subscriber 不绑定 scope；跨 Session 广播；generation 替换复用 closed node |
| P6 Backpressure/Close | FAIL | BoundedChannel 单独存在 | 未接入 ScopedEventBus；drain 只是 sleep 0.1 后看 queue size |
| P7 Durable/UoW Ports | PARTIAL | 接口文件存在 | transaction 没有权威 state mutation API |
| P8 SQLite Outbox | PARTIAL | claim/receipt/DLQ 基础代码存在 | production 未接；不同内容同 event_id 被 `INSERT OR IGNORE` 静默吞掉；delay_s 未使用 |
| P9 Hook contracts | FAIL | 多个 input dataclass 存在 | `dict[str, Any]` 违反红线；旧 HookContext 仍存在 |
| P10 Immutable registry | PASS | snapshot 捕获 tuple，新增注册不影响旧 snapshot | 仍需并发注册/注销测试 |
| P11 Hook dispatcher | FAIL | failure policy/decision merge 有骨架 | handler 同步执行完才判断超时；任意 object return 被接受；没有真实中止 |
| P12 Runtime ports/outcome | PARTIAL | ports 和 frozen outcome 存在 | Protocol 使用 object/裸 dict；没有 Hook/Cancellation 明确 port；strict typing 失败 |
| P13 Runtime Core | FAIL | 代码很小且无 DB import | 不调用真实 LLM；硬编码 assistant `ok`；`cancelled=False` 永不改变；没有 Hook gate |
| P14 Coordinator/UoW | FAIL | command/coordinator 文件存在 | submit/finalize 只 append fact，不写 state；finalize 丢失 session scope；无 CAS |
| P15 Trace Projection | PARTIAL | receipt + trace 共事务 | 没有 typed visitor/version gap；catch-all envelope；生产 relay 不投递 |
| P16 WS Projection/Gateway | FAIL | 有 callback map | 无 typed mapper、watermark、重连补齐、scope lifecycle；裸 dict/callback |
| P17 Stats Projection | PARTIAL | 收集 run event 元数据 | 内存 list，不是可靠 projection；catch-all `startswith("run.")` |
| P18 Shadow Composition | FAIL | ShadowRunner 与 composition 文件存在 | composition 0% coverage；新 handler 可能执行副作用；没有权威零写证明或真实 comparator |
| P19 Run Cutover | FAIL | 环境分支存在 | 默认 LEGACY；NATIVE append_fact 是 pass；返回假成功 |
| P20 Multi-Agent Cutover | NOT IMPLEMENTED | 目标 coordinator 文件不存在 | 目前只有 `composition/extraction_plan.py` 注释 |
| P21 Context/Message Extraction | NOT IMPLEMENTED | 规划目标文件不存在 | 旧 Runtime 继续装配消息和 context |
| P22 Worktree/Evidence Extraction | NOT IMPLEMENTED | 规划目标文件不存在 | 旧 Runtime 继续持有 worker/evidence 流程 |
| P23 AgentService Split A | PARTIAL | 存在部分 standalone helper | AgentService 仍约 1500 行且仍承担 composition/business logic |
| P24 AgentService Split B | PARTIAL | 有部分 helper 提取 | 规划中的 Resource/Approval Coordinator 不存在，Runtime 私有/服务定位交互仍多 |
| P25 Delete old EventBus | NOT IMPLEMENTED | deprecation status=PENDING | 旧 bus 仍是 server/main 与 run_server 生产入口 |
| P26 Delete old Hooks | NOT IMPLEMENTED | deprecation status=PENDING | 旧 hooks 全部存在，detached daemon thread 仍存在 |
| P27 Delete old Runtime/flags | NOT IMPLEMENTED | deprecation status=PENDING | 2843 行旧 Runtime 仍是主路径，默认 LEGACY |
| P28 Perf/leak/fault cleanup | NOT IMPLEMENTED | tests/benchmarks、tests/faults 不存在 | migration flags 和 ShadowRunner 未删；无性能/泄漏证据 |

---

# 3. AC-1～AC-60 逐项判定

## 3.1 Schema 与边界 AC

| AC | 结果 | 证据 |
|---|---|---|
| AC-1 strict typing | FAIL | mypy strict：71 errors/24 files |
| AC-2 无 Any/dict payload | FAIL | `hook_core/inputs.py:11,19,29`；`schema_registry.py:11,78` |
| AC-3 typed round-trip | FAIL | 只有 encode + json.loads，无 decode/rebuild/equality |
| AC-4 duplicate key startup failure | FAIL | 相同 payload class 的重复 key 被接受 |
| AC-5 拒绝 mutable payload | FAIL | `_payload_to_dict` 明确递归接受 list/tuple/dict |
| AC-6 envelope metadata 完整 | PASS | EventEnvelope 字段齐全 |
| AC-7 Command 不能 publish | PARTIAL | 没有显式 Command/Event 类型防线；可能在属性访问时偶然失败 |
| AC-8 past-tense facts | PASS | 已登记名称均为 submitted/started/completed 等事实 |
| AC-9 无 Command publish | PASS | 静态搜索零匹配 |
| AC-10 Listener 不 import Coordinator/Runtime | PASS | 静态搜索零匹配 |

## 3.2 EventBus AC

| AC | 结果 | 证据 |
|---|---|---|
| AC-11 exact task scope | FAIL | subscribe 无 scope；测试确认跨 Session 同收 |
| AC-12 parent close 清 subscription | FAIL | close 只关 ScopeNode，不清 `_subscribers` |
| AC-13 stale generation | PARTIAL | 小于 current 会拒绝，但 global_id 不校验，future generation 也未正确拒绝 |
| AC-14 1000 child cleanup | FAIL | 没有测试；registry 与 subscription 不属于 scope tree |
| AC-15 无 implicit bubbling | FAIL | 实际按 event type 全局广播，比 bubbling 更宽 |
| AC-16 cross-session identity | FAIL | event_id/source/scope 没有共同参与 routing/dedup identity |
| AC-17 bus infra 无业务 import | FAIL | `eventing/scoped_bus.py:13` import `application.events.envelope` |
| AC-18 bus 无业务方法 | PASS | 新 ScopedEventBus 没有 translate/persist/ws 方法 |
| AC-19 bounded production queue | FAIL | BoundedChannel 未接入 production bus |
| AC-20 handler timeout isolation | FAIL | 同步逐个 handler；慢 handler 阻塞后续 handler；无 error sink |
| AC-21 FIFO 10k | FAIL | 没有对应测试；没有异步 scoped channel |

## 3.3 Hook AC

| AC | 结果 | 证据 |
|---|---|---|
| AC-22 无通用 HookContext | FAIL | 新类型存在，但旧 `hooks/events.py` 仍可 import |
| AC-23 只接收 sum type | FAIL | executor/dispatcher 使用 object，不验证返回 decision class |
| AC-24 fail-closed timeout 不执行 tool | FAIL | timeout 在 handler 返回后才检查，无法按时阻断 |
| AC-25 fail-open warning fact | FAIL | Hook 与 Event publisher 没有该链路 |
| AC-26 Hook error 不污染 bus/repository | PARTIAL | 孤立测试通过，但没有 production integration fault test |
| AC-27 registry revision | PASS | snapshot 测试覆盖 N/N+1 |
| AC-28 无 detached Hook thread | FAIL | 旧 `hooks/dispatcher.py:251-254` 仍使用 daemon thread |
| AC-29 Hook P99 | FAIL | benchmark 文件不存在 |

## 3.4 Runtime/Coordinator/Projection AC

| AC | 结果 | 证据 |
|---|---|---|
| AC-30 Runtime unit isolation | PASS | 新 Runtime 测试只使用 fake ports |
| AC-31 Runtime forbidden imports | PASS | runtime_core 无具体 server/listener import |
| AC-32 Runtime 无 DB write | PASS | runtime_core 无 DB 操作 |
| AC-33 deterministic outcome | PARTIAL | 骨架行为确定，但未覆盖真实 model/tool/hook loop digest |
| AC-34 frozen outcome | PASS | dataclass frozen/slots |
| AC-35 line limits | PASS | runtime_core 远低于限制，但部分原因是功能未实现 |
| AC-36 state/outbox rollback | FAIL | Coordinator transaction 没有 state mutation |
| AC-37 crash recovery projection | FAIL | 新 adapter 与 production relay/delivery 未接通 |
| AC-38 terminal CAS race | FAIL | 新 Coordinator 没有 CAS/state row |
| AC-39 idempotency/content conflict | FAIL | SQLite adapter 使用 INSERT OR IGNORE，未检测不同内容 |
| AC-40 receipt + projection atomic | PASS | TraceProjection 在同一事务写 receipt/trace |
| AC-41 poison 后 good event | PARTIAL | 有 DLQ 基础代码，但测试没有证明后续 good event delivered |
| AC-42 shutdown 精确计数 | FAIL | stop 会额外 claim 一条记录并只返回 0/1，不是精确 pending count |
| AC-43 concrete event subscription | FAIL | ProjectionRunner/Projection 接收通用 envelope |
| AC-44 projection idempotency | PASS | TraceProjection receipt 测试覆盖重复 event |
| AC-45 version gap | FAIL | 无 aggregate gap detector |
| AC-46 WS reconnect watermark | FAIL | WsGateway 只有内存 callback list |
| AC-47 无 Listener 仍完成 | PARTIAL | 设计上可能，但生产 Coordinator/UoW 未完成，无法验证 |
| AC-48 Listener 无 publisher | PASS | 新 listener package 未注入 publisher |

## 3.5 性能、切换与 CI AC

| AC | 结果 | 证据 |
|---|---|---|
| AC-49 enqueue P99 | FAIL | benchmark 不存在 |
| AC-50 dispatch P99 | FAIL | benchmark 不存在 |
| AC-51 subscription retained=0 | FAIL | fault/leak test 不存在，且当前 subscription 不随 scope 清除 |
| AC-52 heap growth | FAIL | 测试不存在 |
| AC-53 bounded overflow policy | PARTIAL | BoundedChannel 单测存在，但未接 production EventBus |
| AC-54 shadow 零副作用 | FAIL | ShadowRunner 直接调用 new_handler，无副作用隔离 |
| AC-55 shadow >=99.5% | FAIL | 无运行报告/样本/分类 |
| AC-56 删除 LEGACY/SHADOW | FAIL | `GRACE_RUNTIME_MODE` 与默认 LEGACY 仍存在 |
| AC-57 旧 API 无法 import | FAIL | 旧 EventBus/Hooks/Runtime 全部可 import |
| AC-58 Team gate | PASS | 指定静态搜索零匹配 |
| AC-59 全门禁 | FAIL | pytest/compile/build 通过；mypy/importlinter/fault/benchmark 失败或缺失 |
| AC-60 CI import contract | FAIL | 唯一 workflow 未运行架构/type/import gates |

---

# 4. 额外发现的高风险实现缺陷

## 4.1 Scope generation 更新后节点仍保持 closed

`ScopeTree.ensure_session()` 对更高 generation 执行：

```python
node.close()
node.generation = generation
return node
```

`close()` 将 `node.closed=True`，后续没有重新打开，也没有创建新 identity。因此所谓“新 generation”仍然是 closed node。正确实现应创建全新的不可变 ScopeNode/ScopeToken identity，旧 node 保持 closed，并从 active index 替换为新 node。

## 4.2 Hook timeout 是事后统计，不是超时控制

`execute_hook()` 直接同步调用 `handler(hook_input)`，等 handler 返回后才比较耗时。一个永久阻塞的 Hook 会永久阻塞 Runtime。正确实现必须由受控 executor 在 deadline 内等待；超时后返回确定策略，并确保底层可取消进程或隔离执行单元。

## 4.3 Runtime Core 是演示桩，不是 Agent Runtime

`runtime_core/step_loop.py`：

- 不调用 `llm.invoke()`；只在两个 port 存在时构造固定 `{"content": "ok"}`。
- `cancelled` 初始化为 False 后从不变化。
- 没有调用 HookDispatcher。
- token 永远为 0。
- 不消费真实 model response/tool call schema。

该模块不能替代旧 SessionRuntime，也不应进入 Shadow 对比，因为其输出没有业务等价性。

## 4.4 新架构无法可靠打包

`pyproject.toml` 的 setuptools include 只覆盖 `agent*`、`server*`、`app*` 等旧集合。`runtime_core` 甚至缺少 `__init__.py`。开发工作区依靠 `pythonpath=["."]` 可以 import，不代表 wheel/editable install 的部署产物包含这些包。

## 4.5 delivery pipeline 被显式丢弃

`RuntimeComposition._deliver(record)` 是 `pass`，但 Relay 会在 callback 无异常返回后调用 `mark_delivered()`。这意味着一旦新 relay 启动，事件可能没有进入任何 Projection，却被永久标记 delivered。

这是数据丢失而非“待完善 mapper”。

---

# 5. 下一轮正确执行方案

## 5.1 总原则

下一轮不得把当前新目录视为可靠基线继续补方法。除 P1/P10 中经验证的值对象和 snapshot 思路外，其余模块必须按契约重新审查，必要时整体替换。

执行顺序必须是：

```text
修正验收门禁
  -> 修正类型/打包基础
  -> 重建 Scope/EventBus
  -> 重建 Hook timeout/decision contract
  -> 实现真实 Runtime loop
  -> 实现权威 UoW/Coordinator
  -> 接通 Outbox/Projection
  -> Shadow 无副作用验证
  -> Native 单入口切换
  -> Multi-agent/context/worktree extraction
  -> 删除旧链路
  -> 性能/泄漏/故障门禁
```

禁止事项：

- 禁止把现有错误测试保留为“兼容行为”。
- 禁止增加第二个环境变量绕开 `GRACE_RUNTIME_MODE`。
- 禁止在 `_StorageTx.append_fact()` 中只补 outbox INSERT 而不实现 state mutation。
- 禁止在 Listener 中判断 session_id 来掩盖 Bus 无 scope 的事实。
- 禁止再次用 marker、deprecation log、注释或提交名称代替代码删除。
- 禁止先删旧 Runtime，再让未完成的新 Runtime 接管生产。

## 5.2 修复阶段

每阶段最多 3 个文件；完成后必须停下并提交机器输出。

| 阶段 | 目标 | 允许文件 | 硬验收 |
|---|---|---|---|
| R0 | 纠正虚假门禁 | `tests/eventing/test_scope_isolation.py`、`tests/application/events/test_run_fact_schema.py`、`tests/test_runtime_architecture_gates.py` | 新测试在旧实现上必须失败；证明测试能抓到现有缺陷 |
| R1 | Packaging + strict typing baseline | `pyproject.toml`、`runtime_core/__init__.py`、`.github/workflows/architecture.yml` | 新包全部被 find_packages 发现；CI 安装 mypy/import-linter 并执行 gate |
| R2 | JSON value/schema codec | `application/events/envelope.py`、`application/events/schema_registry.py`、对应 schema test | typed encode→decode→equal；拒绝 mutable/object payload；重复 key 必失败 |
| R3 | Hook JSON 输入边界 | `hook_core/inputs.py`、`hook_core/decisions.py`、`tests/hook_core/test_contracts.py` | 无 Any/裸 dict；非法 return 失败 |
| R4 | Scope identity/tree | `core/eventing/scope.py`、`eventing/scope_tree.py`、scope value test | global/session/task/generation 全 identity；新 generation 是新 node |
| R5 | Scoped subscription | `eventing/subscription.py`、`eventing/scoped_bus.py`、scope isolation test | Task A 只到 Task A；close 清 registry；1000 child retained=0 |
| R6 | Delivery/backpressure | `eventing/bounded_channel.py`、`eventing/scoped_bus.py`、bus lifecycle test | bounded queue 真正接入 bus；timeout 不阻塞 sibling；FIFO 10k |
| R7 | Hook executor | `hook_core/executor.py`、`hook_core/dispatcher.py`、dispatch fault test | 超时在 deadline 返回；fail-closed 不调用 tool；无 daemon thread |
| R8 | Runtime ports/loop | `runtime_core/ports.py`、`runtime_core/step_loop.py`、runtime isolation test | 真实 fake LLM response→Hook→Tool→Outcome；取消可注入；无硬编码 `ok` |
| R9 | Coordinator transaction | `application/transactions/unit_of_work.py`、`application/coordinators/run_coordinator.py`、atomicity test | state+fact 同事务；故障双回滚；terminal CAS 20 threads 只有一个 winner |
| R10 | Outbox correctness | `infrastructure/outbox/sqlite_store.py`、`infrastructure/outbox/relay.py`、outbox failure test | content conflict 告警；真实 retry delay；stop 精确计数；poison 不挡 good |
| R11 | Typed projections | `listeners/projection_runner.py`、`listeners/trace_projection.py`、trace test | concrete schema visitor；version gap；receipt+row 原子 |
| R12 | WS recovery | `listeners/ws_gateway.py`、`server/ws/event_mapper.py`、WS test | snapshot+watermark+live；terminal 不重复；session isolation |
| R13 | Native composition | `composition/runtime_composition.py`、`server/services/run_submission.py`、native integration test | 无 pass；真实 Run/Message/Outbox；崩溃恢复投影 |
| R14 | Safe shadow | `listeners/shadow.py`、composition file、shadow integration test | fake tool/null projection；权威表零新写；真实样本 digest 报告 |
| R15 | Run production cutover | `server/services/chat_pipeline.py`、`server/main.py`、run E2E test | API 只进 Coordinator；旧 EventBus 不再由 server/main 创建 |
| R16 | Multi-agent cutover | 新 coordinator、batch tool、scope-chain test | fresh TaskContext；child result 经 Coordinator 显式桥接；无 Team |
| R17 | Context extraction | ContextAssembler、ConversationService、Runtime adapter | Runtime 只收 snapshot；旧 Runtime 对应逻辑删除 |
| R18 | Workspace/evidence extraction | WorkspaceLeaseService、EvidenceCollector、Runtime adapter | lease 显式；evidence 作为 outcome；无 Runtime DB write |
| R19 | AgentService purification A | application bootstrap、maintenance scheduler、AgentService | memory lifecycle 移出；service 行数/依赖 gate |
| R20 | AgentService purification B | ResourceCoordinator、ApprovalCoordinator、AgentService | command/gate direct-call；EventBus 不驱动业务命令 |
| R21 | Delete old EventBus | 旧 bus、旧 mapper/import、architecture gate | 旧 EventBus import 必失败；eventing 无 FastAPI/SQLite/business import |
| R22 | Delete old Hooks | 旧 hook files、architecture gate | HookContext/旧 dispatcher import 必失败；无 daemon Hook thread |
| R23 | Delete old Runtime/flags | 旧 runtime、composition、architecture gate | 只有 Native path；无 LEGACY/SHADOW/env fallback |
| R24 | Final fault/perf/leak | benchmark、fault、leak 三个 test 文件 | AC-29、49–53 达到 V2.0 原阈值，不得调低 |

## 5.3 R0 必须先失败的测试

R0 的目的不是让测试变绿，而是证明验收门禁能够捕获当前问题。R0 完成时以下测试必须在旧实现上红：

```python
def test_no_cross_session_leak():
    # publish Session A
    assert received_a == [event]
    assert received_b == []

def test_typed_round_trip():
    decoded = registry.decode(envelope.canonical_json())
    assert decoded == envelope
    assert type(decoded.payload) is type(envelope.payload)

def test_native_submission_is_atomic():
    result = submit_native(...)
    assert runs.count(result.run_id) == 1
    assert messages.count(result.turn_id) == 1
    assert outbox.count_for(result.run_id, "run.submitted.v1") == 1
```

如果 R0 新增测试仍然全绿，说明测试没有触达当前缺陷，禁止进入 R1。

## 5.4 每阶段交付报告

```text
Phase: Rx
Document: RUNTIME_HOOKS_EVENTBUS_P1_P28_COMPLETION_AUDIT_2026-08-02.md v1.0.0
Files changed: 精确列出，必须 <= 3
Red test before implementation: 命令 + 失败摘要
Green test after implementation: 命令 + passed 数量
Static gates: rg/mypy/import contract 输出
Production path exercised: yes/no + 入口
Placeholders introduced: 必须为 0
Rollback point: commit/database snapshot
Open risks: 明确列表
STOP: 等待确认，不自动进入下一阶段
```

---

# 6. 下一位执行模型的唯一开工指令

```text
你只能执行 docs/RUNTIME_HOOKS_EVENTBUS_P1_P28_COMPLETION_AUDIT_2026-08-02.md 的 R0。
先完整读取第 0、1、2、3、4、5.1、5.3 节。
只允许修改 R0 列出的三个测试文件，不修改生产代码。
R0 的成功条件是：新增/修正的验收测试能够稳定暴露当前实现的 scope 泄漏、伪 round-trip 和 Native 假提交，测试应在现有生产代码上失败。
不要为了让测试变绿而保留现有错误语义。
输出第 5.4 节报告后立即停止，不得进入 R1。
如果需要第 4 个文件、放宽断言或添加 sleep/retry，立即停止并报告阻塞。
```

---

# 7. 最终判定

当前可准确声明的状态是：

> “P1–P18 的若干隔离骨架和单元测试已经创建；旧生产架构仍为唯一有效路径；P19 的 Native 路径不可用且存在静默丢写风险；P20–P28 未按验收标准完成。”

在 R0–R24 全部通过之前，禁止使用以下表述：

- “28/28 complete”
- “CC-Native 已落地”
- “生产已切换”
- “Runtime/Hooks/EventBus 已完成职责分离”

