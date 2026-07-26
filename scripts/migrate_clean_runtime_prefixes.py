"""Safely remove Grace Code's legacy UNVERIFIED prefix from persisted content.

Dry-run is the default. Use ``--apply`` to create a SQLite backup and update
matched rows in one transaction. Plan files are only scanned when ``--repo``
is supplied; each changed file receives a sibling ``.runtime-prefix.bak``.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


LEGACY_PREFIX = re.compile(
    r"\A\[UNVERIFIED — (?:no test environment available|"
    r"project has no Git fact source|tests ran but failed|"
    r"test/validation did not run or was unavailable)\. "
    r"Code changes were made but NOT independently verified\.\]\r?\n\r?\n"
)


def clean_text(value: str) -> tuple[str, bool]:
    cleaned, count = LEGACY_PREFIX.subn("", value, count=1)
    return cleaned, count == 1


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def clean_text_column(
    conn: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    text_column: str,
    counter_key: str,
    counts: Counter[str],
    apply: bool,
    where: str = "",
) -> None:
    if not table_exists(conn, table):
        return
    query = f"SELECT {id_column}, {text_column} FROM {table}"
    if where:
        query += f" WHERE {where}"
    for row_id, value in conn.execute(query).fetchall():
        if not isinstance(value, str):
            continue
        cleaned, matched = clean_text(value)
        if not matched:
            continue
        counts[counter_key] += 1
        if apply:
            conn.execute(
                f"UPDATE {table} SET {text_column}=? WHERE {id_column}=?",
                (cleaned, row_id),
            )


def clean_json_summary(
    conn: sqlite3.Connection,
    *,
    column: str,
    counter_key: str,
    counts: Counter[str],
    apply: bool,
) -> None:
    if not table_exists(conn, "sessions"):
        return
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    if column not in columns:
        return
    for session_id, raw in conn.execute(
        f"SELECT id, {column} FROM sessions WHERE {column} IS NOT NULL"
    ).fetchall():
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("summary"), str):
            continue
        cleaned, matched = clean_text(payload["summary"])
        if not matched:
            continue
        counts[counter_key] += 1
        if apply:
            payload["summary"] = cleaned
            conn.execute(
                f"UPDATE sessions SET {column}=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), session_id),
            )


def clean_trace_events(
    conn: sqlite3.Connection, counts: Counter[str], apply: bool,
) -> None:
    if not table_exists(conn, "session_trace_events"):
        return
    rows = conn.execute(
        "SELECT id, event_json FROM session_trace_events "
        "WHERE event_type='run_terminal'"
    ).fetchall()
    for event_id, raw in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        summary = payload.get("summary")
        if not isinstance(summary, str):
            continue
        cleaned, matched = clean_text(summary)
        if not matched:
            continue
        counts["trace_events"] += 1
        if apply:
            payload["summary"] = cleaned
            payload["legacy_content_cleaned"] = True
            conn.execute(
                "UPDATE session_trace_events SET event_json=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), event_id),
            )


def clean_plan_files(repo: Path, counts: Counter[str], apply: bool) -> None:
    plans_dir = repo / ".grace" / "plans"
    if not plans_dir.is_dir():
        return
    for plan_path in plans_dir.glob("*.md"):
        content = plan_path.read_text(encoding="utf-8")
        cleaned, matched = clean_text(content)
        if not matched:
            continue
        counts["plan_files"] += 1
        if apply:
            backup = plan_path.with_suffix(plan_path.suffix + ".runtime-prefix.bak")
            shutil.copy2(plan_path, backup)
            plan_path.write_text(cleaned, encoding="utf-8")


def migrate(db_path: Path, *, repo: Path | None, apply: bool) -> Counter[str]:
    counts: Counter[str] = Counter()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)

    if apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = db_path.with_name(f"{db_path.name}.{stamp}.bak")
        with sqlite3.connect(db_path) as source, sqlite3.connect(backup_path) as target:
            source.backup(target)
        print(f"backup: {backup_path}")

    with sqlite3.connect(db_path) as conn:
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        try:
            clean_text_column(
                conn, table="session_messages", id_column="id",
                text_column="content", counter_key="session_messages",
                counts=counts, apply=apply, where="role='assistant'",
            )
            clean_text_column(
                conn, table="sessions", id_column="id",
                text_column="summary", counter_key="sessions",
                counts=counts, apply=apply,
            )
            clean_text_column(
                conn, table="runs", id_column="id",
                text_column="summary", counter_key="runs",
                counts=counts, apply=apply,
            )
            clean_text_column(
                conn, table="plan_revisions", id_column="id",
                text_column="content", counter_key="plan_revisions",
                counts=counts, apply=apply,
            )
            clean_json_summary(
                conn, column="agent_result_json",
                counter_key="agent_result_json", counts=counts, apply=apply,
            )
            clean_json_summary(
                conn, column="fork_result_json",
                counter_key="fork_result_json", counts=counts, apply=apply,
            )
            clean_trace_events(conn, counts, apply)
            if apply:
                conn.execute("COMMIT")
        except Exception:
            if apply:
                conn.execute("ROLLBACK")
            raise

    if repo is not None:
        clean_plan_files(repo.resolve(), counts, apply)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this flag the command is a dry-run.",
    )
    args = parser.parse_args()

    counts = migrate(
        args.db.resolve(),
        repo=args.repo.resolve() if args.repo else None,
        apply=args.apply,
    )
    print("mode:", "apply" if args.apply else "dry-run")
    for key in (
        "session_messages", "sessions", "runs", "plan_revisions",
        "agent_result_json", "fork_result_json", "plan_files", "trace_events",
    ):
        print(f"{key} matched: {counts[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
