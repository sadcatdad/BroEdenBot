"""Database-backed streak administration for the local dashboard."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dashboard.db import find_database_path, readonly_connection, table_names, writable_connection
from utils.settings import get_int_setting, get_setting
from utils.streaks import STREAK_SCHEMA, compute_streaks, is_streak_milestone


MEMBER_LIMIT = 250
RECENT_LIMIT = 25


def streak_timezone() -> ZoneInfo:
    name = str(get_setting("STREAK_TIMEZONE", "America/Chicago") or "").strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/Chicago")


def configured_guild_id() -> str:
    return str(get_setting("GUILD_ID", "") or os.getenv("GUILD_ID", "")).strip()


def initialize_streak_dashboard_schema() -> None:
    path = find_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with writable_connection(path) as connection:
        connection.executescript(STREAK_SCHEMA)
        connection.commit()


def _parse_positive_id(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{label} must be a positive Discord ID.")
    return text


def _parse_date(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD.") from exc


def _effective_current(current: int, last_date: object, today: date) -> int:
    try:
        parsed = date.fromisoformat(str(last_date or ""))
    except ValueError:
        return 0
    return int(current) if parsed >= today - timedelta(days=1) else 0


def _member_names(connection, guild_id: str) -> dict[str, tuple[str, str]]:
    if "stats_message_activity" not in table_names(connection):
        return {}
    rows = connection.execute(
        """
        SELECT user_id, display_name, username
        FROM stats_message_activity
        WHERE CAST(guild_id AS TEXT) = ?
          AND id IN (
              SELECT MAX(id) FROM stats_message_activity
              WHERE CAST(guild_id AS TEXT) = ?
              GROUP BY user_id
          )
        """,
        (guild_id, guild_id),
    ).fetchall()
    return {
        str(row["user_id"]): (
            str(row["display_name"] or ""),
            str(row["username"] or ""),
        )
        for row in rows
    }


def streaks_overview(query: str = "", guild_id: str = "") -> dict[str, Any]:
    initialize_streak_dashboard_schema()
    selected_guild = str(guild_id or configured_guild_id()).strip()
    today = datetime.now(streak_timezone()).date()
    result: dict[str, Any] = {
        "guild_id": selected_guild,
        "today": today.isoformat(),
        "members": [],
        "summary": {
            "members": 0,
            "active": 0,
            "tracked_days": 0,
            "best_longest": 0,
        },
        "runtime": None,
        "restores": [],
        "adjustments": [],
        "query": str(query or "").strip(),
    }
    if not selected_guild:
        return result

    with readonly_connection(find_database_path()) as connection:
        names = _member_names(connection, selected_guild)
        rows = connection.execute(
            """
            SELECT m.guild_id, m.user_id, m.current_streak, m.longest_streak,
                   m.last_qualified_date, m.updated_at,
                   COUNT(d.activity_date) AS tracked_days
            FROM member_streaks m
            LEFT JOIN streak_days d
              ON d.guild_id = m.guild_id AND d.user_id = m.user_id
            WHERE m.guild_id = ?
            GROUP BY m.guild_id, m.user_id
            ORDER BY m.current_streak DESC, m.longest_streak DESC, m.user_id
            LIMIT ?
            """,
            (selected_guild, MEMBER_LIMIT),
        ).fetchall()
        search = result["query"].casefold()
        members = []
        for row in rows:
            user_id = str(row["user_id"])
            display_name, username = names.get(user_id, ("", ""))
            label = display_name or username or user_id
            if search and search not in " ".join(
                (user_id, display_name, username)
            ).casefold():
                continue
            member = dict(row)
            member["display_name"] = display_name
            member["username"] = username
            member["label"] = label
            member["effective_current"] = _effective_current(
                int(row["current_streak"]), row["last_qualified_date"], today
            )
            members.append(member)
        result["members"] = members

        aggregate = connection.execute(
            """
            SELECT COUNT(*) AS members,
                   COALESCE(MAX(longest_streak), 0) AS best_longest
            FROM member_streaks WHERE guild_id = ?
            """,
            (selected_guild,),
        ).fetchone()
        tracked = connection.execute(
            "SELECT COUNT(*) AS count FROM streak_days WHERE guild_id = ?",
            (selected_guild,),
        ).fetchone()
        result["summary"] = {
            "members": int(aggregate["members"]),
            "active": sum(
                1
                for row in connection.execute(
                    """
                    SELECT current_streak, last_qualified_date
                    FROM member_streaks WHERE guild_id = ?
                    """,
                    (selected_guild,),
                ).fetchall()
                if _effective_current(
                    int(row["current_streak"]), row["last_qualified_date"], today
                ) > 0
            ),
            "tracked_days": int(tracked["count"]),
            "best_longest": int(aggregate["best_longest"]),
        }
        runtime = connection.execute(
            "SELECT * FROM streak_runtime_state WHERE guild_id = ?",
            (selected_guild,),
        ).fetchone()
        result["runtime"] = dict(runtime) if runtime else None
        result["restores"] = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM streak_restore_requests
                WHERE guild_id = ? ORDER BY id DESC LIMIT ?
                """,
                (selected_guild, RECENT_LIMIT),
            ).fetchall()
        ]
        result["adjustments"] = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM streak_adjustments
                WHERE guild_id = ? ORDER BY id DESC LIMIT ?
                """,
                (selected_guild, RECENT_LIMIT),
            ).fetchall()
        ]
    return result


def queue_streak_restore(
    *,
    guild_id: object,
    start_date: object,
    end_date: object,
    requested_by: str,
) -> tuple[int, bool]:
    initialize_streak_dashboard_schema()
    guild = _parse_positive_id(guild_id, "Guild ID")
    start = _parse_date(start_date, "Start date")
    end = _parse_date(end_date, "End date")
    today = datetime.now(streak_timezone()).date()
    if start > end:
        raise ValueError("Start date must be on or before end date.")
    if end > today:
        raise ValueError("Restore ranges cannot include future dates.")
    max_days = max(1, get_int_setting("STREAK_RESTORE_MAX_DAYS", 14))
    if (end - start).days + 1 > max_days:
        raise ValueError(f"Restore ranges are limited to {max_days} days.")

    local_zone = streak_timezone()
    start_utc = datetime.combine(start, time.min, local_zone).astimezone(timezone.utc)
    end_utc = datetime.combine(
        end + timedelta(days=1), time.min, local_zone
    ).astimezone(timezone.utc)
    now = datetime.now(timezone.utc).isoformat()
    with writable_connection(find_database_path()) as connection:
        existing = connection.execute(
            """
            SELECT id FROM streak_restore_requests
            WHERE guild_id = ? AND status IN ('pending', 'processing')
              AND start_at_utc <= ? AND end_at_utc >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (guild, end_utc.isoformat(), start_utc.isoformat()),
        ).fetchone()
        if existing:
            return int(existing["id"]), False
        cursor = connection.execute(
            """
            INSERT INTO streak_restore_requests (
                guild_id, start_at_utc, end_at_utc, requested_by,
                request_source, status, created_at
            ) VALUES (?, ?, ?, ?, 'dashboard', 'pending', ?)
            """,
            (
                guild,
                start_utc.isoformat(),
                end_utc.isoformat(),
                str(requested_by or "dashboard")[:100],
                now,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid), True


def _recompute_member(connection, guild_id: str, user_id: str) -> tuple[int, int]:
    rows = connection.execute(
        """
        SELECT activity_date FROM streak_days
        WHERE guild_id = ? AND user_id = ? ORDER BY activity_date
        """,
        (guild_id, user_id),
    ).fetchall()
    days = [date.fromisoformat(str(row["activity_date"])) for row in rows]
    current, longest = compute_streaks(
        days, datetime.now(streak_timezone()).date()
    )
    last_date = max(days).isoformat() if days else None
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO member_streaks (
            guild_id, user_id, current_streak, longest_streak,
            last_qualified_date, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (guild_id, user_id) DO UPDATE SET
            current_streak = excluded.current_streak,
            longest_streak = excluded.longest_streak,
            last_qualified_date = excluded.last_qualified_date,
            updated_at = excluded.updated_at
        """,
        (guild_id, user_id, current, longest, last_date, now),
    )
    return current, longest


def adjust_streak_day(
    *,
    guild_id: object,
    user_id: object,
    activity_date: object,
    action: str,
    reason: str,
    changed_by: str,
) -> dict[str, int]:
    initialize_streak_dashboard_schema()
    guild = _parse_positive_id(guild_id, "Guild ID")
    user = _parse_positive_id(user_id, "User ID")
    day = _parse_date(activity_date, "Activity date")
    if day > datetime.now(streak_timezone()).date():
        raise ValueError("Activity dates cannot be in the future.")
    normalized_action = str(action or "").strip().casefold()
    if normalized_action not in {"add", "remove"}:
        raise ValueError("Adjustment action must be add or remove.")
    explanation = " ".join(str(reason or "").split())
    if len(explanation) < 3:
        raise ValueError("Provide a short reason for the audit trail.")
    now = datetime.now(timezone.utc).isoformat()

    with writable_connection(find_database_path()) as connection:
        existing = connection.execute(
            """
            SELECT message_id FROM streak_days
            WHERE guild_id = ? AND user_id = ? AND activity_date = ?
            """,
            (guild, user, day.isoformat()),
        ).fetchone()
        if normalized_action == "add" and existing:
            raise ValueError("That member already has a qualifying day on this date.")
        if normalized_action == "remove" and not existing:
            raise ValueError("No qualifying streak day exists for that member and date.")
        source_message_id: Optional[str] = (
            str(existing["message_id"]) if existing else None
        )
        cursor = connection.execute(
            """
            INSERT INTO streak_adjustments (
                guild_id, user_id, activity_date, action, reason,
                changed_by, source_message_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild,
                user,
                day.isoformat(),
                normalized_action,
                explanation[:500],
                str(changed_by or "dashboard")[:100],
                source_message_id,
                now,
            ),
        )
        adjustment_id = int(cursor.lastrowid)
        if normalized_action == "add":
            source_message_id = f"dashboard:{adjustment_id}"
            connection.execute(
                """
                INSERT INTO streak_days (
                    guild_id, user_id, activity_date, message_id,
                    channel_id, message_hash, created_at
                ) VALUES (?, ?, ?, ?, 'dashboard', ?, ?)
                """,
                (
                    guild,
                    user,
                    day.isoformat(),
                    source_message_id,
                    f"dashboard-adjustment:{adjustment_id}",
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE streak_adjustments SET source_message_id = ? WHERE id = ?
                """,
                (source_message_id, adjustment_id),
            )
        else:
            connection.execute(
                """
                DELETE FROM streak_milestones
                WHERE guild_id = ? AND source_message_id = ?
                """,
                (guild, source_message_id),
            )
            connection.execute(
                """
                DELETE FROM streak_days
                WHERE guild_id = ? AND user_id = ? AND activity_date = ?
                """,
                (guild, user, day.isoformat()),
            )
        current, longest = _recompute_member(connection, guild, user)
        if normalized_action == "add" and is_streak_milestone(current):
            connection.execute(
                """
                INSERT OR IGNORE INTO streak_milestones (
                    guild_id, user_id, milestone_days, source_message_id, earned_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (guild, user, current, source_message_id, now),
            )
        connection.commit()
    return {"current": current, "longest": longest, "adjustment_id": adjustment_id}
