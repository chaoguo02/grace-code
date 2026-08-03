"""Phase 5 A3: Replace magic values in agent/core.py with named constants."""
import re

with open("agent/core.py", encoding="utf-8") as f:
    content = f.read()

# ── 1. Add import before context_trimming import ──
old_imp = "from agent.context_trimming import _snip_history"
new_imp = """from agent.constants import (
    BUDGET_COMPACT_PCT, BUDGET_WARNING_PCT, COMPLETION_BLOCK_THRESHOLD,
    DEFAULT_HISTORY_BUDGET_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_REQUEST_BUDGET_TOKENS, DEFAULT_TRUNCATE_OUTPUT_CHARS,
    DIFF_PREVIEW_MAX_CHARS, FINDING_DESC_CHARS, MAX_TOOL_RESULTS_EXTRACT,
    NO_THOUGHT_SENTINEL, RECENT_FILES_WINDOW, RECOVERY_MAX_FINDINGS,
    SESSION_MEMORY_MSG_WINDOW, SUMMARY_TRUNCATION_CHARS,
    TEST_FAILURE_REFLECTION_LIMIT, TOOL_EXTRACT_CHARS,
    TRUNCATION_BUFFER_TOKENS,
)
from agent.context_trimming import _snip_history"""
assert old_imp in content, "Import line not found"
content = content.replace(old_imp, new_imp)
print("1. Import block added")

# ── 2. Precise replacements ──
reps = [
    # Budget
    ("request_budget_tokens or 200_000", "request_budget_tokens or DEFAULT_REQUEST_BUDGET_TOKENS"),
    ("self._cfg.request_budget_tokens or 110_000", "self._cfg.request_budget_tokens or DEFAULT_HISTORY_BUDGET_TOKENS"),
    ('getattr(self._cfg, "max_tokens", 32000) - 100', 'getattr(self._cfg, "max_tokens", DEFAULT_MAX_OUTPUT_TOKENS) - TRUNCATION_BUFFER_TOKENS'),
    ('getattr(self._cfg, "max_tokens", 32000)', 'getattr(self._cfg, "max_tokens", DEFAULT_MAX_OUTPUT_TOKENS)'),
    # Budget monitoring
    ("self._cfg.request_budget_tokens or 200_000", "self._cfg.request_budget_tokens or DEFAULT_REQUEST_BUDGET_TOKENS"),
    ("_budget_pct / _budget_total * 100) if _budget_total else 0", "_budget_pct / _budget_total * BUDGET_COMPACT_PCT) if _budget_total else 0"),
    ("if step > COMPLETION_BLOCK_THRESHOLD and _budget_pct > 80:", "if step > COMPLETION_BLOCK_THRESHOLD and _budget_pct > BUDGET_WARNING_PCT:"),
    ("if step > COMPLETION_BLOCK_THRESHOLD and _budget_pct > BUDGET_WARNING_PCT and self.compactor", "if step > COMPLETION_BLOCK_THRESHOLD and _budget_pct > BUDGET_WARNING_PCT and self.compactor"),
    # Display
    ("_git_state.current_diff[:3000]", "_git_state.current_diff[:DIFF_PREVIEW_MAX_CHARS]"),
    ("if len(content) > 2000:\n                content = content[:2000] + \"...\"", "if len(content) > SUMMARY_TRUNCATION_CHARS:\n                content = content[:SUMMARY_TRUNCATION_CHARS] + \"...\""),
    ("content[:2000]", "content[:SUMMARY_TRUNCATION_CHARS]"),
    ("content[:500]", "content[:TOOL_EXTRACT_CHARS]"),
    ("if len(tool_contents) >= 5:", "if len(tool_contents) >= MAX_TOOL_RESULTS_EXTRACT:"),
    ("f.get('title','')[:200]", "f.get('title','')[:FINDING_DESC_CHARS]"),
    ("findings[-10:]", "findings[-RECOVERY_MAX_FINDINGS:]"),
    ("max_chars: int = 8000", "max_chars: int = DEFAULT_TRUNCATE_OUTPUT_CHARS"),
    # Loop control
    ("_MAX_STOP_HOOK_RETRIES = 3", "_MAX_STOP_HOOK_RETRIES = COMPLETION_BLOCK_THRESHOLD"),
    ("_recent_files = sorted(self._accessed_files)[-20:]", "_recent_files = sorted(self._accessed_files)[-RECENT_FILES_WINDOW:]"),
    ("for msg in messages[-20:]:", "for msg in messages[-SESSION_MEMORY_MSG_WINDOW:]:"),
    ("== \"(no thought)\"", "== NO_THOUGHT_SENTINEL"),
    ('reflection_counts.get("test_failed", 0) >= 3:', 'reflection_counts.get("test_failed", 0) >= TEST_FAILURE_REFLECTION_LIMIT:'),
]

count = 0
for old, new in reps:
    if old in content:
        content = content.replace(old, new)
        count += 1
    else:
        print(f"SKIP: {old[:60]}")

print(f"Applied {count}/{len(reps)} replacements")

# Post: re-apply budget line since "200_000" was consumed by first pattern
if "DEFAULT_REQUEST_BUDGET_TOKENS" not in content:
    content = content.replace(
        "self._cfg.request_budget_tokens or 200_000",
        "self._cfg.request_budget_tokens or DEFAULT_REQUEST_BUDGET_TOKENS",
    )

# Fix _block_count >= 3 for completion block
content = content.replace(
    "if _block_count >= 3:",
    "if _block_count >= COMPLETION_BLOCK_THRESHOLD:",
)

with open("agent/core.py", "w", encoding="utf-8") as f:
    f.write(content)
print("agent/core.py written")
