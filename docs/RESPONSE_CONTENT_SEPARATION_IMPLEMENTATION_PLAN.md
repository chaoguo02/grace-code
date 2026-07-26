# Grace Code 回答正文与运行状态分离：详细实施计划

> 文档状态：已实施（2026-07-26 完成缺口补齐与回归）  
> 编写日期：2026-07-26  
> 目标问题：`[UNVERIFIED ...]`、验证结果、Git 状态、终止原因等 Runtime 信息被拼进 assistant 回答正文，并继续污染消息、Session、Plan、Subagent 和 WebSocket 数据。

---

## 1. 结论与目标

Grace Code 当前把 `RunResult.summary` 同时当作以下几种不同概念使用：

1. 模型生成的最终回答正文；
2. Session 列表中的运行摘要；
3. Run 的最终结果；
4. Plan 正文；
5. Subagent 回传文本；
6. WebSocket 完成事件的显示内容；
7. 失败、取消和验证状态的展示载体。

这些概念不能共用一段可变字符串。最终目标是：

```text
assistant 正文 = 模型实际生成的用户可见回答

运行状态 = status / termination_reason / error
验证状态 = verification_status / verification_reason / checks
工作区变化 = workspace_delta / changed_files / patch
执行指标 = steps / tokens / duration
交互状态 = approvals / plan_state
```

### 1.1 必须遵守的核心约束

- 不允许 Runtime 向 assistant 正文添加前缀、后缀或诊断段落。
- `summary` 必须保持为干净的模型输出；如果后续重命名，建议使用 `assistant_content` 或 `final_content`。
- 测试失败、测试未运行、Git diff、终止原因必须以结构化字段传递。
- `session_messages.role == "assistant"` 只能保存对话正文，不能保存 Runtime notice。
- Plan 文本、Plan contract 和运行验证结果必须分离。
- WebSocket 实时结果和 `/timeline` 重放结果必须使用相同的数据结构。
- 刷新页面后，状态卡片必须能够从后端恢复，不能只依赖内存中的 WebSocket 事件。
- 历史清理只能删除 Grace Code 能够确定是自己生成的固定前缀，不能模糊删除用户或模型的合法文本。

### 1.2 非目标

本次改造不负责：

- 重做完整 Chat UI；
- 修改 HITL 的权限决策语义；
- 修改 Build、Plan、Explore 的 Agent 能力；
- 修改测试是否通过的判定标准；
- 删除审计日志或 trace event；
- 隐藏真实错误。

本次只改变“数据放在哪里”和“前端如何展示”，不降低审计能力。

---

## 2. 当前污染链路

### 2.1 污染源

文件：`agent/core.py`

关键位置：

- `_GitState`：约第 354 行；
- `_refresh_git_state()`：约第 526 行；
- `_build_run_result()`：约第 788 行；
- 拼接 `[UNVERIFIED ...]`：约第 821–843 行；
- `CompletionFacts` 构造：约第 1922–1928 行。

当前逻辑大意：

```python
_v_needs_tag = verification_status in (
    UNVERIFIED,
    UNAVAILABLE,
    FAILED,
)

_needs_unverified_tag = git_state.has_changes or (
    had_any_write and not is_git_repo
)

if success and _needs_unverified_tag and _v_needs_tag:
    summary = f"[UNVERIFIED ...]\n\n{summary}"
```

主要问题：

1. `summary` 在构造 `RunResult` 之前被修改，结构化数据层失去干净正文。
2. `git_state.has_changes` 代表当前工作树相对基准 commit 的变化，并不可靠地代表“本轮 Agent 产生的净变化”。
3. 工作区原本存在未提交文件时，只读回答也可能被认定为“代码发生变化”。
4. `VerificationStatus` 和 `VerificationReason` 已经存在，没有必要再次编码进字符串。

### 2.2 污染扩散点

#### A. 主 Session assistant 消息

文件：`agent/session/runtime.py`

约第 1180 行附近：

```python
LLMMessage(role="assistant", content=result.summary)
```

一旦 `result.summary` 被污染，固定警告会作为正常 assistant 消息写入 `session_messages`。

#### B. Session summary

文件：

- `agent/session/runtime.py`，约第 1210–1260 行；
- `agent/session/session_store.py`，`set_summary()` 约第 667 行；
- `app/storage/sqlite.py`，`set_summary()` 代理约第 280 行。

`sessions.summary` 当前同时承担“最近回答预览”和“最终运行状态描述”。需要保留其正文语义，不能写 Runtime notice。

#### C. Run 记录

文件：`app/storage/sqlite.py`

关键位置：

- `runs` 表：约第 120 行；
- `create_run()`：约第 287 行；
- `update_run()`：约第 320 行。

当前 `runs` 表具有：

```text
status
summary
steps_taken
total_tokens
error
```

但缺少：

```text
termination_reason
verification_status
verification_reason
workspace_delta
```

因此下游只能从 `summary` 或 trace 中猜测运行结果。

#### D. `run_terminal` WebSocket 事件

文件：

- `agent/session/runtime.py`，`_finalize_run()` 约第 1290 行；
- `server/services/event_bus.py`；
- `web/src/types/events.ts`，`WsRunTerminalEvent` 约第 223 行；
- `web/src/stores/chatStore.ts`，`run_terminal` 处理约第 586 行。

当前 `_finalize_run()` 发送：

```json
{
  "type": "run_terminal",
  "status": "completed",
  "summary": "...",
  "steps_taken": 10,
  "total_tokens": 12000,
  "error": ""
}
```

结构里没有验证和 workspace 字段。前端在没有流式 text block 时，会把 `run_terminal.summary` 直接补成正文，因此污染会再次进入 UI。

#### E. Plan revision 与 Plan 文件

文件：`server/services/chat_pipeline.py`

关键位置：

- `finish()`：约第 417 行；
- 写入 plan revision：约第 430–436 行；
- fallback Plan 文件写入：约第 442–468 行。

这里直接使用 `result.summary` 创建 Plan revision 和 `.grace/plans/{session_id}.md`。如果 summary 被加了 Runtime 前缀，Plan 资产也会被污染。

#### F. Subagent

活动实现文件：`agent/session/runtime_spawn.py`

关键位置：

- `append_message(... child_result.summary)`：约第 265 行；
- child session `set_summary()`：约第 274–288 行。

兼容/转换文件：

- `agent/session/subagent.py`；
- `agent/session/task_tool.py`；
- `agent/session/models.py` 中 `AgentRunResult`。

特别注意：

`agent/session/runtime.py` 中约第 1320 行之后保留了一组被 monkey patch 覆盖的旧实现，代码注释已经标明是 dead code。修改 Subagent 执行路径时，必须先确认活动实现，优先修改 `runtime_spawn.py`，不能只改 `runtime.py` 中的旧方法。

---

## 3. 目标数据模型

### 3.1 `RunResult` 的职责

文件：`agent/task.py`

当前 `RunResult` 已经包含：

```python
summary
patch
error
termination_reason
verification_status
verification_reason
contract
```

短期不需要立即重命名 `summary`，但必须确立：

```python
summary: str
"""仅包含最终 assistant 正文；禁止 Runtime 拼接。"""
```

建议增加一个结构化工作区结果：

```python
@dataclass(frozen=True)
class WorkspaceDelta:
    has_changes: bool = False
    changed_files: tuple[str, ...] = ()
    patch: str = ""
    source: str = "git"
    is_run_scoped: bool = False
```

然后在 `RunResult` 中增加：

```python
workspace_delta: WorkspaceDelta | None = None
```

兼容阶段可以继续保留 `patch`，但新代码优先读取 `workspace_delta`。

### 3.2 验证结果

现有枚举：

- `VerificationStatus`
- `VerificationReason`

建议补充一个可扩展的检查列表：

```python
@dataclass(frozen=True)
class VerificationCheck:
    name: str
    status: str       # passed | failed | skipped | unavailable
    command: str = ""
    detail: str = ""
    duration_ms: int = 0
```

`RunResult` 可增加：

```python
verification_checks: tuple[VerificationCheck, ...] = ()
```

这样前端可以展示“类型检查通过、单测未运行”，而不是只有一个总状态。

### 3.3 前端规范化结构

文件：`web/src/types/events.ts`

建议扩展 `WsRunTerminalEvent`：

```ts
export interface RunVerification {
  status: "not_applicable" | "verified" | "unverified" | "unavailable" | "failed";
  reason:
    | "none"
    | "not_run"
    | "no_test_environment"
    | "no_version_control"
    | "test_failed"
    | "no_net_change";
  checks?: Array<{
    name: string;
    status: "passed" | "failed" | "skipped" | "unavailable";
    command?: string;
    detail?: string;
    duration_ms?: number;
  }>;
}

export interface RunWorkspaceDelta {
  has_changes: boolean;
  changed_files: string[];
  patch_available: boolean;
  source?: "git" | "tool_journal";
  is_run_scoped?: boolean;
}

export interface WsRunTerminalEvent extends EventEnvelope {
  type: "run_terminal";
  status: "completed" | "failed" | "cancelled";
  summary: string; // 干净正文，仅作为流式正文丢失时的 fallback
  termination_reason?: string;
  verification?: RunVerification;
  workspace_delta?: RunWorkspaceDelta;
  steps_taken: number;
  total_tokens: number;
  error?: string;
}
```

不要把 Runtime notice 增加到 `ContentBlock` 的 `text` 类型里。可以选择：

1. 将运行结果放进 `ConversationTurn.meta`，由 `BlocksMessage` 底部渲染；
2. 新增 `RunOutcomeCard`，由 `ChatView` 在 assistant block 后渲染；
3. 对详细 trace 使用单独的 `runtime_notice` timeline event。

推荐使用第 1 + 第 3 种：

- 普通聊天视图：简洁状态条；
- Verbose/Trace 视图：完整运行事件。

---

## 4. 分步骤实施指导

### 阶段 0：先固定行为测试

在修改生产代码前，先增加能够复现当前问题的测试。

建议文件：

- `tests/test_core_result_purity.py`
- `tests/test_run_verification_projection.py`
- `web/src/stores/chatStore.test.ts`

最低测试场景：

1. 仓库启动前已有未提交改动，本轮没有调用写工具：
   - `summary` 不应出现 `[UNVERIFIED`；
   - `verification_status` 应为 `NOT_APPLICABLE` 或符合只读任务的状态；
   - `had_any_write == False`。
2. 本轮修改代码但未运行测试：
   - `summary` 保持模型正文；
   - `verification_status == UNVERIFIED`；
   - `verification_reason == NOT_RUN`。
3. 本轮修改代码且测试失败：
   - `summary` 保持模型正文；
   - `verification_status == FAILED`；
   - 错误显示在验证卡片，不进入正文。
4. 本轮没有 Git，但实际执行写工具：
   - 正文干净；
   - `verification_reason == NO_VERSION_CONTROL`。
5. Plan 模式：
   - Plan revision 和 Plan 文件第一行不能是 `[UNVERIFIED`。

这一步的测试应该先失败，以证明测试确实覆盖当前缺陷。

---

### 阶段 1：停止污染 `RunResult.summary`

### 修改位置

文件：`agent/core.py`

方法：`_build_run_result()`

删除约第 821–843 行中修改 `summary` 的代码。

删除的是“字符串投影”，不是验证判定：

```python
# 删除
if status == RunStatus.SUCCESS and _needs_unverified_tag and _v_needs_tag:
    ...
    summary = f"[{_tag} ...]\n\n{summary}"
```

保留：

```python
verification_status=ctx.tsm.verification_status
verification_reason=ctx.tsm.verification_reason
termination_reason=ctx.tsm.termination_reason
```

### 修改后的不变量

在 `_build_run_result()` 开头记录：

```python
clean_summary = summary
```

构造完成后可以在测试或 debug assertion 中保证：

```python
assert result.summary == clean_summary
```

生产环境不一定保留 assert，但单元测试必须覆盖。

### 同时修复误判

当前 `_refresh_git_state()` 会将基准 commit 以来的所有 dirty diff 放入 `has_changes`。仅记录 `_baseline_dirty_files` 还不能识别同一个 dirty 文件在本轮是否再次变化。

推荐方案：

1. Agent run 开始时记录完整基准 patch 的 hash：

   ```python
   _baseline_patch_hash: str
   _baseline_file_hashes: dict[str, str]
   ```

2. run 结束时重新计算 patch/file hash。
3. 只把与基准快照不同的文件记为本轮 delta。
4. 删除文件、新增文件和未跟踪文件必须纳入快照。

短期止血条件可以改成：

```python
effective_has_changes = (
    ctx.completion_ctx.had_any_write
    and ctx.git_state.has_changes
)
```

并在 `CompletionFacts` 中使用 `effective_has_changes`，避免只读回答继承仓库原有 dirty 状态。

但是短期条件不能替代真正的 run-scoped delta，因为 Agent 可能写入一个本来就 dirty 的文件。最终仍需做前后快照比较。

---

### 阶段 2：让 `runs` 成为运行结果的结构化事实源

### 修改位置

文件：`app/storage/sqlite.py`

给 `runs` 表增加：

```sql
ALTER TABLE runs ADD COLUMN termination_reason TEXT NOT NULL DEFAULT 'none';
ALTER TABLE runs ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'not_applicable';
ALTER TABLE runs ADD COLUMN verification_reason TEXT NOT NULL DEFAULT 'none';
ALTER TABLE runs ADD COLUMN verification_checks_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE runs ADD COLUMN workspace_delta_json TEXT NOT NULL DEFAULT '{}';
```

迁移风格沿用当前 `_init_db()` 中 `ALTER TABLE ...` + 捕获 `sqlite3.OperationalError` 的方式，或者先读取 `PRAGMA table_info(runs)` 后按缺失列增加。推荐后一种，迁移意图更清楚。

### 扩展 `update_run()`

文件：

- `app/storage/sqlite.py`
- `agent/session/session_store.py` 中对应代理方法

增加参数：

```python
termination_reason: str | None = None
verification_status: str | None = None
verification_reason: str | None = None
verification_checks: list[dict] | None = None
workspace_delta: dict | None = None
```

JSON 字段在 storage 边界统一序列化，不允许调用方提前重复 `json.dumps()`。

### 修改 `_finalize_run()`

文件：`agent/session/runtime.py`

约第 1290 行的 `_finalize_run()` 调用 `update_run()` 时写入上述字段：

```python
termination_reason=result.termination_reason.value,
verification_status=result.verification_status.value,
verification_reason=result.verification_reason.value,
verification_checks=[...],
workspace_delta={...},
```

如果 `result is None`：

- `summary = ""`；
- 终止原因来自捕获的 Runtime 状态；
- `error` 保留真实错误；
- 不创建伪造 assistant 正文。

### API 读取

为 Session timeline 提供 run 信息，推荐二选一：

1. 扩展 `GET /api/sessions/{id}/timeline`，把 terminal run 投影为结构化 event；
2. 增加 `GET /api/sessions/{id}/runs`。

推荐两者都做：

- timeline 用于 Chat 恢复；
- runs 用于 Stats/Debug/审计详情。

---

### 阶段 3：规范主 Session 的消息持久化

### 修改位置

文件：`agent/session/runtime.py`

主 Session 正常完成时：

```python
if result is not None and result.summary:
    append assistant message(result.summary)
```

这一行为可以保留，前提是阶段 1 已保证 summary 纯净。

失败和取消时需要区分：

#### 模型已经生成部分用户可见正文

- 可以保存这部分正文为 assistant message；
- run 状态另外保存为 failed/cancelled；
- 不把异常详情拼进正文。

#### 模型没有生成正文

- 不创建诸如 `"Task cancelled: ..."`、`"Session execution failed..."` 的 assistant message；
- 只写 `runs.error`、`termination_reason` 和 `run_terminal`；
- 前端显示 Runtime 状态卡。

当前以下文本应停止作为 Session summary 或 assistant 正文：

```text
Task cancelled: ...
Session execution failed before producing a result
Session terminated: permission circuit breaker tripped
LLM call failed: ...
```

这些文本可以作为 `error` 或结构化事件的默认本地化文案。

### `sessions.summary` 的新定义

`sessions.summary` 只保存：

- 最近一次非空的干净 assistant 正文；或
- Plan session 的干净 Plan 正文。

运行失败且没有回答时，不要覆盖上一次干净 summary。失败状态由 `sessions.status` 和最新 run 表达。

这可以避免 Session 侧栏标题/预览突然变成 Runtime 异常。

---

### 阶段 4：扩展 `run_terminal`，让实时和重放一致

### 后端

修改：

- `agent/session/runtime.py::_finalize_run()`
- `server/events.py`
- `server/services/event_bus.py`

建议在 `server/events.py` 增加明确的 `WsRunTerminal` dataclass，不再长期维护裸 dict：

```python
@dataclass
class WsRunTerminal:
    type: Literal["run_terminal"] = "run_terminal"
    run_id: str = ""
    turn_id: str = ""
    turn_index: int = 0
    status: str = ""
    summary: str = ""
    termination_reason: str = "none"
    verification: dict = field(default_factory=dict)
    workspace_delta: dict = field(default_factory=dict)
    steps_taken: int = 0
    total_tokens: int = 0
    error: str = ""
    timestamp: str = ""
```

`summary` 仍然保留，用作流式正文丢失时的恢复数据，但它必须是干净正文。

### Timeline

`run_terminal` 必须：

1. 先持久化；
2. 再广播；
3. `/timeline` 刷新时返回相同 payload；
4. 通过 `event_id/run_id/turn_id` 去重。

不要实时路径一套字段、timeline 重放另一套字段。

### 前端类型

修改：`web/src/types/events.ts`

扩展 `WsRunTerminalEvent`，字段与后端 dataclass 一一对应。新增字段在过渡期使用可选属性，保证旧 trace 仍可读取。

---

### 阶段 5：前端状态展示，不进入正文

### Store

修改：`web/src/stores/chatStore.ts`

当前 `run_terminal` 完成时会在缺少 text block 的情况下执行：

```ts
blocks.push({ type: "text", content: re.summary });
```

可以保留这个 fallback，但必须满足：

- `re.summary` 是干净正文；
- Runtime 状态不能通过 `summary` 进入这里。

给 `ConversationTurn.meta` 增加：

```ts
outcome?: {
  status: "completed" | "failed" | "cancelled";
  terminationReason?: string;
  verification?: RunVerification;
  workspaceDelta?: RunWorkspaceDelta;
  error?: string;
};
```

处理 `run_terminal` 时：

- 文本写入 `assistantResponse.blocks`；
- 验证、变更、失败信息写入 `turn.meta.outcome`；
- `isRunning` 和 turn lifecycle 正常收敛；
- 不创建伪造的 TextBlock。

### 组件拆分

建议新增：

```text
web/src/components/RunOutcomeBar.tsx
web/src/components/VerificationDetails.tsx
web/src/components/WorkspaceDeltaCard.tsx
```

`BlocksMessage.tsx` 只负责：

- Text block；
- Thought block；
- Tool use block；
- 简洁 metrics。

不要继续把所有运行结果都塞进 `BlocksMessage`。推荐由 `ChatView` 组合：

```tsx
<BlocksMessage ... />
<RunOutcomeBar outcome={turn.meta.outcome} />
```

### 视觉规范

#### 默认收起状态

回答正文下方显示单行：

```text
✓ Completed   Validation: Not run   3 files changed   12.4K tokens
```

#### 状态颜色

- `verified/passed`：成功色，但不能只依赖颜色；
- `unverified/not_run`：中性灰或轻提示色，不使用危险红；
- `unavailable`：中性提示；
- `failed`：错误色；
- `cancelled`：中性或警告色。

#### 文案规范

不要使用：

```text
Code changes were made but NOT independently verified.
```

建议使用：

```text
Validation not run
No test command was available for this run
```

语气应描述事实，不制造“回答不可信”的正文感。

#### 展开内容

点击状态条后展示：

- 验证命令；
- 通过/失败/跳过；
- changed files；
- patch/diff 入口；
- termination reason；
- error detail；
- run ID。

#### 可访问性

- 状态不能只靠颜色；
- 展开按钮使用 `aria-expanded`；
- error detail 使用 `role="alert"` 仅在新错误实时出现时触发；
- 历史重放不能反复触发屏幕阅读器告警；
- 键盘可访问；
- 长命令和路径允许复制。

---

### 阶段 6：Plan、HITL 和 Subagent 的边界

### Plan

修改：`server/services/chat_pipeline.py`

Plan revision 和 Plan 文件只能使用干净 `result.summary`。验证状态存入 run，不写入 Plan Markdown。

Plan 文件允许包含 Plan contract frontmatter，但不允许包含：

- `[UNVERIFIED ...]`
- 测试运行状态
- token/step 信息
- Session 终止信息

Plan 审批状态继续由 `plan_state` 表达，不编码进 Plan 文本。

### HITL

HITL 信息继续使用：

- `approval_required`
- `approval_timeout`
- tool approval endpoint
- Plan approval state

不要将以下内容保存为 assistant 正文：

```text
Waiting for approval
Tool approval timed out
Plan rejected
```

这些是 timeline/action card。

### Subagent

修改活动实现：`agent/session/runtime_spawn.py`

原则：

- `AgentRunResult.summary` 是 child Agent 的干净工作汇报；
- `status/error/failure_diagnosis/warning/worktree` 保持结构化；
- parent notification 中 `<summary>` 只放干净 summary；
- failure diagnosis 放独立 XML 节点或结构化字段；
- 不把父/子 Runtime 状态拼进 child assistant 消息。

修改前必须验证 monkey patch 绑定关系，避免只改 `runtime.py` 的 dead code。

---

### 阶段 7：历史污染数据清理

### 清理范围

需要检查：

- `session_messages.content`
- `sessions.summary`
- `runs.summary`
- `agent_result_json.summary`
- `fork_result_json.summary`
- Plan revisions
- `.grace/plans/*.md`
- trace event 中历史 `run_terminal.summary`

### 安全匹配规则

只允许匹配字符串开头的已知固定格式：

```regex
\A\[UNVERIFIED — (?:no test environment available|project has no Git fact source|tests ran but failed|test/validation did not run or was unavailable)\. Code changes were made but NOT independently verified\.\]\r?\n\r?\n
```

规则要求：

- 必须从字符串第一个字符开始；
- 必须完整匹配 Grace Code 固定句式；
- 只移除一次；
- 删除后保留剩余 Markdown 原样；
- 不处理正文中间出现的 `[UNVERIFIED]`；
- 先 dry-run 输出统计，再显式执行。

### 迁移工具建议

新增脚本：

```text
scripts/migrate_clean_runtime_prefixes.py
```

命令建议：

```bash
python scripts/migrate_clean_runtime_prefixes.py --db <path> --dry-run
python scripts/migrate_clean_runtime_prefixes.py --db <path> --apply
```

脚本输出：

```text
session_messages matched: N
sessions matched: N
runs matched: N
plan_revisions matched: N
plan_files matched: N
trace_events matched: N
```

执行前备份 SQLite 文件。事务内更新数据库，任何异常整体回滚。

### 是否必须改历史 trace

可以分两阶段：

1. 首先清理用户直接看到的 message/session/plan；
2. trace 保留原始审计记录，但 API 投影时增加 `legacy_content_cleaned=true` 并返回清理后的展示值。

如果 trace 被定义为不可变审计日志，优先选择第二种，不直接修改原始事件。

---

### 阶段 8：API 与兼容策略

### Session detail

可以在 `SessionDetail` 中增加：

```json
{
  "latest_run": {
    "id": "...",
    "status": "completed",
    "verification_status": "unverified",
    "verification_reason": "not_run",
    "termination_reason": "none"
  }
}
```

不要让前端通过解析 `summary` 判断这些状态。

### Timeline

确保 `/api/sessions/{id}/timeline` 返回：

- message：仅对话正文；
- event：运行和工具事件；
- plan_state：Plan 生命周期；
- run_terminal：结构化运行结果。

### 向后兼容

- 新字段先设为 optional；
- 旧 `run_terminal` 没有 verification 时，前端显示“Validation status unavailable”，不能从 summary 猜；
- 不再生成新污染数据；
- 历史清理单独执行；
- 前端不能长期保留解析 `[UNVERIFIED` 的兼容逻辑，最多保留一个有删除期限的展示层 sanitizer。

---

## 5. 测试计划

### 5.1 后端单元测试

建议新增：

### `tests/test_core_result_purity.py`

- success + dirty baseline + no write；
- success + write + no test；
- success + write + passed test；
- success + write + failed test；
- non-git + write；
- summary 包含用户自己输入的合法 `[UNVERIFIED]` 时不应被 Runtime 改写。

断言：

```python
assert result.summary == model_summary
assert result.verification_status is expected_status
assert result.verification_reason is expected_reason
```

### `tests/test_run_terminal_projection.py`

- `_finalize_run()` 将结构化字段写入 runs；
- 广播 payload 与 DB 内容一致；
- `result is None` 时 summary 为空；
- 失败时 error 不进入 summary；
- CAS 冲突不重复广播。

### `tests/test_session_message_purity.py`

- assistant message 只保存 clean summary；
- Runtime notice 不写入 `session_messages`；
- cancelled without content 不新增 assistant message；
- failed without content 不覆盖上一条 session summary。

### `tests/test_plan_content_purity.py`

- Plan revision 没有 Runtime prefix；
- Plan 文件 frontmatter 后只出现 Plan 正文；
- verification 存在于 run，不存在于 Plan Markdown。

### `tests/test_subagent_result_purity.py`

- child summary 干净；
- failure diagnosis 独立；
- notification summary 不带 Runtime notice；
- 测试活动的 `runtime_spawn.py` 路径，而不是 dead code。

### 5.2 Storage migration 测试

- 从旧 schema 启动，新增列成功；
- 重复启动幂等；
- 旧 runs 记录使用安全默认值；
- JSON 字段序列化/反序列化；
- migration dry-run 不写数据；
- apply 可事务回滚。

### 5.3 前端测试

建议新增：

- `web/src/stores/chatStore.test.ts`
- `web/src/components/RunOutcomeBar.test.tsx`

覆盖：

1. `run_terminal.summary` 作为缺失流式正文的 fallback；
2. verification 不生成 TextBlock；
3. error 不进入 assistant Markdown；
4. 页面刷新后 timeline 能恢复 outcome；
5. 旧事件缺字段时安全降级；
6. duplicate `run_terminal` 不重复添加正文；
7. failed/cancelled 显示状态卡；
8. 状态条键盘展开与 ARIA。

### 5.4 集成测试

完整场景：

| 场景 | 正文 | 状态展示 |
|---|---|---|
| 只读问答，仓库原本 dirty | 只有回答 | Validation 不适用 |
| 修改代码，未运行测试 | 只有回答 | Validation not run |
| 修改代码，测试通过 | 只有回答 | Verified |
| 修改代码，测试失败 | 只有回答 | Validation failed |
| 用户取消 | 已生成的部分正文可保留 | Cancelled |
| LLM 调用失败且无正文 | 无伪造 assistant 文本 | Model error |
| Plan 等待审批 | Plan 正文 | Plan review card |
| Tool 等待审批 | 不改变正文 | Approval card |
| Subagent 失败 | Parent 获得干净汇报或无汇报 | Child failed detail |

---

## 6. 推荐提交批次

不要把所有变化塞进一个大提交。建议按以下顺序：

### Commit 1：Characterization tests

- 只添加当前缺陷复现测试；
- 不改行为。

### Commit 2：Pure `RunResult.summary`

- 删除 Runtime 字符串拼接；
- 修复只读任务继承 dirty workspace 的误判；
- 保持结构化 verification 字段。

### Commit 3：Persist structured run outcome

- runs schema migration；
- storage API；
- `_finalize_run()`；
- run 查询。

### Commit 4：WebSocket/timeline contract

- 后端 typed event；
- timeline replay；
- TypeScript event types。

### Commit 5：Frontend presentation

- chatStore outcome；
- `RunOutcomeBar`；
- verification/workspace details；
- CSS 与可访问性。

### Commit 6：Plan/Subagent purity

- Plan revision/file；
- active Subagent path；
- notification。

### Commit 7：Historical migration

- dry-run/apply 脚本；
- 备份和回滚说明；
- legacy compatibility tests。

---

## 7. 验收标准

只有满足以下全部条件才算完成：

- [x] 任意新 assistant 消息正文不包含 Runtime 自动生成的 `[UNVERIFIED ...]`。
- [x] 只读任务不会因为仓库原有 dirty 文件显示“代码被修改但未验证”。
- [x] 修改代码但未测试时，用户仍能看到明确的 Validation 状态。
- [x] Validation 状态刷新后不丢失。
- [x] failed/cancelled/model error 不伪装成 assistant 回答。
- [x] `sessions.summary` 只包含干净正文。
- [x] `runs` 能独立回答状态、验证、终止原因和工作区变化。
- [x] Plan revision 和 Plan 文件不包含运行状态前缀。
- [x] Subagent summary 与 failure diagnosis 分离。
- [x] WebSocket 实时和 timeline 重放展示一致。
- [x] 历史迁移支持 dry-run、备份、事务回滚。
- [x] 前端状态组件支持键盘、ARIA，且不只依赖颜色。
- [x] 本改造相关的后端、前端和迁移测试全部通过。

### 7.1 实际落点

- `RunResult.summary` 保持纯正文；验证与工作区信息由 `VerificationCheck`、
  `WorkspaceDelta` 和现有 verification 枚举承载。
- Git 运行基线记录脏文件内容哈希，能够区分“运行前已有 dirty”和
  “本轮再次修改同一个 dirty 文件”。
- `runs` 表持久化终止原因、验证结果、检查列表和工作区变化；
  `/api/sessions/{id}/runs` 提供结构化查询。
- `run_terminal` 同时提供兼容扁平字段和规范化的 `verification`、
  `workspace_delta`；timeline 的 turn meta 从同一 runs 事实源恢复。
- `RunOutcomeBar` 在回答正文下方展示完成、验证、变更和错误状态，
  详细内容可通过键盘展开，Runtime 信息不生成 `TextBlock`。
- 主 Session 和活动 Subagent 在无模型正文的失败/取消场景中不再创建
  伪 assistant 消息，也不会用 Runtime 错误覆盖已有 Session 摘要。
- `scripts/migrate_clean_runtime_prefixes.py` 默认 dry-run，`--apply` 时先
  备份 SQLite，并在事务内清理精确匹配的历史固定前缀。

### 7.2 回归说明

本改造的目标测试、前端组件测试和生产构建已经通过。全仓测试仍包含
依赖外部 Web 服务的 manual/e2e 用例，以及与本改造无关的既有契约测试
失败；因此验收结论以本功能相关测试集为准，不把外部环境不可用记录为
回答正文，也不使用 `UNVERIFIED` 文本前缀报告。

---

## 8. 风险与注意事项

### 风险 1：只删除字符串，状态完全不可见

不能只删 `_build_run_result()` 的拼接逻辑。必须同步把 verification 投影到 runs、WebSocket 和前端，否则用户失去重要信息。

### 风险 2：继续把 `summary` 当万能字段

即使没有 `[UNVERIFIED]`，未来新功能仍可能把别的状态拼进正文。应在类型注释、测试和 code review checklist 中明确 summary 纯净性。

### 风险 3：误清理历史正文

历史迁移必须使用 anchored exact pattern。不能使用简单的：

```python
text.replace("[UNVERIFIED", "")
```

### 风险 4：修改了 Subagent dead code

必须修改 `runtime_spawn.py` 的活动实现，并用集成测试证明调用路径。

### 风险 5：实时与重放不一致

`run_terminal` 必须先持久化再广播，timeline 直接读取相同事件。不要在前端为实时和刷新分别维护两套推断逻辑。

### 风险 6：把验证失败当运行失败

运行成功但测试失败时：

```text
run status = completed
verification status = failed
```

这两个维度必须正交。不能因为测试失败把完整 Agent run 改成 `failed`，除非产品明确规定测试失败就是任务失败。

---

## 9. 最终推荐架构

```text
Model final answer
        │
        ▼
RunResult.summary ───────────────► assistant message / session clean summary
        │
        ├─ status ───────────────► runs.status / run_terminal.status
        ├─ termination_reason ───► runs / runtime status card
        ├─ verification ─────────► runs / validation UI
        ├─ workspace_delta ──────► runs / changed-files UI
        ├─ metrics ──────────────► runs / compact footer
        └─ contract/artifacts ───► plan/artifact UI
```

最重要的设计原则是：

> assistant 正文表达“Agent 对用户说了什么”；结构化运行结果表达“系统观察到这次运行发生了什么”。两者永远不能再通过字符串拼接混为一体。
