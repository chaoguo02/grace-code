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

---

# 6. CC 行为纠偏（2026-08-03 补充，基于 CC 实际行为/官方文档/开源代码）

> 此节纠正本计划及前置设计规范中的若干 CC 误读，避免后续继续误导。引用依据：
> Anthropic Tool Use API / Claude Code Hooks 文档 / CC 开源镜像与社区源码分析。

## 6.1 Schema strict — 不是 CC 编排机制 ❌

- `strict: true` 是 **OpenAI Structured Outputs** 概念；Anthropic 对应的是 **JSON Schema Validation**（`input_schema` + `additionalProperties: false`），且是**服务端后置校验**，非生成时前置约束。
- CC 源码**无全局 strict 注入**。CC 靠详尽的 `description` + few-shot 引导；参数错了，靠 `validation_error` ToolFailure 让模型下一轮自修正。
- **结论**：本计划不将 "strict 前置强制" 列为目标（之前讨论中的建议作废）。

## 6.2 tool_choice 动态切换 — CC 几乎不用 Runtime 主动接管 ⚠️

- CC StepLoop 绝大多数 turn **硬编码 auto**，不会因空转/权限拒绝自动切 any/tool/none；这些模式供外部调用者（IDE/测试）使用。
- CC 防失控靠：System Prompt 强指令 + Max Turns 硬上限（~200）+ Token Budget 熔断（compact/summarize）+ PostToolUse Hook 注入纠正上下文。
- **结论**：`LLMPort.invoke(tool_choice=...)` 参数支持 + auto 默认即 CC 对齐；**不实现** "动态切换策略函数"（那是 LangGraph/AutoGen 思路）。

## 6.3 PreToolUse 四决策 ✅ 已对齐

- allow/deny/ask/defer 是 CC Hooks 明确定义的返回值，[step_loop.py](runtime_core/step_loop.py) 四条路径齐全。
- `defer` 实际语义：**等待并行 batch 其他工具完成后重评**，服务于并行安全，非通用条件延迟。当前实现将其标记为拒绝（近似），完整语义可选后续增强。

## 6.4 PostToolBatch 触发点 ✅ / 作用被高估 ⚠️

- 触发点存在（[step_loop.py](runtime_core/step_loop.py#L188)）✅。
- 返回值**仅限** `additionalContext`（追加对话历史）和 `updatedToolOutput`（替换本批结果）；**不能**改 tool_choice 或注入 system prompt。是"只读聚合 + 有限写入"的观察点，**不是**编排决策点（决策始终由 LLM 下一轮推理做出）。
- **结论**：不实现 "hook 写入 forced_tool_choice"（非 CC 行为）。

## 6.5 结构化错误语义 ✅ CC 核心 / ERROR_RETRY_MAP 是超越增强

- CC 依赖 `is_error: true` + 可读 error message 驱动模型自愈；**没有** Runtime 静默重试——timeout/network 也作为 `tool_result(is_error=true)` 回传，模型自行决定重试。
- 本实现 `ToolFailure.error_type` 枚举 + `ERROR_RETRY_MAP` 是**超越 CC 的增强**（automatic 类自动指数退避重试），不是 CC 标准行为。保留它，但**不标榜为 CC 对齐**——它是有价值的自主设计。
- **结论**：结构化错误类型 = CC 对齐 ✅；自动重试 = 自主增强（区别于 CC）。

## 6.6 对当前实现的最终判断

| 项 | CC 对齐 | 自主增强 |
|---|---|---|
| PreToolUse 四决策 | ✅ | — |
| PostToolBatch 触发点 | ✅ | — |
| 结构化 error_type + is_error | ✅ | — |
| tool_choice 参数 + auto 默认 | ✅ | — |
| Runtime 自动重试（ERROR_RETRY_MAP） | — | ✅ 超越 CC |
| 并行 sibling 失败隔离（G19） | — | ✅ 优于 CC（CC 会取消整批） |
| RiskLevel + TrustAccumulator 权限累积 | — | ✅ 超越 CC |
