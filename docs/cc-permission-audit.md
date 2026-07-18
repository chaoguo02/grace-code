# Claude Code vs Forge Agent — Permission System Audit

> 逐层对照 CC 实现，标注每个细节的差异、缺失和问题。

---

## 对照表说明

每条标注等级：
- ✅ **正确** — 与 CC 一致
- ⚠️ **偏差** — 方向对但细节有差异
- ❌ **缺失/错误** — 需要修复
- 🔧 **建议** — 功能完整但可增强

---

## 1. Pipeline 层次结构

| # | CC 层 | Forge 层 | 对照 |
|---|-------|----------|------|
| 1 | PreToolUse Hooks | `_layer2_hooks` | ✅ |
| 2 | Deny Rules | `check()` inline lines 342-356 | ⚠️ |
| 3 | Ask Rules | `check()` inline lines 357-361 | ⚠️ |
| 4 | Permission Mode | `_layer4_permission_mode` | ✅ |
| — | Prompt-based Perms | `_match_approved_prompt` (Layer 4.5) | ✅ |
| 5 | Allow Rules | `check()` lines 381-397 | ✅ |
| 6 | canUseTool Callback | `_layer6_callback` | ✅ |

### ❌ 问题 1: `_layer3_rules()` 是孤儿方法，从未被调用

`hitl/pipeline.py:462` — `_layer3_rules()` 完整实现了 deny > session_allow > ask > allow 的规则匹配逻辑，但 `check()` 方法在 342-361 行内联了另一套 deny/ask 规则迭代逻辑。`_layer3_rules()` 没被任何地方调用。

**CC 做法**: 规则评估是独立的一层，由 `check()` 调用。
**修复**: `check()` 应该调用 `_layer3_rules()` 而不是内联。规则匹配逻辑不应重复。

```python
# 当前 (check() line 342-361):
for rule in self._deny_rules:     # ← 重复逻辑
    if rule.matches(tool_name, params): ...
for rule in self._ask_rules:      # ← 重复逻辑
    if rule.matches(tool_name, params): ...

# 应该:
tier = self._layer3_rules(tool_name, params)
if tier is PermissionRuleTier.DENY: ...
elif tier is PermissionRuleTier.ASK: ...
# ALLOW 走到 Layer 5
```

### ❌ 问题 2: Layer 3 deny 不过 `_apply_tool_check`

`check()` line 350-356 — deny rule 匹配后直接返回 `PermissionResult`，不经过 `_apply_tool_check()`。这意味着 deny 不会递增 `_total_denials` 和 `_denial_counters`。

**CC 做法**: 所有 deny 都应该记录 denial counter，用于连续拒绝熔断。
**修复**: Layer 3 deny 也走 `_apply_tool_check()`。

### ❌ 问题 3: 总拒绝上限 (20) 被注释掉了

`check()` line 313-318 — `if self._total_denials >= 20` 的逻辑在注释里，未执行。

**CC 做法**: `max total denials: 20` + `max consecutive denials: 3 per tool type`。应该启用。
**修复**: 取消注释，移到合适位置（在 deny 规则匹配后但在返回前检查）。

---

## 2. Permission Mode (Layer 4)

| Mode | CC 行为 | Forge 实现 | 对照 |
|------|---------|------------|------|
| `default` / `manual` | 敏感操作弹 prompt | `return None` → 走完管线到 Layer 6 | ✅ |
| `bypassPermissions` | 全部允许，但保留 ask rules、requiresUserInteraction、root/home rm 熔断 | `return ALLOW` 无条件 | ⚠️ |
| `acceptEdits` | Edit/Write + mkdir/touch/mv/cp 等常用文件命令 | 仅 Edit/Write | ⚠️ |
| `plan` | Write/Edit/Bash 全部拒绝 | Write/Edit/Bash 全部拒绝 | ✅ |
| `dontAsk` | allow 列表外全部 deny | 只读工具 + allow 规则通过，其余 deny | ✅ |
| `auto` | LLM 分类器审批 | 未实现 | 🔧 |

### ⚠️ 问题 4: `bypassPermissions` 缺少保留的熔断检查

CC 文档明确：

> "A few prompts still fire in this mode. Explicit ask rules, connector tools your organization set to ask, and MCP tools marked requiresUserInteraction still prompt. Removals targeting the filesystem root or home directory, such as rm -rf / and rm -rf ~, also prompt as a circuit breaker."

我们的实现 (`pipeline.py:529-534`) 无条件 ALLOW。

**修复**: `bypassPermissions` 中仍需检查：
1. 是否匹配 ask 规则 → 路由到 Layer 6
2. Bash 参数是否为 `rm -rf /` 或 `rm -rf ~` → 路由到 Layer 6
3. MCP 工具 `requiresUserInteraction` → 路由到 Layer 6

### ⚠️ 问题 5: `acceptEdits` 不包含常用文件命令

CC 文档：

> "Automatically accepts file edits and common filesystem commands such as mkdir, touch, mv, and cp"

我们只 auto-approve `Write` 和 `Edit`，不包括 Bash 命令如 `mkdir`、`touch`、`mv`、`cp`。

**修复**: `acceptEdits` 中增加对 Bash 常用文件操作的检查。或者更简单的：在 `_READONLY_SAFE_TOOLS` 旁边定义一个 `_FILESYSTEM_SAFE_COMMANDS` 集合。

---

## 3. Layer 2: PreToolUse Hooks

### ❌ 问题 6: `HookControl.CONTINUE` + `updated_input` 被错误地当作 ALLOW

`pipeline.py:451-457`:

```python
if dispatch_result.updated_input:
    return PermissionResult(
        decision=PermissionDecision.ALLOW,  # ← 错误: 不应返回 ALLOW
        ...)
```

**CC 做法**: hook 返回 CONTINUE 表示"不决策，管道继续"。如果附带 `updated_input`，输入应该被修改但管线应该继续评估（后续的 deny rules、permission mode 等仍然运行）。

**问题**: 当前代码在 hook 修改输入后直接返回 ALLOW，跳过了 Layers 3/4/5/6。一个只修改参数的 hook 会导致所有后续安全检查被绕过。

**修复**: 不应该在这里返回 ALLOW。`updated_input` 需要通过 `PermissionResult` 传回去但不终止管线评估。这需要修改 `check()` 的流程——或者在当前阶段无法完美处理时，将其视为 CONTINUE（返回 None）并将 updated_input 暂存到 pipeline 状态中，在 `execute_tool()` 时应用。

---

## 4. Interactive Callback (Layer 6)

### ✅ 正确: 路径优先级

`_layer6_callback` 的顺序：AUTO → Web callback → TTY callback → deny。与 CC 的 `canUseTool` 回调语义一致。

### ⚠️ 问题 7: `control_response` 缺少 `updatedInput`

CC 的 `control_response` 协议:

```json
{"type":"control_response","request_id":"...","decision":"allow","updatedInput":{...}}
```

前端可以在批准时修改工具参数（如修改 Bash 命令、限制文件路径）。我们的 `ToolApprovalBody` 和 `PromptDecision` 都缺少 `updatedInput` 字段。

**影响**: 用户无法在审批时限制工具参数（如"允许但只读这个文件"）。
**修复**: `PromptDecision` 增加 `updated_params` 字段；`ToolApprovalBody` 增加 `updated_input` 字段；前端审批卡片增加参数编辑功能。

### ✅ 正确: `ALWAYS_ALLOW` 持久化

`_apply_decision()` → ALWAYS_ALLOW → 推断规则 → 追加到 `_session_rules` → 通过 `save_rule_to_settings()` 写入 `settings.json`。与 CC 的 "Yes, don't ask again" 一致。

---

## 5. 子代理权限继承

| CC 规则 | Forge 实现 | 对照 |
|---------|------------|------|
| Parent bypassPermissions → child forced | `_resolve_child_permission_mode()` bypassPermissions 强制继承 | ✅ |
| Parent plan → child forced | plan 强制继承 | ✅ |
| Parent acceptEdits/auto/dontAsk → child can't upgrade | child 可用自己的 mode 但禁止升级到 bypassPermissions | ✅ |
| Deny rules pass to child | `apply_inherited_state()` deny_rules 强制继承 | ✅ |
| Allow rules pass to child | allow_rules 继承 | ✅ |
| Session rules pass to child | session_rules 继承 | ✅ |
| Web callback for child | `subagent.py` 注入独立的 broker + callback | ✅ |
| `settings.local.json` not inherited | — | ⚠️ |

### ⚠️ 问题 8: CC 已知 Bug 我们也可能踩

CC Issue #37442/#33901/#27661:
- Subagent 不继承 `settings.local.json` 权限
- Subagent 不继承 `bypassPermissions` mode
- PreToolUse hooks 不传递给子代理

我们的实现中：
- ✅ bypassPermissions 继承已处理
- ⚠️ PreToolUse hooks 是否传递给子代理？在 `run_child_agent()` 中，子代理的 `cfg.hook_dispatcher` 是从 `wrapped_registry._hook_dispatcher` 传入的（per-session dispatcher），这包含了父会话注册的 hooks。**应该可以工作，但需要验证。**

---

## 6. ApprovalBroker

### ✅ 正确: 同步阻塞语义

`threading.Event.wait()` 与 CC 的 `stdin.readline()` 语义等价。

### ⚠️ 问题 9: 超时后没有 fallback 通知

当审批超时（60秒），agent 线程返回 DENY 并继续。但前端可能仍显示审批卡片（没有被清理）。需要超时后清理前端的 `toolApproval` 状态。

**修复**: 超时时发送一个 WS 事件 `{"type": "approval_timeout", "request_id": "..."}` 让前端清理。

### ⚠️ 问题 10: 并发审批

如果 agent 在同一 step 发出多个 tool_calls（parallel），每个都需要审批。当前 `toolApproval` 状态只能存一个。

**CC 做法**: 每个 tool_call 有独立的 `request_id`，多个 `control_request` 可以同时 pending。
**修复**: `toolApproval` 改为 `Map<string, ToolApproval>` (keyed by request_id)。

---

## 7. `scoped()` / `for_agent()` 浅拷贝问题

### ⚠️ 问题 11: `scoped()` 共享可变列表

`pipeline.py:275-284` — `scoped()` 使用 `copy.copy(self)`，这是浅拷贝。`_deny_rules`、`_allow_rules` 等 list 引用被共享。如果 scoped pipeline 修改了这些列表，原始 pipeline 也会被影响。

**当前风险**: 低（因为 `scoped()` 主要用于设置 `_project_root`，不修改规则列表）。但如果后续代码在 scoped pipeline 上调用 `apply_inherited_state()`，会污染原始 pipeline。

**修复**: `scoped()` 应该 `copy.copy()` 这些列表，或文档明确说明不可修改。

---

## 8. Web 审批前端

### ⚠️ 问题 12: `ToolApprovalCard` 只处理单个审批

`chatStore.ts` — `toolApproval` 是单个对象，不是 map。Agent 发出多个 tool_calls 时只能看到最后一个。

### ⚠️ 问题 13: 审批卡片不显示 "Always Allow" 选项

CC 的 prompt 有三个选项：
1. Allow Once
2. Always Allow（持久化到 settings）
3. Deny

我们的 `ToolApprovalCard` 只有 Allow / Deny 两个按钮。

**修复**: 增加 "Always Allow" 按钮，调用 `resolveToolApproval` 时传标记，后端调用 `PromptAction.ALWAYS_ALLOW`。

---

## 9. 配置加载

### ✅ 正确: 层级加载

builtin → user → project → local。与 CC 一致。

### ⚠️ 问题 14: `.forge-agent/` vs `.grace/` vs `.claude/`

`settings_loader.py:17` 默认路径是 `.grace/settings.json`，但我们的 `agent_service.py` 用的是 `.forge-agent/settings.json`。两者不一致。

**修复**: 统一使用 `.forge-agent/` 并更新 `settings_loader.py` 的默认值。

---

## 10. Denial Tracking

### ❌ 问题 15: 连续拒绝熔断不完整

CC: max 3 consecutive denials per tool + max 20 total denials。当前：
- Line 346: `if consecutive >= 3` — 只是在 reason 里加了提示文字，没有真正熔断（没有返回特殊的终止信号给 agent）
- Line 313-318: total 20 检查被注释掉了

**修复**: 
1. 连续 3 次拒绝 → 返回 DENY 且注入 feedback 到 LLM 上下文（"You MUST change your approach"）
2. 总拒绝数 ≥ 20 → 返回 DENY 且触发 session 级熔断

---

## 11. 补充建议

### 🔧 建议 1: `updatedInput` 全链路

从 CC `control_response` → `PromptDecision.updated_params` → `ToolRegistry.execute_tool()` 的 `actual_params`。此功能需要全链路支持。

### 🔧 建议 2: 工具可见性过滤

CC: bare-name deny rule (`Bash`) 从 LLM schema 中移除该工具。我们目前没有实现。
**文件**: `PolicyAwareToolRegistry._is_tool_visible()` / `core/base.py:ToolRegistry._build_schemas()`

### 🔧 建议 3: Hook `PERMISSION_REQUEST` 事件接入

`hooks/events.py` 已定义 `PERMISSION_REQUEST` 事件但未在 `_layer6_callback` 中使用。CC 的 PermissionRequest hook 可以在审批决策前后运行。

### 🔧 建议 4: 审批卡片的 risk 评估

CC 支持 `Ctrl+E` 显示工具调用的风险等级（Low/Med/High）。我们的前端可以显示 `tool_name` + `params` 的简单风险判断（如：Read=低风险，Bash(rm)=高风险）。

---

## 总结：需要修的

| # | 等级 | 位置 | 问题 |
|---|------|------|------|
| 1 | ❌ | `pipeline.py:462` | `_layer3_rules()` 孤儿方法，check() 内联重复逻辑 |
| 2 | ❌ | `pipeline.py:350` | Layer 3 deny 不递增 denial counter |
| 3 | ❌ | `pipeline.py:313` | 总拒绝上限 (20) 被注释掉 |
| 4 | ❌ | `pipeline.py:451` | Hook CONTINUE + updated_input 错误返回 ALLOW |
| 5 | ⚠️ | `pipeline.py:529` | `bypassPermissions` 缺少 ask rules/root-rm 熔断 |
| 6 | ⚠️ | `pipeline.py:535` | `acceptEdits` 缺少 mkdir/touch/mv/cp |
| 7 | ⚠️ | `pipeline.py` | `control_response` 缺少 `updatedInput` |
| 8 | ⚠️ | `pipeline.py:275` | `scoped()` 浅拷贝共享可变列表 |
| 9 | ⚠️ | `approval_broker.py` | 超时后不通知前端清理 |
| 10 | ⚠️ | `chatStore.ts` | 只支持单个审批 (非并发) |
| 11 | ⚠️ | `ToolApprovalCard.tsx` | 缺少 "Always Allow" 按钮 |
| 12 | ⚠️ | `settings_loader.py:17` | `.grace/` vs `.forge-agent/` 不一致 |
| 13 | 🔧 | `pipeline.py` | 无工具可见性过滤 (bare-name deny) |
| 14 | 🔧 | `hooks/` | PERMISSION_REQUEST hook 未接入 |
