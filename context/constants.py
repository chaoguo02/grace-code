"""Shared constants for the context management module.

Imported by both ``session_store.py`` and ``runtime.py`` — the single
source of truth for runtime-injected message filtering.
"""

# Runtime-injected prompt-engineering messages start with these prefixes.
# They must never appear in the frontend or persist to the database.
#
# Sorted by length descending — a simple ``startswith`` in this order
# guarantees that ``[RUNTIME A]`` does not shadow ``[RUNTIME AB]``.
# Callers should iterate in the given order; the first match wins.
#
# If you add a new prefix, insert it in the correct length-descending
# position.  Do NOT append to the end unless it is the shortest prefix.
_RUNTIME_PREFIXES: tuple[str, ...] = (
    # ── Longest prefixes first (26+ chars) ──
    "[PREVIOUS SESSION CONTEXT]",
    "[SESSION START HOOK CONTEXT]",     # Phase 0: added (was leaking)
    "[Stop hook blocked completion]",   # Phase 0: added (was leaking)
    "[Earlier conversation summarized",
    "[Conversation compacted",
    "[ACCUMULATED FINDINGS]",
    "[RUNTIME EVIDENCE STATE]",         # Phase 0: added (was leaking)
    "<task-notification>",              # Phase 0: added (was leaking)
    "[Parent message from ",            # Phase 0: partial-prefix (was leaking)

    # ── Medium prefixes (14-22 chars) ──
    "[PRELOADED SKILLS]",
    "[RUNTIME BLOCK]",                  # Phase 0: added (was leaking)
    "[MEMORY RESTORED]",
    "[AGENT MEMORY]",
    "[ACTIVE POLICY]",
    "[PLAN CONTEXT]",
    "[FEEDBACK]",
    "[ENVIRONMENT]",
    "[SYSTEM]",

    # ── Shorter prefixes ──
    "[TASK ANCHOR]",
    "[TASK MODE]",
    "[Subagent: ",                      # Phase 0: partial-prefix (was leaking)
    "[Skill: ",                         # Phase 0: partial-prefix (was leaking)
)


def matches_runtime_prefix(content: str) -> bool:
    """Return True if *content* starts with any runtime-injected prefix.

    Uses the length-descending prefix list above to avoid substring
    shadowing (e.g., ``[RUNTIME A]`` matching before ``[RUNTIME AB]``).
    """
    return any(content.startswith(prefix) for prefix in _RUNTIME_PREFIXES)
