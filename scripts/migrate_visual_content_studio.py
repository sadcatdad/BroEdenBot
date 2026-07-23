#!/usr/bin/env python3
"""Initialize or validate the additive Visual Content Studio schema."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Path to the shared SQLite database")
    parser.add_argument("--asset-dir", help="Persistent visual asset directory")
    parser.add_argument("--backup-dir", help="Directory for a pre-migration SQLite backup")
    parser.add_argument("--validate-only", action="store_true", help="Validate without modifying schema")
    return parser.parse_args()


def quick_check(path: Path) -> None:
    with sqlite3.connect(str(path.resolve())) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError("SQLite quick_check failed: {}".format(result))


def validate_schema(path: Path) -> None:
    required = {
        "visual_assets",
        "visual_themes",
        "visual_templates",
        "visual_template_versions",
        "visual_template_variants",
        "visual_schedules",
        "visual_asset_usage",
        "visual_asset_discord_storage",
        "visual_asset_storage_jobs",
        "visual_global_settings",
        "visual_audit_log",
        "visual_schema_migrations",
    }
    with sqlite3.connect(str(path.resolve())) as connection:
        present = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = required - present
        if missing:
            raise RuntimeError("Missing Visual Content Studio tables: {}".format(", ".join(sorted(missing))))
        registered = connection.execute("SELECT COUNT(*) FROM visual_templates").fetchone()[0]
        if registered < 1:
            raise RuntimeError("No visual templates were registered.")
        version = connection.execute("SELECT MAX(version) FROM visual_schema_migrations").fetchone()[0]
        if version != 2:
            raise RuntimeError("Unexpected Visual Content Studio schema version: {}".format(version))


def main() -> int:
    args = parse_args()
    database = Path(args.database).expanduser().resolve()
    if not database.is_file():
        raise SystemExit("Database not found: {}".format(database))
    quick_check(database)
    if args.validate_only:
        validate_schema(database)
        print("Visual Content Studio schema validation passed.")
        return 0

    if args.backup_dir:
        backup_dir = Path(args.backup_dir).expanduser().resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / "pre-visual-studio-{}.sqlite".format(timestamp)
        with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        quick_check(backup)
        print("Database backup: {}".format(backup))

    os.environ["DATABASE_PATH"] = str(database)
    if args.asset_dir:
        os.environ["VISUAL_ASSET_DIR"] = str(Path(args.asset_dir).expanduser().resolve())
    from utils.visual_studio.repository import initialize_visual_studio_schema
    from utils.visual_studio.storage import ensure_asset_directories

    initialize_visual_studio_schema(str(database))
    root = ensure_asset_directories()
    quick_check(database)
    validate_schema(database)
    print("Visual Content Studio migration passed.")
    print("Persistent assets: {}".format(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
