#!/usr/bin/env python3
"""Initialize or validate the additive Discord Events Hub schema."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def quick_check(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {result}")


def validate(path: Path) -> None:
    required = {
        "dashboard_scheduled_events", "dashboard_event_ownership", "dashboard_event_artwork",
        "event_dashboard_actions", "dashboard_event_sync_status",
        "reminder_items", "reminder_occurrences", "reminder_subscriptions",
        "reminder_deliveries",
    }
    with sqlite3.connect(path) as connection:
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = required - present
        if missing:
            raise RuntimeError("Missing Events Hub tables: " + ", ".join(sorted(missing)))
        action_columns = {row[1] for row in connection.execute("PRAGMA table_info(event_dashboard_actions)")}
        if {
            "idempotency_key", "image_bytes", "attempt_count", "failure_reason",
            "storage_channel_id", "storage_thread_id", "storage_message_id",
            "storage_attachment_url",
        } - action_columns:
            raise RuntimeError("Events Hub queued-action schema is incomplete.")
    quick_check(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--backup-dir")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    database = Path(args.database).expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"Database not found: {database}")
    quick_check(database)
    if not args.validate_only:
        if args.backup_dir:
            destination = Path(args.backup_dir).expanduser().resolve()
            destination.mkdir(parents=True, exist_ok=True)
            backup = destination / f"pre-events-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite"
            with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
                source.backup(target)
            quick_check(backup)
            print(f"Database backup: {backup}")
        os.environ["DATABASE_PATH"] = str(database)
        from utils.events import initialize_events_schema
        initialize_events_schema()
    validate(database)
    print("Events Hub schema validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
