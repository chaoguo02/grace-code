# Plan Mode CC 逐项对比

---

## 我们的完整 Plan 流程（逐函数追踪）

### 阶段 1：进入 Plan Mode

```
entry/cli.py:330  --agent plan
  → entry/modes/v2_runner.py:197  run_v2_mode(agent_name="plan")
    → 查找 _BUILTIN_AGENTS["plan"] → intent=ANALYSIS, permission_mode="plan"
    → runtime.create_root_session(agent_name="plan")
    → runtime.run_session(session.id, agent_name="plan", intent=ANALYSIS)
      → agent/core.py:292  _run_body()
        → agent/session/agent_factory.py:146  验证 spec.permission_mode=="plan"
                                              要求 intent=ANALYSIS + delegation=READ_ONLY
        → agent/session/registry_builder.py:82  build_registry_for_session(plan_spec)
          → 声明工具集: _DEFAULT_READONLY_TOOLS
            {Read, Glob, Grep, file_view, WebFetch, WebSearch, git_status, git_diff, Bash,
             artifact_list, artifact_read, ...}
          → 创建 PhasePolicy(allowed_tools=..., permission_mode="plan")
```

**关键代码**：`_DEFAULT_READONLY_TOOLS` 包含 Bash（P4 时加入）。Plan 的 permission_mode 在 agent definition 中设为 "plan"。

### 阶段 2：工具调用时的权限拦截

```
agent/core.py:_run_body()
  → registry.execute_tool(name, params)
    → core/base.py:692  PermissionPipeline.check(tool, params)
      → Step 2: _layer2_hooks() → HookDispatcher.dispatch(PreToolUse)
      → Step 3: Deny Rules
      → Step 4: _layer4_permission_mode(tool_name)            ← 核心拦截点
        → PhasePolicy.is_tool_blocked_by_permission_mode(name)
          → permission_mode=="plan" → tool in {"Write", "Edit"}?  ← 刚修，不再拦 Bash
      → Step 5: Allow Rules
      → Step 6: canUseTool Callback
```

**关键代码**：`core/policy.py:183-184`。Plan mode 在 Step 4 拦截 Write/Edit。Bash 不拦截——允许只读命令，危险命令由 L0 `_BLOCKED_PATTERNS` 兜底。

### 阶段 3：Plan 执行

```
run_v2_mode() ANALYSIS 分支
  → runtime.run_session(session.id, "plan", intent=ANALYSIS)
    → agent 只用只读工具 → 产生 plan text
  → runtime_prompt_builder.py:63  如果 spec.permission_mode=="plan":
      注入 Plan mode system prompt (来自 prompts/modes/plan.md)
      注入 JSON contract 格式要求
  → 返回 result (plan text)
```

### 阶段 4：Plan 审批

```
run_v2_mode()
  → _plan_approval_loop()  (Batch C 提取)
    → 保存 Markdown plan 到磁盘
    → best-effort JSON contract 提取
    → interaction.show_plan() → 用户选择 [1]Execute [2]Edit [3]Re-plan [4]Save [5]Abort
    → [1] Execute:
      → run_v2_mode(agent_name="build", plan_file=plan_path)
        → 注入 [PLAN CONTEXT] 到 build_messages
        → 创建新 root session → 以 EDIT intent 执行
```

---

## CC 的完整 Plan 流程

### 阶段 1：进入 Plan Mode

```
两个入口:
  A) 用户 /plan → handlePlanModeTransition(currentMode, 'plan')
  B) 模型调 EnterPlanModeTool → 需要用户审批

→ handlePlanModeTransition(from, to)          [src/bootstrap/state.ts:1349]
    → 保存当前 mode 到 prePlanMode

→ prepareContextForPlanMode()                  [src/utils/permissions/permissionSetup.ts]
    → 创建新 ToolPermissionContext, mode='plan'
    → 工具只保留 isReadOnly()==true 的

→ context.setAppState → toolPermissionContext 更新
```

### 阶段 2：工具调用时的权限拦截

```
工具调用 → hasPermissionsToUseToolInner()
  → plan mode 下调用 isReadOnly() 判断:
    → Read/Grep/Glob/Agent: isReadOnly()=true → 允许
    → Write/Edit/Bash: isReadOnly()=false → 拒绝
```

**⚠️ CC 的已知 Bug**：官方文档和源码注释说 `isReadOnly()` 是唯一的 gatekeeper，但实际测试表明 Plan mode 下的写操作**没有被工具层拦截**——只是 prompt 层告诉模型不要用（Issue #19874, #14570）。

> "The router dispatches Edit, Write, and Bash exactly the same in plan mode as it does in default mode. The only difference is the string the model sees."
> — CC 源码分析

### 阶段 3：Plan 执行

```
5-phase 工作流 (默认):
  1. Initial Understanding → 启动 Explore Agent
  2. Design → 启动 Plan Agent(s), 1-3 个根据订阅级别
  3. Review → 合成结果, 向用户提问
  4. Final Plan → 写入 ~/.claude/plans/<slug>.md
  5. Call ExitPlanMode → 提交审批

迭代工作流 (备选):
  Explore → Update plan file → Ask user → 重复
```

### 阶段 4：Plan 审批 + 退出

```
模型调 ExitPlanModeV2Tool({ plan, allowedPrompts })
  → 验证当前在 Plan Mode
  → 用户审批 (可以编辑 plan 文件)
    → Approve / Edit then approve / Reject

→ handlePlanModeTransition(plan, saved_prePlanMode)
    → 恢复 prePlanMode (default/auto/bypassPermissions)
    → Circuit breaker: 如果 auto mode gate 在 plan 期间禁用了, fallback 到 default

→ applyPermissionUpdate() → 恢复原权限上下文
→ 注入 prompt-based permissions (allowedPrompts):
    模型声明 [{ tool: 'Bash', prompt: 'run tests' }]
    → 审批后这些命令自动允许
```

---

## 逐项对比

| 步骤 | CC | forge-agent | 对齐？ |
|------|-----|-------------|--------|
| **进入方式** | `/plan` 命令 + `EnterPlanModeTool` (含用户审批) | `EnterPlanModeTool` (设置 `_pending_mode_switch`) | ⚠️ 缺少交互模式审批 |
| **状态保存** | `prePlanMode` 保存到 STATE | `_pending_mode_switch` 设标志 | ✅ |
| **工具过滤** | `isReadOnly()` 过滤工具列表 | `_DEFAULT_READONLY_TOOLS` 预过滤 + Step 4 拦截 Write/Edit | ✅ 我们更强 |
| **强制执行** | ❌ 已知 Bug: 仅 prompt 层, 无工具层拦截 | ✅ Step 4 工具层拦截 `is_tool_blocked_by_permission_mode` | ✅ 我们更好 |
| **Bash 可用性** | Plan agent 有 Bash, 命令级限制 | Plan agent 有 Bash, Step 4 不拦截, L0+管道防护 | ✅ |
| **Plan 文件** | `~/.claude/plans/<slug>.md` | `state/plans/<hash>.md` | ✅ |
| **系统提示** | 全量 + 稀疏交替注入 (~1200→0→75 tokens) | 注入 `prompts/modes/plan.md` + JSON contract 格式 | ⚠️ 我们无节流 |
| **审批 UI** | Approve / Edit then approve / Reject | [1]Execute [2]Edit [3]Re-plan [4]Save [5]Abort | ✅ |
| **审批后** | 同一 Session 恢复 prePlanMode + 注入 prompt-based permissions | 创建新 build root session + 注入 [PLAN CONTEXT] | ⚠️ 不等价 |
| **prompt-based permissions** | 模型声明需要的 Bash 命令, 审批后自动允许 | ❌ 无 | ❌ 缺失 |
| **退出恢复** | `handlePlanModeTransition` 恢复 mode + circuit breaker fallback | `_check_pending_mode_switch` 清除标志, 恢复 PhasePolicy | ✅ |
| **Plan plan 文件编辑** | 用户可以直接编辑 plan 文件 | [2] Edit plan file → 暂停等用户按键 | ✅ |

---

## 差距清单

### Gap 1：进入 Plan 模式缺少用户审批（模型自主进入时）

**CC**: `EnterPlanModeTool.call()` → `checkPermissions` 返回 `ask` → 用户必须确认。  
**我们**: `EnterPlanModeTool.execute()` 直接设 `_pending_mode_switch`, 无交互式审批。

**正确做法**: 在交互模式(ch)下, `EnterPlanModeTool` 应触发 PermissionPipeline 的确认回调。Headless 模式(run)下不需要。

### Gap 2：退出 Plan 后 prompt-based permissions

**CC**: `ExitPlanModeV2Tool` 接受 `allowedPrompts: [{tool: 'Bash', prompt: 'run tests'}]`, 审批后这些命令自动允许。  
**我们**: 无此机制。

**正确做法**: 在 `ExitPlanModeTool` 的 params 中增加 `allowedPrompts`, 审批通过后在 PermissionPipeline 中注册为 session allow rules。

### Gap 3：系统提示节流

**CC**: 全量→无→稀疏→无→全量(每 25 轮)。  
**我们**: 每轮都注入完整 plan mode prompt + JSON contract 要求。

**正确做法**: 在 `runtime_prompt_builder.py` 中加入节流逻辑, 第 1 轮注入完整, 之后跳过, 每 5 轮注入稀疏提醒, 每 25 轮重新注入完整。

### Gap 4：退出后 Session 连续性

**CC**: 同一 Session, 恢复 `prePlanMode`, 保留对话历史。  
**我们**: 创建新 build root session, plan 上下文通过 plan file 注入。

**正确做法**: `_plan_approval_loop` 中的 TRIGGER_BUILD 不应递归创建新 session。应改为: 在同一 session 上切换 intent 为 EDIT + 恢复 permission_mode 为 default + 继续执行。

---

## 修改优先级

| Gap | 优先级 | 原因 |
|-----|--------|------|
| Gap 4 (Session 连续性) | P1 | 架构差异最大 |
| Gap 2 (prompt-based permissions) | P2 | 增强用户体验 |
| Gap 3 (系统提示节流) | P2 | 节省 token |
| Gap 1 (进入审批) | P3 | headless 模式不适用 |
