#!/usr/bin/env python3
"""Migration script that auto-generates CREATE TABLE SQL from DDL schemas.

Usage:
    python db/migrate.py --stdout         # Print SQL to stdout
    python db/migrate.py --output <file>  # Write SQL to a file
    python db/migrate.py                   # Write to db/schema.sql by default
"""

import argparse
import logging
import sys
from pathlib import Path

# Insert project root into sys.path so we can import from src
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.user_service import UserService

logger = logging.getLogger(__name__)


def generate_create_table(schema: dict) -> str:
    """Convert a DDL schema dict into a CREATE TABLE IF NOT EXISTS SQL string."""
    table_name = schema["table"]
    cols = []
    for col in schema["columns"]:
        col_def = f'    {col["name"]} {col["type"]}'
        constraints = col.get("constraints", "")
        if constraints:
            col_def += f" {constraints}"
        cols.append(col_def)
    columns_sql = ",\n".join(cols)
    return f"CREATE TABLE IF NOT EXISTS {table_name} (\n{columns_sql}\n);"


def generate_sql(schemas: list[dict]) -> str:
    """Generate full SQL string from a list of DDL schema dicts."""
    statements = [generate_create_table(s) for s in schemas]
    return "\n\n".join(statements) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Generate CREATE TABLE SQL from DDL schemas in src/models.py"
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print generated SQL to stdout instead of writing to a file",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Write generated SQL to the specified file",
    )
    args = parser.parse_args()

    sql = generate_sql(UserService.get_schemas())

    if args.stdout:
        print(sql)
    elif args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(sql, encoding="utf-8")
        logger.info("Schema written to %s", output_path)
    else:
        default_path = _project_root / "db" / "schema.sql"
        default_path.write_text(sql, encoding="utf-8")
        logger.info("Schema written to %s", default_path)


if __name__ == "__main__":
    main()
