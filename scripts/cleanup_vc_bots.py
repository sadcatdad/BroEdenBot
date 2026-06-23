#!/usr/bin/env python3
"""Remove excluded users from VC stats tables."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.exclusions import (
    env_csv_ids,
    fetch_role_member_ids,
    load_excluded_user_cache,
    parse_csv_ids,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean excluded bot/user rows from VC stats."
    )
    parser.add_argument("--database", type=Path, default=Path("data.db"))
    parser.add_argument("--role-ids", default="")
    parser.add_argument("--excluded-user-cache", type=Path)
    parser.add_argument("--guild-id", type=int)
    parser.add_argument("--use-env", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser.parse_args()


def placeholders(values: set[int]) -> str:
    return ", ".join("?" for _ in values)


def load_targets(args: argparse.Namespace) -> tuple[set[int], set[int]]:
    role_ids = parse_csv_ids(args.role_ids)
    user_ids: set[int] = set()
    if args.use_env:
        role_ids.update(env_csv_ids("VC_EXCLUDED_ROLE_IDS"))
        user_ids.update(env_csv_ids("VC_EXCLUDED_USER_IDS"))
    if args.excluded_user_cache:
        user_ids.update(load_excluded_user_cache(args.excluded_user_cache))
    if args.guild_id and role_ids:
        user_ids.update(asyncio.run(fetch_role_member_ids(args.guild_id, role_ids)))
    return role_ids, user_ids


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def main() -> int:
    args = parse_args()
    role_ids, user_ids = load_targets(args)
    if not user_ids:
        print("No excluded user IDs were found. Generate/pass --excluded-user-cache first.")
        return 2
    if not args.dry_run and not args.yes:
        print("Refusing to delete without --yes. Re-run with --dry-run first.")
        return 2

    connection = sqlite3.connect(args.database)
    try:
        params = list(sorted(user_ids))
        guild_sql = ""
        if args.guild_id:
            guild_sql = "AND guild_id = ?"
            params.append(args.guild_id)
        where = f"user_id IN ({placeholders(user_ids)}) {guild_sql}"
        rows = connection.execute(
            f"""
            SELECT user_id, MAX(username), MAX(display_name), COUNT(*),
                   COALESCE(SUM(duration_seconds), 0)
            FROM vc_sessions
            WHERE {where}
            GROUP BY user_id
            ORDER BY COALESCE(SUM(duration_seconds), 0) DESC
            """,
            params,
        ).fetchall()
        imported_rows = []
        if table_exists(connection, "vc_imported_sessions"):
            imported_rows = connection.execute(
                f"""
                SELECT user_id, MAX(user_name), MAX(display_name), COUNT(*),
                       COALESCE(SUM(duration_seconds), 0)
                FROM vc_imported_sessions
                WHERE {where}
                GROUP BY user_id
                ORDER BY COALESCE(SUM(duration_seconds), 0) DESC
                """,
                params,
            ).fetchall()
        active_rows = 0
        if table_exists(connection, "vc_active_sessions"):
            active_rows = connection.execute(
                f"SELECT COUNT(*) FROM vc_active_sessions WHERE {where}",
                params,
            ).fetchone()[0]
        total_sessions = sum(int(row[3] or 0) for row in rows)
        total_duration = sum(int(row[4] or 0) for row in rows)
        total_imported_sessions = sum(int(row[3] or 0) for row in imported_rows)
        total_imported_duration = sum(int(row[4] or 0) for row in imported_rows)
        print("Back up data.db before a real cleanup.")
        print(f"Role IDs used: {', '.join(str(i) for i in sorted(role_ids)) or 'none'}")
        print(f"Excluded user count: {len(user_ids):,}")
        print(f"Matching VC sessions: {total_sessions:,}")
        print(f"Matching imported VC sessions: {total_imported_sessions:,}")
        print(f"Matching active sessions: {active_rows:,}")
        print(
            "Total duration to remove: "
            f"{total_duration + total_imported_duration:,} seconds"
        )
        print("Per-user breakdown:")
        for user_id, username, display_name, session_count, duration in rows:
            label = display_name or username or str(user_id)
            print(f"  {user_id} {label}: sessions={session_count:,} duration={duration:,}s")
        for user_id, username, display_name, session_count, duration in imported_rows:
            label = display_name or username or str(user_id)
            print(
                f"  {user_id} {label}: imported_sessions={session_count:,} "
                f"duration={duration:,}s"
            )
        if args.dry_run:
            return 0
        connection.execute(f"DELETE FROM vc_sessions WHERE {where}", params)
        if table_exists(connection, "vc_imported_sessions"):
            connection.execute(f"DELETE FROM vc_imported_sessions WHERE {where}", params)
        if table_exists(connection, "vc_active_sessions"):
            connection.execute(f"DELETE FROM vc_active_sessions WHERE {where}", params)
        if table_exists(connection, "vc_xp_user_state"):
            connection.execute(f"DELETE FROM vc_xp_user_state WHERE {where}", params)
        connection.commit()
        print(
            f"Deleted {total_sessions:,} vc_sessions rows and "
            f"{total_imported_sessions:,} vc_imported_sessions rows."
        )
        if args.vacuum:
            connection.execute("VACUUM")
            print("Vacuum complete.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
