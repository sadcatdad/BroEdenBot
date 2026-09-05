#!/usr/bin/env python3
"""Apply or validate the additive Event Drops migration without touching other data."""
from __future__ import annotations
import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.event_drops import EventDrops


def validate(path):
    with sqlite3.connect(path) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed.")
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {
            "event_drop_schema",
            "event_drop_campaigns",
            "event_drop_channels",
            "event_drop_roles",
            "event_drop_assets",
            "event_drops",
            "event_drop_claims",
            "event_drop_members",
            "event_drop_worker",
            "event_drop_variants",
            "event_drop_variant_assets",
            "event_drop_snapshots",
        }
        if required - tables:
            raise RuntimeError(
                "Missing tables: " + ", ".join(sorted(required - tables))
            )
        if db.execute("SELECT MAX(version) FROM event_drop_schema").fetchone()[0] != 2:
            raise RuntimeError("Unexpected migration version.")
        for table, expected in {
            "event_drop_campaigns": {"variants_enabled"},
            "event_drop_assets": {"campaign_pool"},
            "event_drops": {
                "variant_id",
                "variant_name",
                "rarity",
                "variant_selection",
            },
        }.items():
            if expected - {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                raise RuntimeError(f"Missing Drop Variant columns in {table}.")
        if db.execute(
            "SELECT 1 FROM event_drops d LEFT JOIN event_drop_snapshots s ON s.drop_id=d.id WHERE s.drop_id IS NULL LIMIT 1"
        ).fetchone():
            raise RuntimeError("An Event Drop is missing its configuration snapshot.")
        for table in required:
            if db.execute(f"PRAGMA foreign_key_check({table})").fetchone():
                raise RuntimeError("Event Drops foreign key validation failed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--backup-dir")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    path = Path(args.database).expanduser().resolve()
    if not path.is_file():
        parser.error("Database must already exist.")
    with sqlite3.connect(path) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed.")
        if args.backup_dir and not args.validate_only:
            directory = Path(args.backup_dir).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
            backup = directory / (
                "pre-event-drops-"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                + ".sqlite"
            )
            with sqlite3.connect(backup) as target:
                db.backup(target)
            print(f"Backup: {backup}")
    if not args.validate_only:
        EventDrops(path).initialize()
    validate(path)
    print("Event Drops schema validation passed.")


if __name__ == "__main__":
    main()
