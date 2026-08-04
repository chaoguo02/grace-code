"""Runtime Core -- Model -> Hook -> Tool -> Outcome loop.

The runtime executes agent steps: receive immutable RuntimeExecution,
call LLM, route typed ModelActions, invoke Hook gates, execute tools,
and produce a terminal RuntimeOutcome.

Does NOT import: server, listeners, SQLite, AgentService, WebSocket.
"""

from __future__ import annotations

from runtime_core.native_message import (
    # ContentBlock types
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ContentBlock,
    # Message types
    NativeMessage,
    NativeRole,
    NativeConversation,
)
