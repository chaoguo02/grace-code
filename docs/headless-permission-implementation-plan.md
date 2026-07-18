# Headless Interactive Permission — 分批实现计划

> 原则：不打补丁，从 Runtime 流转根本上对齐 Claude Code 的权限模型。每一步都有 CC 依据。

---

## 0. Claude Code 权限模型 (参考基准)

### 0.1 CC 的 6 层管线 (有序)

来自 [CC Permissions Docs](https://code.claude.com/docs/en/permissions) + [Agent SDK Permissions](https://code.claude.com/docs/en/agent-sdk/permissions):

```
1. PreToolUse Hooks      → 最先执行，可 deny/allow/pass (exit 0=pass, exit 2=block)
2. Deny Rules            → 匹配即拒绝，bypassPermissions 也无法绕过
3. Ask Rules             → 匹配则路由到 canUseTool callback（dontAsk 下直接 deny）
4. Permission Mode       → default | acceptEdits | plan | auto | dontAsk | bypassPermissions
5. Allow Rules           → 匹配即允许
6. canUseTool Callback   → 兜底交互回调（TTY prompt 或 headless control_request）
```

**关键规则**:
- Permission rules 由 Claude Code 执行，不由 model 决定
- Deny > Ask > Allow，不可颠倒
- 规则格式: `Tool(pattern)`, `Tool(param:value)`, 支持 `*` glob
- Headless 模式: `--permission-prompt-tool stdio` + `control_request/control_response` NDJSON 协议 → **CLI 阻塞等待响应，默认 60s 超时**

### 0.2 CC 的 6 种 Permission Mode

| Mode | 行为 |
|------|------|
| `default` (manual) | 敏感操作弹 prompt |
| `acceptEdits` | 自动批准文件编辑 + 常规文件命令 (mkdir, touch, mv, cp) |
| `plan` | 只读 |
| `auto` | 后台安全分类器自动审批 |
| `dontAsk` | allow 规则外的工具直接拒绝 |
| `bypassPermissions` | 全部自动批准（沙箱内使用） |

### 0.3 CC 的 Headless 协议 (我们 Web 模式的等价物)

```
CC headless:
  claude --output-format stream-json --input-format stream-json --permission-prompt-tool stdio
  
  当工具需要审批:
  → stdout: {"type":"control_request","request_id":"...","tool":"Edit","input":{...}}
  → CLI 阻塞等待 stdin
  → 用户/host 发送: {"type":"control_response","request_id":"...","decision":"allow","updatedInput":{...}}
  → CLI 继续执行

Forge Web 等价:
  当工具需要审批:
  → WS push: {"type":"approval_required","request_id":"...","tool":"Edit","params":{...}}
  → Agent 线程阻塞在 threading.Event
  → 前端用户点击 Allow/Deny
  → POST /api/sessions/{id}/tool-approve {"request_id":"...","decision":"allow"}
  → Event.set() → Agent 线程继续
```

---

## 1. 当前状态审计

### 1.1 已有的基础设施

| 组件 | 文件 | 状态 |
|------|------|------|
| 6层管线 | `hitl/pipeline.py:121-333` | ✅ 完整，CC 对齐 |
| Layer 1 (validateInput) | `pipeline.py:337-348` | ✅ |
| Layer 2 (PreToolUse Hooks) | `pipeline.py:352-389` | ✅ |
| Layer 3 (Deny/Ask/Allow Rules) | `pipeline.py:393-424` | ✅ glob 规则匹配 |
| Layer 4 (Permission Mode) | `pipeline.py:449-475` | ⚠️ 缺 `dontAsk` 处理 |
| Layer 4.5 (Prompt-based) | `pipeline.py:491-520` | ✅ |
| Layer 5 (Path Sandbox) | `pipeline.py:588-613` | ✅ |
| Layer 6 (Callback) | `pipeline.py:524-584` | ⚠️ 缺 WebSocket 回调 |
| 规则 DSL | `hitl/permission_rule.py` | ✅ `Tool(pattern)` 格式 |
| 配置加载 | `hitl/settings_loader.py` | ✅ 读 `.grace/settings.json` |
| Hook 系统 | `hooks/` | ✅ 10 种事件 |
| PhasePolicy | `core/policy.py:148` | ✅ permission_mode 字段 |
| PolicyAwareToolRegistry | `core/policy_registry.py:20` | ✅ |
| ToolRegistry.execute_tool | `core/base.py:550` | ✅ Pipeline 集成 |
| Mode Switching | `agent/mode_switching.py` | ✅ |
| SessionRuntime | `agent/session/runtime.py` | ✅ |
| 子代理 permission 继承 | `runtime.py:1630` | ⚠️ 不完整 |

### 1.2 当前问题的精确位置

**`hitl/pipeline.py:535-539`** — Layer 6 callback:
```python
if self._confirm_callback is None:
    return PermissionResult(
        decision=PermissionDecision.DENY,
        reason="interactive approval unavailable in headless mode",
    )
```

**`server/services/agent_service.py:88`** — Web 入口:
```python
approval_mode="auto",  # workaround: 全局自动批准
```

**`hitl/pipeline.py:449-475`** — Layer 4 `dontAsk` 缺失:
```python
if mode == "dontAsk":
    # ← 未处理! 直接返回 None，落到 Layer 6，然后被 deny
    pass
```

### 1.3 Runtime 流转分析

当前 runtime 的工具执行路径 (`agent/core.py` → `ToolRegistry.execute_tool()` → `PermissionPipeline.check()`):

```
ReActAgent._run_body()
  → step loop
    → LLM 返回 tool_calls
    → registry.execute_tool(name, params, thought)    [core/base.py:550]
      → pipeline.check(tool, params, thought)           [base.py:591, 同步阻塞]
        → Layer 1-5 评估
        → Layer 6: _layer6_callback()                   [pipeline.py:524]
          → if AUTO: return ALLOW (当前 workaround)
          → if PROMPT + callback: 阻塞等待用户输入 (CLI 的 terminal_confirm)
          → if PROMPT + no callback: DENY (我们的问题)
      → if DENY: return PERMISSION_DENIED error          [base.py:593-603]
      → tool.execute(actual_params)                      [base.py:625]
    → observation → history
```

**关键**: `pipeline.check()` 是同步的。CC 的 headless 协议也是同步阻塞的（CLI 阻塞在 stdin 等待 control_response）。所以 **阻塞 Agent 线程等待 Web 审批是正确的设计**。

---

## 2. Batch 1: WebSocket 交互审批回调 (P0, 预计 3-4 天)

### 2.1 目标

当工具需要交互审批时，Agent 线程阻塞等待，WebSocket 推送审批请求到前端，用户点击 Allow/Deny 后继续执行。

### 2.2 CC 依据

> "The CLI blocks waiting for a response with a default ~60 second timeout."
> — CC headless NDJSON control_request/control_response 协议

> "Permission rules are enforced by Claude Code, not by the model."
> — 权限决策必须由 Runtime 执行，不由 LLM 决定

### 2.3 实现文件

#### Step 1: `hitl/pipeline.py` — 新增 `WebConfirmCallback` 协议

```python
# 在 PermissionPipeline 中新增:

class WebConfirmCallback(Protocol):
    """CC-aligned: 异步等待 Web 端审批，阻塞 Agent 线程最多 timeout 秒。"""
    def wait_for_decision(
        self, request: PermissionRequest, timeout: float = 60.0
    ) -> PromptDecision:
        ...

# 修改 __init__ 增加参数:
def __init__(self, ..., web_confirm_callback: WebConfirmCallback | None = None):
    self._web_confirm_callback = web_confirm_callback

# 修改 _layer6_callback (line 524):
def _layer6_callback(self, tool_name, params, thought) -> PermissionResult:
    if self._approval_mode is ToolApprovalMode.AUTO:
        return ALLOW  # 显式 AUTO 模式

    # 1. 优先走 WebSocket 回调 (headless Web 模式)
    if self._web_confirm_callback is not None:
        request = PermissionRequest(...)
        decision = self._web_confirm_callback.wait_for_decision(
            request, timeout=60.0
        )
        return self._apply_decision(decision, tool_name, params)

    # 2. 其次走 TTY 回调 (CLI 模式)
    if self._confirm_callback is not None:
        ...

    # 3. 无回调 → deny
    return DENY("interactive approval unavailable")
```

**依据**: CC 的 `canUseTool` callback 支持两种形式: TTY prompt（CLI）和 `control_request`（headless）。我们的 `WebConfirmCallback` 就是 `control_request` 的等价物。

#### Step 2: `server/services/approval_broker.py` — **NEW**: 审批中介

```python
"""
ApprovalBroker — Web 审批的中介层。

每个 session 一个 broker 实例。Agent 线程调用 wait_for_decision() 阻塞，
WebSocket handler 调用 resolve() 唤醒。

这是 CC control_request/control_response 协议在 Web 环境下的等价实现。
"""

import threading
import uuid
from dataclasses import dataclass

@dataclass
class PendingApproval:
    request_id: str
    tool_name: str
    params: dict
    thought: str
    event: threading.Event       # Agent 线程阻塞在此
    decision: PromptDecision | None = None
    created_at: float = 0.0

class ApprovalBroker:
    """Per-session approval broker. Thread-safe."""

    def __init__(self, session_id: str):
        self._session_id = session_id
        self._pending: dict[str, PendingApproval] = {}
        self._lock = threading.Lock()

    def wait_for_decision(self, request, timeout=60.0) -> PromptDecision:
        """Agent 线程调用: 阻塞等待审批决策。"""
        req_id = uuid.uuid4().hex[:12]
        pending = PendingApproval(
            request_id=req_id,
            tool_name=request.tool_name,
            params=request.params,
            thought=request.thought,
            event=threading.Event(),
        )
        with self._lock:
            self._pending[req_id] = pending

        # TODO: push approval_required WS event here (via EventBus callback)

        if not pending.event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            return PromptDecision(action=PromptAction.DENY, note="Approval timed out")

        return pending.decision or PromptDecision(action=PromptAction.DENY)

    def resolve(self, request_id: str, decision: PromptDecision) -> bool:
        """WebSocket handler 调用: 提交审批决策。"""
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return False
        pending.decision = decision
        pending.event.set()
        return True
```

**依据**: CC 的 `control_request` 携带 `request_id`，`control_response` 通过 `request_id` 匹配。我们的 `ApprovalBroker` 用 `request_id` + `threading.Event` 实现相同的阻塞-唤醒语义。

#### Step 3: `agent/session/runtime.py` — 在 SessionRuntime 中集成 ApprovalBroker

```python
# 修改 SessionRuntime.__init__ 或 run_session():

# 为每个 session 创建一个 ApprovalBroker
approval_broker = ApprovalBroker(session_id)

# 创建 WebConfirmCallback 适配器
class RuntimeWebConfirmCallback:
    def __init__(self, broker, event_bus, session_id):
        self._broker = broker
        self._event_bus = event_bus
        self._session_id = session_id

    def wait_for_decision(self, request, timeout=60.0):
        # 1. 通过 EventBus 推送审批请求到前端
        if self._event_bus:
            self._event_bus.publish_raw(self._session_id, {
                "type": "approval_required",
                "request_id": request.tool_name + "_" + uuid4().hex[:8],
                "tool_name": request.tool_name,
                "params": request.params,
                "thought": request.thought,
            })
        # 2. 阻塞等待
        return self._broker.wait_for_decision(request, timeout)

# 在 pipeline 创建时注入:
pipeline = PermissionPipeline(
    ...,
    web_confirm_callback=RuntimeWebConfirmCallback(...),
)
```

#### Step 4: `server/routers/approvals.py` — 新增工具审批端点

```python
@router.post("/api/sessions/{session_id}/tool-approve")
async def approve_tool(
    session_id: str,
    body: ToolApprovalRequest,  # {request_id, decision: "allow"|"deny", note}
    service=Depends(get_service),
):
    """CC control_response 等价物: 前端提交工具审批决策。"""
    broker = service.get_approval_broker(session_id)
    decision = PromptDecision(
        action=PromptAction.ALLOW_ONCE if body.decision == "allow"
        else PromptAction.DENY,
        note=body.note or "",
    )
    ok = broker.resolve(body.request_id, decision)
    return {"resolved": ok}
```

#### Step 5: `web/src/stores/chatStore.ts` + `web/src/components/ChatView.tsx`

- 新增 `toolApproval: {request_id, tool_name, params, thought} | null` 状态
- `handleWsEvent` 处理 `approval_required` 事件
- ChatView 的 composer 区域展示审批 UI (Allow / Deny 按钮，参考 plan approve/reject)
- 调用 `POST /api/sessions/{id}/tool-approve`

### 2.4 Runtime 流转验证

```
Agent 线程 (background)            Event Loop (main)            Frontend
─────────────────────────          ─────────────────            ────────
ReActAgent._run_body()
  → registry.execute_tool("Edit")
    → pipeline.check(tool, params)
      → _layer6_callback()
        → web_confirm_callback
          .wait_for_decision()     → EventBus.publish_raw()    → WS push approval_required
          → threading.Event              │                      → 用户看到审批 UI
            .wait(timeout=60)            │                      → 用户点击 Allow
            ║ BLOCKED ║                  │                      → POST /tool-approve
            ║          ║           ← resolve() ←───────────────
            ║          ║           Event.set()
          → return ALLOW
        → tool.execute(params)
    → observation → history
```

**CC 等价**: CC CLI 的 `control_request` → stdin 阻塞 → `control_response` → 继续。我们的实现只是把 stdin/stdout 换成了 WebSocket + HTTP。

---

## 3. Batch 2: `dontAsk` 模式 + 规则引擎完善 (P0, 预计 2-3 天)

### 3.1 目标

实现 `dontAsk` 权限模式: allow 列表外的工具在 Layer 4 直接拒绝，不到 Layer 6。

### 3.2 CC 依据

> "dontAsk: Auto-denies tools unless pre-approved via /permissions or permissions.allow rules."
> — CC Permission Modes

> "Deny rules are evaluated in order: deny, then ask, then allow. The first match determines the outcome."
> — CC Permission Rules

### 3.3 实现文件

#### Step 1: `hitl/pipeline.py:_layer4_permission_mode()` — 增加 `dontAsk`

```python
def _layer4_permission_mode(self, tool_name):
    mode = self._permission_mode
    if not mode or mode == "default":
        return None
    if mode == "bypassPermissions":
        return ALLOW("bypassPermissions mode")
    if mode == "acceptEdits":
        if tool_name in {"Write", "Edit"}:
            return ALLOW("acceptEdits: auto-approved")
        return None
    if mode == "plan":
        if tool_name in {"Write", "Edit", "Bash"}:
            return DENY("plan mode: read-only")
        return None
    if mode == "dontAsk":
        # CC-aligned: allow 列表中已批准的才通过，其余在 Layer 4 拒绝
        # 注意: deny 规则已在 Layer 3 处理，这里只处理 allow
        for rule in self._allow_rules + self._session_rules:
            if rule.tool_name == tool_name or rule.tool_name == "*":
                return ALLOW("dontAsk: pre-approved tool")
        return DENY(
            f"dontAsk mode: '{tool_name}' is not in the allow list. "
            "Add it via /permissions or permissions.allow in settings.json."
        )
    return None
```

**依据**: CC 的 `dontAsk` 模式 "auto-denies tools unless pre-approved"。注意这里 `dontAsk` 的拒绝发生在 Layer 4，不需要走到 Layer 6 callback，与 CC SDK 的逻辑一致。

#### Step 2: `server/services/agent_service.py` — Web 默认使用 `dontAsk`

```python
# 在 agent_service.py 的 pipeline 创建处设置 permission_mode:
pipeline.set_permission_mode("dontAsk")

# 同时加载 .grace/settings.json 的 allow 规则
from hitl.settings_loader import load_permission_settings
settings = load_permission_settings(repo_path)
for rule_text in settings.get("allow", []):
    pipeline._allow_rules.append(PermissionRule.parse(rule_text))
```

#### Step 3: 规则配置路径标准化

- 项目级: `.forge-agent/settings.json`（版本控制）
- 本地级: `.forge-agent/settings.local.json`（gitignore）
- 用户级: `~/.forge-agent/settings.json`

加载顺序（后加载的优先级更高）:
1. 内置默认值 (`_builtin_defaults()`)
2. 用户级 settings
3. 项目级 settings
4. 本地 settings (最高优先级)
5. 会话级规则 (来自 "Always Allow" 的 ALWAYS_ALLOW 选择)

**依据**: CC 的配置层级: `Managed > CLI flags > settings.local.json > settings.json > ~/.claude/settings.json`

---

## 4. Batch 3: Permission Mode 继承与流转 (P1, 预计 2-3 天)

### 4.1 目标

确保 permission_mode 在以下场景中正确流转:
1. Plan → Build mode 切换
2. 父会话 → 子代理
3. 跨 session 的规则持久化

### 4.2 CC 依据

> "When parent uses bypassPermissions, acceptEdits, or auto, all subagents inherit that mode and cannot override it."
> — CC Agent SDK Permissions

> "Permission rules are saved to .claude/settings.local.json and apply to future sessions."
> — CC Permissions Docs

### 4.3 实现文件

#### Step 1: `agent/session/runtime.py:_resolve_child_permission_mode()` — 完整继承

```python
def _resolve_child_permission_mode(
    self, parent_pipeline, child_definition
) -> str:
    """CC-aligned: 子代理继承父管线 permission mode。

    CC 规则:
    - 父 = bypassPermissions → 子必须 bypassPermissions (不可降级)
    - 父 = acceptEdits/auto → 子继承 (不可升级到 bypassPermissions)
    - 父 = plan → 子 = plan (只读)
    - 父 = dontAsk → 子 = dontAsk + 父的 allow 规则
    - 否则 → 子使用自己的 AgentDefinition.permission_mode
    """
    parent_mode = parent_pipeline.permission_mode

    # bypassPermissions 是最高权限，强制继承
    if parent_mode == "bypassPermissions":
        return "bypassPermissions"

    # plan 是只读，子代理不能写
    if parent_mode == "plan":
        return "plan"

    # acceptEdits/auto/dontAsk: 父管线优先
    if parent_mode in ("acceptEdits", "auto", "dontAsk"):
        child_mode = child_definition.permission_mode
        # 子代理不能升级权限
        if child_mode == "bypassPermissions":
            return parent_mode
        return child_mode or parent_mode

    # default 模式: 子代理用自己的配置
    return child_definition.permission_mode or "default"
```

#### Step 2: `agent/mode_switching.py` — Plan→Build 自动切换 permission mode

```python
def check_pending_mode_switch(registry, history):
    switch = getattr(registry, "_pending_mode_switch", None)
    if not switch:
        return

    mode = switch.get("mode", "")
    if mode == "plan":
        # 进入 plan 模式: 保存当前 mode，切换到 plan
        pipeline.save_pre_plan_mode()
        pipeline.set_permission_mode("plan")
    else:
        # 退出 plan 模式: 恢复到之前保存的 mode
        # 关键: 这是 Build 阶段，需要 write 权限
        restored = pipeline.restore_pre_plan_mode()
        # 如果之前是 dontAsk，allow 规则已从 settings 加载，直接恢复即可
```

#### Step 3: `agent/session/subagent.py` — 子代理 pipeline 继承父管线规则

```python
# 在子代理创建时:
child_pipeline = parent_pipeline.for_agent(child_definition.name)
child_pipeline.set_permission_mode(
    _resolve_child_permission_mode(parent_pipeline, child_definition)
)
# 继承父会话的 session_rules (Always Allow 规则)
for rule in parent_pipeline.session_rules:
    if rule.tier == PermissionRuleTier.ALLOW:
        child_pipeline._session_rules.append(rule)
```

---

## 5. Batch 4: Web 审批 UI + 持久化 (P1, 预计 2 天)

### 5.1 目标

- 前端审批卡片（工具名 + 参数 + Allow/Deny 按钮）
- "Always Allow" 功能（持久化到 settings.local.json）
- 审批超时处理

### 5.2 实现文件

- `web/src/components/ToolApprovalCard.tsx` — **NEW**
- `web/src/stores/chatStore.ts` — `toolApproval` 状态
- `web/src/components/ChatView.tsx` — 审批 UI
- `hitl/settings_loader.py` — `save_rule_to_settings()` 已存在 ✅

---

## 6. 完整 Runtime 流转（最终状态）

```
SessionRuntime.run_session(session_id, agent_name, task_description, intent)
  │
  ├─ 1. 加载权限配置
  │    load_permission_settings(repo_path)
  │    → deny/ask/allow 规则
  │    → permission_mode (from AgentDefinition or CLI)
  │
  ├─ 2. 创建 ApprovalBroker (per session)
  │    broker = ApprovalBroker(session_id)
  │
  ├─ 3. 构建 PermissionPipeline
  │    pipeline = PermissionPipeline(
  │        rules=loaded_rules,
  │        web_confirm_callback=RuntimeWebConfirmCallback(broker, event_bus),
  │        approval_mode=PROMPT,  # 走 Layer 6 回调
  │        permission_mode=effective_mode,
  │    )
  │
  ├─ 4. 构建 ToolRegistry
  │    registry = PolicyAwareToolRegistry(base_registry, phase_policy)
  │    registry._permission_pipeline = pipeline
  │
  ├─ 5. ReActAgent.run()
  │    └─ _run_body() step loop
  │         ├─ LLM 调用 → action.tool_calls
  │         ├─ registry.execute_tool(name, params, thought)
  │         │    ├─ pipeline.check(tool, params, thought)        ← synchronous
  │         │    │    ├─ L1: validateInput()
  │         │    │    ├─ L2: PreToolUse Hooks
  │         │    │    ├─ L3: Deny Rules → DENY if matched
  │         │    │    ├─ L3: Ask Rules  → route to L6
  │         │    │    ├─ L4: Permission Mode (dontAsk/plan/bypassPermissions/acceptEdits)
  │         │    │    ├─ L5: Allow Rules → ALLOW if matched
  │         │    │    └─ L6: web_confirm_callback.wait_for_decision()
  │         │    │         ├─ EventBus.push("approval_required", {request_id, tool, params})
  │         │    │         ├─ threading.Event.wait(timeout=60)
  │         │    │         │    └─ [Frontend] 用户点击 Allow/Deny
  │         │    │         │    └─ POST /tool-approve → broker.resolve()
  │         │    │         │    └─ Event.set()
  │         │    │         └─ return ALLOW/DENY
  │         │    ├─ if DENY: return PERMISSION_DENIED error
  │         │    └─ tool.execute(actual_params)
  │         └─ observation → history → next step
  │
  └─ 6. Session 完成
       pipeline.stats → 记录在 session metadata
```

---

## 7. 执行顺序

| Batch | 内容 | 依赖 | 预计 |
|-------|------|------|------|
| **Batch 1** | WebSocket 审批回调 (ApprovalBroker + pipeline 集成) | 无 | 3-4 天 |
| **Batch 2** | `dontAsk` 模式 + 规则引擎完善 | Batch 1 | 2-3 天 |
| **Batch 3** | Permission mode 继承与流转 | Batch 1 | 2-3 天 |
| **Batch 4** | Web 审批 UI + 持久化 | Batch 1 | 2 天 |

**总计**: 约 2 周 (可以 Batch 3/4 并行)

---

## 8. 不做什么（明确排除）

- ❌ 不做 `auto` 模式（需要 LLM 分类器，CC 的也是实验性的）
- ❌ 不做 MCP 工具权限匹配（`mcp__<server>__<tool>` 格式）— 留到后续
- ❌ 不改变现有 CLI 模式（`terminal_confirm` 保持不变）
- ❌ 不在 Pipeline 中引入 asyncio（保持同步阻塞设计，与 CC 一致）
