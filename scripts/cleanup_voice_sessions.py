#!/usr/bin/env python3
"""Dry-run and mark ignored VC sessions without deleting source data."""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_DATABASE = Path("data.db")


def parse_csv_ids(value: str | None) -> set[int]:
    ids: set[int] = set()
    for item in str(value or "").replace("\n", ",").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            parsed = int(text)
        except ValueError:
            continue
        if parsed > 0:
            ids.add(parsed)
    return ids


def env_csv_ids(name: str) -> set[int]:
    return parse_csv_ids(os.getenv(name, ""))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit and mark ignored voice sessions. No rows are deleted."
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--guild-id", type=int)
    parser.add_argument(
        "--excluded-channel-ids",
        default=os.getenv("EXCLUDED_VOICE_CHANNEL_IDS", ""),
        help="Comma-separated voice channel IDs to exclude.",
    )
    parser.add_argument(
        "--excluded-user-ids",
        default=os.getenv("VC_EXCLUDED_USER_IDS", ""),
        help="Comma-separated bot/user IDs to exclude from VC stats.",
    )
    parser.add_argument(
        "--max-duration-hours",
        type=float,
        default=12.0,
        help="Audit threshold for long sessions.",
    )
    parser.add_argument(
        "--include-long-sessions",
        action="store_true",
        help="Also mark sessions longer than --max-duration-hours ignored.",
    )
    args = parser.parse_args()
    if args.apply == args.dry_run:
        parser.error("Choose exactly one of --dry-run or --apply.")
    if args.max_duration_hours <= 0:
        parser.error("--max-duration-hours must be positive.")
    return args


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(connection, table):
        return set()
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def ensure_ignore_columns(connection: sqlite3.Connection, table: str) -> None:
    existing = columns(connection, table)
    if "ignored_at" not in existing:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN ignored_at TEXT')
    if "ignored_reason" not in existing:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN ignored_reason TEXT')


def placeholders(values: Iterable[int]) -> str:
    return ", ".join("?" for _ in values)


def select_sessions(
    connection: sqlite3.Connection,
    *,
    table: str,
    channel_column: str,
    user_column: str,
    name_column: str,
    excluded_channel_ids: set[int],
    excluded_user_ids: set[int],
    max_seconds: int,
    guild_id: int | None,
    include_long_sessions: bool,
) -> list[sqlite3.Row]:
    cols = columns(connection, table)
    ignored_filter = "AND ignored_at IS NULL" if "ignored_at" in cols else ""
    conditions = []
    params: list[object] = []
    if guild_id:
        conditions.append("guild_id = ?")
        params.append(guild_id)
    invalid_conditions = [
        "duration_seconds <= 0",
        "joined_at IS NULL",
        "left_at IS NULL",
        "left_at < joined_at",
    ]
    if include_long_sessions:
        invalid_conditions.append("duration_seconds > ?")
        params.append(max_seconds)
    if excluded_channel_ids:
        invalid_conditions.append(
            f"{channel_column} IN ({placeholders(excluded_channel_ids)})"
        )
        params.extend(sorted(excluded_channel_ids))
    if excluded_user_ids:
        invalid_conditions.append(
            f"{user_column} IN ({placeholders(excluded_user_ids)})"
        )
        params.extend(sorted(excluded_user_ids))
    conditions.append("(" + " OR ".join(invalid_conditions) + ")")
    where = " AND ".join(conditions)
    return connection.execute(
        f"""
        SELECT id, guild_id, {user_column} AS user_id, {name_column} AS username,
               {channel_column} AS channel_id, duration_seconds,
               joined_at, left_at
        FROM {table}
        WHERE {where} {ignored_filter}
        """,
        params,
    ).fetchall()


def reason_for_row(
    row: sqlite3.Row,
    *,
    excluded_channel_ids: set[int],
    excluded_user_ids: set[int],
    max_seconds: int,
    include_long_sessions: bool,
) -> str:
    if int(row["user_id"] or 0) in excluded_user_ids:
        return "excluded_user"
    if int(row["channel_id"] or 0) in excluded_channel_ids:
        return "excluded_channel"
    duration = int(row["duration_seconds"] or 0)
    if duration <= 0:
        return "invalid_duration"
    if not row["joined_at"] or not row["left_at"] or str(row["left_at"]) < str(row["joined_at"]):
        return "invalid_timestamps"
    if include_long_sessions and duration > max_seconds:
        return "duration_over_threshold"
    return "matched"


def duplicate_extra_ids(
    connection: sqlite3.Connection,
    table: str,
    *,
    user_column: str,
    channel_column: str,
    channel_name_column: str,
    guild_id: int | None,
) -> list[int]:
    cols = columns(connection, table)
    ignored_filter = "AND ignored_at IS NULL" if "ignored_at" in cols else ""
    guild_filter = "WHERE guild_id = ?" if guild_id else "WHERE 1 = 1"
    params: list[object] = [guild_id] if guild_id else []
    rows = connection.execute(
        f"""
        SELECT GROUP_CONCAT(id) AS ids, COUNT(*) AS count
        FROM {table}
        {guild_filter} {ignored_filter}
        GROUP BY {user_column}, {channel_column}, {channel_name_column}, joined_at, left_at
        HAVING COUNT(*) > 1
        """,
        params,
    ).fetchall()
    extras: list[int] = []
    for row in rows:
        ids = sorted(int(value) for value in str(row["ids"]).split(",") if value)
        extras.extend(ids[1:])
    return extras


def print_summary(
    title: str,
    rows: list[sqlite3.Row],
    duplicate_ids: list[int],
    *,
    excluded_channel_ids: set[int],
    excluded_user_ids: set[int],
    max_seconds: int,
    include_long_sessions: bool,
) -> Counter[str]:
    reasons: Counter[str] = Counter()
    channels: Counter[str] = Counter()
    users: Counter[str] = Counter()
    seconds = 0
    for row in rows:
        reason = reason_for_row(
            row,
            excluded_channel_ids=excluded_channel_ids,
            excluded_user_ids=excluded_user_ids,
            max_seconds=max_seconds,
            include_long_sessions=include_long_sessions,
        )
        reasons[reason] += 1
        seconds += int(row["duration_seconds"] or 0)
        channels[str(row["channel_id"] or "name-only/unknown")] += int(
            row["duration_seconds"] or 0
        )
        users[str(row["user_id"] or "unknown")] += int(row["duration_seconds"] or 0)
    reasons["duplicate_extra_rows"] += len(duplicate_ids)
    print(f"\n{title}")
    print(f"  sessions matched: {len(rows):,}")
    print(f"  duplicate extra rows found: {len(duplicate_ids):,}")
    print(f"  total hours matched: {seconds / 3600.0:,.1f}")
    print("  reasons:")
    for reason, count in reasons.most_common():
        print(f"    {reason}: {count:,}")
    print("  top affected channels:")
    for channel, value in channels.most_common(10):
        print(f"    {channel}: {value / 3600.0:,.1f}h")
    print("  top affected users:")
    for user, value in users.most_common(10):
        print(f"    {user}: {value / 3600.0:,.1f}h")
    return reasons


def mark_rows(
    connection: sqlite3.Connection,
    table: str,
    row_ids: Iterable[int],
    reason: str,
    timestamp: str,
) -> int:
    ids = sorted(set(row_ids))
    if not ids:
        return 0
    connection.execute(
        f"""
        UPDATE {table}
        SET ignored_at = ?,
            ignored_reason = COALESCE(ignored_reason, ?)
        WHERE id IN ({placeholders(ids)})
          AND ignored_at IS NULL
        """,
        [timestamp, reason, *ids],
    )
    return connection.total_changes


def main() -> int:
    args = parse_args()
    excluded_channel_ids = parse_csv_ids(args.excluded_channel_ids)
    excluded_user_ids = parse_csv_ids(args.excluded_user_ids)
    max_seconds = int(args.max_duration_hours * 3600)
    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    try:
        live_rows: list[sqlite3.Row] = []
        imported_rows: list[sqlite3.Row] = []
        live_dupes: list[int] = []
        imported_dupes: list[int] = []
        if table_exists(connection, "vc_sessions"):
            live_rows = select_sessions(
                connection,
                table="vc_sessions",
                channel_column="channel_id",
                user_column="user_id",
                name_column="username",
                excluded_channel_ids=excluded_channel_ids,
                excluded_user_ids=excluded_user_ids,
                max_seconds=max_seconds,
                guild_id=args.guild_id,
                include_long_sessions=args.include_long_sessions,
            )
            live_dupes = duplicate_extra_ids(
                connection,
                "vc_sessions",
                user_column="user_id",
                channel_column="channel_id",
                channel_name_column="channel_name",
                guild_id=args.guild_id,
            )
        if table_exists(connection, "vc_imported_sessions"):
            imported_rows = select_sessions(
                connection,
                table="vc_imported_sessions",
                channel_column="voice_channel_id",
                user_column="user_id",
                name_column="user_name",
                excluded_channel_ids=excluded_channel_ids,
                excluded_user_ids=excluded_user_ids,
                max_seconds=max_seconds,
                guild_id=args.guild_id,
                include_long_sessions=args.include_long_sessions,
            )
            imported_dupes = duplicate_extra_ids(
                connection,
                "vc_imported_sessions",
                user_column="user_id",
                channel_column="voice_channel_id",
                channel_name_column="voice_channel_name",
                guild_id=args.guild_id,
            )

        print(f"Mode: {'apply' if args.apply else 'dry run'}")
        print(f"Database: {args.database}")
        print(f"Excluded channel IDs: {', '.join(map(str, sorted(excluded_channel_ids))) or 'none'}")
        print(f"Excluded user IDs: {', '.join(map(str, sorted(excluded_user_ids))) or 'none'}")
        print(f"Long-session threshold: {args.max_duration_hours:g}h")
        print(f"Long sessions marked on apply: {'yes' if args.include_long_sessions else 'no'}")
        print_summary(
            "vc_sessions",
            live_rows,
            live_dupes,
            excluded_channel_ids=excluded_channel_ids,
            excluded_user_ids=excluded_user_ids,
            max_seconds=max_seconds,
            include_long_sessions=args.include_long_sessions,
        )
        print_summary(
            "vc_imported_sessions",
            imported_rows,
            imported_dupes,
            excluded_channel_ids=excluded_channel_ids,
            excluded_user_ids=excluded_user_ids,
            max_seconds=max_seconds,
            include_long_sessions=args.include_long_sessions,
        )
        if args.dry_run:
            print("\nNo database changes were made.")
            return 0

        for table in ("vc_sessions", "vc_imported_sessions"):
            if table_exists(connection, table):
                ensure_ignore_columns(connection, table)
        timestamp = datetime.now(timezone.utc).isoformat()
        before = connection.total_changes
        mark_rows(connection, "vc_sessions", (row["id"] for row in live_rows), "voice_cleanup", timestamp)
        mark_rows(connection, "vc_sessions", live_dupes, "duplicate_session", timestamp)
        mark_rows(connection, "vc_imported_sessions", (row["id"] for row in imported_rows), "voice_cleanup", timestamp)
        mark_rows(connection, "vc_imported_sessions", imported_dupes, "duplicate_session", timestamp)
        connection.commit()
        changed = connection.total_changes - before
        print(f"\nMarked {changed:,} rows ignored. No rows were deleted.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
