# Hook 系统 CC-Native 验收标准

> 基于 `HOOK_CORE_INTEGRATION_DESIGN.md` v2.0.0
> 日期：2026-08-02

---

## AC-H1: HookEvent 覆盖全部 16 事件

```
Given: from hook_core.events import HookEvent
Then:
  - len(HookEvent) >= 16
  - 包含全部旧 10 事件: PRE_TOOL_USE, POST_TOOL_USE, POST_TOOL_USE_FAILURE,
    PERMISSION_REQUEST, USER_PROMPT_SUBMIT, STOP,
    SESSION_START, SUBAGENT_START, SUBAGENT_STOP, NOTIFICATION
  - 包含新增 6 事件: PERMISSION_DENIED, STOP_FAILURE, SESSION_END,
    PRE_COMPACT, POST_COMPACT, POST_TOOL_BATCH
  - BLOCKABLE_EVENTS = {PRE_TOOL_USE, PERMISSION_REQUEST, USER_PROMPT_SUBMIT,
    STOP, SUBAGENT_STOP, PRE_COMPACT, POST_TOOL_BATCH}
```

## AC-H2: Matcher 无 regex

```python
# 精确匹配
m = HookMatcher("Bash")
assert m.matches("Bash") and not m.matches("BashRead")

# 竖线分隔
m = HookMatcher("Edit|Write|NotebookEdit")
assert m.matches("Edit") and m.matches("Write") and not m.matches("Read")

# 前缀通配符
m = HookMatcher("mcp__github__*")
assert m.matches("mcp__github__search") and not m.matches("Bash")

# 全匹配
m = HookMatcher("*"); m2 = HookMatcher("")
assert m.matches("anything") and m2.matches("anything")
```

**静态门禁**: `grep -r "import re\|re.compile\|regex" hook_core/matcher.py` → 无匹配。

## AC-H3: PreToolUse 四态决策 + 优先级

```
deny > defer > ask > allow

Given: 两个 PreToolUse hook
  hook_a 返回 allow, hook_b 返回 deny
When: dispatcher.dispatch("PreToolUse", input)
Then: result.blocked == True, block_reason 来自 hook_b

Given: hook_a 返回 defer, hook_b 返回 allow
Then: result.permission == "allow" (defer 不决定)
```

## AC-H4: Stop stop_hook_active 防护

```
Given: Stop hook 注册为 block
When: input.stop_hook_active == True
Then: hook 不得 block，返回 continue
```

## AC-H5: TRANSFORM 合并

```
Given: hook_a 返回 updated_input={"x": 1}, hook_b 返回 updated_input={"y": 2}
Then: merged updated_input == {"x": 1, "y": 2}
```

## AC-H6: FAIL_CLOSED on blockable event

```
Given: PreToolUse, FAIL_CLOSED, handler raises RuntimeError
Then: blocked=True, block_reason contains "fail-closed"
```

## AC-H7: FAIL_OPEN on non-blockable event

```
Given: PostToolUse, FAIL_OPEN, handler raises RuntimeError
Then: NOT blocked, warning logged
```

## AC-H8: EVENT_DEFAULT semantics

```
Given: PreToolUse (blockable), EVENT_DEFAULT, handler raises
Then: 行为同 FAIL_CLOSED → blocked

Given: Notification (non-blockable), EVENT_DEFAULT, handler raises
Then: 行为同 FAIL_OPEN → NOT blocked
```

## AC-H9: Internal callable handler

```
Given: hook 注册为 internal callable: lambda input: PreToolUseDecision(permission="deny")
Then: execute_hook 调用该 callable → 得到 PreToolUseDecision
```

## AC-H10: Command handler (subprocess, exit code protocol)

```
Given: command="python -c '...'"，工具为 blockable
When: subprocess exits with code 2
Then: 行为同 BLOCK

When: subprocess exits with code 0, stdout={"permissionDecision": "allow"}
Then: 行为同 ALLOW

When: subprocess exits with code 1 (其他)
Then: NON_BLOCKING_ERROR, 不 block
```

## AC-H11: 旧接口兼容（hook_bootstrap 切换后）

```
python -m pytest tests/test_hook_contract.py tests/test_tool_execution_pipeline.py -v
→ 100% 通过
```

## AC-H12: 新 hook_core 不 import 旧 hooks/

```
grep -r "from hooks\." hook_core/
→ 无匹配（hook_core 独立，不依赖旧模块）
```

## AC-H13: 全量回归

```
python -m pytest tests/ -q
→ 全部通过（含新架构 + 旧测试）
```
