#!/usr/bin/env python3
"""Initialize or validate the additive My BROfile schema and access grants."""

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
    with sqlite3.connect(str(path)) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError("SQLite quick_check failed: {}".format(result))


def validate_schema(path: Path) -> None:
    required_tables = {"brofiles", "brofile_media", "brofile_badges", "visual_assets"}
    required_permissions = {"brofiles.view", "brofiles.edit", "brofiles.manage"}
    with sqlite3.connect(str(path)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = required_tables - tables
        if missing_tables:
            raise RuntimeError(
                "Missing My BROfile tables: {}".format(
                    ", ".join(sorted(missing_tables))
                )
            )
        permissions = {
            row[0]
            for row in connection.execute(
                """
                SELECT permission_key FROM dashboard_permissions
                WHERE permission_key LIKE 'brofiles.%'
                """
            )
        }
        missing_permissions = required_permissions - permissions
        if missing_permissions:
            raise RuntimeError(
                "Missing My BROfile permissions: {}".format(
                    ", ".join(sorted(missing_permissions))
                )
            )
        migration = connection.execute(
            """
            SELECT 1 FROM dashboard_rbac_migrations
            WHERE migration_key = '2026_07_brofile_foundation_access'
            """
        ).fetchone()
        if migration is None:
            raise RuntimeError("My BROfile access grants were not applied.")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                "SQLite foreign-key validation found {} issue(s).".format(
                    len(foreign_key_errors)
                )
            )


def main() -> int:
    args = parse_args()
    database = Path(args.database).expanduser().resolve()
    if not database.is_file():
        raise SystemExit("Database not found: {}".format(database))
    quick_check(database)
    if args.validate_only:
        validate_schema(database)
        print("My BROfile schema validation passed.")
        return 0

    if args.backup_dir:
        backup_dir = Path(args.backup_dir).expanduser().resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / "pre-brofile-{}.sqlite".format(timestamp)
        with sqlite3.connect(str(database)) as source, sqlite3.connect(str(backup)) as target:
            source.backup(target)
        quick_check(backup)
        print("Database backup: {}".format(backup))

    os.environ["DATABASE_PATH"] = str(database)
    if args.asset_dir:
        os.environ["VISUAL_ASSET_DIR"] = str(
            Path(args.asset_dir).expanduser().resolve()
        )

    from dashboard.rbac import initialize_rbac_schema
    from dashboard.users import initialize_dashboard_users
    from utils.brofiles import initialize_brofile_schema
    from utils.visual_studio.repository import initialize_visual_studio_schema
    from utils.visual_studio.storage import ensure_asset_directories

    initialize_dashboard_users()
    initialize_visual_studio_schema(str(database))
    initialize_brofile_schema()
    initialize_rbac_schema()
    asset_root = ensure_asset_directories()
    (asset_root / "brofiles").mkdir(parents=True, exist_ok=True)

    quick_check(database)
    validate_schema(database)
    print("My BROfile migration passed.")
    print("Persistent profile media: {}".format(asset_root / "brofiles"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
