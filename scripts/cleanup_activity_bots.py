#!/usr/bin/env python3
"""Remove excluded users from message activity stats."""

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
        description="Clean excluded bot/user rows from stats_message_activity."
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
        role_ids.update(env_csv_ids("ACTIVITY_EXCLUDED_ROLE_IDS"))
        user_ids.update(env_csv_ids("ACTIVITY_EXCLUDED_USER_IDS"))
    if args.excluded_user_cache:
        user_ids.update(load_excluded_user_cache(args.excluded_user_cache))
    if args.guild_id and role_ids:
        user_ids.update(asyncio.run(fetch_role_member_ids(args.guild_id, role_ids)))
    return role_ids, user_ids


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
                   COALESCE(SUM(message_count), 0)
            FROM stats_message_activity
            WHERE {where}
            GROUP BY user_id
            ORDER BY COALESCE(SUM(message_count), 0) DESC
            """,
            params,
        ).fetchall()
        total_rows = sum(int(row[3] or 0) for row in rows)
        total_messages = sum(int(row[4] or 0) for row in rows)
        print("Back up data.db before a real cleanup.")
        print(f"Role IDs used: {', '.join(str(i) for i in sorted(role_ids)) or 'none'}")
        print(f"Excluded user count: {len(user_ids):,}")
        print(f"Matching rows: {total_rows:,}")
        print(f"Total message_count to remove: {total_messages:,}")
        print("Per-user breakdown:")
        for user_id, username, display_name, row_count, message_count in rows:
            label = display_name or username or str(user_id)
            print(f"  {user_id} {label}: rows={row_count:,} messages={message_count:,}")
        if args.dry_run:
            return 0
        connection.execute(f"DELETE FROM stats_message_activity WHERE {where}", params)
        connection.commit()
        print(f"Deleted {total_rows:,} stats_message_activity rows.")
        if args.vacuum:
            connection.execute("VACUUM")
            print("Vacuum complete.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
