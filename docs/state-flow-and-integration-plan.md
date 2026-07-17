# 全系统状态流转图 + 缺失能力接入方案

> 目标: 先明确每个系统的函数级流转，再确定缺失能力挂载在哪里

---

## 一、Agent 主循环流转

```
agent/core.py:292  ReActAgent._run_body()
  │
  ├─ for step in range(1, max_steps + 1):           [line 554]
  │   │
  │   ├─ runtime_controller.check()                  [line 584]
  │   │   → Circuit breaker / Max steps / Budget / Consecutive failures
  │   │
  │   ├─ _build_messages()                           [line 1498]
  │   │   → MicroCompact → compaction → recovery     [Z-1~5, Y-1~4]
  │   │
  │   ├─ backend.complete(messages, tools)            [line ~640]
  │   │   → LLM 响应 → Action(TOOL_CALL | FINISH | GIVE_UP)
  │   │
  │   ├─ [如果是 TOOL_CALL]:
  │   │   ├─ _check_pending_mode_switch()            [line 1218]
  │   │   │   → agent/mode_switching.py:check_pending_mode_switch()
  │   │   │
  │   │   ├─ registry.execute_tool(name, params)     [tools/base.py:653]
  │   │   │   ├─ PermissionPipeline.check()           [hitl/pipeline.py:202]
  │   │   │   │   ├─ Step 1: validateInput            ← 这里可以挂 updatedInput ❌
  │   │   │   │   ├─ Step 2: PreToolUse hooks          ← 这里触发 dispatcher
  │   │   │   │   │   → HookDispatcher.dispatch(PRE_TOOL_USE, ctx)
  │   │   │   │   │   → 返回 BLOCK / APPROVE / (缺少 ASK, updatedInput)
  │   │   │   │   ├─ Step 3: Deny Rules
  │   │   │   │   ├─ Step 4: Permission Mode
  │   │   │   │   ├─ Step 5: Allow Rules
  │   │   │   │   └─ Step 6: canUseTool Callback
  │   │   │   └─ tool.execute(params)                 ← 实际执行
  │   │   │
  │   │   └─ [执行后]                                  ← 这里可以挂 PostToolUse output 替换 ❌
  │   │       → 当前: 直接返回 ToolResult
  │   │       → 缺少: PostToolUse hook 可以在返回前替换 output
  │   │
  │   ├─ [如果是 FINISH]:
  │   │   ├─ completion_guard.check()                 [line 851]
  │   │   ├─ _run_stop_hook()                         [line 871]
  │   │   │   → HookDispatcher.dispatch(STOP, ctx)     ← 纯 dispatcher (N-1)
  │   │   └─ _finish_run()
  │   │
  │   └─ [观察结果处理]                                 [line ~1100]
  │
  └─ return RunResult
```

---

## 二、Plan mode 流转

```
entry/modes/v2_runner.py:197  run_v2_mode()
  │
  ├─ [agent_name == "plan"]:
  │   ├─ runtime.create_root_session(agent_name="plan")
  │   ├─ runtime.run_session(session.id, agent_name="plan")
  │   │   └─ agent/core.py:_run_body()
  │   │       → 只读分析 → 产生 plan text
  │   │
  │   └─ _plan_approval_loop()                        [Batch C 提取]
  │       ├─ 保存 Markdown plan 到磁盘
  │       ├─ best-effort JSON contract 提取
  │       ├─ interaction.show_plan() → 用户审批
  │       └─ [用户选 Execute]:
  │           └─ run_v2_mode(agent_name="build", plan_file=plan_path)
  │               └─ 创建新 root session → 注入 plan content → 执行
  │
  └─ [agent_name == "build"]:
      ├─ if plan_file: 注入 [PLAN CONTEXT]
      ├─ runtime.create_root_session(agent_name="build")
      └─ runtime.run_session()
          → 正常 EDIT intent 执行
```

Plan mode 信号流转:
```
tools/plan_mode_tool.py:57  EnterPlanModeTool.execute()
  → registry._pending_mode_switch = {"mode": "plan"}

agent/core.py:1218  _check_pending_mode_switch()
  → agent/mode_switching.py:check_pending_mode_switch()
  → PhasePolicy.permission_mode = "plan"
  → PermissionPipeline._layer4_permission_mode() → 阻止 Write/Edit/Bash
```

---

## 三、Subagent 派发流转

```
agent/session/task_tool.py  AgentTool.execute()
  │
  ├─ 解析 subagent_type / description / prompt / isolation / execution_placement
  ├─ is_fork = (subagent_type == "fork")
  │
  ├─ [named subagent]:
  │   ├─ definition = agent_registry.get(subagent_type)
  │   ├─ _build_subagent_prompt()                     ← _SUBAGENT_PROTOCOL (Batch D)
  │   ├─ AgentSpawnRequest.named(definition, ...)
  │   │   └─ execution_placement = definition.background ? BACKGROUND : FOREGROUND
  │   └─ spawn_agent(request)
  │
  ├─ [fork subagent]:
  │   ├─ requires spawn_context (parent snapshot)
  │   ├─ AgentSpawnRequest.fork(workspace_mode, ...)
  │   └─ spawn_agent(request)
  │
  └─ spawn_agent(request)                             [runtime.py:732]
      │
      ├─ 验证: parent.can_spawn / delegation_policy.permits()
      ├─ 创建 child session (SessionStore)             [line 814]
      ├─ [fork]: 继承 parent messages → child DB        [line 864]
      ├─ [named]: 注入 child definition messages
      │
      ├─ _resolve_child_permission_mode()
      │   → child.metadata["permission_mode_override"]
      │
      ├─ connect_agent_servers(definition)             ← MCP lifecycle [M1]
      │
      ├─ execute = _execute_child_session(child, ...)
      │   │
      │   └─ run_child_agent()                         [subagent.py:53]
      │       │
      │       ├─ build_restricted_registry()            [subagent_registry_factory.py]
      │       │   └─ build_registry_for_session()
      │       │       └─ per-session HookDispatcher     ← 克隆 + agent hooks [N-2+P]
      │       │
      │       ├─ 构建 AgentConfig:
      │       │   ├─ cfg.hook_dispatcher = per-session dispatcher  [P fix]
      │       │   ├─ cfg.stop_hook_event = SUBAGENT_STOP
      │       │   └─ cfg.circuit_breaker = clone_for_subagent()
      │       │
      │       ├─ agent = ReActAgent(backend, registry, config)
      │       └─ agent.run(task, event_log)
      │           └─ _run_body() → ... → 返回 AgentRunResult
      │
      └─ foreground: 返回 result
          background: _start_background_execution(execute)
```

---

## 四、Hook 派发流转

```
PermissionPipeline.check()                            [hitl/pipeline.py:202]
  │
  ├─ Step 2: _layer2_hooks()
  │   └─ HookDispatcher.dispatch(HookEvent.PRE_TOOL_USE, ctx)
  │       └─ Phase 2: registry.find_external(event, matcher_subject, tool_input)
  │           → 返回 ExternalHookConfig 列表
  │           → 依次执行 command hooks
  │           → Exit 0=success, Exit 2=BLOCK
  │           ⚠️ 缺少: Exit 0 + updatedInput → 修改 params
  │           ⚠️ 缺少: "ask" decision → 路由到 permission dialog
  │
  │ HookContext 结构:
  │   event, tool_name, tool_input, session_id, ...
  │   ⚠️ 缺少: 没有返回 updatedInput 的字段
  │
  └─ [工具执行后]
      → 当前: 直接使用原始 ToolResult
      ⚠️ 缺少: PostToolUse hook 可以替换 output
      ⚠️ 缺少: PostToolUse hook 可以注入 additionalContext
```

---

## 五、缺失能力清单 + 接入点

### 1. PreToolUse: updatedInput (修改工具参数)

**CC 行为**: PreToolUse hook 返回 `{"permissionDecision":"allow", "updatedInput":{...}}`，修改后的参数传给工具执行。

**接入点**: `hitl/pipeline.py:274-300` — `_layer2_hooks()`

**改动**:
1. `hooks/protocol.py` — `ExitCode` 枚举扩展: `MODIFY(1)` — exit 1 表示修改参数但允许
2. `hooks/dispatcher.py:_dispatch()` — 收集 hook 返回的 `updatedInput`
3. `hitl/pipeline.py:_layer2_hooks()` — 如果有 updatedInput，替换 params
4. `tools/base.py:execute_tool()` — 使用修改后的 params 调用 tool.execute()

### 2. PostToolUse: updatedToolOutput (替换工具输出)

**CC 行为**: PostToolUse hook 返回 `{"decision":"block", "reason":"..."}` 或修改 `tool_response`。

**接入点**: `tools/base.py:703` — `execute_tool()` 返回前

**改动**:
1. 在 `execute_tool()` 返回前，dispatch POST_TOOL_USE hook
2. 如果 hook 返回 `updatedToolOutput`，替换 result.output
3. 如果 hook 返回 `additionalContext`，注入到 result

### 3. Stop hook: SubagentStop 自动转换

**CC 行为**: Subagent 的 Stop hook 自动转换为 SubagentStop 事件。父 agent 的 SubagentStop hook 可以在子代理完成时触发。

**接入点**: `agent/session/subagent.py:169` — `cfg.stop_hook_event = HookEvent.SUBAGENT_STOP`

**状态**: ✅ 已在 run_child_agent 中设置。无需额外工作。之前的报告误判为缺失。

---

## 六、实施顺序

```
Batch Q-1: PreToolUse updatedInput (修改工具参数)
  - hooks/protocol.py: 扩展 HookControl 枚举
  - hooks/dispatcher.py: 收集 updatedInput
  - hitl/pipeline.py: _layer2_hooks() 消费 updatedInput
  - tools/base.py: execute_tool() 使用修改后 params

Batch Q-2: PostToolUse updatedToolOutput (替换工具输出)
  - hooks/dispatcher.py: 新增 PostToolUse 阶段处理
  - tools/base.py: execute_tool() 执行后 dispatch PostToolUse
  - 支持 updatedToolOutput + additionalContext

Batch Q-3: 端到端验证
  - 编写 test hook 验证 updatedInput + updatedToolOutput 链路
  - 回归测试
```
