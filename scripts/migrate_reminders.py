#!/usr/bin/env python3
"""Run and validate the idempotent canonical reminder migration."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import aiosqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.reminder_service import ReminderService
from utils.settings import settings_database_path
from utils.sqlite import configure_connection, configure_sync_connection


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=settings_database_path())
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def validate(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    configure_sync_connection(connection)
    try:
        required = {
            "reminder_items",
            "reminder_occurrences",
            "reminder_subscriptions",
            "reminder_deliveries",
            "reminder_audit",
            "reminder_dashboard_actions",
            "reminder_migrations",
        }
        found = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = sorted(required - found)
        if missing:
            raise RuntimeError("Missing canonical reminder tables: " + ", ".join(missing))
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(required)
        }
        invalid_deliveries = int(connection.execute(
            """
            SELECT COUNT(*) FROM reminder_deliveries d
            LEFT JOIN reminder_occurrences o ON o.id = d.occurrence_id
            WHERE o.id IS NULL
            """
        ).fetchone()[0])
        if invalid_deliveries:
            raise RuntimeError(f"Found {invalid_deliveries} orphaned delivery rows.")
        return counts
    finally:
        connection.close()


async def migrate(path: Path) -> dict[str, int]:
    database = await aiosqlite.connect(path)
    database.row_factory = aiosqlite.Row
    await configure_connection(database, foreign_keys=True)
    try:
        report = await ReminderService(database).initialize()
        return report.as_dict()
    finally:
        await database.close()


async def main() -> int:
    args = parse_args()
    path = args.database.expanduser().resolve()
    if not path.is_file():
        print(f"Database not found: {path}", file=sys.stderr)
        return 2
    output: dict[str, object] = {"database": str(path)}
    if not args.validate_only:
        output["migration"] = await migrate(path)
    output["counts"] = validate(path)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
