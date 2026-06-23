"""Shared storage helpers for the dashboard stats graphics manager."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from utils.settings import settings_database_path
from utils.sqlite import configure_sync_connection


STAT_TABLES = {
    "roster": "role_stat_embeds",
    "report": "tracked_stats_reports",
    "activity": "tracked_activity_reports",
}
EDITABLE_SOURCES = {"roster", "report"}
MAX_TITLE_LENGTH = 100
MAX_BODY_LENGTH = 500


def parse_stat_id(stat_id: str) -> tuple[str, int]:
    try:
        source, raw_id = str(stat_id).split("-", 1)
        record_id = int(raw_id)
    except (TypeError, ValueError):
        raise ValueError("Invalid stat ID.")
    if source not in STAT_TABLES or record_id <= 0:
        raise ValueError("Invalid stat ID.")
    return source, record_id


def _connect() -> sqlite3.Connection:
    path = settings_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    return configure_sync_connection(connection)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def initialize_stats_manager_schema() -> None:
    with _connect() as connection:
        for table in STAT_TABLES.values():
            columns = _columns(connection, table)
            if not columns:
                continue
            if "status" not in columns:
                connection.execute(
                    f"""
                    ALTER TABLE "{table}"
                    ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
                    """
                )
            if "last_error" not in columns:
                connection.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN last_error TEXT'
                )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT,
                result_message TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_stat_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stat_id TEXT NOT NULL,
                discord_user_id INTEGER NOT NULL,
                username TEXT,
                display_name TEXT,
                role_id INTEGER,
                joined_at TEXT,
                category TEXT,
                captured_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dashboard_stat_members_stat
            ON dashboard_stat_members (stat_id, display_name, username)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dashboard_actions_pending
            ON dashboard_actions (status, action_type, id)
            """
        )
        connection.commit()


def _safe_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def list_stats() -> list[dict[str, Any]]:
    initialize_stats_manager_schema()
    records: list[dict[str, Any]] = []
    with _connect() as connection:
        if _table_exists(connection, "role_stat_embeds"):
            for row in connection.execute(
                """
                SELECT *
                FROM role_stat_embeds
                ORDER BY updated_at DESC, id DESC
                """
            ):
                records.append(_record_from_row("roster", row))
        if _table_exists(connection, "tracked_stats_reports"):
            for row in connection.execute(
                """
                SELECT *
                FROM tracked_stats_reports
                ORDER BY updated_at DESC, id DESC
                """
            ):
                records.append(_record_from_row("report", row))
        if _table_exists(connection, "tracked_activity_reports"):
            for row in connection.execute(
                """
                SELECT *
                FROM tracked_activity_reports
                ORDER BY updated_at DESC, id DESC
                """
            ):
                records.append(_record_from_row("activity", row))
        member_counts = {
            str(row["stat_id"]): int(row["member_count"])
            for row in connection.execute(
                """
                SELECT stat_id, COUNT(*) AS member_count
                FROM dashboard_stat_members
                GROUP BY stat_id
                """
            ).fetchall()
        }
    for record in records:
        record["member_count"] = member_counts.get(record["stat_id"], 0)
    records.sort(key=lambda item: str(item["updated_at"] or ""), reverse=True)
    return records


def _record_from_row(source: str, row: sqlite3.Row) -> dict[str, Any]:
    record_id = int(row["id"])
    title = _safe_value(row, "title")
    report_type = _safe_value(row, "report_type")
    if not title:
        title = (
            f"{report_type} activity report"
            if source == "activity"
            else f"{source.title()} stat {record_id}"
        )
    role_ids = [
        _safe_value(row, key)
        for key in (
            "role_id",
            "role_1_id",
            "role_2_id",
            "has_role_id",
            "missing_role_id",
        )
        if _safe_value(row, key)
    ]
    status = _safe_value(row, "status", "active") or "active"
    last_error = _safe_value(row, "last_error")
    return {
        "stat_id": f"{source}-{record_id}",
        "source": source,
        "id": record_id,
        "guild_id": _safe_value(row, "guild_id"),
        "channel_id": _safe_value(row, "channel_id"),
        "message_id": _safe_value(row, "message_id"),
        "role_id": role_ids[0] if role_ids else None,
        "role_ids": role_ids,
        "title": str(title),
        "body": _safe_value(row, "body", "") or "",
        "image_url": _safe_value(row, "image_url"),
        "report_type": report_type,
        "config_json": _safe_value(row, "config_json"),
        "created_at": _safe_value(row, "created_at"),
        "updated_at": _safe_value(row, "updated_at"),
        "status": status,
        "last_error": last_error,
        "display_status": "error" if last_error and status == "active" else status,
        "editable": source in EDITABLE_SOURCES,
    }


def get_stat(stat_id: str) -> Optional[dict[str, Any]]:
    source, record_id = parse_stat_id(stat_id)
    initialize_stats_manager_schema()
    table = STAT_TABLES[source]
    with _connect() as connection:
        if not _table_exists(connection, table):
            return None
        row = connection.execute(
            f'SELECT * FROM "{table}" WHERE id = ?',
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        record = _record_from_row(source, row)
        count = connection.execute(
            "SELECT COUNT(*) FROM dashboard_stat_members WHERE stat_id = ?",
            (stat_id,),
        ).fetchone()[0]
        record["member_count"] = int(count)
        record["members"] = [
            dict(member)
            for member in connection.execute(
                """
                SELECT discord_user_id, username, display_name, role_id,
                       joined_at, category, captured_at
                FROM dashboard_stat_members
                WHERE stat_id = ?
                ORDER BY COALESCE(display_name, username), discord_user_id
                LIMIT 100
                """,
                (stat_id,),
            ).fetchall()
        ]
        return record


def update_stat(
    stat_id: str,
    *,
    title: str,
    body: str,
) -> None:
    source, record_id = parse_stat_id(stat_id)
    if source not in EDITABLE_SOURCES:
        raise ValueError("This stat type does not support dashboard editing.")
    clean_title = str(title or "").strip()
    clean_body = str(body or "").strip()
    if not clean_title:
        raise ValueError("Title is required.")
    if len(clean_title) > MAX_TITLE_LENGTH:
        raise ValueError(f"Title must be {MAX_TITLE_LENGTH} characters or fewer.")
    if len(clean_body) > MAX_BODY_LENGTH:
        raise ValueError(f"Body must be {MAX_BODY_LENGTH} characters or fewer.")
    initialize_stats_manager_schema()
    table = STAT_TABLES[source]
    with _connect() as connection:
        columns = _columns(connection, table)
        if not columns:
            raise ValueError("Stats table is not available.")
        assignments = ["title = ?", "body = ?"]
        values: list[Any] = [clean_title, clean_body]
        values.append(record_id)
        cursor = connection.execute(
            f'UPDATE "{table}" SET {", ".join(assignments)} WHERE id = ?',
            values,
        )
        if cursor.rowcount != 1:
            raise ValueError("Stat was not found.")
        connection.commit()


def archive_stat(stat_id: str) -> None:
    source, record_id = parse_stat_id(stat_id)
    initialize_stats_manager_schema()
    table = STAT_TABLES[source]
    with _connect() as connection:
        if not _table_exists(connection, table):
            raise ValueError("Stat was not found.")
        cursor = connection.execute(
            f'UPDATE "{table}" SET status = ? WHERE id = ?',
            ("archived", record_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Stat was not found.")
        connection.commit()


def queue_stat_refresh(stat_id: str, requested_by: str) -> int:
    record = get_stat(stat_id)
    if record is None:
        raise ValueError("Stat was not found.")
    if record["status"] != "active":
        raise ValueError("Archived stats cannot be refreshed.")
    with _connect() as connection:
        existing = connection.execute(
            """
            SELECT id
            FROM dashboard_actions
            WHERE action_type = 'refresh_stat'
              AND status IN ('pending', 'processing')
              AND payload_json = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (json.dumps({"stat_id": stat_id}, separators=(",", ":")),),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cursor = connection.execute(
            """
            INSERT INTO dashboard_actions (
                action_type, payload_json, status, requested_by
            ) VALUES ('refresh_stat', ?, 'pending', ?)
            """,
            (
                json.dumps({"stat_id": stat_id}, separators=(",", ":")),
                requested_by,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def export_stat_csv(stat_id: str) -> Optional[bytes]:
    record = get_stat(stat_id)
    if record is None:
        raise ValueError("Stat was not found.")
    if not record["members"]:
        return None
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "discord_user_id",
            "username",
            "display_name",
            "role_id",
            "joined_at",
            "category",
            "captured_at",
            "exported_at",
        ]
    )
    exported_at = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT discord_user_id, username, display_name, role_id,
                   joined_at, category, captured_at
            FROM dashboard_stat_members
            WHERE stat_id = ?
            ORDER BY COALESCE(display_name, username), discord_user_id
            """,
            (stat_id,),
        ).fetchall()
    for row in rows:
        writer.writerow([*row, exported_at])
    return output.getvalue().encode("utf-8-sig")


def pending_dashboard_actions(limit: int = 10) -> list[dict[str, Any]]:
    initialize_stats_manager_schema()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, action_type, payload_json, requested_by, created_at
            FROM dashboard_actions
            WHERE action_type IN ('refresh_stat', 'reindex_knowledge')
              AND status = 'pending'
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def pending_refresh_actions(limit: int = 10) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that only process stats refreshes."""
    initialize_stats_manager_schema()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, action_type, payload_json, requested_by, created_at
            FROM dashboard_actions
            WHERE action_type = 'refresh_stat' AND status = 'pending'
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_action_processing(action_id: int) -> bool:
    initialize_stats_manager_schema()
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE dashboard_actions
            SET status = 'processing'
            WHERE id = ? AND status = 'pending'
            """,
            (action_id,),
        )
        connection.commit()
        return cursor.rowcount == 1


def complete_action(action_id: int, success: bool, message: str) -> None:
    initialize_stats_manager_schema()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE dashboard_actions
            SET status = ?, processed_at = ?, result_message = ?
            WHERE id = ?
            """,
            (
                "completed" if success else "failed",
                datetime.now(timezone.utc).isoformat(),
                str(message)[:1_000],
                action_id,
            ),
        )
        connection.commit()


def replace_member_snapshot(stat_id: str, members: list[dict[str, Any]]) -> None:
    initialize_stats_manager_schema()
    captured_at = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM dashboard_stat_members WHERE stat_id = ?",
            (stat_id,),
        )
        connection.executemany(
            """
            INSERT INTO dashboard_stat_members (
                stat_id, discord_user_id, username, display_name, role_id,
                joined_at, category, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    stat_id,
                    member["discord_user_id"],
                    member.get("username"),
                    member.get("display_name"),
                    member.get("role_id"),
                    member.get("joined_at"),
                    member.get("category"),
                    captured_at,
                )
                for member in members
            ],
        )
        connection.commit()


def update_stat_result(stat_id: str, success: bool, message: str) -> None:
    source, record_id = parse_stat_id(stat_id)
    initialize_stats_manager_schema()
    table = STAT_TABLES[source]
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute(
            f"""
            UPDATE "{table}"
            SET last_error = ?, updated_at = CASE WHEN ? THEN ? ELSE updated_at END
            WHERE id = ?
            """,
            (None if success else str(message)[:1_000], int(success), now, record_id),
        )
        connection.commit()
