# P1 实施指导：权限管线增强

> 基于 2026-07-19 Claude Code 逆向工程研究 + 官方文档 + GitHub issues
> 所有实现建议均标注来源 URL 和对应代码位置

---

## 目录

1. [P1-5: 硬安全门路径保护](#p1-5-硬安全门路径保护)
2. [P1-6: requiresUserInteraction 标记](#p1-6-requiresuserinteraction-标记)
3. [P1-7: decision_reason + tool_use_id](#p1-7-decision_reason--tool_use_id)
4. [P1-8: \*\* 递归 glob 通配](#p1-8--递归-glob-通配)
5. [P1-9: 拒绝计数重置](#p1-9-拒绝计数重置)

---

## P1-5: 硬安全门路径保护

### 目标

保护 `.git/`、`.forge-agent/`、`.vscode/`、`.idea/` 等关键目录和配置文件免于被 LLM 意外修改，即使用户启用了 `bypassPermissions` 模式。

### CC 的实现方式

CC 在权限管线 Phase 1 中硬编码了**受保护路径列表**，这些检查在 `bypassPermissions` 之前执行（bypass-immune）：

**受保护目录:**
- `.git/`
- `.claude/`
- `.vscode/`
- `.idea/`

**受保护文件:**
- `.gitconfig`、`.gitmodules`
- `.bashrc`、`.bash_profile`、`.zshrc`、`.zprofile`、`.profile`
- `.ripgreprc`、`.mcp.json`、`.claude.json`

**实现机制:**
- 在 `hasPermissionsToUseToolInner()` Phase 1 步骤 1g 检查
- 创建一个合成的 `policySettings` 规则 (`ruleBehavior: "ask"`)
- **不可被覆盖**: `bypassPermissions` 不能跳过、`PreToolUse` hooks 不能跳过、`allowedTools` 不能跳过
- 额外的路径规范化防护：NTFS ADS 检测、8.3 短名防止、尾部点/空格检测

> **来源:** [GitHub issue #36044 — bypassProtectedDirectories](https://github.com/anthropics/claude-code/issues/36044), [DeepWiki filesystem permissions](https://deepwiki.com/cablate/claude-code-research/6.2-filesystem-permissions-sandbox-and-tool-permission-hooks), [dev.to analysis](https://dev.to/gentic_news/how-claude-codes-deterministic-permission-system-actually-works-ikj)

### 我们的现状

- [hitl/pipeline.py:472-484](hitl/pipeline.py#L472-L484) — Layer 1 `_layer1_validate` 调用 `tool.permission_denial_reason(params)`
- [tools/file_tool.py](tools/file_tool.py) — FileWriteTool 无受保护路径检查
- [tools/file_edit_tool.py](tools/file_edit_tool.py) — FileEditTool 无受保护路径检查

### 实施步骤

**Step 1: 定义受保护路径列表**

在 `hitl/pipeline.py` Layer 1 中添加：

```python
# hitl/pipeline.py — PermissionPipeline class

# CC-aligned: protected paths that ALWAYS require confirmation,
# even in bypassPermissions mode.  These are bypass-immune.
_PROTECTED_DIRS: frozenset[str] = frozenset({
    ".git", ".forge-agent", ".grace", ".claude",
    ".vscode", ".idea",
})

_PROTECTED_FILES: frozenset[str] = frozenset({
    ".gitconfig", ".gitmodules",
    ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile",
    ".ripgreprc", ".mcp.json", ".claude.json",
    "settings.json", "settings.local.json",  # forge-agent specific
})

def _is_protected_path(self, tool_name: str, params: dict) -> str | None:
    """Check if a file operation targets a protected path.
    Returns the protected path string if so, None otherwise.
    """
    if tool_name not in ("Write", "Edit", "Read"):
        return None
    path = params.get("file_path") or params.get("path") or ""
    if not path:
        return None
    
    # Normalize and check each path component
    parts = Path(path).parts
    for part in parts:
        if part in self._PROTECTED_DIRS:
            return f"Protected directory: {part}/"
        if part in self._PROTECTED_FILES:
            return f"Protected file: {part}"
    return None
```

**Step 2: 在 Layer 1 中调用**

修改 `_layer1_validate`:

```python
def _layer1_validate(self, tool, params):
    # 1. Tool's own denial check
    reason = tool.permission_denial_reason(params)
    if reason:
        return PermissionResult(DENY, layer=INPUT_VALIDATION, reason=reason)
    
    # 2. CC-aligned: protected path check (bypass-immune)
    protected = self._is_protected_path(tool.name, params)
    if protected:
        return PermissionResult(
            decision=PermissionDecision.ASK,  # ← ASK, not DENY
            layer=PermissionLayer.INPUT_VALIDATION,
            reason=f"Protected path requires confirmation: {protected}",
        )
    
    return None
```

**关键设计决策**: 受保护路径返回 `ASK` 而非 `DENY`。CC 的做法是让用户在交互模式下确认，而非直接拒绝。这允许高级用户在确认后修改 `.forge-agent/settings.json` 等文件。

**Step 3: 对 bypassPermissions 强制生效**

在 `_layer4_permission_mode` 中，`bypassPermissions` 当前已检查 `_ROOT_REMOVAL_PATTERNS`（`rm -rf /`）。受保护路径在 Layer 1 返回 `ASK` 后会直接跳到 Layer 6，绕过 Layer 4。所以 `bypassPermissions` 不会覆盖此检查 — 正确行为。

### 行为准则

1. **受保护路径返回 ASK（不是 DENY）** — 用户可以确认后修改
2. **bypass-immune** — 任何权限模式都不能跳过
3. **仅对 Write/Edit 工具生效** — Read 不应被拦截
4. **路径归一化** — 处理 `../`、符号链接、大小写

### 涉及文件

| 文件 | 改动 |
|------|------|
| `hitl/pipeline.py` | 添加 `_PROTECTED_DIRS`、`_PROTECTED_FILES`、`_is_protected_path()`；修改 `_layer1_validate` |

---

## P1-6: requiresUserInteraction 标记

### 目标

支持工具级别的 `requiresUserInteraction` 元数据标记，强制该工具**始终**需要用户确认，即使用户已添加 allow 规则或启用了 bypass 模式。

### CC 的实现方式

CC 的 MCP 工具可以声明 `_meta["anthropic/requiresUserInteraction"]`。当此标记为 true 时：

- **始终**到达 `canUseTool` 回调（即使 allow 规则匹配）
- `bypassPermissions` 模式下也会触发（v2.1.199+）
- `dontAsk` 模式下被**拒绝**（因为 dontAsk 永远不提示）
- `auto` 模式下**跳过**分类器（直接提示用户）

**已知 Bug:**
- `bypassPermissions` 在某些版本中仍拒绝这些工具（[#58757](https://github.com/anthropics/claude-code/issues/58757)）
- 通配符权限对 MCP 工具不生效（[#34739](https://github.com/anthropics/claude-code/issues/34739)）

> **来源:** [Claude Code Agent SDK permissions docs](https://code.claude.com/docs/en/agent-sdk/permissions), [GitHub issue #58757](https://github.com/anthropics/claude-code/issues/58757), [GitHub issue #34739](https://github.com/anthropics/claude-code/issues/34739)

### 我们的现状

- [core/base.py](core/base.py) — `BaseTool` 有 `metadata` 属性（`ToolMetadata`），但无 `requires_user_interaction` 字段
- [hitl/pipeline.py](hitl/pipeline.py) — 权限管线不检查此标记

### 实施步骤

**Step 1: 添加 metadata 字段**

在 `core/base.py` 的 `ToolMetadata` 中添加：

```python
@dataclass
class ToolMetadata:
    # ... existing fields ...
    requires_user_interaction: bool = False
    """CC-aligned: when True, this tool ALWAYS prompts for user confirmation,
    even in bypassPermissions mode or when an allow rule matches."""
```

**Step 2: 在管线中检查**

在 `hitl/pipeline.py` 的 `check()` 或 `_layer6_callback()` 中添加：

```python
# In check(), before Layer 3 rules:
# CC-aligned: requiresUserInteraction tools always go to Layer 6
if getattr(tool.metadata, 'requires_user_interaction', False):
    result = self._layer6_callback(
        tool_name, params, thought, force_interactive=True,
    )
    self._stats.record(result)
    return self._apply_tool_check(result, tool, params)
```

`force_interactive=True` 确保即使 `approval_mode=auto` 也会显示审批卡片。

**Step 3: dontAsk 模式特殊处理**

在 `_layer4_permission_mode` 的 `dontAsk` 分支中：

```python
if mode == "dontAsk":
    # CC: requiresUserInteraction tools are denied in dontAsk mode
    if getattr(tool_metadata, 'requires_user_interaction', False):
        return PermissionResult(
            decision=PermissionDecision.DENY,
            layer=PermissionLayer.RULE,
            reason="dontAsk mode: tool requires user interaction",
        )
    # ... existing dontAsk logic ...
```

### 行为准则

1. **始终提示** — 匹配 allow 规则、bypassPermissions、auto 模式都不能跳过
2. **dontAsk 拒绝** — 无人值守模式不应该运行需要交互的工具
3. **force_interactive=True** — 确保审批卡片显示

### 涉及文件

| 文件 | 改动 |
|------|------|
| `core/base.py` | ToolMetadata 添加 `requires_user_interaction` 字段 |
| `hitl/pipeline.py` | check() 中提前检查 + dontAsk 特殊处理 |

---

## P1-7: decision_reason + tool_use_id

### 目标

在 `approval_required` WS 事件中携带 `decision_reason`（为什么需要审批）和 `tool_use_id`（关联工具调用），与 CC 的 `control_request` 协议对齐。

### CC 的协议格式

```json
{
  "type": "control_request",
  "request_id": "req_abc123",
  "request": {
    "subtype": "can_use_tool",
    "tool_name": "Bash",
    "input": {"command": "git add -A", "description": "Stage all changes"},
    "decision_reason": "Command not in allowlist",
    "tool_use_id": "toolu_xyz"
  }
}
```

- **`decision_reason`**: 人类可读的审批原因（如 `"Matched ask rule: shell(git push *)"`）
- **`tool_use_id`**: 工具调用的唯一 ID，防止重复执行

> **来源:** [Runloop AI — Claude Code SDK Protocol](https://docs.runloop.ai/docs/axons/broker/claude-protocol), [DeepWiki — Structured I/O](https://deepwiki.com/farion1231/claude-code/13.2-structured-and-remote-io), [turboclaude-protocol docs](https://docs.rs/turboclaude-protocol/latest/turboclaude_protocol/protocol/index.html)

### 我们的现状

- **前端已支持**: [chatStore.ts:17-18](web/src/stores/chatStore.ts#L17-L18) `ToolApproval` 接口已有 `decisionReason` 和 `toolUseId` 字段
- **前端已映射**: [chatStore.ts:127-130](web/src/stores/chatStore.ts#L127-L130) `handleWsEvent` 从 WS 事件读取 `ev.decision_reason`、`ev.tool_use_id`
- **后端缺失**: `_build_web_confirm_callback` 的 `push_event` 未发送这些字段
- **管线缺失**: `PermissionPipeline` 未生成 `decision_reason`

### 实施步骤

**Step 1: 管线生成 decision_reason**

在 `check()` 中记录为何需要审批：

```python
# In check(), when reaching Layer 6:
_decision_reason = ""
if tier is PermissionRuleTier.ASK:
    # Find which ask rule matched
    for rule in self._ask_rules:
        if rule.matches(tool_name, params):
            _decision_reason = f"Matched ask rule: {rule.raw}"
            break
    if not _decision_reason:
        _decision_reason = "No allow rule matched"
elif tier is None:
    # Reached Layer 4/5/6 without matching any rule
    _decision_reason = "No matching rule — requires interactive approval"
```

**Step 2: 传递到回调**

在 `PermissionRequest` dataclass 中添加字段：

```python
@dataclass
class PermissionRequest:
    tool_name: str
    params: dict
    thought: str = ""
    agent_name: str = ""
    decision_reason: str = ""     # ← 新增
    tool_use_id: str = ""         # ← 新增
```

**Step 3: push_event 发送字段**

在 `agent_service.py` 的 `push_event` 中：

```python
def push_event(req_id: str) -> None:
    if event_bus is not None:
        event_bus.publish_raw(session_id, {
            "type": "approval_required",
            "request_id": req_id,
            "tool_name": _req_info["tool_name"],
            "params": _req_info["params"],
            "thought": _req_info["thought"],
            "decision_reason": _req_info.get("decision_reason", ""),  # ← 新增
            "tool_use_id": _req_info.get("tool_use_id", ""),          # ← 新增
        })
```

**Step 4: ReActAgent 传递 tool_use_id**

在 `agent/core.py` 工具调用处，提取 LLM 返回的 `tool_use.id` 并传递给管线。

### 行为准则

1. **decision_reason 可溯源** — 说明是哪个规则导致的审批
2. **tool_use_id 唯一** — 使用 LLM 返回的 `tool_use.id`
3. **向后兼容** — 旧前端未升级时忽略新字段

### 涉及文件

| 文件 | 改动 |
|------|------|
| `hitl/pipeline.py` | PermissionRequest 添加字段 + check() 生成 decision_reason |
| `server/services/agent_service.py` | push_event 发送新字段 |
| `agent/core.py` | 提取 tool_use.id 传递给管线 |

---

## P1-8: \*\* 递归 glob 通配

### 目标

支持 `Edit(src/**)` 这类递归匹配所有子目录的 glob 语法。

### CC 的实现方式（及其 Bug）

CC **文档**声称支持 `Edit(src/**)`，但**实际实现有严重 Bug**：

| 模式 | 预期 | 实际 |
|------|------|------|
| `Edit(/path/**)` | 递归匹配所有文件 | 仅匹配 1 层深度 |
| `Read(/repo/**)` | 匹配所有文件 | 逐个文件提示 |
| Bash `*` | 正确匹配 | ✅ |

**根因:** Python 的 `pathlib.PurePath.match()` **不支持** `**` 递归匹配。CC 的修复 (v2.1.214) 只处理了**作用域**问题（防止跨目录匹配），未让 `**` 真正递归工作。

> **来源:** [GitHub issue #57746 — ** broken](https://github.com/anthropics/claude-code/issues/57746), [GitHub issue #6881 — glob patterns don't work](https://github.com/anthropics/claude-code/issues/6881), [GitHub issue #72739 — settings don't suppress prompts](https://github.com/anthropics/claude-code/issues/72739)

### 我们的现状

- [hitl/permission_rule.py:100-132](hitl/permission_rule.py#L100-L132) — `_pattern_to_regex()` 只支持 `*`（单 segment），不支持 `**`

当前转换逻辑:
```python
if pattern.endswith(" *"):
    # 前缀匹配: ^escaped_prefix(\s.*)?$
elif "*" in pattern:
    # 中间 * → 单 token: [^ ]*
else:
    # 精确匹配
```

### 实施步骤

**Step 1: 添加 `**` 支持**

修改 `_pattern_to_regex()`:

```python
def _pattern_to_regex(pattern: str) -> str:
    """Convert a Tool(pattern) glob to a regex.
    
    ** → recursive: matches zero or more path segments (.* for file paths,
          \S* for bash commands)
    Trailing " *" → prefix match.
    Middle * → matches one non-space token [^ ]*.
    No * → exact match.
    """
    # Handle ** (recursive glob) — applicable to file paths
    if "**" in pattern:
        # ** matches any number of path segments
        escaped = re.escape(pattern)
        # Replace \*\* with .*  (recursive match)
        # But preserve trailing " *" if present
        escaped = escaped.replace(r"\*\*", ".*")
        # Fix: ** in file paths should match across / separators
        escaped = escaped.replace(r"\.\*", ".*")  # unescape the dot in .*
        return f"^{escaped}$"
    
    if pattern.endswith(" *"):
        prefix = pattern[:-2]
        escaped = re.escape(prefix)
        return f"^{escaped}(\\s.*)?$"
    elif "*" in pattern:
        escaped = re.escape(pattern).replace(r"\*", "[^ ]*")
        return f"^{escaped}$"
    else:
        return f"^{re.escape(pattern)}$"
```

**Step 2: 适配 `_extract_match_target`**

对文件路径工具（Write/Edit/Read），match target 是文件路径（含 `/` 分隔符），`**` 应匹配 `.*`（跨路径段）。对 Bash 命令，`**` 无意义（命令不包含 `/` 路径段）。

在 `_glob_match` 中根据工具类型选择匹配策略：

```python
def _glob_match(pattern: str, target: str, tool_name: str = "") -> bool:
    regex = _pattern_to_regex(pattern, tool_name)
    return bool(re.match(regex, target, re.IGNORECASE))
```

### 行为准则

1. **仅文件路径工具支持 `**`** — Write/Edit/Read 的 `**` 匹配跨目录
2. **Shell 规则不需要 `**`** — Shell 命令不含路径分隔符
3. **向后兼容 `*`** — 现有单 segment 匹配不受影响

### 涉及文件

| 文件 | 改动 |
|------|------|
| `hitl/permission_rule.py` | `_pattern_to_regex()` 添加 `**` → `.*` 转换 |

---

## P1-9: 拒绝计数重置

### 目标

当工具调用**成功**后，重置该工具的连续拒绝计数。CC 的 `recordSuccess()` 函数在每次成功的工具调用后清零连续计数，避免正常的"拒绝→调整→成功"流程触发熔断。

### CC 的实现方式

```typescript
// denialTracking.ts (community-observed)
function recordDenial(toolName: string) {
    consecutiveDenials[toolName] = (consecutiveDenials[toolName] || 0) + 1;
    totalDenials++;
}

function recordSuccess(toolName: string) {
    consecutiveDenials[toolName] = 0;  // ← RESET on success
    // totalDenials is NOT reset (cumulative session counter)
}
```

- **连续拒绝计数重置** — 工具成功后清零
- **总计拒绝计数不重置** — 20 次总数限制是会话级别的

> **来源:** [wuwangzhang1216 deep analysis](https://github.com/wuwangzhang1216/claude-code-source-all-in-one/blob/main/claude-code-deep-analysis/05-permission-system.en.md), [GitHub claude-code-best permission-model doc](https://github.com/claude-code-best/claude-code/blob/main/docs/safety/permission-model.mdx)

### 我们的现状

- [hitl/pipeline.py:569-586](hitl/pipeline.py#L569-L586) — `_apply_tool_check` ：
  - ALLOW 时记录 `record_approval()`（如果有 circuit_breaker）
  - DENY 时 `_total_denials += 1` 和 `_denial_counters[tool.name] += 1`
  - **缺失**: ALLOW 时没有重置 `_denial_counters[tool.name]`

### 实施步骤

**Step 1: 在 `_apply_tool_check` 中添加重置逻辑**

```python
def _apply_tool_check(self, result, tool, params):
    if result.decision is PermissionDecision.ALLOW:
        l5 = self._layer5_check(tool, params)
        if l5 is not None:
            result = l5
    if result.decision is PermissionDecision.ALLOW:
        # CC-aligned: reset consecutive denial counter on success
        _tool_name = tool.name if tool and hasattr(tool, 'name') else ""
        if _tool_name and _tool_name in self._denial_counters:
            _was = self._denial_counters[_tool_name]
            if _was > 0:
                logger.debug("Reset denial counter for %s (was %d consecutive)", _tool_name, _was)
            self._denial_counters[_tool_name] = 0
        if getattr(self, '_circuit_breaker', None) is not None:
            self._circuit_breaker.record_approval()
    else:
        self._total_denials += 1
        if tool is not None and hasattr(tool, 'name'):
            self._denial_counters[tool.name] = self._denial_counters.get(tool.name, 0) + 1
        if getattr(self, '_circuit_breaker', None) is not None:
            self._circuit_breaker.record_denial()
    return result
```

### 行为准则

1. **仅重置连续计数** — `_total_denials` 保持累积（会话级别）
2. **在 `_apply_tool_check` 中重置** — 所有通过管线的 ALLOW 都触发重置
3. **Layer 5 拒绝也计入** — 但 Layer 5 拒绝后的成功应重置

### 涉及文件

| 文件 | 改动 |
|------|------|
| `hitl/pipeline.py` | `_apply_tool_check` 添加计数器重置逻辑 |

---

## 实施顺序建议

```
Batch P1-5: 硬安全门路径保护       (1 文件)
Batch P1-8: ** 递归 glob          (1 文件)
Batch P1-9: 拒绝计数重置           (1 文件, 可与 P1-5 合并)
Batch P1-7: decision_reason + tool_use_id  (3 文件)
Batch P1-6: requiresUserInteraction (2 文件)
```

每批 ≤3 文件，commit 后全局反思。

---

## 参考来源汇总

- [Claude Code Agent SDK permissions docs](https://code.claude.com/docs/en/agent-sdk/permissions)
- [Claude Code permission modes docs](https://code.claude.com/docs/en/permission-modes)
- [Claude Code permissions docs](https://code.claude.com/docs/en/permissions)
- [GitHub issue #36044 — bypassProtectedDirectories](https://github.com/anthropics/claude-code/issues/36044)
- [GitHub issue #57746 — ** broken](https://github.com/anthropics/claude-code/issues/57746)
- [GitHub issue #6881 — glob patterns don't work](https://github.com/anthropics/claude-code/issues/6881)
- [GitHub issue #58757 — requiresUserInteraction regression](https://github.com/anthropics/claude-code/issues/58757)
- [GitHub issue #34739 — MCP wildcard permissions](https://github.com/anthropics/claude-code/issues/34739)
- [Runloop AI — Claude Code SDK Protocol](https://docs.runloop.ai/docs/axons/broker/claude-protocol)
- [DeepWiki — Structured I/O](https://deepwiki.com/farion1231/claude-code/13.2-structured-and-remote-io)
- [DeepWiki — Headless & SDK Mode](https://deepwiki.com/ChinaSiro/claude-code-sourcemap/10.2-headless-and-sdk-mode)
- [DeepWiki — Filesystem permissions](https://deepwiki.com/cablate/claude-code-research/6.2-filesystem-permissions-sandbox-and-tool-permission-hooks)
- [turboclaude-protocol docs](https://docs.rs/turboclaude-protocol/latest/turboclaude_protocol/protocol/index.html)
- [wuwangzhang1216 deep analysis — permission system](https://github.com/wuwangzhang1216/claude-code-source-all-in-one/blob/main/claude-code-deep-analysis/05-permission-system.en.md)
