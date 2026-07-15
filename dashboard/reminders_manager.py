"""Read models and queued mutations for the reminder dashboard."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from dashboard.db import find_database_path
from utils.reminder_service import initialize_schema_sync
from utils.sqlite import configure_sync_connection


VALID_TYPES = {"", "personal", "event"}
VALID_STATUSES = {"", "upcoming", "completed", "cancelled", "deleted", "failed"}
VALID_RECURRENCE = {"", "once", "recurring"}
VALID_ACTIONS = {"edit", "duplicate", "cancel", "retry", "archive"}


@contextmanager
def reminder_connection(*, writable: bool = False) -> Iterator[sqlite3.Connection]:
    path = find_database_path()
    if writable:
        connection = sqlite3.connect(path, timeout=30)
        configure_sync_connection(connection)
        initialize_schema_sync(connection)
    else:
        if not path.is_file():
            connection = sqlite3.connect(":memory:")
            configure_sync_connection(connection)
            initialize_schema_sync(connection)
        else:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
            configure_sync_connection(connection, readonly=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _filters(
    *,
    guild_id: str = "",
    reminder_type: str = "",
    status: str = "",
    creator: str = "",
    channel: str = "",
    recurrence: str = "",
    date_from: str = "",
    date_to: str = "",
) -> tuple[str, list[Any]]:
    if reminder_type not in VALID_TYPES:
        raise ValueError("Invalid reminder type filter.")
    if status not in VALID_STATUSES:
        raise ValueError("Invalid reminder status filter.")
    if recurrence not in VALID_RECURRENCE:
        raise ValueError("Invalid recurrence filter.")
    clauses: list[str] = []
    parameters: list[Any] = []
    for column, value in (
        ("r.guild_id", guild_id),
        ("r.reminder_type", reminder_type),
        ("r.status", status),
        ("r.creator_user_id", creator),
        ("r.destination_channel_id", channel),
    ):
        if value:
            clauses.append(f"{column} = ?")
            parameters.append(value)
    if recurrence == "once":
        clauses.append("r.recurrence_type = 'none'")
    elif recurrence == "recurring":
        clauses.append("r.recurrence_type != 'none'")
    if date_from:
        clauses.append("r.scheduled_at_utc >= ?")
        parameters.append(date_from)
    if date_to:
        clauses.append("r.scheduled_at_utc <= ?")
        parameters.append(date_to)
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), parameters


def reminder_overview(*, guild_id: str = "", **filters: str) -> dict[str, Any]:
    where, parameters = _filters(guild_id=guild_id, **filters)
    with reminder_connection() as connection:
        counts = {
            str(row["status"]): int(row["total"])
            for row in connection.execute(
                f"SELECT r.status, COUNT(*) AS total FROM reminder_items r {where} GROUP BY r.status",
                parameters,
            ).fetchall()
        }
        subscriber_clause = "AND r.guild_id = ?" if guild_id else ""
        subscriber_params = (guild_id,) if guild_id else ()
        active_subscriptions = int(connection.execute(
            f"""
            SELECT COUNT(*) FROM reminder_subscriptions s
            JOIN reminder_items r ON r.id = s.reminder_id
            WHERE s.status = 'active' {subscriber_clause}
            """,
            subscriber_params,
        ).fetchone()[0])
        failed_deliveries = int(connection.execute(
            f"""
            SELECT COUNT(*) FROM reminder_deliveries d
            JOIN reminder_occurrences o ON o.id = d.occurrence_id
            JOIN reminder_items r ON r.id = o.reminder_id
            WHERE d.status IN ('failed', 'permanent_failure') {subscriber_clause}
            """,
            subscriber_params,
        ).fetchone()[0])
    return {
        "upcoming": counts.get("upcoming", 0),
        "completed": counts.get("completed", 0),
        "cancelled": counts.get("cancelled", 0),
        "failed": counts.get("failed", 0),
        "active_subscriptions": active_subscriptions,
        "failed_deliveries": failed_deliveries,
    }


def list_reminders(*, limit: int = 100, **filters: str) -> list[dict[str, Any]]:
    where, parameters = _filters(**filters)
    parameters.append(max(1, min(500, int(limit))))
    with reminder_connection() as connection:
        return [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT r.*,
                       (SELECT COUNT(*) FROM reminder_subscriptions s
                        WHERE s.reminder_id = r.id AND s.status = 'active') AS subscriber_count,
                       (SELECT COUNT(*) FROM reminder_deliveries d
                        JOIN reminder_occurrences o ON o.id = d.occurrence_id
                        WHERE o.reminder_id = r.id AND d.status = 'sent') AS sent_deliveries,
                       (SELECT COUNT(*) FROM reminder_deliveries d
                        JOIN reminder_occurrences o ON o.id = d.occurrence_id
                        WHERE o.reminder_id = r.id
                          AND d.status IN ('failed', 'permanent_failure')) AS failed_deliveries
                FROM reminder_items r {where}
                ORDER BY CASE r.status WHEN 'upcoming' THEN 0 ELSE 1 END,
                         r.scheduled_at_utc ASC LIMIT ?
                """,
                parameters,
            ).fetchall()
        ]


def reminder_detail(reminder_id: int, *, guild_id: str = "") -> Optional[dict[str, Any]]:
    with reminder_connection() as connection:
        parameters: list[Any] = [reminder_id]
        guild_sql = "AND guild_id = ?" if guild_id else ""
        if guild_id:
            parameters.append(guild_id)
        reminder = connection.execute(
            f"SELECT * FROM reminder_items WHERE id = ? {guild_sql}",
            parameters,
        ).fetchone()
        if reminder is None:
            return None
        return {
            "reminder": dict(reminder),
            "occurrences": [dict(row) for row in connection.execute(
                "SELECT * FROM reminder_occurrences WHERE reminder_id = ? ORDER BY occurrence_index",
                (reminder_id,),
            ).fetchall()],
            "subscriptions": [dict(row) for row in connection.execute(
                "SELECT * FROM reminder_subscriptions WHERE reminder_id = ? ORDER BY created_at_utc DESC",
                (reminder_id,),
            ).fetchall()],
            "deliveries": [dict(row) for row in connection.execute(
                """
                SELECT d.* FROM reminder_deliveries d
                JOIN reminder_occurrences o ON o.id = d.occurrence_id
                WHERE o.reminder_id = ? ORDER BY d.due_at_utc DESC, d.id DESC LIMIT 200
                """,
                (reminder_id,),
            ).fetchall()],
            "audit": [dict(row) for row in connection.execute(
                "SELECT * FROM reminder_audit WHERE reminder_id = ? ORDER BY created_at_utc DESC, id DESC LIMIT 100",
                (reminder_id,),
            ).fetchall()],
        }


def queue_reminder_action(
    reminder_id: int,
    *,
    action: str,
    requested_by: str,
    guild_id: str = "",
    payload: Optional[dict[str, Any]] = None,
) -> int:
    if action not in VALID_ACTIONS:
        raise ValueError("Unsupported reminder action.")
    with reminder_connection(writable=True) as connection:
        parameters: list[Any] = [reminder_id]
        guild_sql = "AND guild_id = ?" if guild_id else ""
        if guild_id:
            parameters.append(guild_id)
        reminder = connection.execute(
            f"SELECT id, guild_id, status FROM reminder_items WHERE id = ? {guild_sql}",
            parameters,
        ).fetchone()
        if reminder is None:
            raise ValueError("Reminder was not found in the selected guild.")
        if action in {"edit", "cancel"} and reminder["status"] != "upcoming":
            raise ValueError("Only upcoming reminders can be edited or cancelled.")
        if action == "archive" and reminder["status"] == "upcoming":
            raise ValueError("Cancel an upcoming reminder before archiving it.")
        safe_payload = json.dumps(payload or {}, separators=(",", ":"), default=str)
        cursor = connection.execute(
            """
            INSERT INTO reminder_dashboard_actions (
                reminder_id, guild_id, action, payload_json, requested_by,
                status, requested_at_utc
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                reminder_id,
                reminder["guild_id"],
                action,
                safe_payload[:4000],
                requested_by[:200],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        action_id = int(cursor.lastrowid)
        connection.commit()
        return action_id
