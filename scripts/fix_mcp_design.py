"""Apply MCP design doc fixes: CC principles, 3 new gaps, checklist updates."""
with open("docs/MCP_SYSTEM_NORMALIZATION_DESIGN.md", "r", encoding="utf-8") as f:
    content = f.read()

# CC Principle annotations for existing DDRs
content = content.replace(
    "#### #3: Rate Limit Tool Reloads\n\n**Problem**",
    "#### #3: Rate Limit Tool Reloads\n\n**CC Principle**: Trust Boundary (#2.3) — servers are zero-trust; a misbehaving server must not degrade host performance.\n\n**Problem**"
)
content = content.replace(
    "#### #4: Remove Dead SSE Response Storage",
    "#### #4: Remove Dead SSE Response Storage\n\n**CC Principle**: Protocol Compliance (#2.4)."
)
content = content.replace(
    "#### #5: WsMCPBridge Concurrent Requests",
    "#### #5: WsMCPBridge Concurrent Requests\n\n**CC Principle**: Protocol Compliance (#2.4) — MCP request-response protocol has no response routing by ID."
)
content = content.replace(
    "#### #6: Daemon Thread for SyncMCPToolManager",
    "#### #6: Daemon Thread for SyncMCPToolManager\n\n**CC Principle**: Lifecycle Ownership (#2.5) — watchdog is not a critical lifecycle participant."
)
content = content.replace(
    "#### #7: Agent-Scoped MCP Isolation",
    "#### #7: Agent-Scoped MCP Isolation\n\n**CC Principle**: MCP is a Transport (#2.2) + Lifecycle Ownership (#2.5)."
)
content = content.replace(
    "#### #8: Stale ToolAvailability After Connect",
    "#### #8: Stale ToolAvailability After Connect\n\n**CC Principle**: Lifecycle Ownership (#2.5) — reconnect must restore availability state."
)

# #9: CC convergence statement
old9 = "#### #9: Health Metrics Counter\n\n**Problem**: No call latency, error rate, or reconnect frequency"
new9 = (
    "#### #9: Health Metrics Counter\n\n"
    "**CC alignment**: CC uses OpenTelemetry spans for MCP health metrics. "
    "Our in-process counters are a v1 approximation. Converges to OTel span "
    "attributes when TraceContext integration is wired into the MCP bridge layer.\n\n"
    "**Problem**: No call latency, error rate, or reconnect frequency"
)
content = content.replace(old9, new9)

# #5 Checklist: add sequential regression test
old5 = "- [ ] WsMCPBridge._rpc_call acquires asyncio.Lock"
new5 = (
    "- [ ] WsMCPBridge._rpc_call acquires asyncio.Lock\n"
    "- [ ] Sequential WsMCPBridge calls (call A -> await -> call B -> await) complete successfully after lock integration"
)
content = content.replace(old5, new5)

print("All fixes applied")
with open("docs/MCP_SYSTEM_NORMALIZATION_DESIGN.md", "w", encoding="utf-8") as f:
    f.write(content)
