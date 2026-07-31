"""Phase 0 pre-scan: Audit non-standard MessageKind values in existing databases.

Usage: python scripts/phase0_pre_scan.py [db_path]

If no db_path is given, scans all *.db files under ~/.grace/
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Standard kind values that will remain after Phase 1
VALID_KINDS: set[str | None] = {"runtime_notice", None, ""}

# Legacy kind values being removed
LEGACY_KINDS: set[str] = {
    "user", "assistant", "system", "tool_result",
    "compaction_boundary", "plan_context",
}

# Tables to scan
SCAN_TABLES = [
    ("session_messages", "content"),
    ("session_message_archive", "content"),
]


def scan_db(db_path: str) -> dict[str, dict[str, int]]:
    """Scan one SQLite database for kind value distribution."""
    results: dict[str, dict[str, int]] = {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for table, sample_col in SCAN_TABLES:
            # Check if table exists
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue

            # Check if the table has a 'kind' column (session_messages doesn't
            # store kind directly — we check content for compaction markers)
            if table == "session_message_archive":
                # This table stores messages archived during compaction.
                # Check content for kind markers.
                rows = conn.execute(
                    f"SELECT content FROM {table} LIMIT 10000"
                ).fetchall()
                kind_counts: dict[str, int] = {}
                for row in rows:
                    content = str(row["content"] or "")
                    if content.startswith("[Earlier conversation summarized"):
                        kind_counts.setdefault("compaction_boundary_legacy", 0)
                        kind_counts["compaction_boundary_legacy"] += 1
                    elif content.startswith("[Conversation compacted"):
                        kind_counts.setdefault("compaction_boundary_legacy", 0)
                        kind_counts["compaction_boundary_legacy"] += 1
                if kind_counts:
                    results[table] = kind_counts
                continue

            # For session_messages: check content for injection markers
            # that should have been filtered by _RUNTIME_PREFIXES but might
            # have leaked due to Phase 0 gap
            rows = conn.execute(
                f"SELECT id, role, content FROM {table} ORDER BY id DESC LIMIT 10000"
            ).fetchall()
            leaked: dict[str, int] = {}
            for row in rows:
                content = str(row["content"] or "")
                if content.startswith("[RUNTIME EVIDENCE STATE]"):
                    leaked.setdefault("[RUNTIME EVIDENCE STATE]", 0)
                    leaked["[RUNTIME EVIDENCE STATE]"] += 1
                elif content.startswith("[RUNTIME BLOCK]"):
                    leaked.setdefault("[RUNTIME BLOCK]", 0)
                    leaked["[RUNTIME BLOCK]"] += 1
                elif content.startswith("[SESSION START HOOK CONTEXT]"):
                    leaked.setdefault("[SESSION START HOOK CONTEXT]", 0)
                    leaked["[SESSION START HOOK CONTEXT]"] += 1
                elif content.startswith("[Stop hook blocked"):
                    leaked.setdefault("[Stop hook blocked *]", 0)
                    leaked["[Stop hook blocked *]"] += 1
                elif content.startswith("[Subagent:"):
                    leaked.setdefault("[Subagent: *]", 0)
                    leaked["[Subagent: *]"] += 1
                elif content.startswith("[Skill:"):
                    leaked.setdefault("[Skill: *]", 0)
                    leaked["[Skill: *]"] += 1
                elif content.startswith("<task-notification>"):
                    leaked.setdefault("<task-notification>", 0)
                    leaked["<task-notification>"] += 1
                elif content.startswith("[Parent message from"):
                    leaked.setdefault("[Parent message from *]", 0)
                    leaked["[Parent message from *]"] += 1
            if leaked:
                results[table] = leaked
    finally:
        conn.close()
    return results


def main() -> int:
    paths: list[str] = []

    if len(sys.argv) > 1:
        paths = sys.argv[1:]
    else:
        grace_dir = Path.home() / ".grace"
        if grace_dir.exists():
            db_files = list(grace_dir.rglob("*.db"))
            if db_files:
                paths = [str(p) for p in db_files]
                print(f"Found {len(paths)} database(s) under {grace_dir}")
        if not paths:
            print("No databases found. Usage: python scripts/phase0_pre_scan.py <db_path>")
            return 0

    total_non_standard = 0
    for path in paths:
        print(f"\n{'='*60}")
        print(f"Scanning: {path}")
        results = scan_db(path)

        if not results:
            print("  No relevant data found (tables missing or empty)")
            continue

        for table, counts in results.items():
            print(f"  Table: {table}")
            for kind_value, count in sorted(counts.items()):
                marker = ""
                if kind_value in LEGACY_KINDS:
                    marker = " ← LEGACY (will be mapped to None)"
                elif kind_value not in VALID_KINDS:
                    marker = " ← NON-STANDARD"
                    total_non_standard += count
                print(f"    {kind_value!r}: {count}{marker}")

    print(f"\n{'='*60}")
    if total_non_standard > 0:
        print(f"WARNING: FOUND {total_non_standard} non-standard rows.")
        print("   Decision required: UPDATE these rows to NULL before migration, or")
        print("   implement a compat mapping in the deserialization layer.")
    else:
        print("OK: All rows have standard kind values (or tables are empty).")
        print("   Compat layer is pure insurance — no data migration needed.")
    return 0 if total_non_standard == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
