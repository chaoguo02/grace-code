# 双 Runtime 迁移完成 + 未闭环修复执行规划书

> 文档版本：1.0.0
> 创建日期：2026-08-03
> 当前基线：双 Runtime 并行迁移中间态（约 65-70% 完成）
> 目标状态：迁移完成、重复/过时实现移除、未闭环点闭合、沙箱缺口明确记录（本次不动 Docker）
> 前置审计：`RUNTIME_HOOKS_EVENTBUS_38_TO_90_CC_NATIVE_EXECUTION_PLAN_2026-08-02.md`（G0-G44 大迁移的局部收敛）
> 状态：**APPROVED FOR IMPLEMENTATION — 尚未执行本文阶段**
> 核心原则：真实迁移而非假迁移、机器证据优先、每阶段 Before Test 先失败、沙箱只做 LocalRuntime

---

# 0. 使用说明与不可违背的施工纪律

## 0.1 本文解决什么问题

本文不是继续给当前中间态追加补丁。它定义一条可停、可验收、可证明的收敛路线，把当前状态从：

```text
新路径（runtime_core/）框架已构建但未接真实后端
  + 旧路径（SessionRuntime + ToolExecutionPipeline）仍是实际执行入口
  + MCP 存在 2 个工厂不可达的死 Bridge 类
  + 两套 JSON Schema 校验重叠，1 个内联校验函数是死代码
  + ToolRegistryAdapter 只有测试用，生产从未实例化
  + 文件写非原子（O_TRUNC 原地截断）
  + Checkpoint 机制只写不读（db_path 永远 None）
  + Artifact 无跨进程持久化接线
  + _ROOT_REMOVAL_PATTERNS 声称已同步但漏了 2 项
  + RiskLevel 枚举被赋值但无任何运行时消费者
  + 沙箱 env 白名单是死代码；seccomp/AppArmor 缺口未记录
```

收敛为：

```text
重复实现和过时实现全部移除（以 import failure + rg 零命中为证据）
  + 未闭环点全部接线（以 Before Test 先失败、After Test 通过为证据）
  + 迁移真实性由机器可验证的测试判定，而非注释/commit message
  + 沙箱缺口以 S1-S3 清单显式记录，Docker 沙箱闭环单独立项
```

## 0.2 规范性关键词

- **MUST / 必须**：不满足即阶段失败。
- **MUST NOT / 禁止**：出现即停止本阶段。
- **SHOULD / 应当**：只能通过新 ADR 和重新执行本计划偏离。
- **真实迁移**：删除旧实现后，测试仍绿（迁移不依赖旧代码继续存在）。
- **假迁移**：只加 deprecation marker / 注释 / 空 shim，旧 import 仍可用。本文全部阶段禁止假迁移。
- **Before Test**：先写并运行，必须稳定失败且失败原因正是目标缺陷；若不失败则测试无效。
- **Target Test**：实现后必须通过的测试集合。
- **回归切片**：阶段涉及文件的既有测试，确保不破坏现有行为。
- **Authority**：对某项状态/决策做最终裁决的唯一组件。

## 0.3 绝对禁止

1. 禁止删除旧文件后添加 re-export / shim / 兼容别名让测试变绿。
2. 禁止用 deprecation marker、commit message、注释、清单勾选证明迁移完成。
3. 禁止以"删除后测试绿"作为唯一证据；必须同时提供 `import failure` 和 `rg` 零命中。
4. 禁止一个阶段修改超过 3 个文件；需要第 4 个文件时拆阶段。
5. 禁止 Before Test 不先失败就进入实现（测试必须能暴露旧缺陷）。
6. 禁止用 sleep、随机 retry、放宽断言、提高阈值让测试变绿。
7. 禁止为绕过失败测试而降低原有断言强度。
8. 禁止本次迁移触碰 Docker 沙箱隔离逻辑（S1-S3 独立立项）；仅允许 U4 修 `_workspace_root` 属性缺失。
9. 禁止在未运行回归切片的情况下进入下一阶段。
10. 禁止把"应该接线但会影响权限语义"的改动悄悄并入清理阶段；语义改动必须显式列出（如 M4）。

## 0.4 施工循环

每个阶段严格执行：

```text
完整读取本阶段
  -> 检查只涉及允许文件
  -> 运行 Before Test，确认测试能暴露旧缺陷
  -> 实现本阶段完整契约
  -> 运行 Target Tests
  -> 运行回归切片
  -> grep/import-failure 静态门
  -> git diff --check
  -> 输出阶段报告
  -> 停止，等待确认（或由执行者连续进入下一阶段但保留每阶段报告）
```

遇到以下任一情况立即停止：

- 需要第 4 个文件；
- 需要兼容层/shim 才能通过；
- 删除旧文件后只能通过重新添加旧 import 修复测试；
- Before Test 在未修改生产代码时不失败；
- 无法说明删除对象的证据链（谁还在 import 它）；
- 需要改动 Docker 沙箱隔离行为（除 U4）。

## 0.5 版本治理

- 改变 Authority、删除目标、阶段顺序或验证标准属于 MAJOR 变更，必须重新执行本计划并升级版本号。
- 阶段报告必须写明本文版本；版本不一致时停止。

---

# 1. Step 1 — 现状调研与严肃质询

## 1.1 实际执行路径快照（2026-08-03 基线）

| 入口 | 实际路径 | 结论 |
|---|---|---|
| `entry/cli.py run/chat` | `SessionRuntime` → `Agent.run()` → `StreamingExecutor` → `ToolExecutionPipeline` | 100% 旧路径 |
| Web `agent_service.chat()` | `ChatPipeline._execute_native()` → `AgentRuntime.run()` 但 `tool_registry=None` → H2 假数据 | 形式上新路径，实为空壳 |
| Web submit run | `RunCoordinator.submit()` | 真实新路径（提交层） |

关键事实（来自本轮代码级探索）：

- `core/base.py:983` 的 `ToolRegistry.execute_tool()` 硬导入并使用 `ToolExecutionPipeline`（T23/T27 DEPRECATED 但仍在执行）。
- `composition/runtime_composition.py` 的 `assemble()` 在 `server/main.py:378` 被调用时 `llm_backend=None`、`tool_registry=None`，导致 `_RealLLM`/`_RealTools` 走 H1/H2 假数据模式。
- `agent/session/checkpoint.py` 的 `CheckpointManager.restore()` 全局无调用者；`_capture_turn_checkpoint()` 因 `db_path` 永远 None 永远 early-return。
- `agent/mcp/client.py` 5 个 Bridge 类中 `HttpMCPBridge`（DEPRECATED）和 `SseMCPBridge`（继承前者）工厂不可达。
- `infrastructure/tool_registry_adapter.py` 仅出现在注释和测试中。

## 1.2 五项严肃质询

### Q1：哪些是"重复实现"，应该移除？

- 两套 JSON Schema 校验：`llm/tool_call_validator.py` 手工 required 循环 + `core/schema_validator.py` jsonschema（后者已含 required）。→ M2 收敛为单一来源。
- `_validate_json_value()`（`tool_call_validator.py:144-213`）从未被调用。→ M2 删除。
- `ToolRegistryAdapter` 适配到 `ToolRegistryPort`，但 `RuntimePorts` 不含该端口，生产不用。→ M3 删除。
- 两个 blocked-patterns 列表高度重叠但语义不同（一个"拒绝"，一个"绕过 bypass 后确认"）。→ 不合并，M6 补同步。

### Q2：哪些是"过时实现"，应该删除？

- `HttpMCPBridge`（~150 行 DEPRECATED）+ `SseMCPBridge`（~100 行）工厂不可达。→ M1 删除。
- `SANDBOX_ENV_WHITELIST_PREFIXES` 常量从未被 `_start_container()` 使用。→ M7 删除。
- `entry/renderer.py:_risk_color()` 从未被调用。→ M8 删除。

### Q3：哪些是"未闭环"，应该接线？

- `CheckpointManager.restore()` 无人调用。→ U1 接线（默认禁用）。
- Artifact `storage_dir` 未接线，跨进程重启丢失。→ U3 接线。
- `allowed_write_paths` 在 native 路径（`_execute_via_registry` 直接调 `tool.execute`）被绕过。→ M4 接线。
- `assemble()` 未传真实 backend/registry，Native 路径是空壳。→ M9 接线。
- `DockerRuntime` 缺 `_workspace_root`，与 ShellTool 组合永远失败。→ U4 修复。

### Q4：迁移真实性如何判定（真实 vs 假迁移）？

本文采用"三重证据"判定标准，缺一不可：

1. **Before Test 先失败**：测试在未修改生产代码时必须失败，且失败原因正是目标缺陷。
2. **删除后 import failure + rg 零命中**：旧实现必须不可再被 import（`python -c "import X"` 抛 ModuleNotFoundError），生产代码 `rg` 零命中。
3. **Target Tests + 回归切片全绿**：删除/接线后既有行为不回归。

任何仅满足"测试绿"而不满足 2 的迁移，判定为假迁移，必须回退。

### Q5：沙箱缺口的边界在哪里？

- 本次只做 LocalRuntime 相关收敛（M6 patterns 同步、M7 死代码、U4 `_workspace_root`）。
- Docker 沙箱隔离（seccomp/AppArmor、网络白名单、OOM 检测、env 白名单实装、容器行为测试）属于独立"沙箱闭环"工作包，以 S1-S3 清单记录在本文，本次不实现。

---

# 2. Step 2 — 目标设计规范

## 2.1 目标态边界

| 关注点 | 现状 | 目标态 |
|---|---|---|
| 工具执行权威 | 双路径（旧 pipeline 实际运行，native 空壳） | 单一 native 工具执行链（`StepLoop` → `_RealTools` → registry），旧 `ToolExecutionPipeline` 仍是 CLI 路径（P27 独立），但不再被误用 |
| 工具注册 | `ToolRegistry` + adapter 双表示 | 只用 `ToolRegistry`，删 adapter |
| 参数校验 | 双校验器 | 单一 `SchemaValidator`（jsonschema）为唯一标准 |
| MCP 传输 | 5 类 Bridge，2 个死 | 3 类可路由 Bridge（stdio/streamable-http/ws） |
| 文件写入 | O_TRUNC 原地写 | tmp + `os.replace()` 原子写，保留 symlink/TOCTOU 防护 |
| Checkpoint | 只写不读（且写不生效） | 默认禁用；显式配置后 capture/restore 闭环 |
| Artifact | 内存 + 可选磁盘 | 启动接线 `storage_dir`，跨进程可恢复 |
| 沙箱 | env 白名单死代码 + Docker 缺口未记录 | 死代码移除，缺口以 S1-S3 显式记录 |

## 2.2 删除对象清单

| 删除对象 | 阶段 | 删除后证据 |
|---|---|---|
| `HttpMCPBridge` 类 | M1 | `python -c "from agent.mcp.client import HttpMCPBridge"` 失败 |
| `SseMCPBridge` 类 | M1 | 同上 |
| `test_http_bridge_resource_override` 等 3 个损坏测试 | M1 | 从测试文件删除 |
| 手工 required 检查循环 | M2 | `rg missing_required llm/tool_call_validator.py` 零命中（保留错误类型） |
| `_validate_json_value()` | M2 | 删除后无 import 引用 |
| `infrastructure/tool_registry_adapter.py` | M3 | `python -c "import infrastructure.tool_registry_adapter"` 失败 |
| `tests/infrastructure/test_tool_registry_adapter.py` | M3 | 文件删除 |
| `SANDBOX_ENV_WHITELIST_PREFIXES` | M7 | `rg SANDBOX_ENV_WHITELIST_PREFIXES` 零命中 |
| `_risk_color()` | M8 | `rg _risk_color` 零命中 |

---

# 3. Step 3 — 分阶段开发路线图

## 3.1 总体阶段表

每阶段最多修改 3 个文件。按执行顺序排列（依赖前置）。总工时约 8 人日。

| 阶段 | 目标 | 依赖 | 工时 | 类型 |
|---|---|---:|---:|---|
| M6 | `_ROOT_REMOVAL_PATTERNS` 同步 + 测试 | None | 0.5 | 修复 |
| M7 | 删除 `SANDBOX_ENV_WHITELIST_PREFIXES` 死代码 | M6 | 0.3 | 删除 |
| M1 | 删除 `HttpMCPBridge`/`SseMCPBridge` + 测试 | M6 | 0.5 | 删除 |
| M2 | `validate_tool_calls` 校验去重 + 删死函数 | M1 | 0.5 | 去重 |
| M3 | 删除 `ToolRegistryAdapter` + 测试 | M2 | 0.3 | 删除 |
| M5 | 文件写原子化 | M3 | 1.0 | 修复 |
| U3 | Artifact `storage_dir` 接线 | M5 | 0.5 | 接线 |
| U4 | `DockerRuntime._workspace_root` 属性 | U3 | 0.3 | 修复 |
| M4 | native 路径接入 `PolicyAwareToolRegistry` | U4 | 1.0 | 接线 |
| M9 | `assemble()` 传真实 backend/registry | M4 | 1.0 | 接线 |
| M8 | 删除 `_risk_color` 死代码 + 注释 | M9 | 0.5 | 删除 |
| U1 | Checkpoint 接线 | M8 | 1.5 | 接线 |

## 3.2 阶段通用报告模板

```text
Phase: Mxx/Uxx
Document version: 1.0.0
Baseline SHA: ...
Files changed: [必须 <= 3]
Before test: 命令 + 失败原因
Implementation summary: 精确说明移除/接线变化
Target tests: 命令 + passed/failed 数量
Regression slice: 命令 + 结果
Deletion proof: import failure + rg 零命中
STOP: 等待确认，不进入下一阶段
```

## 3.3 阶段详情

### M6 — `_ROOT_REMOVAL_PATTERNS` 同步

允许修改：

1. `hitl/pipeline.py`
2. `tests/test_p1_32_bash_sandbox.py`

Before Test（必须先失败）：

- 新增断言：`_ROOT_REMOVAL_PATTERNS` 包含 `":(){:|:&};:"` 和 `"chown -R"`。当前列表缺这 2 项，断言失败。

实现：

- `hitl/pipeline.py:909-919` 的 `_ROOT_REMOVAL_PATTERNS` 补充 fork bomb 和 `chown -R`。
- 保留与 `_BLOCKED_PATTERNS` 的语义差异注释（一个"拒绝执行"，一个"绕过 bypass 后强制交互确认"）。

Target Tests：

```powershell
python -m pytest tests/test_p1_32_bash_sandbox.py -q
```

回归切片：`tests/test_shell_safety.py`。

### M7 — 删除 `SANDBOX_ENV_WHITELIST_PREFIXES` 死代码

允许修改：

1. `core/process.py`

Before Test：

- `rg -n "SANDBOX_ENV_WHITELIST_PREFIXES" core/process.py tools/ entry/` 确认除定义处外零引用。若存在生产引用则本阶段失败（说明不是死代码）。

实现：

- 删除 `core/process.py:700` 附近的 `SANDBOX_ENV_WHITELIST_PREFIXES` 常量（6 行）。

Deletion proof：

```powershell
rg -n "SANDBOX_ENV_WHITELIST_PREFIXES" .
# 必须零命中
```

回归切片：`python -c "import core.process"`。

### M1 — 删除 `HttpMCPBridge` / `SseMCPBridge`

允许修改：

1. `agent/mcp/client.py`
2. `tests/test_mcp_normalization.py`

Before Test：

- 当前 `tests/test_mcp_normalization.py` 有 3 个已损坏测试（`test_http_bridge_resource_override`、`test_sanitize_env_strips_api_keys`、`test_sanitize_env_preserves_config_env`）。运行确认这 3 个失败——它们是"旧实现损坏"的现存证据。

实现：

- 删除 `agent/mcp/client.py:778-932`（`HttpMCPBridge`）和 `938-1041`（`SseMCPBridge`）整类。
- `WsMCPBridge` 独立继承 `MCPToolBridge`，不受影响；保留。
- `tests/test_mcp_normalization.py` 删除上述 3 个损坏测试。

Deletion proof：

```powershell
python -c "from agent.mcp.client import HttpMCPBridge"  # 必须 ModuleNotFoundError
python -c "from agent.mcp.client import SseMCPBridge"   # 必须 ModuleNotFoundError
```

Target Tests：

```powershell
python -m pytest tests/test_mcp_normalization.py tests/test_mcp_streamable.py -q
```

回归切片：`tests/test_mcp_lifecycle.py`、`tests/test_weather_mock_mcp.py`。

### M2 — `validate_tool_calls` 校验去重

允许修改：

1. `llm/tool_call_validator.py`
2. `core/schema_validator.py`
3. `tests/test_schema_validator.py`

Before Test：

- 现有 `tests/test_schema_validator.py` 对缺失必填参数返回 `valid=False` 的断言，在删除手工检查后仍应通过（因为 `SchemaValidator` 的 jsonschema 已含 required）。先运行确认该测试当前绿，作为基线。

实现：

- `llm/tool_call_validator.py`：删除 line 69-82 的手工 required 循环（Check 2），保留 Check 1（工具名存在性）、Check 2b（SchemaValidator）、Check 3（重复检测）。`missing_required` 错误类型保留由 `_invalid_params` 路径产生。
- `llm/tool_call_validator.py`：删除 `_validate_json_value()`（line 144-213，完全死代码）。
- `core/schema_validator.py` 的 `format_errors_for_llm()`：对 `keyword == "required"` 的错误加友好格式 `Missing required parameter '{field}'. Please retry with the required parameter.`，保留原本对 LLM 的引导性。

Deletion proof：

```powershell
rg -n "_validate_json_value" llm/
# 必须零命中
```

Target Tests：

```powershell
python -m pytest tests/test_schema_validator.py tests/test_tool_execution_pipeline.py -q
```

注意：`tests/test_tool_execution_pipeline.py` 当前有 5 个失败（`tool_availability_guard` 关键字），它们是既存损坏测试，不属于本阶段范围；本阶段只要求本阶段相关测试绿，损坏测试清单记录到最终审计。

回归切片：`tests/test_unified_execution.py`。

### M3 — 删除 `ToolRegistryAdapter`

允许修改：

1. 删除 `infrastructure/tool_registry_adapter.py`
2. 删除 `tests/infrastructure/test_tool_registry_adapter.py`
3. `run_server.py`

Before Test：

- `rg -n "ToolRegistryAdapter" --glob "!tests/**" .` 确认生产代码零引用（仅注释）。若有生产引用则本阶段失败。

实现：

- 删除两个文件。
- `run_server.py:37` 附近注释中提及 `ToolRegistryAdapter` 的行删除。

Deletion proof：

```powershell
python -c "import infrastructure.tool_registry_adapter"  # 必须 ModuleNotFoundError
rg -n "ToolRegistryAdapter" .  # 必须零命中
```

回归切片：`pytest tests/composition/test_native_object_graph.py -q`。

### M5 — 文件写原子化

允许修改：

1. `core/base.py`
2. `tools/file_tool.py`
3. `tools/file_edit_tool.py`

Before Test：

- 新增原子写测试（`tests/test_read_before_edit_cache.py` 或同目录新测试）：mock `os.write` 抛异常中断写流程，断言目标文件保持原内容、无 `.tmp` 残留。当前 `safe_open_for_write` 是 O_TRUNC 原地写，先写内容，若中断会留坏文件 → 测试失败。

实现：

- `core/base.py`：新增 `atomic_write_bytes(full_path: str, data: bytes) -> str | None`，内部：
  - 复用 symlink 检查（Windows `is_symlink()`，POSIX `O_NOFOLLOW` 语义保留在写 tmp 前检查）；
  - 写 `.{name}.tmp.{pid}` 临时文件；
  - `os.replace(tmp, full_path)` 原子替换（Windows 下 `os.replace` = `MoveFileExW(MOVEFILE_REPLACE_EXISTING)`，原子）。
- `tools/file_tool.py`（FileWriteTool.execute 写路径）：将 `safe_open_for_write()` + `os.write()` + `os.close()` 替换为 `atomic_write_bytes()`。
- `tools/file_edit_tool.py`（FileEditTool.execute 编辑写路径）：同上替换。
- `safe_open_for_write()` / `safe_create_file()` 保留（新建文件 `safe_create_file` O_EXCL 无截断问题，不改）。

Target Tests：

```powershell
python -m pytest tests/test_read_before_edit_cache.py tests/test_shell_safety.py -q
```

回归切片：`tests/test_tool_result_contract.py`、`tests/test_e2e_core.py`。

### U3 — Artifact `storage_dir` 接线

允许修改：

1. `agent/core.py`
2. `tests/test_artifact_layer.py`

Before Test：

- 新增测试：构造 `AgentConfig(artifact_storage_dir=tmp)`，start agent，存储一个 artifact，断言磁盘上出现 `art_*.json`。当前 `_artifact_store` 初始化后未调 `set_storage_dir()` → 磁盘无文件，测试失败。

实现：

- `agent/core.py` 找到 `_artifact_store` 初始化位置，若 `self._cfg.artifact_storage_dir` 非空则调用 `self._artifact_store.set_storage_dir(self._cfg.artifact_storage_dir)`。
- `state_paths.py` 已有 `artifacts` 属性（`root/"artifacts"`）可作为调用方默认值。

Target Tests：

```powershell
python -m pytest tests/test_artifact_layer.py -q
```

### U4 — `DockerRuntime._workspace_root` 属性

允许修改：

1. `core/process.py`
2. `tests/test_shell_safety.py`

Before Test：

- 新增测试：`ShellTool(runtime=mock_docker_runtime)` 调 `_execute_parameterized`，断言不返回 "Workspace root is not set"。当前 DockerRuntime 无 `_workspace_root` → `getattr` 返回 None → early-return 失败。

实现：

- `core/process.py` 的 `DockerRuntime.__init__`：添加 `self._workspace_root = Path(CONTAINER_WORKDIR)`（即 `/workspace`）。

Target Tests：

```powershell
python -m pytest tests/test_shell_safety.py -q
```

回归切片：`python -c "from core.process import DockerRuntime; DockerRuntime('/tmp')._workspace_root"`。

### M4 — native 路径接入 `PolicyAwareToolRegistry`

允许修改：

1. `composition/runtime_composition.py`
2. `tests/composition/test_native_object_graph.py`

Before Test：

- 新增测试：构造带 `allowed_write_paths={workspace_tmp}` 的 `PolicyAwareToolRegistry`，native `_RealTools` 执行一个写 workspace 外路径的工具，断言返回拒绝。当前 `_RealTools.execute()` 直接 `_execute_via_registry` → `tool.execute()`，绕过策略 → 测试失败。

实现：

- `composition/runtime_composition.py` 的 `_RealTools.execute()`：检测 `self._registry` 是否为 `PolicyAwareToolRegistry`（或其具有 `execute_tool` 方法），是则通过 `self._registry.execute_tool(tool_name, params_dict)` 执行；否则保持现有 `_execute_via_registry` 路径。
- 不修改 `_execute_via_registry()` 函数本身（保持纯工具执行语义）。

Target Tests：

```powershell
python -m pytest tests/composition/test_native_object_graph.py -q
```

回归切片：`tests/test_tool_execution_pipeline.py`（本阶段只要求不新增失败）。

### M9 — `assemble()` 传真实 backend/registry

允许修改：

1. `server/main.py`
2. `composition/runtime_composition.py`（如需要）
3. `tests/integration/test_native_run_e2e.py`

Before Test：

- `rg -n "H2 fake output" composition/ server/` 确认当前假数据字符串存在（这是"Native 空壳"的证据）。

实现：

- `server/main.py:378` 的 `assemble(db_path)` 改为 `assemble(db_path, llm_backend=service._backend, tool_registry=service._registry)`。
- 确认 `composition/runtime_composition.py` 的 `assemble()` 签名已接受这两个参数并传入 `_RealLLM`/`_RealTools`；如缺则补。
- 不删除 `ToolExecutionPipeline`，不改 CLI 路径（CLI 的 P27 完整切换独立立项）。

Target Tests：

```powershell
python -m pytest tests/integration/test_native_run_e2e.py -q
rg -n "H2 fake output" composition/ server/  # 执行路径上不再出现
```

回归切片：`tests/composition/test_native_object_graph.py`。

风险：接线后 Web `_execute_native()` 路径会用真实 LLM/tool。若该路径存在其他未覆盖缺陷，本阶段暴露并记录，不掩盖。

### M8 — 删除 `_risk_color` 死代码

允许修改：

1. `entry/renderer.py`
2. `core/types.py`

Before Test：

- `rg -n "_risk_color" entry/ tests/` 确认零调用。若有调用则本阶段失败。

实现：

- `entry/renderer.py` 删除 `_risk_color()` 函数。
- `core/types.py:164` 的 `RiskLevel` 枚举上方加注释：`# Used as declarative metadata by tools; not yet consumed by PermissionPipeline (see MIGRATION_GAP_CLOSURE_EXECUTION_PLAN).`

Deletion proof：

```powershell
rg -n "_risk_color" .  # 必须零命中
```

回归切片：`pytest tests/test_prompt_renderer.py -q`。

### U1 — Checkpoint 接线

允许修改：

1. `agent/agent_config.py`
2. `agent/core.py`
3. `tests/test_checkpoint_roundtrip.py`（新增）

Before Test：

- 新增 `tests/test_checkpoint_roundtrip.py`：构造 `AgentConfig(checkpoint_db_path=tmp)`，断言 `CheckpointManager(tmp)` 可被 `_capture_turn_checkpoint` 写入、`restore` 可读出、`IdempotentToolCache` 包含先前 tool results。当前 `AgentConfig` 无 `checkpoint_db_path` 字段 → 测试失败。

实现：

- `agent/agent_config.py`：添加 `checkpoint_db_path: str = ""`（空 = 禁用，zero overhead）。
- `agent/core.py` 的 `run()` 初始化阶段：若 `checkpoint_db_path` 非空，则 `CheckpointManager(db_path).restore(session_id)` 喂给新 `IdempotentToolCache`。
- `agent/core.py` 的 `_capture_turn_checkpoint()`：`getattr(self._cfg, "checkpoint_db_path", None)` 在加字段后即生效；若该函数硬编码了别处读取需修正为读 cfg 字段。
- 默认 `checkpoint_db_path = ""`，不产生任何 SQLite I/O。

Target Tests：

```powershell
python -m pytest tests/test_checkpoint_roundtrip.py -q
```

回归切片：`tests/test_compaction_trigger.py`（如有）、`tests/test_e2e_core.py`。

---

# 4. Step 4 — 机器可验证验收标准

- [ ] **AC-1 Critical**：M1 后 `from agent.mcp.client import HttpMCPBridge` 失败，`rg "HttpMCPBridge|SseMCPBridge"` 生产零命中。
- [ ] **AC-2 Critical**：M2 后 `rg "_validate_json_value"` 零命中；缺失必填参数仍返回 `valid=False` 且错误含友好提示。
- [ ] **AC-3 Critical**：M3 后 `import infrastructure.tool_registry_adapter` 失败，`rg "ToolRegistryAdapter"` 零命中。
- [ ] **AC-4 Critical**：M5 后写入中断不留残文件，目标文件保持原内容，`.tmp` 无残留。
- [ ] **AC-5 Critical**：M7 后 `rg "SANDBOX_ENV_WHITELIST_PREFIXES"` 零命中。
- [ ] **AC-6 Critical**：M8 后 `rg "_risk_color"` 零命中。
- [ ] **AC-7 Critical**：U1 后默认 `checkpoint_db_path=""` 零 SQLite I/O；配置后 capture/restore 闭环测试绿。
- [ ] **AC-8 Required**：M4 后 `allowed_write_paths` 在 native 路径被强制；越界写被拒绝。
- [ ] **AC-9 Required**：M9 后 `rg "H2 fake output"` 在执行路径零命中；native E2E 用真实工具。
- [ ] **AC-10 Required**：M6 后 `_ROOT_REMOVAL_PATTERNS` 含 fork bomb + `chown -R`。
- [ ] **AC-11 Required**：U3 后配置 `artifact_storage_dir` 的 agent 跨进程重启 artifact 可恢复。
- [ ] **AC-12 Required**：U4 后 `DockerRuntime._workspace_root == /workspace`，ShellTool 不再误报。
- [ ] **AC-13 Required**：每阶段 Before Test 先失败记录可追溯（阶段报告含失败原因）。
- [ ] **AC-14 Required**：全阶段完成后全量 `pytest tests/ -q --ignore=tests/test_smoke_e2e.py`，除既存损坏测试外无新增失败；损坏测试清单单独记录。

# 5. 风险登记表

| 风险 | 等级 | 触发信号 | 预防 | 处置 |
|---|---|---|---|---|
| 假迁移 | Critical | 删除后测试靠 shim 绿 | import failure + rg 零命中证据 | 回退该阶段，不加 shim |
| M9 接线暴露 native 空壳缺陷 | High | `_execute_native()` 报错 | 集成测试先行 | 记录缺陷，修复或回退 M9 |
| M2 删手工校验改变错误消息 | Medium | LLM 自我纠正退化 | `format_errors_for_llm` 保留 required 友好提示 | 调整消息格式 |
| 原子写引入 TOCTOU 回归 | Medium | symlink 写入漏检 | `atomic_write_bytes` 复用 symlink 检查 | 回退 M5 |
| 损坏测试干扰回归判定 | Medium | 失败测试被误判为本阶段引入 | 阶段报告区分"既存损坏"与"新增失败" | 基线清单记录 |
| U1 接线引入每 turn I/O | Low | 开启后性能下降 | 默认禁用 | 文档说明启用条件 |

# 6. 最终 Definition of Done

只有同时满足以下条件，本次迁移才算完成：

1. M1/M3/M7/M8 的删除对象全部不可 import、`rg` 零命中。
2. M2 校验链单一权威（`SchemaValidator`），友好错误保留。
3. M5 文件写原子化，TOCTOU/symlink 防护不回归。
4. M4/M9 让 native 工具执行链接入策略与真实后端，无假数据。
5. U1/U3/U4 三个未闭环点接线完成，各有 Before Test 证据。
6. M6 双 pattern 列表语义分开、内容同步。
7. AC-1～AC-14 全部通过，阶段报告可追溯。
8. 沙箱 Docker 缺口以 S1-S3 清单显式记录在本文，单独立项。

# 7. 沙箱缺口清单（本次不实现，独立立项）

| 项 | 缺口 | 目标阶段 |
|---|---|---|
| S1 | seccomp/AppArmor 系统调用过滤缺失 | 沙箱闭环 |
| S2 | 网络白名单只有全开/全关，无域名级白名单 | 沙箱闭环 |
| S3 | Docker 容器无行为测试（写 /etc 是否被拒、rm -rf 是否隔离） | 沙箱闭环 |
| S4 | OOM 退出码(137) 未检测 | 沙箱闭环 |
| S5 | `_start_container()` 未实装 env 白名单过滤 | 沙箱闭环 |

# 8. 下一位执行模型的第一条指令

```text
你只能执行 docs/MIGRATION_GAP_CLOSURE_EXECUTION_PLAN_2026-08-03.md 的阶段 M6。
开始前完整读取第 0、1、2、3.2、3.3/M6 节。
只允许修改 M6 列出的两个文件。
先写并运行 Before Test，必须稳定复现 _ROOT_REMOVAL_PATTERNS 缺 2 项；如果测试在未修改生产代码时不失败，说明测试无效，停止并报告。
实现时禁止合并两个 pattern 列表的语义，禁止改动 Docker 隔离逻辑。
完成后运行 M6 Target Tests、回归切片、git diff --check，并按第 3.2 节模板报告。
报告后立即停止，不得进入 M7。
如果需要第 4 个文件、修改数据库 schema、使用 Any、增加 sleep/retry 或降低断言，立即停止并报告阻塞。
```
