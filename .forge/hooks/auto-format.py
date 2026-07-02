#!/usr/bin/env python3
"""
PostToolUse Hook Example: Auto-format notification after git operations.

This hook is invoked after every successful `shell` tool execution.
It demonstrates the stdin JSON protocol and additional_context injection.

Usage in .forge-agent/settings.json:
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "shell",
        "hooks": [{"type": "command", "command": "python .forge/hooks/auto-format.py"}]
      }
    ]
  }
}
"""

import json
import sys


def main():
    context = json.load(sys.stdin)

    tool_output = context.get("tool_output") or {}
    if not tool_output.get("success"):
        return

    cmd = context.get("tool_input", {}).get("cmd", "")
    if cmd.startswith("git"):
        print(json.dumps({
            "additional_context": "Detected git operation. Consider running code formatter."
        }))


if __name__ == "__main__":
    main()
