#!/usr/bin/env python3
"""Initialize or validate dashboard RBAC, audit, and Discord-verification schema."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
SCHEMA_VERSION = 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Path to the shared SQLite database")
    parser.add_argument("--backup-dir", help="Directory for a pre-migration SQLite backup")
    parser.add_argument("--validate-only", action="store_true", help="Validate without modifying schema")
    return parser.parse_args()


def quick_check(path: Path) -> None:
    with sqlite3.connect(str(path)) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError("SQLite quick_check failed: {}".format(result))


def validate_schema(path: Path) -> None:
    required_tables = {
        "dashboard_users", "dashboard_permissions", "dashboard_roles",
        "dashboard_role_permissions", "dashboard_user_role_assignments",
        "dashboard_discord_role_mappings", "dashboard_user_permission_overrides",
        "dashboard_audit_log", "dashboard_schema_migrations",
    }
    required_user_columns = {
        "discord_guild_id", "discord_role_ids_json", "discord_verified_at",
        "discord_verification_status", "access_source",
    }
    required_triggers = {"dashboard_audit_log_no_update", "dashboard_audit_log_no_delete"}
    with sqlite3.connect(str(path)) as connection:
        present = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = required_tables - present
        if missing:
            raise RuntimeError("Missing dashboard tables: {}".format(", ".join(sorted(missing))))
        columns = {row[1] for row in connection.execute("PRAGMA table_info(dashboard_users)")}
        missing_columns = required_user_columns - columns
        if missing_columns:
            raise RuntimeError("Missing dashboard user columns: {}".format(", ".join(sorted(missing_columns))))
        triggers = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        if required_triggers - triggers:
            raise RuntimeError("Append-only audit triggers are missing.")
        version = connection.execute(
            "SELECT MAX(version) FROM dashboard_schema_migrations"
        ).fetchone()[0]
        if version != SCHEMA_VERSION:
            raise RuntimeError("Unexpected dashboard schema version: {}".format(version))
        permission_count = connection.execute("SELECT COUNT(*) FROM dashboard_permissions").fetchone()[0]
        role_count = connection.execute("SELECT COUNT(*) FROM dashboard_roles").fetchone()[0]
        if permission_count < 1 or role_count < 6:
            raise RuntimeError("Dashboard permission or system-role seed is incomplete.")


def main() -> int:
    args = parse_args()
    database = Path(args.database).expanduser().resolve()
    if not database.is_file():
        raise SystemExit("Database not found: {}".format(database))
    quick_check(database)
    if args.validate_only:
        validate_schema(database)
        print("Dashboard RBAC schema validation passed.")
        return 0

    if args.backup_dir:
        backup_dir = Path(args.backup_dir).expanduser().resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / "pre-dashboard-rbac-{}.sqlite".format(timestamp)
        with sqlite3.connect(str(database)) as source, sqlite3.connect(str(backup)) as target:
            source.backup(target)
        quick_check(backup)
        print("Database backup: {}".format(backup))

    os.environ["DATABASE_PATH"] = str(database)
    from dashboard.rbac import initialize_rbac_schema
    from dashboard.users import initialize_dashboard_users
    from utils.settings import initialize_settings_from_env

    initialize_dashboard_users()
    initialize_settings_from_env()
    initialize_rbac_schema()
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT OR REPLACE INTO dashboard_schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (SCHEMA_VERSION, "dashboard_rbac_audit_and_guild_verification", datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
    quick_check(database)
    validate_schema(database)
    print("Dashboard RBAC migration passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
