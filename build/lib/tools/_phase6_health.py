"""Phase 6 pre-flight: legacy module health check."""
import os, re

modules = {
    "agent/loop/types.py": "LoopAction, StepResult, CompletionBlockTracker",
    "agent/constants.py": "18 budget/display/loop sentinel constants",
    "server/services/chat_pipeline.py": "ChatPipeline, ChatPipelinePorts, ChatRequest, PreparedChatRun",
    "web/src/utils/format.ts": "formatBytes, formatRuntime, formatValue",
    "web/src/utils/status.ts": "summarizeStatus",
    "web/src/utils/target.ts": "summarizeTarget",
    "web/src/utils/markdown.ts": "renderMarkdownSafe",
    "web/src/hooks/useWebSocket.ts": "connectWebSocket, disconnectWebSocket, scheduleReconnect",
}

print("{:<48} {:>5}  {:>4}  {:>6}  {}".format("Module", "Lines", "Doc", "Public", "Types"))
print("-" * 95)

for path, expected_api in modules.items():
    if not os.path.exists(path):
        print(f"{path:<48} MISSING")
        continue
    lines = len(open(path, encoding="utf-8-sig").readlines())
    content = open(path, encoding="utf-8-sig").read()
    has_doc = "OK" if '"""' in content[:400] or "/**" in content[:400] else "MISS"
    is_ts = path.endswith(".ts")
    exports = len(re.findall(r"^export ", content, re.MULTILINE))
    defs = len(re.findall(r"^(def |class )", content, re.MULTILINE))
    public = exports if is_ts else defs
    has_types = "OK" if is_ts or "->" in content else "MISS"

    print(f"{path:<48} {lines:>5}  {has_doc:>4}  {public:>6}  {has_types}")
