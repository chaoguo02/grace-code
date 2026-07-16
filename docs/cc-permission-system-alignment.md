# CC 权限系统对齐 — 架构差距与改进计划

> 依据来源:
> - [Configure permissions - Claude Code Docs](https://code.claude.com/docs/en/agent-sdk/permissions)
> - [Choose a permission mode - Claude Code Docs](https://code.claude.com/docs/en/permission-modes)
> - [Source analysis](https://deepwiki.com/alesha-pro/claude-code/3.1-permission-modes-and-rules) ([alt](https://deepwiki.com/yasasbanukaofficial/claude-code/10-permissions-and-security))
> - 调研日期: 2026-07-16

---

## 一、权限评估顺序对比

### CC 的 6 步评估顺序

```
Step 1: Hooks (PreToolUse)
  → hook 返回 deny → 直接阻断 (即使在 bypassPermissions 模式)
  → hook 返回 allow → 不短路后续步骤

Step 2: Deny Rules
  → 裸名规则 (如 "Bash") 在注册时就从上下文中移除工具
  → 作用域规则 (如 "Bash(rm *)") 在此步检查
  → deny 规则在 bypassPermissions 模式下仍然生效 (最高优先级)

Step 3: Ask Rules
  → 匹配 ask 规则 → 进入 canUseTool callback
  → AskUserQuestion、requiresUserInteraction 的 MCP 工具始终走 callback
  → dontAsk 模式: 不调 callback, 直接拒绝

Step 4: Permission Mode ← 核心步骤
  → bypassPermissions: 全部放行 (前面的 deny/ask 仍然生效)
  → acceptEdits: 自动批准文件操作 (Write/Edit/filesystem shell)
  → plan: 文件编辑类路由到 callback (allow rules 也无法覆盖)
  → default/dontAsk: 继续到下一步

Step 5: Allow Rules
  → 匹配 allow 规则 → 批准
  → bypassPermissions 模式下, allow rules 不生效 (全部已放行)

Step 6: canUseTool Callback
  → 以上均未匹配 → 调用用户的回调
  → CC SDK: callback 返回 { allow, deny, ask } 三元组
```

### 我们的实现

```
Layer 1: validateInput
  → 检查 tool.permission_denial_reason(params) — 硬编码安全底线

Layer 2: PreToolUse Hooks
  → 通过 HookDispatcher 执行

Layer 3: Rule matching
  → session_rules (deny) → ask_rules → allow_rules → 默认 ASK

Layer 4: Interactive Prompt
  → if auto_approve → ALLOW
  → if _confirm_callback is None → DENY (headless 模式问题根源)
  → else → 回调确认
```

### 关键差距

| CC 的步骤 | 我们的对应 | 差距 |
|-----------|-----------|------|
| Step 1 Hooks | Layer 2 ✓ | 顺序正确 |
| Step 2 Deny Rules | Layer 3 (部分) | 没有裸名/作用域区分; deny 不 bypass-proof |
| Step 3 Ask Rules | Layer 3 (部分) | 一致 |
| **Step 4 Permission Mode** | **❌ 缺失** | permission_mode 只存在于 PhasePolicy, PermissionPipeline 不消费它 |
| Step 5 Allow Rules | Layer 3 (部分) | 一致 |
| Step 6 canUseTool Callback | Layer 4 | 类似但细节不同 |

**根本问题**: 我们的 PermissionPipeline 不知道 permission_mode 的存在。

---

## 二、permissionMode 对评估的影响

### bypassPermissions

| CC 行为 | 我们的行为 |
|---------|-----------|
| Step 4 直接放行, skip Step 5-6 | ❌ 不被识别, fallthrough 到 Layer 4 |
| deny/ask 规则仍然生效 | 目前正确 |
| allowed_tools 不约束此模式 | ❌ 我们约束 (in _is_tool_visible) |
| 子代继承此模式且无法覆盖 | ❌ 不适用(模式不存在) |

### acceptEdits

| CC 行为 | 我们的行为 |
|---------|-----------|
| Write/Edit 自动批准 | ❌ 不被识别 |
| 文件系统 shell (mkdir, touch, rm, mv, cp, sed) 自动批准 | ❌ 不被识别 |
| Bash 非文件系统命令仍需提示 | ❌ 全部走 pipeline |
| 仅工作目录内路径有效 | ✅ _check_path 存在 |

### plan

| CC 行为 | 我们的行为 |
|---------|-----------|
| 文件编辑**从不**自动批准, allow rules 也无法覆盖 | ✅ PhasePolicy.is_tool_blocked_by_permission_mode 实现 |
| Read-only 工具正常工作 | ✅ 正常 |
| 编辑操作始终路由到 canUseTool callback | ❌ 直接返回错误消息, 不走 callback |

### dontAsk

| CC 行为 | 我们的行为 |
|---------|-----------|
| 仅预批准的工具可运行 | ❌ 不被识别 |
| canUseTool callback **从不调用** (与 default 的关键区别) | ❌ 无区别 |
| AskUserQuestion / requiresUserInteraction 的 MCP 被拒绝 | ❌ 不适用 |

---

## 三、其他架构差距

### 3.1 裸名 vs 作用域 deny 规则

| 方面 | CC | 我们 |
|------|-----|------|
| 裸名 `"Bash"` | **注册时从上下文中移除工具定义** — 模型看不到它 | PolicyAwareToolRegistry 用 denied_tools 过滤 `_is_tool_visible` ✅ 效果类似 |
| 作用域 `"Bash(rm *)"` | 调用时在 Step 2 检查 | Layer 3 `check_scoped_rules` ✅ |
| 裸名在 bypass 下是否生效 | 生效 (Step 2 在 Step 4 之前) | ✅ `_is_tool_visible` 在注册层过滤, bypass 无法绕过 |

### 3.2 拒绝跟踪 (Denial Tracking / Circuit Breaker)

| CC | 我们 |
|-----|------|
| `maxConsecutive: 3` — 同一工具连续被拒 | ❌ 无此机制 |
| `maxTotal: 20` — 总拒绝上限 | ❌ 无此机制 |
| 触发时告知模型改变策略 | ❌ 无对应提示 |

我们的 `CircuitBreaker` 在 `agent/circuit_breaker.py` 中, 但语义不同 (用于检测死循环, 非权限拒绝频率)。

### 3.3 prePlanMode 保存/恢复

| CC | 我们 |
|-----|------|
| 进入 plan 时: 保存 `mode` 到 `prePlanMode` | `plan_mode_tool.py` 通过 `_pending_mode_switch` 通知主循环 |
| plan 中: 强制只读, 所有写操作拒绝 | ✅ PhasePolicy.is_tool_blocked_by_permission_mode |
| 退出 plan 时: 从 `prePlanMode` 恢复 `mode` | ❌ 退出时没有恢复到之前的 permission_mode |
| Circuit breaker: 如果 auto mode gate 在 plan 期间被禁用, fallback 到 default | ❌ 无此防护 |

### 3.4 Background / Headless 自动拒绝

| CC | 我们 |
|-----|------|
| `shouldAvoidPermissionPrompts = true` 时自动拒绝 | Layer 4 `_confirm_callback is None → DENY` 效果类似 |
| 后台 agent 使用权限快照 | ❌ 无此概念 |

### 3.5 subagent 权限继承

| CC | 我们 |
|-----|------|
| Parent 用 `bypassPermissions/acceptEdits/auto` 时, 所有子代继承且**无法覆盖** | ❌ 未实现 |
| Child 的 `permissionMode` frontmatter 只在 parent 为 default 时生效 | ❌ 未实现 |

---

## 四、改进计划 (批次数)

### Batch P1: 重构 PermissionPipeline 评估顺序 (5 个文件)

**核心目标**: 将 permission_mode 引入 PermissionPipeline, 对齐 CC 6 步评估。

| 文件 | 改动 |
|------|------|
| `hitl/pipeline.py` | 重构 `check_permission()` 为 6 步: hooks → deny → ask → mode → allow → callback |
| `hitl/pipeline.py` | Step 4: 新增 `_layer4_permission_mode()` — 消费 `self._permission_mode` |
| `hitl/pipeline.py` | 新增 `set_permission_mode()` / `get_permission_mode()` 接口 |
| `hitl/pipeline.py` | bypassPermissions: Step 4 放行, skip 5-6 |
| `hitl/pipeline.py` | acceptEdits: Step 4 批准 Write/Edit/filesystem shell |
| `hitl/pipeline.py` | plan: 编辑操作路由到 callback (而不是直接拒绝) |
| `hitl/pipeline.py` | dontAsk: Step 6 跳过, 未匹配直接拒绝 |
| `agent/v2/runtime.py` | 在创建 registry/permission pipeline 时传入 `spec.permission_mode` |
| `agent/v2/registry_builder.py` | 将 `spec.permission_mode` 传递到 PermissionPipeline |
| `agent/policy_registry.py` | `PolicyAwareToolRegistry.execute_tool()` 将 permission_mode 同步到 PermissionPipeline |
| `agent/v2/subagent_registry_factory.py` | 子代的 permission_mode 传递给 PermissionPipeline |

### Batch P2: 拒绝跟踪 (Denial Tracking) (2 个文件)

| 文件 | 改动 |
|------|------|
| `hitl/pipeline.py` | 新增 `_denial_counter: dict[str, int]` — 跟踪连续拒绝 |
| `hitl/pipeline.py` | maxConsecutive=3 时返回特殊错误, 提示模型改变策略 |
| `hitl/pipeline.py` | 全局 `_total_denials: int`, max=20 |
| `agent/core.py` | 拒绝错误消息包含"改变策略"指令 |

### Batch P3: prePlanMode 保存/恢复 + subagent 权限继承 (3 个文件)

| 文件 | 改动 |
|------|------|
| `agent/v2/models.py`/pipeline.py | `PermissionPipeline` 新增 `_saved_permission_mode` 用于 prePlanMode |
| `tools/plan_mode_tool.py` | EnterPlanMode 保存当前 mode → `_saved_permission_mode` |
| `tools/plan_mode_tool.py` | ExitPlanMode 恢复 `_saved_permission_mode` |
| `agent/v2/runtime.py` | `spawn_agent()` 中子代继承父的 permission_mode (bypass/acceptEdits/auto 不可覆盖) |
| `agent/v2/runtime.py` | Child frontmatter `permissionMode` 仅在 parent mode 为 default 时生效 |

### Batch P4: Background/Headless 权限快照 (2 个文件)

| 文件 | 改动 |
|------|------|
| `hitl/pipeline.py` | `_layer4_prompt()` 区分 auto_approve 模式 (--auto-approve) 和 headless 模式 |
| `entry/cli.py` | `--auto-approve` 时设置 `permission_mode = bypassPermissions` (安全降级) |
| `entry/cli.py` | `--agent plan --plan-action execute` 时自动 bypass 交互式确认 |

---

## 五、执行顺序

```
P1 (PermissionPipeline 重构) → P2 (拒绝跟踪) → P3 (prePlanMode+继承) → P4 (headless)
```

每个批次完成后:
```bash
pytest tests/test_plan_approval.py tests/test_plan_prompt_contract.py tests/test_cli_v2_orchestration.py tests/test_hooks.py -q
pytest tests/test_cc_alignment_features.py -q
git commit -m "Batch P<N>: <description>"
```
