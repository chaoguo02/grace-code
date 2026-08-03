# Tool 系统 CC-Native 执行计划（基于当前代码差距核实）

> 文档版本：1.0.0
> 创建日期：2026-08-03
> 前置：`docs/TOOL_SYSTEM_CC_NATIVE_PLAN_2026-08-06.md`（设计规范 T0-T5）
> 基准：当前 HEAD（f0b4171，含 G0-G44 + H0-H8 + LocalRuntime Phase 1-3）
> 核心结论：**设计规范中的 P0/P1 差距大部分已在当前代码实现**，真实剩余工作收敛为 3 个阶段（R1-R3）。

---

# 0. 使用说明与施工纪律（先读本节）

## 0.1 本文解决什么问题

设计规范（TOOL_SYSTEM_CC_NATIVE_PLAN）基于旧基准 f6a534f 评估，声称实现度约 55%、存在 P0-1/P0-2 等差距。**这些差距在当前代码已闭合**。本文基于当前 HEAD 逐条核实，给出真实剩余差距与可执行阶段，避免重复劳动。

## 0.2 核心原则

- **先核实，再动手**：每阶段 Before Test 证明缺口真实存在（须 FAIL），才实现。
- **每阶段 ≤3 文件**（对齐设计规范约束），禁止一次性大改。
- **测试用 FakeToolPort**，零真实工具执行（对齐设计规范验收）。
- **不重复已实现项**：T0/T1/T2 已验证闭合，不在本计划重复。

## 0.3 固定执行循环

```text
核实本阶段差距 → Before Test（须 FAIL）→ 实现 → Target Tests（须 PASS）
→ Regression Slice → git diff --check → 阶段报告 → 停止等待确认
```

---

# 1. 实现度核实（纠正过时评估）

对照设计规范 T0-T5，逐条核实当前 HEAD：

| 阶段/差距 | 设计规范声称 | 当前代码实际 | 状态 |
|---|---|---|---|
| P0-1 错误类型结构化 | ❌ `error_type: str` 自由文本 | ✅ `ToolErrorType(StrEnum)` 8 枚举 [ports.py:24](runtime_core/ports.py#L24)；`ToolFailure.error_type: ToolErrorType` [ports.py:72](runtime_core/ports.py#L72)；`ERROR_RETRY_MAP` 全覆盖 [ports.py:37](runtime_core/ports.py#L37) | **已闭合** |
| P0-2 元数据桥接 | ❌ 双系统割裂 | ✅ `ToolMetadata.from_base_tool()` [tool_scheduler.py:31](runtime_core/tool_scheduler.py#L31) | **已闭合** |
| T0 retry 执行 | ❌ 不做 retry | ✅ `_execute_via_registry` MAX_RETRIES=3 + 指数退避 + 按 ERROR_RETRY_MAP 分类 [composition.py:190-225](composition/runtime_composition.py#L190) | **已闭合** |
| T1 tool_choice | ❌ 不支持 | ✅ `LLMPort.invoke/stream(tool_choice=...)` [ports.py:139](runtime_core/ports.py#L139) | **已闭合** |
| T1 PostToolBatch | ❌ 未实现 | ✅ `step_loop.py:188` 批处理后触发 `PostToolBatchInput` | **已闭合** |
| T1 PreToolUse/PermissionRequest | ❌ 未接线 | ✅ PreToolUse gate [step_loop.py:297](runtime_core/step_loop.py#L297)；ASK→PermissionRequest 升级 [step_loop.py:324](runtime_core/step_loop.py#L324) | **已闭合** |
| T2 tool_result 回填 | ⚠️ 部分 | ✅ `ToolSuccess.to_chat_block`/`ToolFailure.to_chat_block` CC 格式 + `tool_use_id` 回填 [step_loop.py:302](runtime_core/step_loop.py#L302) | **已闭合** |
| T5 tool_use_id→evidence | ❌ 未关联 | ✅ `step_loop.py:60` `tool_use_id=self.tool_call.id # T20` | **已闭合** |
| **T3 权限分级接线 Native** | ❌ 未接入 | ⚠️ `hitl.PermissionPipeline`（rules+mode+RiskLevel+Trust，Phase 2 完成）**存在但 Native StepLoop 未消费**；`assemble()` 的 `_perm_rules` 存储未接线 [composition.py:466-468](composition/runtime_composition.py#L466) | **R1 真实差距** |
| **T4 动态注册** | ❌ 静态 lookup | ⚠️ `ToolRegistryPort` 协议定义 [ports.py:152](runtime_core/ports.py#L152)；`_RealTools` 用静态 lookup/resolve，**无 register/unregister 动态接口** | **R2 真实差距** |
| **T5 行为对齐测试** | ❌ | ⚠️ 有 `test_runtime_architecture_gates.py` 但缺 CC tool_result 格式/错误消息行为对齐测试 | **R3 真实差距** |

**结论**：T0/T1/T2 全部闭合，实现度约 **88%**（非文档评估的 55%）。真实剩余 = 3 个阶段。

---

# 2. 真实剩余差距

## R1（对应 T3）— PermissionPipeline 未接入 Native StepLoop

- **现状**：`hitl.PermissionPipeline` 完整（6 层 + RiskLevel/TrustAccumulator），但 Native 路径的 `_RealHooks.check()` 只走 `HookDispatcher`；`assemble()` 加载的 `permission_rules` 存入 `_perm_rules` 后**从未被消费**。
- **影响**：Native 生产路径下，permission_rules / 权限模式 / 信任累积不生效，仅靠 hook gate 决策。这是文档评估最准的一条。

## R2（对应 T4）— Native 无动态工具注册

- **现状**：`ToolRegistryPort` 协议定义了 `register/unregister/resolve`，但 `_RealTools` 实现只持有 `_lookup`/`_registry.resolve`，无 `register()`/`unregister()` 动态接口。
- **影响**：MCP 工具运行期动态接入/移除、alias 解析不完整。

## R3（对应 T5）— 缺 CC 行为对齐测试

- **现状**：有功能测试，但无"相同输入 → 与 CC 相同的 tool_result 格式 / 错误消息 / 重试次数"行为对齐测试。

---

# 3. 阶段执行计划

## R1 — PermissionPipeline 接入 Native StepLoop

**涉及文件（≤3）**：`composition/runtime_composition.py`、`runtime_core/ports.py`（若需扩展 HookGatePort）、`tests/runtime_core/test_permission_native_gate.py`

### Before Test（须 FAIL）

`test_permission_native_gate.py::test_permission_rules_block_in_native` — 配置 `deny Write` 规则，Native 路径执行 Write，断言当前"未被规则拦截"（hook 决策绕过规则）。

### 设计

- `assemble()` 把 `_perm_rules` 真正接线：将 permission_rules 转换为 hook 可消费的 gate（在 `_RealHooks.check()` 的 PreToolUse 分支，先经 `hitl.PermissionPipeline.check()` 再返回）。
- 复用 Phase 2 已接线的 RiskLevel/TrustAccumulator 作为 pipeline 输入（T3 验收含信任激活）。

### Target Tests

- deny 规则在 Native 拦截 Write/Edit。
- ask 规则走 PermissionRequest（Phase 2 信任累积后 LOW 风险自动放行）。
- bypassPermissions 模式跳过检查。
- Regression：`tests/test_native_run_e2e.py`、`tests/composition/test_native_object_graph.py`。

## R2 — Native 动态工具注册

**涉及文件（≤3）**：`composition/runtime_composition.py`（`_RealTools` 实现 register/unregister）、`runtime_core/ports.py`（ToolRegistryPort 已定义，校验）、`tests/composition/test_native_object_graph.py`

### Before Test（须 FAIL）

`test_native_object_graph.py::test_native_dynamic_register` — 调用 `components.runtime_ports.tools.register(...)`，断言当前无该方法。

### 设计

- `_RealTools` 实现 `register()`/`unregister()`/`list_names()`/`metadata_for()`，内部维护可变注册表 + 静态 lookup 合并。
- MCP 工具经 `mcp__server__tool` 前缀解析后动态注入注册表。

### Target Tests

- 运行期 register 新工具 → 可解析。
- unregister → 不可解析。
- `mcp__` 前缀 alias 解析。

## R3 — CC 行为对齐测试

**涉及文件（≤3）**：`tests/runtime_core/test_behavioral_parity.py`、`tests/test_runtime_architecture_gates.py`、`runtime_core/ports.py`（仅修正格式差异，若无则不加）

### Before Test（须 FAIL）

`test_behavioral_parity.py::test_tool_result_cc_format` — 断言 tool_result 是 CC 格式（`type: tool_result`, `tool_use_id`, `content`），当前可能在某分支缺失。

### 设计

- 固定 FakeToolPort 序列，断言 tool_result 结构、is_error 映射、重试次数与 CC 一致。
- 校验 8 种 ToolErrorType 各自的 retry 行为（ERROR_RETRY_MAP 全覆盖）。

### Target Tests

- 8 种错误类型 retry 行为验证。
- tool_result CC 格式（success + failure + denied）。
- 重试次数符合 ERROR_RETRY_MAP。

---

# 4. 验收总闸门（每阶段完成须全绿）

```text
tests/runtime_core/test_permission_native_gate.py   (R1 新增)
tests/composition/test_native_object_graph.py       (R2 扩展)
tests/runtime_core/test_behavioral_parity.py        (R3 新增)
tests/test_runtime_architecture_gates.py            (R3 扩展)
```

回归切片：`tests/integration/test_native_run_e2e.py`、`tests/composition/*`、`tests/runtime_core/*`（每阶段报告列明本次运行文件）。

---

# 5. 明确不做（No-Go）

- ❌ 不重复 T0/T1/T2（已闭合）。
- ❌ 不做 tool_search / 数千工具 lazy-load（超出当前阶段，作后续迭代）。
- ❌ 不改 `ToolErrorType` / `ERROR_RETRY_MAP` / `from_base_tool`（已验证正确）。
