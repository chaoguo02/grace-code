# Hook System

Event-driven lifecycle hooks for extending Forge Agent behavior without modifying core code.

## Overview

The Hook system lets you attach custom scripts to lifecycle events (tool execution, session start/stop, user input). Hooks communicate via a stdin/stdout JSON protocol with exit-code-based decisions, aligned with Claude Code's hook architecture.

**Use cases:**
- Block dangerous commands before execution (PreToolUse)
- Auto-format code after file writes (PostToolUse)
- Log or observe all shell executions (PostToolUse)
- Validate user input before processing (UserPromptSubmit)
- Clean up resources on session end (Stop)

## Supported Events

| Event | Blockable | Description |
|---|---|---|
| `PreToolUse` | Yes | Fires before tool execution. Can block the tool call. |
| `PostToolUse` | No | Fires after successful tool execution. |
| `PostToolUseFailure` | No | Fires after failed tool execution. |
| `SessionStart` | No | Fires when a session begins running. |
| `Stop` | No | Fires when a session completes. |
| `UserPromptSubmit` | Yes | Fires when user input is submitted. Can block processing. |
| `SubagentStop` | No | Fires when a child/subagent session completes. |

**Blockable** means the hook can prevent the action from proceeding (exit code 2).

## Exit Code Protocol

| Exit Code | Effect |
|---|---|
| `0` | Allow. Optional JSON stdout is parsed for `decision` or `additional_context`. |
| `2` | Block (only for blockable events). `stderr` is used as the block reason. |
| Other | Non-blocking error. Logged and execution continues. |

## stdin Context (JSON)

Every hook receives the full event context on stdin:

```json
{
  "event": "PostToolUse",
  "session_id": "abc123",
  "tool_name": "shell",
  "tool_input": {"cmd": "git add ."},
  "tool_output": {"success": true, "output": "...", "error": ""},
  "user_input": "",
  "timestamp": "2026-07-01T12:00:00+08:00"
}
```

## stdout Response (JSON, optional)

Hooks can return structured output on stdout:

```json
{
  "decision": "allow",
  "reason": "Approved by policy",
  "additional_context": "Extra info injected into the conversation",
  "updated_input": {"cmd": "modified command"}
}
```

All fields are optional. If stdout is empty or non-JSON, the hook is treated as a silent pass-through.

## Configuration

Add hooks to `.forge-agent/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "shell",
        "if": "tool_input.cmd matches 'rm *'",
        "hooks": [
          {
            "type": "command",
            "command": "python .forge/hooks/block-rm.py",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "shell",
        "hooks": [
          {
            "type": "command",
            "command": "python .forge/hooks/auto-format.py"
          }
        ]
      }
    ]
  }
}
```

### Matcher Syntax

- `"*"` — match all tools
- `"shell"` — exact tool name match
- `"file_write|file_edit"` — pipe-separated alternation
- `"if"` field — conditional: `"tool_input.cmd matches 'git *'"` (glob pattern on a field)

### Timeout

Default: 60 seconds. Set `"timeout": 5` for fast hooks that should not delay execution.

## Example: Auto-format Hook

`.forge/hooks/auto-format.py`:

```python
#!/usr/bin/env python3
import json
import sys

context = json.load(sys.stdin)
tool_output = context.get("tool_output") or {}

if not tool_output.get("success"):
    sys.exit(0)

cmd = context.get("tool_input", {}).get("cmd", "")
if cmd.startswith("git"):
    print(json.dumps({
        "additional_context": "Detected git operation. Consider running code formatter."
    }))
```

## Example: Block Dangerous Commands

`.forge/hooks/block-rm.py`:

```python
#!/usr/bin/env python3
import json
import sys

context = json.load(sys.stdin)
cmd = context.get("tool_input", {}).get("cmd", "")

if "rm -rf" in cmd and "/" in cmd:
    print("Blocked: recursive delete on root-like path", file=sys.stderr)
    sys.exit(2)
```

## Internal Hooks (Python Callables)

For performance-critical integrations (like ProactiveMemory), hooks can be registered as Python callables that run in-process without subprocess overhead:

```python
from hooks import HookDispatcher, HookEvent, HookMatcher, HookRegistry, InternalHook

registry = HookRegistry()
registry.register_internal(HookEvent.POST_TOOL_USE, InternalHook(
    callback=lambda ctx: my_observer(ctx.tool_name, ctx.tool_output),
    matcher=HookMatcher(pattern="shell"),
))

dispatcher = HookDispatcher(registry)
```

Internal hooks are always executed before external (command) hooks and cannot block events.

## Architecture

```
Event Fire ──> Matcher ──> Internal Hooks (in-process) ──> External Hooks (subprocess) ──> Decision
                            (cheap, no block)               (stdin JSON, exit code)
```

The dispatcher short-circuits on the first blocking result (exit 2) for blockable events. For non-blockable events, all hooks run regardless of exit codes.
