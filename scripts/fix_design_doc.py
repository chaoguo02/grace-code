"""Apply final design doc fixes before Phase 1 implementation."""
import re

with open("docs/TOOL_SYSTEM_NORMALIZATION_DESIGN.md", "r", encoding="utf-8") as f:
    content = f.read()

# Fix 1: Remove stale CHECKLIST item — hard cap constant
old1 = "- [ ] Hard cap threshold is configurable constant `TOOL_DESC_TOKEN_BUDGET` (default 4,000), NOT hardcoded literal"
new1 = "- [ ] Dynamic budget calculation implemented: `remaining_for_tools = max(1000, available_context - conversation_tokens - system_prompt_tokens - RESERVE_FOR_RESPONSE)`. No static token budget constant exists."
if old1 in content:
    content = content.replace(old1, new1)
    print("Fix 1 OK: stale checklist item replaced")
else:
    print("Fix 1 FAILED: pattern not found")

# Fix 2: Add prerequisite GATE to checklist (non-zero exit script)
old2 = "- [ ] **PREREQUISITE (before selection logic)**: Audit all tool descriptions. Any >200 tokens must be trimmed to <=200. One-time audit script reports"
new2 = "- [ ] **PREREQUISITE GATE (blocks selection logic implementation)**: Audit all built-in tool descriptions. Any >200 tokens must be trimmed to <=200. One-time audit script runs and **exits non-zero if ANY tool exceeds limit**. Selection logic implementation is BLOCKED on zero failures."
if old2 in content:
    content = content.replace(old2, new2)
    print("Fix 2 OK: prerequisite gate added")
else:
    # Try regex match
    m = re.search(r"PREREQUISITE.*before selection", content)
    if m:
        print(f"Fix 2: found at pos {m.start()}, length={m.end()-m.start()}")
        print(f"  Content: {repr(content[m.start():m.start()+120])}")
    else:
        print("Fix 2 FAILED: not found")

# Fix 3+5: Update all Shell digest rules from cmd_word to two-word variant
old3 = "Shell → sha256(cmd_word)"
new3 = "Shell → sha256(first_word + (“|” + second_word if second_word starts_with_“-” else “”))"
count3 = content.count(old3)
if count3 > 0:
    content = content.replace(old3, new3)
    print(f"Fix 3 OK: replaced {count3} Shell digest references")

old3b = "Shell → sha256(first_word_of_cmd)"
new3b = "Shell → sha256(first_word + second_word if second_word is a flag)"
if old3b in content:
    content = content.replace(old3b, new3b)
    print("Fix 3b OK: replaced alt Shell digest")

# Fix 4: Add known limitation note for Shell digest after the digest table
# Find the end of the params_digest table — after "Network" row, before next section
table_end = content.find(
    "**Implementation**:\n\n```python\n# hitl/pipeline.py"
)
if table_end < 0:
    table_end = content.find(
        "```python\n\n# hitl/pipeline.py"
    )
if table_end < 0:
    print("Fix 4 FAILED: could not find insertion point")
else:
    limitation = """**Known limitation**: The Shell digest rule uses only
``first_word`` (optionally ``second_word`` if it is a flag like ``-c``
or ``-e``) in v1.  This places ``python script.py`` and ``python -c
"...destructive..."`` in **different** trust buckets (because ``-c``
is a flag), but ``python script1.py`` and ``python script2.py`` in
the **same** bucket.  This is a deliberate trade-off: (a) destructive
Python commands would be caught by the safety floor (Layer 1 —
protected paths, cmd injection patterns), (b) the user explicitly
approved Python invocation twice before auto-trust activates, and (c)
a two-word digest for all shell commands would prevent trust
accumulation for any interpreter (too strict).  The convergence path
is narrowing to ``sha256(first_two_words)`` when a post-hoc safety
review layer is implemented.  Tracked in DDR divergence log above.

"""
    content = content[:table_end] + limitation + content[table_end:]
    print("Fix 4 OK: known limitation added")

with open("docs/TOOL_SYSTEM_NORMALIZATION_DESIGN.md", "w", encoding="utf-8") as f:
    f.write(content)
print("\nDone")
