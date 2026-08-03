# Tool 系统 CC-Native 细粒度重建执行规划书

> 文档版本：1.0.0
> 创建日期：2026-08-06
> 当前基线：H0-H8 + Phase A-C + R1-R3 完成
> 核心原则：先做新的，再兼容旧的，最后删旧的。每阶段 ≤5 文件。

---

# 0. 使用说明与不可违背的施工纪律

## 0.1 本文解决什么问题

当前 Tool 系统存在三层断裂：

```text
CC 标准层（Anthropic Tool Use + Claude Code Tools Reference）
    ↓ 未对齐
现有系统层（core/base.py BaseTool + core/tool_execution.py Pipeline）
    ↓ 未桥接
Native Runtime 层（runtime_core/ports.py ToolPort + composition/_RealTools）
```

本文定义 28 个原子阶段，将三层断裂逐步闭合。

## 0.2 核心原则

- **先做新的**：新类型、新协议、新 adapter 独立创建，不与旧代码耦合。
- **再兼容旧的**：旧 `BaseTool.execute()` → 新 `ToolPort.execute()` 通过 adapter 桥接。
- **最后删旧的**：旧代码标记 DEPRECATED 后删除。
- **禁止通过 monkey-patch 旧 ToolRegistry 实现 Native 功能。**

## 0.3 固定执行循环

```text
完整读取本阶段 → 检查允许文件(≤5) → Before Test → 实现 → Target Tests
→ Static Gates → Regression Slice → git diff --check → 阶段报告 → 停止等待
```

---

# 1. 25 条 CC 事实基线

## 1.1 工具定义与注册

| # | CC 事实 | 要求 |
|---|---|---|
| T1 | 工具通过 JSON Schema 定义：`name`、`description`、`input_schema` | `ToolCall.params` 必须是 JSON Schema-validatable 的 `FrozenJsonObject` |
| T2 | `description` 是模型选择工具的主要依据 | 每个 BaseTool 的 schema.description 必须面向模型可读 |
| T3 | `strict: true` 确保模型输出严格遵守 schema | 所有生产工具应启用 strict mode |
| T4 | `tool_choice`：`auto`/`any`/`tool`/`none` | LLMPort.invoke() 必须支持 tool_choice 参数 |

## 1.2 工具调用生命周期

| # | CC 事实 | 要求 |
|---|---|---|
| T5 | `tool_use` block：`id`、`name`、`input` | ModelAction.ToolCall 携带 id/name/params |
| T6 | `tool_result` block：`tool_use_id`、`content`、`is_error` | ToolOutcome 区分 success/failure |
| T7 | `is_error: true` → 模型被告知失败并可重试 | ToolFailure 映射到 is_error 语义 |
| T8 | `tool_result` 内容可以是纯文本或 content blocks | ToolSuccess.output 支持结构化内容 |

## 1.3 并行工具执行

| # | CC 事实 | 要求 |
|---|---|---|
| T9 | 模型可返回多个 tool_use block，并行执行 | ToolCallBatch 默认并行（H5） |
| T10 | `disable_parallel_tool_use: true` 强制串行 | tool_choice 参数支持 |
| T11 | 并行工具独立失败——不影响同 batch 其他工具 | ToolScheduler sibling failure 隔离（G19） |

## 1.4 Hook 与工具交互

| # | CC 事实 | 要求 |
|---|---|---|
| T12 | PreToolUse：`permissionDecision` + `updatedInput` | StepLoop 在 tool 执行前调用 HookDispatcher |
| T13 | PostToolUse：不可回滚，可注入 context/改写 output | StepLoop PostToolUse 失败不阻断 |
| T14 | PostToolBatch：整批完成后，下次模型调用前 | 批处理后触发 PostToolBatch hook |
| T15 | PermissionRequest：独立 decision schema | PreToolUse ask → PermissionRequest |

## 1.5 错误处理

| # | CC 事实 | 要求 |
|---|---|---|
| T16 | 工具失败返回 `is_error: true` | ToolFailure 结构化，包含 error_type |
| T17 | 不同错误类型不同 retry 策略 | RetryPolicy 绑定到每个工具 |
| T18 | PostToolUseFailure hook 在失败时触发 | 失败时 HookDispatcher 接收 PostToolUseFailureInput |

## 1.6 权限与分级

| # | CC 事实 | 要求 |
|---|---|---|
| T19 | 每个工具有 permission mode 行为 | ToolMetadata 携带 permission_mode |
| T20 | permission_rules 在 settings.json 配置 | Composition Root 加载 permission rules |

## 1.7 可插拔与发现

| # | CC 事实 | 要求 |
|---|---|---|
| T21 | MCP 工具：`mcp__server__tool` 命名 | ToolRegistry 支持动态 MCP 工具 |
| T22 | 内置工具稳定名称集合 | 内置工具始终可用 |
| T23 | tool_search server tool 按需加载 | 大型工具集 lazy-loading |

## 1.8 元数据与可观测

| # | CC 事实 | 要求 |
|---|---|---|
| T24 | tool_use_id 追踪 + token 计入 | ToolPort.execute() 记录到 TokenUsagePort |
| T25 | tool use system prompt tokens 计入 cost | Composition 层计算工具 schema token 预算 |

---

# 2. 当前代码与 CC 标准的差距矩阵

| # | 领域 | CC 要求 | 当前状态 | 等级 |
|---|---|---|---|---|
| G1 | 错误类型 | ToolErrorType enum | `error_type: str` 自由文本 | P0 |
| G2 | 元数据桥接 | 统一 metadata 系统 | core vs runtime_core 两套独立 | P0 |
| G3 | tool_choice | auto/any/tool/none | LLMPort 不支持 | P1 |
| G4 | PostToolBatch | batch-level hook | 未实现 | P1 |
| G5 | 结果回填 | tool_result is_error 语义 | ToolFailure.error_type 非结构化 | P1 |
| G6 | 权限分级 | permission_mode 接线 | Native 路径不用 PermissionPipeline | P1 |
| G7 | Retry 策略 | per-tool retry + exponential backoff | _execute_via_registry 不做 retry | P1 |
| G8 | strict schema | strict: true | StepLoop 无 schema validation | P1 |
| G9 | MCP 工具 | mcp__server__tool 解析 | _execute_via_registry 无 alias 解析 | P2 |
| G10 | 工具可插拔 | 动态 register/unregister | Native 路径静态 lookup | P2 |
| G11 | tool_search | server-side discovery | 无等价物 | P2 |
| G12 | tool_use_id 追踪 | evidence 关联 | tool_use_id 未传入 ToolEvidence | P2 |
| G13 | tool schema token | cost 预算 | Composition 层未计算 | P2 |

---

# 3. 目标设计规范

## 3.1 工具执行流水线（目标态）

```text
LLM Response (ToolCallBatch)
  → Scheduler 分组（并行安全 vs 串行/冲突）
    → PreToolUse Hook Gate（每个 tool call）
      → allow → ToolPort.execute()
      → deny → ToolDenied
      → ask → PermissionRequest
      → defer → 保存 continuation
    → PostToolUse Hook（成功） / PostToolUseFailure Hook（失败）
  → PostToolBatch Hook（整批完成）
  → Conversation Block + Live Event + Evidence
```

## 3.2 ToolErrorType 枚举

```python
class ToolErrorType(StrEnum):
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    NETWORK_ERROR = "network_error"
    VALIDATION_ERROR = "validation_error"
    TOOL_NOT_FOUND = "tool_not_found"
    EXECUTION_ERROR = "execution_error"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    CANCELLED = "cancelled"

ERROR_RETRY_MAP = {
    ToolErrorType.TIMEOUT: RetryMode.AUTOMATIC,
    ToolErrorType.NETWORK_ERROR: RetryMode.AUTOMATIC,
    ToolErrorType.RESOURCE_EXHAUSTED: RetryMode.AUTOMATIC,
    ToolErrorType.VALIDATION_ERROR: RetryMode.APPROVAL,
    ToolErrorType.EXECUTION_ERROR: RetryMode.APPROVAL,
    ToolErrorType.PERMISSION_DENIED: RetryMode.NEVER,
    ToolErrorType.TOOL_NOT_FOUND: RetryMode.NEVER,
    ToolErrorType.CANCELLED: RetryMode.NEVER,
}
```

## 3.3 ToolMetadata 桥接

```python
# runtime_core/tool_scheduler.py
@staticmethod
def from_base_tool(tool) -> ToolMetadata:
    return ToolMetadata(
        name=tool.name,
        read_only=tool.isReadOnly(),
        concurrency_safe=(tool.concurrency_mode() == ToolConcurrency.PARALLEL_SAFE),
        resource_key=(tool.metadata.path_parameter
                      if hasattr(tool.metadata, 'path_parameter') else ""),
    )
```

---

# 4. 分阶段开发路线图

## 4.1 总体阶段表

| 阶段 | 目标 | 依赖 | 工时 | 差距消灭 |
|---|---|---|---|---|
| T0 | ToolErrorType 枚举 + ERROR_RETRY_MAP | None | 0.5 | G1 |
| T1 | ToolFailure + ToolSuccess 结构化字段 | T0 | 0.5 | G1/G5 |
| T2 | ToolMetadata.from_base_tool() 桥接 | None | 0.5 | G2 |
| T3 | ToolScheduler 使用桥接函数 | T2 | 0.5 | G2 |
| T4 | ToolRegistryPort 协议定义 | None | 0.5 | — |
| T5 | LLMPort.tool_choice 参数 | None | 1.0 | G3 |
| T6 | StepLoop 传递 tool_choice | T5 | 0.5 | G3 |
| T7 | PostToolBatchInput 类型 + hook 接线 | None | 1.0 | G4 |
| T8 | StepLoop PostToolBatch 触发点 | T7 | 0.5 | G4 |
| T9 | tool_result CC 格式回填 | None | 1.0 | G5 |
| T10 | StepLoop conversation 回填 | T9 | 0.5 | G5 |
| T11 | PermissionPipeline port 定义 | None | 0.5 | G6 |
| T12 | Permission rules 加载（settings.json） | T11 | 1.0 | G6 |
| T13 | StepLoop PreToolUse ask → PermissionRequest | T12 | 1.0 | G6 |
| T14 | _execute_via_registry 错误分类 + retry | T1 | 1.0 | G7 |
| T15 | ToolMetadata.retry_policy 读取 | T3/T14 | 0.5 | G7 |
| T16 | strict schema validation in StepLoop | None | 0.5 | G8 |
| T17 | MCP 工具前缀解析（mcp__server__tool） | T4 | 0.5 | G9 |
| T18 | 动态工具 register/unregister | T4/T17 | 1.0 | G10 |
| T19 | ToolRegistryPort 接入 _RealTools | T18 | 1.0 | G10 |
| T20 | tool_use_id → ToolEvidence 追踪 | None | 0.5 | G12 |
| T21 | Tool-level token cost tracking | None | 0.5 | G13 |
| T22 | Tool execution audit log | None | 0.5 | G12 |
| T23 | Old ToolExecutionPipeline 标记 DEPRECATED | None | 0.5 | — |
| T24 | _RealTools 接入 ToolRegistryPort | T19 | 1.0 | G9/G10 |
| T25 | run_server.py 注入 tool_registry 到 assemble() | T24 | 0.5 | G10 |
| T26 | E2E 验证矩阵（所有工具类型） | T25 | 1.5 | — |
| T27 | Old tool 路径物理删除 | T23 | 1.0 | G9/G10 |

总预估约 19 人日。T0-T4 可并行。T5-T6、T7-T8、T9-T10、T11-T13 是串行对。T14-T22 可部分并行。T23-T27 必须串行（先标记→再接入→再验证→再删除）。

## 4.2 阶段通用报告模板

```text
Phase: Txx
Document version: 1.0.0
Files changed: [必须 <= 5]
Before test: 命令 + 失败原因
Implementation summary: 精确说明新增/修改的契约
Target tests: 命令 + passed/failed
Static gates: mypy/rg/diff-check
Regression slice: 命令 + 结果
STOP: 等待确认
```

---

## 4.3 T0 — ToolErrorType 枚举 + ERROR_RETRY_MAP

### 目标

定义 `ToolErrorType` 8 值枚举和 `ERROR_RETRY_MAP`。

### 允许修改

1. `runtime_core/ports.py` — 增加 `ToolErrorType`、更新 `ToolFailure`
2. `tests/runtime_core/test_model_action_ports.py` — 验证枚举 + 映射
3. `core/retry_policy.py`（可选）— 如果需要新文件

### Before Test

```python
# 当前 ToolFailure.error_type 是自由文本
tf = ToolFailure(tool_name="read", error="timeout")
assert tf.error_type == ""  # 未结构化
```

### Target Tests

- [ ] 8 种 ToolErrorType 全部可构造
- [ ] ERROR_RETRY_MAP 覆盖全部 8 种
- [ ] ToolFailure 接受 ToolErrorType 值

---

## 4.4 T1 — ToolFailure + ToolSuccess 结构化

### 目标

`ToolFailure` 增加 `error_type: ToolErrorType`、`retryable: bool`。`ToolSuccess` 增加 `tool_use_id`。

### 允许修改

1. `runtime_core/ports.py` — 更新 `ToolFailure`/`ToolSuccess`
2. `runtime_core/step_loop.py` — 构造时传入新字段
3. `composition/runtime_composition.py` — `_execute_via_registry` 更新
4. `tests/runtime_core/test_hook_tool_loop.py` — 验证结构化字段

### Target Tests

- [ ] ToolFailure 构造时 `error_type` 为 `ToolErrorType` 值
- [ ] `retryable` 从 ERROR_RETRY_MAP 自动推导
- [ ] ToolSuccess.tool_use_id 非空

---

## 4.5 T2 — ToolMetadata.from_base_tool() 桥接

### 目标

在 `runtime_core/tool_scheduler.py` 增加 `from_base_tool()` 静态方法。

### 允许修改

1. `runtime_core/tool_scheduler.py` — 增加 `from_base_tool()`
2. `tests/runtime_core/test_parallel_tool_scheduler.py` — 验证桥接

### Before Test

```python
# 当前 runtime_core.ToolMetadata 无法从 BaseTool 构造
# 需要手动创建 read_only/concurrency_safe 字段
```

### Target Tests

- [ ] `ToolMetadata.from_base_tool(read_tool)` → `read_only=True`
- [ ] `ToolMetadata.from_base_tool(write_tool)` → `read_only=False`
- [ ] `concurrency_safe` 从 `tool.concurrency_mode()` 推导

---

## 4.6 T3 — ToolScheduler 接入桥接函数

### 目标

`assemble()` 中注册工具时用 `from_base_tool()` 填充 scheduler metadata。

### 允许修改

1. `composition/runtime_composition.py` — 在 tool 注册时调用 `from_base_tool()`
2. `runtime_core/step_loop.py` — scheduler 接收 metadata

### Target Tests

- [ ] 通过真实 BaseTool 实例创建 scheduler metadata
- [ ] 并行安全判定正确（read-only + concurrency_safe）

---

## 4.7 T4 — ToolRegistryPort 协议

### 目标

定义 `ToolRegistryPort` 协议：`register`/`unregister`/`resolve`/`list_names`/`metadata_for`。

### 允许修改

1. `runtime_core/ports.py` — 增加 `ToolRegistryPort` 协议
2. `tests/runtime_core/test_model_action_ports.py` — 验证协议

### Target Tests

- [ ] ToolRegistryPort 协议 5 个方法签名正确
- [ ] 可用于类型注解

---

## 4.8 T5 — LLMPort.tool_choice 参数

### 目标

`LLMPort.invoke()` 增加 `tool_choice` 参数。

### 允许修改

1. `runtime_core/ports.py` — `LLMPort.invoke` 签名
2. `composition/runtime_composition.py` — `_RealLLM.invoke` 转发
3. `tests/composition/test_native_object_graph.py` — 验证参数传递

### Target Tests

- [ ] `LLMPort.invoke(messages, tools, tool_choice={"type": "auto"})` 编译通过
- [ ] `tool_choice={"type": "any"}` 强制至少一个工具调用

---

## 4.9 T6 — StepLoop 传递 tool_choice

### 目标

StepLoop 构造 LLM 调用时传入 `tool_choice`。

### 允许修改

1. `runtime_core/step_loop.py` — `execute()` 中传递 `tool_choice`
2. `tests/runtime_core/test_real_model_loop.py` — 验证

### Target Tests

- [ ] 默认 `tool_choice={"type": "auto"}` 行为不变
- [ ] FakeLLM 记录接收到的 `tool_choice` 参数

---

## 4.10 T7 — PostToolBatchInput + Hook 接线

### 目标

定义 `PostToolBatchInput` 类型，在 `HookDispatcher` 注册 `PostToolBatch` event。

### 允许修改

1. `hook_core/inputs.py` — 增加 `PostToolBatchInput`
2. `hook_core/decisions.py` — EVENT_DECISION_MAP 增加 PostToolBatch
3. `hook_core/policies.py` — 增加 POSTTOOL_BATCH 策略
4. `tests/hook_core/test_typed_contracts.py` — 验证新类型

### Target Tests

- [ ] PostToolBatchInput 有 `session_id`、`tool_count`
- [ ] EVENT_DECISION_MAP["PostToolBatch"] 存在

---

## 4.11 T8 — StepLoop PostToolBatch 触发点

### 目标

在 ToolCallBatch 所有工具处理完成后，调用 PostToolBatch hook。

### 允许修改

1. `runtime_core/step_loop.py` — 在 tool batch 循环后触发 hook
2. `tests/runtime_core/test_hook_tool_loop.py` — 验证 hook 触发

### Target Tests

- [ ] PostToolBatch hook 在 3-tool batch 完成后被调用 1 次
- [ ] hook 失败不阻断继续执行

---

## 4.12 T9 — tool_result CC 格式回填

### 目标

定义 `ToolResultBlock` 类型（CC 兼容），`ToolOutcome` 可转换为 CC 格式。

### 允许修改

1. `runtime_core/ports.py` — 增加 `ToolResultBlock`、`ToolOutcome.to_chat_block()`
2. `tests/runtime_core/test_model_action_ports.py` — 验证格式

### Target Tests

- [ ] `ToolSuccess.to_chat_block()` 返回 `{"type": "tool_result", "tool_use_id": ..., "content": ...}`
- [ ] `ToolFailure.to_chat_block()` 返回 `{"type": "tool_result", "is_error": true, ...}`

---

## 4.13 T10 — StepLoop conversation 回填

### 目标

StepLoop 将 tool_result 以 CC 格式追加到 conversation。

### 允许修改

1. `runtime_core/step_loop.py` — tool 完成后追加 `tool_result` block
2. `tests/runtime_core/test_hook_tool_loop.py` — 验证 conversation 内容

### Target Tests

- [ ] conversation 中 tool 执行后出现 `tool_result` block
- [ ] `tool_result.tool_use_id` 与 `ToolCall.id` 匹配

---

## 4.14 T11 — PermissionPipeline Port 定义

### 目标

定义 `PermissionPipelinePort` 协议。

### 允许修改

1. `runtime_core/ports.py` — 增加 `PermissionPipelinePort` 协议
2. `tests/runtime_core/test_model_action_ports.py` — 验证

### Target Tests

- [ ] PermissionPipelinePort 有 `check(tool_name, params) → PermissionResult`
- [ ] PermissionResult 包含 `allowed: bool`、`reason: str`

---

## 4.15 T12 — Permission rules 加载

### 目标

`assemble()` 从 settings.json 加载 permission rules 到 PermissionPipeline。

### 允许修改

1. `composition/runtime_composition.py` — 加载 permission rules
2. `server/services/agent_service.py` — 传入 permission settings
3. `tests/composition/test_native_object_graph.py` — 验证加载

### Target Tests

- [ ] settings.json 中 `permission_rules` 被解析
- [ ] `Bash(rm *)` → deny 规则生效

---

## 4.16 T13 — PreToolUse ask → PermissionRequest

### 目标

StepLoop 中 PreToolUse 返回 ask 时，调用 PermissionRequest hook。

### 允许修改

1. `runtime_core/step_loop.py` — ask 时调用 PermissionRequest
2. `tests/runtime_core/test_hook_tool_loop.py` — 验证 escalation

### Target Tests

- [ ] PreToolUse ask → PermissionRequest hook 被调用
- [ ] PermissionRequest 返回 deny → tool 不执行

---

## 4.17 T14 — _execute_via_registry 错误分类 + retry

### 目标

`_execute_via_registry` 根据 ToolErrorType 决定 retry 策略。

### 允许修改

1. `composition/runtime_composition.py` — `_execute_via_registry` 增加 retry loop
2. `tests/composition/test_native_object_graph.py` — 验证 retry 行为

### Target Tests

- [ ] TIMEOUT → retry（最多 max_attempts 次）
- [ ] PERMISSION_DENIED → 不 retry
- [ ] VALIDATION_ERROR → 不 retry（需修正参数）

---

## 4.18 T15 — ToolMetadata.retry_policy 读取

### 目标

从 `BaseTool.metadata.retry_policy` 读取 retry 配置。

### 允许修改

1. `runtime_core/tool_scheduler.py` — `from_base_tool()` 增加 retry_policy
2. `composition/runtime_composition.py` — 使用 retry_policy
3. `tests/runtime_core/test_parallel_tool_scheduler.py` — 验证

### Target Tests

- [ ] `from_base_tool(tool_with_retry_policy)` → ToolMetadata.retry_policy 非 None

---

## 4.19 T16 — Strict schema validation

### 目标

StepLoop 在 tool 执行前用 JSON Schema 验证 params。

### 允许修改

1. `runtime_core/step_loop.py` — 增加 schema validation
2. `core/schema_validator.py`（已存在）— 复用
3. `tests/runtime_core/test_hook_tool_loop.py` — 验证

### Target Tests

- [ ] 无效 params → VALIDATION_ERROR，不执行 tool
- [ ] 有效 params → 正常执行

---

## 4.20 T17 — MCP 工具前缀解析

### 目标

`_execute_via_registry` 支持 `mcp__server__tool` 命名约定。

### 允许修改

1. `composition/runtime_composition.py` — `_execute_via_registry` 增加 MCP 前缀解析
2. `tests/composition/test_native_object_graph.py` — 验证

### Target Tests

- [ ] `mcp__weather__get_forecast` 被解析为 server=weather, tool=get_forecast
- [ ] 未知 MCP server → TOOL_NOT_FOUND | RESOURCE_EXHAUSTED

---

## 4.21 T18 — 动态工具 register/unregister

### 目标

`ToolRegistryPort` 实现：运行时注册/注销工具。

### 允许修改

1. `infrastructure/tool_registry_adapter.py`（新文件）— 实现 ToolRegistryPort
2. `tests/infrastructure/test_tool_registry_adapter.py` — 验证

### Target Tests

- [ ] 注册工具后 `resolve(name)` 可找到
- [ ] 注销工具后 `resolve(name)` 返回 None
- [ ] 并发 register/unregister 安全

---

## 4.22 T19 — ToolRegistryPort 接入 _RealTools

### 目标

`_RealTools` 使用 `ToolRegistryPort` 替代 raw `tool_lookup` callable。

### 允许修改

1. `composition/runtime_composition.py` — `_RealTools` 接受 `ToolRegistryPort`
2. `tests/composition/test_native_object_graph.py` — 验证

### Target Tests

- [ ] `_RealTools.execute("Read", ...)` 通过 ToolRegistryPort 找到 BaseTool
- [ ] 未知工具 → ToolFailure(error_type=TOOL_NOT_FOUND)

---

## 4.23 T20 — tool_use_id → ToolEvidence 追踪

### 目标

`ToolEvidence` 增加 `tool_use_id` 字段，StepLoop 传入。

### 允许修改

1. `runtime_core/outcome.py` — `ToolEvidence` 增加 `tool_use_id`
2. `runtime_core/step_loop.py` — 传入 `tool_use_id`
3. `tests/runtime_core/test_hook_tool_loop.py` — 验证

### Target Tests

- [ ] ToolEvidence.tool_use_id == ToolCall.id

---

## 4.24 T21 — Tool-level token cost tracking

### 目标

每个 tool 执行后记录 token cost 增量。

### 允许修改

1. `runtime_core/step_loop.py` — tool batch 前后 token diff
2. `tests/runtime_core/test_real_model_loop.py` — 验证

---

## 4.25 T22 — Tool execution audit log

### 目标

每次 tool 执行记录到 audit_log。

### 允许修改

1. `listeners/audit_projection.py` — 增加 `tool.executed.v1` 事件处理
2. `tests/listeners/test_stats_audit_projection.py` — 验证

---

## 4.26 T23 — Old ToolExecutionPipeline 标记 DEPRECATED

### 目标

`core/tool_execution.py` 添加 DEPRECATED 标记，指向 Native 路径。

### 允许修改

1. `core/tool_execution.py` — DEPRECATED 注释
2. `tests/test_old_tool_deprecation.py` — 验证

---

## 4.27 T24 — _RealTools 接入 ToolRegistryPort

### 目标

`assemble()` 接受 `ToolRegistryPort` 并注入 `_RealTools`。

### 允许修改

1. `composition/runtime_composition.py` — `assemble(tool_registry_port=...)`
2. `run_server.py` — 传入真实 ToolRegistryPort
3. `tests/composition/test_native_object_graph.py` — 验证

---

## 4.28 T25 — run_server.py 注入 tool_registry

### 目标

production 入口创建 ToolRegistryPort 并传入 `assemble()`。

### 允许修改

1. `run_server.py` — `assemble(db_path, tool_registry_port=...)`
2. `tests/integration/test_native_run_e2e.py` — 验证

---

## 4.29 T26 — E2E 验证矩阵

### 目标

覆盖所有工具类型的端到端测试。

### 允许修改

1. `tests/integration/test_tool_e2e_matrix.py`（新文件）— E2E 矩阵
2. `tests/test_runtime_architecture_gates.py` — 增加工具检查

### 验收清单

- [ ] Read tool → success + evidence
- [ ] Write tool → success + evidence + file_touched
- [ ] Bash tool → success
- [ ] MCP tool → mcp__ prefix resolution
- [ ] Tool timeout → ToolErrorType.TIMEOUT + retry
- [ ] Tool permission denied → ToolErrorType.PERMISSION_DENIED
- [ ] PreToolUse deny → tool not executed
- [ ] PostToolBatch → hook triggered
- [ ] tool_choice: "any" → always calls a tool
- [ ] 5-tool parallel batch → all execute in parallel

---

## 4.30 T27 — Old tool 路径物理删除

### 目标

删除 `core/tool_execution.py` 中 Native 已覆盖的路径。

### 允许修改

1. `core/tool_execution.py` — 删除 DEPRECATED 路径
2. 更新所有 importer

---

# 5. 阶段依赖图

```text
T0 ──→ T1 ──→ T14 ──→ T15
T2 ──→ T3 ──→ T15
T4 ──→ T17 ──→ T18 ──→ T19 ──→ T24 ──→ T25
T5 ──→ T6
T7 ──→ T8
T9 ──→ T10
T11 ──→ T12 ──→ T13
T16 (独立)
T20 (独立)
T21 (独立)
T22 (独立)
T23 ──→ T27

T24 ──→ T25 ──→ T26 ──→ T27
```

可以并行的组：
- {T0, T2, T4, T5, T7, T9, T11, T16}
- {T1, T3, T6, T8, T10, T12, T13, T14, T15, T17, T18, T19, T20, T21, T22}
- {T23, T24, T25, T26, T27}（必须串行）

---

> **文档结束。执行路线：T0 → T1 → ... → T27。共 28 阶段，19 人日。**
