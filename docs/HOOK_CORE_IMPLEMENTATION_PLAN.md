# Hook 系统 CC-Native 实现计划

> 基于 `HOOK_CORE_INTEGRATION_DESIGN.md` v2.0.0
> 日期：2026-08-02

---

## Phase H1: 基础类型系统（从 hook_core 开始重建）

### H1.1 Event 枚举
**文件**: `hook_core/events.py`（新文件，替代旧的 `hooks/events.py`）

```python
class HookEvent(str, Enum):
    # ── Tool execution (per-tool-call) ──
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    POST_TOOL_BATCH = "PostToolBatch"

    # ── Permissions ──
    PERMISSION_REQUEST = "PermissionRequest"
    PERMISSION_DENIED = "PermissionDenied"

    # ── User input (per-turn) ──
    USER_PROMPT_SUBMIT = "UserPromptSubmit"

    # ── Model output (per-turn) ──
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"

    # ── Session lifecycle ──
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"

    # ── Subagents ──
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"

    # ── Compaction ──
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"

    # ── Notifications ──
    NOTIFICATION = "Notification"

BLOCKABLE_EVENTS: frozenset[HookEvent] = frozenset({
    HookEvent.PRE_TOOL_USE,
    HookEvent.PERMISSION_REQUEST,
    HookEvent.USER_PROMPT_SUBMIT,
    HookEvent.STOP,
    HookEvent.SUBAGENT_STOP,
    HookEvent.PRE_COMPACT,
    HookEvent.POST_TOOL_BATCH,
})
```

**校验**: 新事件覆盖旧 10 个 + 6 个 CC 对齐。`BLOCKABLE_EVENTS` 映射自 CC 文档。

### H1.2 Matcher（无 regex）
**文件**: `hook_core/matcher.py`（重写）

```python
@dataclass(frozen=True, slots=True)
class HookMatcher:
    """CC-aligned: exact match or pipe-separated list or prefix wildcard.
    
    NO regex. NO re.compile.  Patterns follow CC permission-rule syntax:
      "*" / ""              → all tools
      "Bash"                → exact match
      "Edit|Write"          → pipe-separated OR
      "mcp__server__*"      → prefix wildcard
    """
    pattern: str = "*"
    
    def matches(self, tool_name: str) -> bool:
        if not self.pattern or self.pattern == "*":
            return True
        for part in self.pattern.split("|"):
            part = part.strip()
            if not part:
                continue
            if part.endswith("*") and tool_name.startswith(part[:-1]):
                return True
            if part == tool_name:
                return True
        return False
```

**关键变化**: 删除 `re` import。简单字符串操作。

### H1.3 Decision 类型
**文件**: `hook_core/decisions.py`（重写）

```python
class PermissionDecision(str, Enum):
    """CC-aligned four-way decision with precedence: deny > defer > ask > allow"""
    DENY = "deny"
    DEFER = "defer"
    ASK = "ask"
    ALLOW = "allow"

    @staticmethod
    def precedence() -> list["PermissionDecision"]:
        return [DENY, DEFER, ASK, ALLOW]

@dataclass(frozen=True, slots=True)
class PreToolUseDecision:
    permission: PermissionDecision = PermissionDecision.ALLOW
    updated_input: dict | None = None
    reason: str = ""

@dataclass(frozen=True, slots=True)
class PostToolUseDecision:
    additional_context: str = ""
    replace_output: str | None = None
    decision: str = ""  # "block" to give feedback to Claude

@dataclass(frozen=True, slots=True)
class StopDecision:
    decision: str = "continue"  # "continue" | "block"
    reason: str = ""

# ... 其他 event 的 decision
```

### H1.4 Input 类型
**文件**: `hook_core/inputs.py`（重写）

补充缺失事件：`PostToolUseFailureInput`、`PermissionDeniedInput`、`StopFailureInput`、`SessionEndInput`、`PreCompactInput`、`PostCompactInput`、`PostToolBatchInput`、`NotificationInput`。

---

## Phase H2: HookDispatcher（可用的核心）

### H2.1 匹配、排序、执行、合并
**文件**: `hook_core/dispatcher.py`

```
dispatch(event_type, input, snapshot=None)
  → registry.get_hooks(event_type, tool_name)
  → sort by priority
  → for each hook:
      execute(hook, input, policy)
      if deny → short-circuit return BLOCK
      if defer → skip to next
      merge allow/transform results
  → return merged DispatchResult
```

关键修复：
- **Precedence**: `deny` 立即短路（不等后续 hook）
- **defer**: 不短路，继续下一个 hook
- **ask**: 记录需要用户确认
- **allow**: 记录批准，但可以被后续 `deny` 覆盖
- **transform**: 合并 `updated_input`、`updated_output`、`additional_context`

### H2.2 Failure policy
```
FAIL_CLOSED + blockable event → block
FAIL_CLOSED + non-blockable → warning
FAIL_OPEN → continue with warning
EVENT_DEFAULT → FAIL_CLOSED for blockable, FAIL_OPEN for non-blockable
```

### H2.3 stop_hook_active 防护
Stop 事件 dispatch 时检查 input.stop_hook_active。如果为 true，所有 hook 自动返回 continue。

---

## Phase H3: Handler 类型

### H3.1 Internal callable
直接调用 Python 函数：`handler(input) → decision`

### H3.2 Command handler（subprocess）
```python
def execute_command_hook(command, input_json, timeout_s):
    process = subprocess.run(command, input=input_json, timeout=timeout_s)
    exit_code = process.returncode
    if exit_code == 2:
        return BlockResult(stderr=process.stderr)
    if exit_code == 0:
        return parse_json_decision(process.stdout)
    return NonBlockingError(stderr=process.stderr)
```

---

## Phase H4: 接入生产

### H4.1 hook_bootstrap 切换
`entry/bootstrap/hook_bootstrap.py` 创建新 `HookDispatcher`。

### H4.2 兼容层
如果旧 `HookDispatcher` 接口仍有调用方依赖，提供薄兼容层。优先更新调用方。

### H4.3 新事件接入
- `PreCompact` → agent compaction 前调用
- `PostCompact` → agent compaction 后调用
- `SessionEnd` → session 关闭时调用
- `PostToolBatch` → 并行工具批次完成后调用
- `StopFailure` → API error 时调用

---

## Phase H5: 端到端验证

与旧版计划一致，增加 CC 对齐测试：
- PreToolUse deny/defer/ask/allow 四态
- Stop stop_hook_active 防护
- Matcher exact/pipe/prefix 三种模式
- MCP 工具命名 `mcp__server__tool`
