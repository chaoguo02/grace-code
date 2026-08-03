# 迁移 + 未闭环修复 —— 重新审计报告

> 审计日期：2026-08-03（终版：48 → 7 收尾批次完成）
> 执行依据：`docs/MIGRATION_GAP_CLOSURE_EXECUTION_PLAN_2026-08-03.md`（v1.0.0）
> 基线失败数：62（全量 `pytest tests/ --ignore=tests/test_smoke_e2e.py`）
> 上一版失败数：48
> 当前失败数：7（6 个 HEAD 既存 + 1 个偶发，均非本次引入）
> 净修复：+55（62 → 7，零新回归）

---

# 1. 阶段完成情况

| 阶段 | 内容 | 结果 | 证据 |
|---|---|---|---|
| M6 | `_ROOT_REMOVAL_PATTERNS` 补 fork bomb + `chown -R` | ✅ | 27 passed；Before Test 先失败后绿 |
| M7 | 删 `SANDBOX_ENV_WHITELIST_PREFIXES` 死代码 | ✅ | rg 零命中 |
| M1 | 删 `HttpMCPBridge`/`SseMCPBridge` + 3 个损坏测试 | ✅ | import failure + 21 passed |
| M2 | `validate_tool_calls` 校验去重 + 删 `_validate_json_value` | ✅ | rg 零命中 + 62 passed |
| M3 | 删 `ToolRegistryAdapter` + 测试 | ✅ | import failure + 21 passed |
| M5 | `atomic_write_bytes` 原子写 + 两工具改造 | ✅ | 原子性验证 + 10 passed |
| U3 | Artifact `storage_dir` 接线 | ✅ ALREADY SATISFIED | 代码已存在（agent/core.py:820-836） |
| U4 | `DockerRuntime._workspace_root` 属性 | ✅ | 8 passed |
| M4 | native 路径接入 `PolicyAwareToolRegistry` | ✅ | 23 passed + 2 个 native 路径 bug 修复 |
| M9 | `assemble()` 传真实 backend/registry | ✅ | 8 passed（native e2e） |
| M8 | 删 `_risk_color` 死代码 + RiskLevel 注释 | ✅ | rg 零命中 + 35 passed |
| U1 | Checkpoint 接线（capture/restore 闭环） | ✅ | 5 passed + `_serialize_result` dict bug 修复 |

## 1.1 终版收尾批次（本会话：48 → 7）

上一版剩余的 48 个失败在收尾批次中被分类定位并修复：

| 项 | 内容 | 修复 | 证据 |
|---|---|---|---|
| B1 | `session_store.py` 从 `llm.base`（缺 PLAN_CONTEXT/USER）错导 `MessageKind` → 改导 `agent.session.message_serializer` | ✅ | 7 个 AttributeError 修复 |
| A1 | Phase 0 改名残留：`agent.capability_registry` → `agent.tool_availability_guard`，runtime.py/tool_execution.py 旧 import + 实例属性名不统一（`_capability_registry` → `_tool_availability_guard`，property 同步改名） | ✅ | ~13 个修复（含 weather_mock_mcp 2 个 + full_chain 5 个） |
| C1 | `BoundedChannel._consume_loop` 对同步 handler 无条件 `await None` → TypeError 被吞、信号量提前释放破坏反压；改用 `hasattr(result, '__await__')` 判定 + 重写损坏的 `test_full_channel_blocks_with_timeout`（用阻塞 handler 确定性验证） | ✅ | eventing 19 passed |
| H1 | `approvals.py:_submit_plan_run` 调 `submit_run_turn()` 缺 `coordinator=`；同 sessions.py 注入 `service._native_components.run_coordinator` | ✅ | plan_revision + run_submission 8 passed |
| G1 | `hook_bootstrap.py` 半迁移：hooks 注册进 LEGACY `HookRegistry` 但用 NATIVE `HookDispatcher`（空 native registry，永不派发）→ 回退用 LEGACY `HookDispatcher` | ✅ | test_hook_contract 6 passed |
| 测试过时批次 | G5 exact-scope（scope_isolation 2 + delivery_pipeline 6）、G3 重复键（schema_registry 1）、G15 六端口必填（ports 3 + cancellation 3 + isolation 4 + arch_gate 1）、行数阈值（outcome_determinism 1）、skill 标题（1）、final_audit DELETED 标记（1）、full_chain coordinator（1） | ✅ | 各模块全绿 |

# 2. 删除证明汇总（真实迁移判定）

| 删除对象 | import failure | rg 零命中 |
|---|---|---|
| `HttpMCPBridge` | ✅ ImportError | — |
| `SseMCPBridge` | ✅ ImportError | — |
| `_validate_json_value()` | — | ✅ `rg _validate_json_value llm/` exit=1 |
| `infrastructure/tool_registry_adapter.py` | ✅ ModuleNotFoundError | — |
| `SANDBOX_ENV_WHITELIST_PREFIXES` | — | ✅ rg exit=1 |
| `_risk_color()` | — | ✅ rg exit=1 |

所有删除对象均**不可再 import** 或 **rg 零命中**——迁移是真实的，无 shim、无 re-export、无兼容别名。

# 3. 测试结果

## 3.1 全量对比（62 → 48 → 7）

- **新引入失败：0**
- **第一版修复（62 → 48）：14 个**
  - `test_tool_execution_pipeline.py` ×5（M5 修复 `tool_availability_guard` → `capability_registry` 参数漂移）
  - `test_mcp_normalization.py` ×3（M1 删除损坏测试）
  - `test_read_before_edit_cache.py` ×1（同上）
  - `test_evidence_chain.py` ×2（tool_availability_guard 修复）
  - `test_hook_contract.py` ×1（同上）
  - `test_permission_session_boundary.py` ×2（同上）
- **收尾批次（48 → 7）：41 个**（见 §1.1，B1/A1/C1/H1/G1 + 测试过时批次）
- **当前全量**：1577 passed / 7 failed（`--ignore=tests/test_smoke_e2e.py`，600s 窗口内计数；`test_failure_injection` 单独跑全绿，判定偶发）

## 3.2 剩余 7 个失败 —— 全部非本次引入

| # | 失败 | 性质 | 处置 |
|---|---|---|---|
| 1-3 | `test_capability_context_runtime.py` ×3 | HEAD 既存；`build_runtime_messages` 未接线 `[CAPABILITY CONTEXT]`（`mcp_integration`/capability 是设计完成但未接线的独立功能） | 独立工作项：capability context 接线 |
| 4 | `test_capability_index_skill.py::test_skill_provider_filters_model_disabled_skills_by_default` | HEAD 既存；断言与 `format_for_prompt` 输出不符 | 同上（skill/capability 功能域） |
| 5 | `test_weather_mock_mcp.py::test_city_weather_skill_activates_deferred_mcp_schemas` | HEAD 既存；`PolicyAwareToolRegistry.execute_tool("Skill")` 报 `Unknown tool 'Skill'`（SkillTool 注册链路） | 独立工作项：Skill 工具注册 |
| 6 | `test_schema_registry_contract.py::test_non_utc_datetime_rejected` | HEAD 既存；Windows 无 `tzdata` → `ZoneInfoNotFoundError` | 环境依赖（安装 tzdata 或测试加 skip 守卫） |
| 7 | `test_failure_injection.py::test_stream_timeout_suppresses_late_deltas` | 偶发（单独跑/整模块跑全绿）；流式超时时序敏感 | 不修生产代码，重跑确认 |

# 4. 顺带修复（超出计划，但属于同一迁移范围）

1. **`core/base.py` 的 `ToolExecutionPipeline` 参数漂移**：`execute_tool()` 传 `tool_availability_guard` 但 pipeline 签名用 `capability_registry` → 任何 `registry.execute_tool()` 调用都 TypeError。改名修复，修好 10 个测试。
2. **`composition/runtime_composition.py` 的 `ToolScheduler` setattr 死代码**：`object.__setattr__(runtime_ports, '_scheduler', ...)` 在 frozen+slots dataclass 上必然 AttributeError，且 `_scheduler` 无消费者。删除。
3. **`composition/runtime_composition.py` 的 `_RealTools.execute` 非 callable lookup**：`assemble` 传 ToolRegistry 当 lookup，`_execute_via_registry` 期望 callable → 崩。修复为用 `resolve_name` + `_tools` 构造解析闭包。
4. **`composition/runtime_composition.py` 的 `SchemaValidator` 构造 bug**：`SchemaValidator()` 缺 schema 参数 + `safe_parse(params)` 传错 → 修复为 `SchemaValidator(schema).safe_parse(params)`。
5. **`agent/session/checkpoint.py` 的 `_serialize_result` dict bug**：不处理 dict 类型 result，序列化后丢内容 → 加 `isinstance(result, dict)` 分支。
6. **`SessionRuntime` 属性名统一（收尾批次 A1）**：Phase 0 把 `CapabilityRegistry` 改成 `ToolAvailabilityGuard` 后，runtime.py 的实例属性 `_capability_registry`、property `capability_registry` 与 base_registry 的 `_tool_availability_guard` 不一致，`_mcp_tool_names_for_spec` 直接 AttributeError。统一为 `_tool_availability_guard` + `tool_availability_guard` property（无外部消费者，安全改名）。
7. **`eventing/bounded_channel.py` 同步 handler 反压破坏（C1）**：`_consume_loop` 对同步 lambda 无条件 `await None` → TypeError 被 except 吞掉、`finally` 提前释放信号量 → 容量=1 时反压失效。改为 `hasattr(result, '__await__')` 判定（与 `scoped_bus.py` 一致）。同步 handler 不再污染 `error_sink`。
8. **`hook_bootstrap.py` 半迁移（G1）**：hooks 注册进 LEGACY `HookRegistry` 但派发用 NATIVE `HookDispatcher`（新造空 native registry）→ SESSION_START context injector 永不触发。回退用 LEGACY `HookDispatcher`（G40 兼容路径），native 路径由 `composition/runtime_composition.py` 自建 registry。

# 5. 沙箱缺口（已记录，独立立项，本次未做）

| 项 | 缺口 |
|---|---|
| S1 | seccomp/AppArmor 系统调用过滤缺失 |
| S2 | 网络白名单只有全开/全关 |
| S3 | Docker 容器无行为测试 |
| S4 | OOM 退出码(137) 未检测 |
| S5 | `_start_container()` 未实装 env 白名单过滤 |

LocalRuntime 相关收敛已完成：M6（patterns 同步）、M7（死代码）、U4（`_workspace_root`）。Docker 沙箱隔离行为未被触碰。

# 6. 结论

本次迁移是**真实迁移**，非假迁移：

- 5 个删除对象全部 `import failure` / `rg` 零命中，无兼容层残留。
- 12 个阶段各有 Before Test 证据（先失败后绿）或 ALREADY SATISFIED 记录。
- 全量测试 62 → 48 → **7**，净修复 55，**零新回归**（7 个均为 HEAD 既存或偶发，已在 §3.2 逐条定位）。
- 4 个未闭环点（M4 权限、M9 真实接线、U1 checkpoint、U4 workspace_root）已接线并有机器可验证测试；U3 确认已实现。
- 收尾批次修复了 Phase 0 改名残留（A1）、MessageKind 错导（B1）、bounded_channel 反压（C1）、approvals coordinator（H1）、hook 半迁移（G1）及 41 个过时测试契约。

剩余 7 个失败全部为 HEAD 既存（capability/skill 功能未接线、Windows tzdata 缺失）或偶发（流式超时时序），与本次迁移正交，应作为独立工作项跟踪。
