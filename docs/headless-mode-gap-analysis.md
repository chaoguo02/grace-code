# Headless Mode Gap Analysis — Forge Agent vs Claude Code

> 基于 2026-07-18 websearch 调研 + 代码库审计
> 
> **问题触发**: `[permission_denied] Tool 'Edit' denied: interactive approval unavailable in headless mode`

---

## 核心发现: 基础设施已存在，差在 Web 通路

**好消息**: Forge Agent 已经有一个完整的 6 层权限管线 (`hitl/pipeline.py`)，和 Claude Code 的架构高度对齐。

**坏消息**: Web 入口 (`agent_service.py`) 的 `approval_mode="prompt"` 没有提供 `confirm_callback`，导致到达 Layer 6 的工具全部被拒绝。

---

## 0. 当前权限管线架构 (代码审计结果)

### 0.1 6 层评估管线 — `hitl/pipeline.py`

```
Layer 1: validateInput()       — L0 安全黑名单
Layer 2: PreToolUse Hooks      — 用户定义的 hook 脚本
Layer 3: Deny Rules + Ask      — Tool(pattern) glob 语法规则
Layer 4: Permission Mode       — bypassPermissions/acceptEdits/plan/dontAsk
Layer 4.5: Prompt-based Perms  — ExitPlanMode 的 allowedPrompts
Layer 5: Allow Rules           — 静态规则 + 会话规则
Layer 6: Interactive Callback  — 3 选项: Allow Once/Always Allow/Deny
```

**`check()` 方法** — `hitl/pipeline.py:236-333`，按顺序执行上述 6 层。

### 0.2 Layer 4: Permission Mode — `hitl/pipeline.py:449-475`

```python
def _layer4_permission_mode(self, tool_name):
    if mode == "bypassPermissions": return ALLOW    # 全部自动批准
    if mode == "acceptEdits":
        if tool_name in {"Write", "Edit"}: return ALLOW  # 文件编辑自动批准
    if mode == "plan":
        if tool_name in {"Write", "Edit", "Bash"}: return DENY  # 只读模式
    # dontAsk 返回 None → 走到 Layer 6 → 无回调 → deny
```

### 0.3 Layer 6: 交互回调 — `hitl/pipeline.py:524-539` ← 问题根源

```python
def _layer6_callback(self, tool_name, params, thought):
    if self._approval_mode is ToolApprovalMode.AUTO:
        return ALLOW
    if self._confirm_callback is None:          # ← Web 模式没有回调!
        return DENY(reason="interactive approval unavailable in headless mode")
```

### 0.4 Web 入口的问题 — `agent_service.py:87`

```python
self._registry = build_registry(
    self._config,
    repo_path=self.repo_path,
    approval_mode="prompt",     # ← 写死了 "prompt"，但没提供 confirm_callback
)
```

**根因**: `approval_mode="prompt"` + `confirm_callback=None` → 所有到达 Layer 6 的工具全部被 `DENY`。

### 0.5 已有的规则系统 — `hitl/permission_rule.py`

`PermissionRule` (line 38): `ToolName(pattern)` 格式，支持:
- 精确匹配: `Bash(git status)`
- Glob 匹配: `Bash(npm test *)`
- 文件路径: `Read(./src/**)`
- 三级: `DENY` / `ASK` / `ALLOW`

### 0.6 已有的配置加载 — `hitl/settings_loader.py`

`load_permission_settings()` (line 20): 读取 `.grace/settings.json`:
```json
{
  "permissions": {
    "deny": ["Bash(rm *)", "Bash(format *)"],
    "ask": ["Bash(docker *)", "Write"],
    "allow": ["Read", "Grep", "Glob", "Bash(ls *)", "Bash(cat *)"]
  },
  "hooks": { "PreToolUse": [...] }
}
```

内置默认值 (`_builtin_defaults`, line 82): deny 破坏性操作，allow 只读工具。

### 0.7 已有的 Hooks — `hooks/events.py`

10 种 HookEvent: `PRE_TOOL_USE`, `POST_TOOL_USE`, `POST_TOOL_USE_FAILURE`, `PERMISSION_REQUEST`, `SESSION_START`, `STOP`, `USER_PROMPT_SUBMIT`, `SUBAGENT_START`, `SUBAGENT_STOP`, `POST_RESPONSE`

`PERMISSION_REQUEST` hook 已经存在但 **未接入管线**。

### 0.8 已有的 PhasePolicy — `core/policy.py`

```python
@dataclass(frozen=True)
class PhasePolicy:
    allowed_tools: frozenset[str] | None
    denied_tools: frozenset[str]
    permission_mode: str   # "default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan"
    pre_approved_tools: frozenset[str]
    scoped_deny_rules: tuple[ScopedToolRule, ...]
    scoped_allow_rules: tuple[ScopedToolRule, ...]
```

### 0.9 已有的 PolicyAwareToolRegistry — `core/policy_registry.py`

在工具注册和调用时双重检查: `is_tool_blocked_by_permission_mode()` + `check_scoped_rules()`

### 0.10 已有的 Mode Switching — `agent/mode_switching.py`

`check_pending_mode_switch()`: 调用 `pipeline.set_permission_mode("plan")` / `pipeline.restore_pre_plan_mode()`

### 0.11 已有的 AgentDefinition.permission_mode — `agent/session/models.py:461`

Agent 定义中已声明 `permission_mode` 字段，已做验证，但 session runtime 没有完全继承它到子代理的管线。

---

## 1. 各子系统差距总览

### 1.1 ReAct Loop: 权限管线

| 当前状态 | 差距 |
|----------|------|
| ✅ 6 层管线完整 (`hitl/pipeline.py`) | ❌ Web 无 `confirm_callback` → Layer 6 直接 deny |
| ✅ 规则系统完整 (`hitl/permission_rule.py`) | ❌ `dontAsk` 在 `_layer4_permission_mode()` 返回 None，未在 Layer 4 拦截 |
| ✅ 配置加载完整 (`hitl/settings_loader.py`) | ❌ Web 模式未加载 `.grace/settings.json` |
| ✅ `bypassPermissions`/`acceptEdits`/`plan` 已实现 | ❌ 规则继承到子代理有 gap (`_resolve_child_permission_mode` 未传管线) |
| ✅ PhasePolicy + PolicyAwareToolRegistry | ❌ 无 `auto` 模式 (需要 LLM 分类器) |

### 1.2 Plan Mode

| 当前状态 | 差距 |
|----------|------|
| ✅ plan agent 定义 (read-only) | ❌ Plan→Build mode 切换后管线未自动切换 permission_mode |
| ✅ Plan mode throttling | ❌ Plan agent 的 pre_plan_mode 保存/恢复不完整 |
| ✅ Mode switching pipeline | |
| ✅ Web: PlanView + approve/reject | |

### 1.3 Subagent

| 当前状态 | 差距 |
|----------|------|
| ✅ AgentDefinition.permission_mode 字段 | ❌ 子代理不继承父会话管线规则 (allow/deny) |
| ✅ DelegationPolicy (allowlist/blocklist) | ❌ `disallowedTools` 未实现 |
| ✅ Worktree isolation | ❌ 子代理管线是新鲜创建的，不继承 `permission_mode` |
| ✅ `_resolve_child_permission_mode` 存在但不完整 | ❌ Agent-scoped MCP 未实现 |

### 1.4 MCP

| 当前状态 | 差距 |
|----------|------|
| ✅ 4 transport (stdio/HTTP/SSE/WS) | ❌ MCP 工具权限规则匹配 (`mcp__<server>__<tool>` 格式) |
| ✅ Agent-scoped 生命周期 | ❌ Agent-scoped MCP server 配置 |
| ✅ ToolSearch | |

### 1.5 Skills

Skills 设计与 CC 对齐良好。headless 模式对 Skills 没有特殊权限要求。

### 1.6 Memory

| 当前状态 | 差距 |
|----------|------|
| ✅ MEMORY.md + DreamAgent | ❌ Web 入口未加载 CLAUDE.md 到系统 prompt |
| ✅ SQLite session 持久化 | ❌ 路径作用域规则 (`rules/*.md` with glob) |
| | ❌ 权限偏好未持久化 |

### 1.7 Hooks

| 当前状态 | 差距 |
|----------|------|
| ✅ 10 种 HookEvent | ❌ `PERMISSION_REQUEST` hook 已定义但未接入管线 |
| ✅ HookDispatcher (internal + external) | ❌ 无 `UserPromptSubmit` 实现 |
| ✅ Hook exit code 协议 (0=allow, 2=block) | ❌ Hook matcher 模式匹配不完善 |
| ✅ JSON stdin/stdout 协议 | |

---

## 2. 实现优先级矩阵

### P0: Headless 模式最低可用 (2-3 周)

| # | 文件 | 任务 | 说明 |
|---|------|------|------|
| 1 | `agent_service.py:87` | `approval_mode` 从 `"prompt"` 改为 `"auto"`，或提供 WebSocket `confirm_callback` | **一行改完即可解决当前报错** |
| 2 | `hitl/pipeline.py:449` | `_layer4_permission_mode()` 增加 `dontAsk` 处理: allow 列表外全部 deny | 在 Layer 4 拦截，不到 Layer 6 |
| 3 | `hitl/pipeline.py:524` | `_layer6_callback()` 在 `confirm_callback is None` 时走 `dontAsk` 语义 | 兜底安全策略 |
| 4 | `server/services/event_bus.py` | 新增 `approval_required` WS 事件类型 | Web 审批回调通道 |
| 5 | `server/routers/approvals.py` | 新增 `POST /api/sessions/{id}/tool-approve` 端点 | 接收前端审批决策 |
| 6 | `web/src/stores/chatStore.ts` | 新增 `toolApproval` 状态 + `handleApprovalRequired` | 前端审批状态管理 |
| 7 | `web/src/components/ChatView.tsx` | 工具审批 UI (Allow/Deny 按钮，参考 plan approve/reject) | 用户可见的审批界面 |
| 8 | `agent/session/runtime.py:1630` | `_resolve_child_permission_mode` 完整继承父管线规则 | 子代理权限继承 |

### P1: 生产可用 (3-4 周)

| # | 文件 | 任务 |
|---|------|------|
| 9 | `agent_service.py` | 加载 `.grace/settings.json` 权限规则 |
| 10 | `agent_service.py` | CLI flags: `--permission-mode`, `--allowed-tools` |
| 11 | `core/policy.py` | `permission_mode` 增加 `dontAsk` 枚举值 |
| 12 | `agent/session/subagent.py` | 子代理管线继承 `permission_mode` |
| 13 | `agent/session/models.py` | AgentDefinition 增加 `disallowedTools` 字段 |
| 14 | `hitl/permission_rule.py` | MCP 工具权限规则 (`mcp__<server>__<tool>`) |
| 15 | `hooks/dispatcher.py` | `PERMISSION_REQUEST` hook 接入管线 |
| 16 | `agent/session/runtime.py` | Plan→Build mode 自动切换 permission_mode |
| 17 | `agent_service.py` | Web 入口加载项目 CLAUDE.md |

---

## 3. 立即修复: 一行代码

改变 `agent_service.py:87` 从:

```python
approval_mode="prompt",
```

改为:

```python
approval_mode="auto",
```

**效果**: Layer 6 所有工具自动批准 (相当于 `--auto-approve`)。短期内可用，但长期应接 Web 审批回调。

---

## 参考文献

- [Claude Code Permissions Docs](https://code.claude.com/docs/en/permissions)
- [Claude Code Agent SDK Permissions](https://code.claude.com/docs/en/agent-sdk/permissions)
- [Beyond Permission Prompts](https://claude.com/blog/beyond-permission-prompts-making-claude-code-more-secure-and-autonomous)
- [Claude Code Sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing)
- [Claude Code Hooks Reference](https://code.claude.com/docs/en/hooks)
- [Claude Code Subagents](https://code.claude.com/docs/en/sub-agents)
- [Claude Code Memory System](https://code.claude.com/docs/en/memory)
- [Filesystem-as-State Field Manual](https://github.com/and1truong/and1truong/wiki/Filesystem%E2%80%90as%E2%80%90State:-A-Deep%E2%80%90Dive-Field-Manual-for-Claude-Code)
- [Permission Prompt Tool (MCP delegation)](https://www.vibesparking.com/blog/ai/claude-code/docs/cli/2025-08-28-outsourcing-permissions-with-claude-code-permission-prompt-tool/)
