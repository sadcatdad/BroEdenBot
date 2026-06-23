"""Read-only aggregated analytics for the local dashboard."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from utils.settings import settings_database_path
from utils.sqlite import configure_sync_connection


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RANGES = {
    "7d": ("7 days", 7),
    "30d": ("30 days", 30),
    "90d": ("90 days", 90),
    "1y": ("1 year", 365),
    "all": ("All time", None),
}
HEATMAP_RANGES = {"30d", "90d", "1y", "all"}
LIMITS = {10, 25, 50, 100}
EXPORT_TYPES = {"overview", "activity", "channels", "members", "voice", "heatmap"}
DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def validate_range(range_key: str, *, heatmap: bool = False) -> str:
    key = str(range_key or "30d").strip().casefold()
    allowed = HEATMAP_RANGES if heatmap else set(RANGES)
    if key not in allowed:
        raise ValueError("Invalid analytics range.")
    return key


def validate_limit(limit: int | str) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid analytics limit.") from exc
    if value not in LIMITS:
        raise ValueError("Invalid analytics limit.")
    return value


def validate_export_type(export_type: str) -> str:
    value = str(export_type or "").strip().casefold()
    if value not in EXPORT_TYPES:
        raise ValueError("Invalid analytics export type.")
    return value


def _connect() -> sqlite3.Connection | None:
    path = settings_database_path()
    if not path.is_file():
        return None
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=5,
    )
    return configure_sync_connection(connection, readonly=True)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _activity_available(connection: sqlite3.Connection) -> bool:
    return {
        "guild_id",
        "channel_id",
        "user_id",
        "activity_date",
        "activity_hour",
        "message_count",
    }.issubset(_columns(connection, "stats_message_activity"))


def _guild_filter(column: str = "guild_id") -> tuple[str, list[Any]]:
    guild_id = os.getenv("GUILD_ID", "").strip()
    if guild_id.isdigit():
        return f"{column} = ?", [int(guild_id)]
    return "1 = 1", []


def _csv_ids(name: str) -> set[int]:
    values: set[int] = set()
    for item in os.getenv(name, "").replace("\n", ",").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            parsed = int(text)
        except ValueError:
            continue
        if parsed > 0:
            values.add(parsed)
    return values


def _excluded_voice_user_ids() -> set[int]:
    return _csv_ids("VC_EXCLUDED_USER_IDS")


def _excluded_voice_channel_ids() -> set[int]:
    return _csv_ids("EXCLUDED_VOICE_CHANNEL_IDS")


def _not_in_sql(column: str, values: set[int]) -> tuple[str, list[Any]]:
    if not values:
        return "", []
    placeholders = ", ".join("?" for _ in values)
    return (
        f" AND ({column} IS NULL OR {column} NOT IN ({placeholders}))",
        list(sorted(values)),
    )


def _normalized_voice_channel_name_sql(column: str) -> str:
    return (
        "NULLIF(TRIM(REPLACE(REPLACE("
        f"{column}, ' [voice]', ''), '[voice]', '')), '')"
    )


def _voice_channel_key_sql(column: str) -> str:
    return f"LOWER(COALESCE({_normalized_voice_channel_name_sql(column)}, 'unknown'))"


def _date_filter(
    range_key: str,
    *,
    column: str = "activity_date",
) -> tuple[str, list[Any]]:
    days = RANGES[range_key][1]
    if days is None:
        return "", []
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    return f" AND {column} >= ?", [cutoff]


def _where(
    range_key: str,
    *,
    guild_column: str = "guild_id",
    date_column: str = "activity_date",
) -> tuple[str, list[Any]]:
    guild_sql, parameters = _guild_filter(guild_column)
    date_sql, date_parameters = _date_filter(range_key, column=date_column)
    return f"{guild_sql}{date_sql}", [*parameters, *date_parameters]


def _channel_categories() -> dict[str, str]:
    path = PROJECT_ROOT / "data" / "channel_categories.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(channel_id): str(details.get("category", "")).strip()
        for channel_id, details in payload.items()
        if isinstance(details, dict) and details.get("category")
    }


def _empty_activity(range_key: str) -> dict[str, Any]:
    return {
        "available": False,
        "range_key": range_key,
        "range_label": RANGES[range_key][0],
        "daily": [],
        "weekly": [],
        "monthly": [],
        "all_time_total": 0,
        "selected_total": 0,
        "delta_percent": None,
        "max_daily": 0,
    }


def get_activity_series(range_key: str = "30d") -> dict[str, Any]:
    range_key = validate_range(range_key)
    result = _empty_activity(range_key)
    connection = _connect()
    if connection is None:
        return result
    try:
        if not _activity_available(connection):
            return result
        where, parameters = _where(range_key)
        daily = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT activity_date AS period, SUM(message_count) AS message_count
                FROM stats_message_activity
                WHERE {where}
                GROUP BY activity_date
                ORDER BY activity_date
                """,
                parameters,
            ).fetchall()
        ]
        if RANGES[range_key][1] is not None:
            by_date = {str(row["period"]): int(row["message_count"] or 0) for row in daily}
            days = int(RANGES[range_key][1] or 0)
            today = datetime.now(timezone.utc).date()
            daily = [
                {
                    "period": (today - timedelta(days=offset)).isoformat(),
                    "message_count": by_date.get(
                        (today - timedelta(days=offset)).isoformat(),
                        0,
                    ),
                }
                for offset in range(days - 1, -1, -1)
            ]
        weekly = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT strftime('%Y-W%W', activity_date) AS period,
                       SUM(message_count) AS message_count
                FROM stats_message_activity
                WHERE {where}
                GROUP BY period
                ORDER BY period
                """,
                parameters,
            ).fetchall()
        ]
        monthly = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT substr(activity_date, 1, 7) AS period,
                       SUM(message_count) AS message_count
                FROM stats_message_activity
                WHERE {where}
                GROUP BY period
                ORDER BY period
                """,
                parameters,
            ).fetchall()
        ]
        guild_sql, guild_parameters = _guild_filter()
        all_time = connection.execute(
            f"""
            SELECT COALESCE(SUM(message_count), 0)
            FROM stats_message_activity
            WHERE {guild_sql}
            """,
            guild_parameters,
        ).fetchone()[0]
        selected_total = sum(int(row["message_count"] or 0) for row in daily)
        delta = None
        days = RANGES[range_key][1]
        if days is not None:
            current_start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
            previous_start = current_start - timedelta(days=days)
            guild_sql, guild_params = _guild_filter()
            row = connection.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN activity_date >= ? THEN message_count END), 0),
                    COALESCE(SUM(CASE WHEN activity_date >= ? AND activity_date < ?
                                      THEN message_count END), 0)
                FROM stats_message_activity
                WHERE {guild_sql}
                """,
                (
                    current_start.isoformat(),
                    previous_start.isoformat(),
                    current_start.isoformat(),
                    *guild_params,
                ),
            ).fetchone()
            current, previous = int(row[0] or 0), int(row[1] or 0)
            if previous:
                delta = (current - previous) / previous * 100
            elif current:
                delta = 100.0
        result.update(
            available=bool(daily) and selected_total > 0,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
            all_time_total=int(all_time or 0),
            selected_total=selected_total,
            delta_percent=delta,
            max_daily=max((int(row["message_count"] or 0) for row in daily), default=0),
        )
        return result
    except (OSError, sqlite3.Error):
        return result
    finally:
        connection.close()


def get_channel_leaderboard(
    range_key: str = "30d",
    limit: int = 25,
) -> list[dict[str, Any]]:
    range_key = validate_range(range_key)
    limit = validate_limit(limit)
    connection = _connect()
    if connection is None:
        return []
    try:
        if not _activity_available(connection):
            return []
        where, parameters = _where(range_key)
        total = int(
            connection.execute(
                f"SELECT COALESCE(SUM(message_count), 0) "
                f"FROM stats_message_activity WHERE {where}",
                parameters,
            ).fetchone()[0]
            or 0
        )
        categories = _channel_categories()
        rows = connection.execute(
            f"""
            SELECT channel_id, MAX(channel_name) AS channel_name,
                   SUM(message_count) AS message_count,
                   COUNT(DISTINCT user_id) AS unique_users,
                   MIN(activity_date) AS first_seen,
                   MAX(activity_date) AS last_seen
            FROM stats_message_activity
            WHERE {where}
            GROUP BY channel_id
            ORDER BY message_count DESC, channel_id
            LIMIT ?
            """,
            (*parameters, limit),
        ).fetchall()
        return [
            {
                **dict(row),
                "percent": (int(row["message_count"] or 0) / total * 100) if total else 0,
                "category": categories.get(str(row["channel_id"]), ""),
            }
            for row in rows
        ]
    except (OSError, sqlite3.Error):
        return []
    finally:
        connection.close()


def get_member_leaderboard(
    range_key: str = "30d",
    limit: int = 25,
) -> list[dict[str, Any]]:
    range_key = validate_range(range_key)
    limit = validate_limit(limit)
    connection = _connect()
    if connection is None:
        return []
    try:
        if not _activity_available(connection):
            return []
        where, parameters = _where(range_key)
        members = connection.execute(
            f"""
            SELECT user_id, MAX(username) AS username,
                   MAX(display_name) AS display_name,
                   SUM(message_count) AS message_count,
                   COUNT(DISTINCT activity_date) AS active_days,
                   MIN(activity_date) AS first_seen,
                   MAX(activity_date) AS last_seen
            FROM stats_message_activity
            WHERE {where}
            GROUP BY user_id
            ORDER BY message_count DESC, user_id
            LIMIT ?
            """,
            (*parameters, limit),
        ).fetchall()
        top_channels: dict[int, tuple[int, str]] = {}
        for row in connection.execute(
            f"""
            SELECT user_id, channel_id, MAX(channel_name) AS channel_name,
                   SUM(message_count) AS message_count
            FROM stats_message_activity
            WHERE {where}
            GROUP BY user_id, channel_id
            ORDER BY user_id, message_count DESC, channel_id
            """,
            parameters,
        ):
            top_channels.setdefault(
                int(row["user_id"]),
                (int(row["channel_id"]), str(row["channel_name"] or "")),
            )
        return [
            {
                **dict(row),
                "top_channel_id": top_channels.get(int(row["user_id"]), (None, ""))[0],
                "top_channel_name": top_channels.get(int(row["user_id"]), (None, ""))[1],
            }
            for row in members
        ]
    except (OSError, sqlite3.Error):
        return []
    finally:
        connection.close()


def get_heatmap(range_key: str = "30d") -> dict[str, Any]:
    range_key = validate_range(range_key, heatmap=True)
    result = {
        "available": False,
        "range_key": range_key,
        "range_label": RANGES[range_key][0],
        "days": [],
        "max_count": 0,
        "busiest": None,
    }
    connection = _connect()
    if connection is None:
        return result
    try:
        if not _activity_available(connection):
            return result
        where, parameters = _where(range_key)
        rows = connection.execute(
            f"""
            SELECT CAST(strftime('%w', activity_hour) AS INTEGER) AS weekday,
                   CAST(strftime('%H', activity_hour) AS INTEGER) AS hour,
                   SUM(message_count) AS message_count
            FROM stats_message_activity
            WHERE {where}
            GROUP BY weekday, hour
            """,
            parameters,
        ).fetchall()
        counts = {
            (int(row["weekday"]), int(row["hour"])): int(row["message_count"] or 0)
            for row in rows
            if row["weekday"] is not None and row["hour"] is not None
        }
        maximum = max(counts.values(), default=0)
        days = [
            {
                "name": DAY_NAMES[weekday],
                "cells": [
                    {
                        "hour": hour,
                        "count": counts.get((weekday, hour), 0),
                        "level": (
                            min(5, max(1, int(counts[(weekday, hour)] / maximum * 5) + 1))
                            if maximum and counts.get((weekday, hour), 0)
                            else 0
                        ),
                    }
                    for hour in range(24)
                ],
            }
            for weekday in range(7)
        ]
        busiest = None
        if counts:
            (weekday, hour), count = max(counts.items(), key=lambda item: item[1])
            busiest = {
                "day": DAY_NAMES[weekday],
                "hour": hour,
                "count": count,
            }
        result.update(
            available=bool(rows),
            days=days,
            max_count=maximum,
            busiest=busiest,
        )
        return result
    except (OSError, sqlite3.Error):
        return result
    finally:
        connection.close()


def _voice_selects(connection: sqlite3.Connection) -> list[str]:
    selects = []
    live = _columns(connection, "vc_sessions")
    if {"guild_id", "user_id", "left_at", "duration_seconds"}.issubset(live):
        ignored_at = "ignored_at" if "ignored_at" in live else "NULL"
        ignored_reason = "ignored_reason" if "ignored_reason" in live else "NULL"
        normalized_channel_name = _normalized_voice_channel_name_sql("channel_name")
        selects.append(
            f"""
            SELECT guild_id, user_id, username, display_name, channel_id,
                   channel_name,
                   {normalized_channel_name} AS normalized_channel_name,
                   joined_at, left_at, duration_seconds,
                   {ignored_at} AS ignored_at,
                   {ignored_reason} AS ignored_reason,
                   'live' AS source
            FROM vc_sessions
            """
        )
    imported = _columns(connection, "vc_imported_sessions")
    if {"guild_id", "user_id", "left_at", "duration_seconds"}.issubset(imported):
        ignored_at = "ignored_at" if "ignored_at" in imported else "NULL"
        ignored_reason = "ignored_reason" if "ignored_reason" in imported else "NULL"
        normalized_channel_name = _normalized_voice_channel_name_sql("voice_channel_name")
        overlap_filter = ""
        if {
            "guild_id",
            "user_id",
            "channel_name",
            "joined_at",
            "left_at",
        }.issubset(live):
            live_ignored_sql = "AND live.ignored_at IS NULL" if "ignored_at" in live else ""
            overlap_filter = f"""
            WHERE NOT EXISTS (
                SELECT 1
                FROM vc_sessions AS live
                WHERE live.guild_id = vc_imported_sessions.guild_id
                  AND live.user_id = vc_imported_sessions.user_id
                  AND {_voice_channel_key_sql("live.channel_name")} =
                      {_voice_channel_key_sql("vc_imported_sessions.voice_channel_name")}
                  AND live.joined_at < vc_imported_sessions.left_at
                  AND live.left_at > vc_imported_sessions.joined_at
                  {live_ignored_sql}
            )
            """
        selects.append(
            f"""
            SELECT guild_id, user_id, user_name AS username, display_name,
                   voice_channel_id AS channel_id,
                   voice_channel_name AS channel_name,
                   {normalized_channel_name} AS normalized_channel_name,
                   joined_at, left_at, duration_seconds,
                   {ignored_at} AS ignored_at,
                   {ignored_reason} AS ignored_reason,
                   'imported' AS source
            FROM vc_imported_sessions
            {overlap_filter}
            """
        )
    return selects


def _voice_where(range_key: str) -> tuple[str, list[Any]]:
    where, parameters = _where(
        range_key,
        guild_column="guild_id",
        date_column="left_at",
    )
    user_sql, user_parameters = _not_in_sql(
        "user_id",
        _excluded_voice_user_ids(),
    )
    channel_sql, channel_parameters = _not_in_sql(
        "channel_id",
        _excluded_voice_channel_ids(),
    )
    return (
        f"{where} AND ignored_at IS NULL{user_sql}{channel_sql}",
        [*parameters, *user_parameters, *channel_parameters],
    )


def get_voice_overview(
    range_key: str = "30d",
    limit: int = 25,
) -> dict[str, Any]:
    range_key = validate_range(range_key)
    limit = validate_limit(limit)
    result = {
        "available": False,
        "tables_found": False,
        "range_key": range_key,
        "range_label": RANGES[range_key][0],
        "sessions": 0,
        "seconds": 0,
        "hours": 0.0,
        "unique_users": 0,
        "top_users": [],
        "top_channels": [],
        "daily": [],
        "weekly": [],
        "recent": [],
        "max_daily": 0,
    }
    connection = _connect()
    if connection is None:
        return result
    try:
        selects = _voice_selects(connection)
        if not selects:
            return result
        union = " UNION ALL ".join(selects)
        where, parameters = _voice_where(range_key)
        total = connection.execute(
            f"""
            SELECT COUNT(*) AS sessions,
                   COALESCE(SUM(duration_seconds), 0) AS seconds,
                   COUNT(DISTINCT user_id) AS unique_users
            FROM ({union}) AS sessions
            WHERE {where}
            """,
            parameters,
        ).fetchone()
        top_users = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT user_id, MAX(username) AS username,
                       MAX(display_name) AS display_name,
                       COUNT(*) AS sessions,
                       SUM(duration_seconds) AS seconds
                FROM ({union}) AS sessions
                WHERE {where}
                GROUP BY CASE
                    WHEN user_id IS NOT NULL THEN 'id:' || user_id
                    ELSE 'name:' || LOWER(COALESCE(display_name, username, 'unknown'))
                END
                ORDER BY seconds DESC, user_id
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()
        ]
        top_channels = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT MAX(channel_id) AS channel_id,
                       MAX(normalized_channel_name) AS channel_name,
                       COUNT(*) AS sessions,
                       SUM(duration_seconds) AS seconds
                FROM ({union}) AS sessions
                WHERE {where}
                GROUP BY LOWER(COALESCE(normalized_channel_name, 'unknown'))
                ORDER BY seconds DESC, channel_id, channel_name
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()
        ]
        daily = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT substr(left_at, 1, 10) AS period,
                       COUNT(*) AS sessions,
                       SUM(duration_seconds) AS seconds
                FROM ({union}) AS sessions
                WHERE {where}
                GROUP BY period
                ORDER BY period
                """,
                parameters,
            ).fetchall()
        ]
        weekly = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT strftime('%Y-W%W', left_at) AS period,
                       COUNT(*) AS sessions,
                       SUM(duration_seconds) AS seconds
                FROM ({union}) AS sessions
                WHERE {where}
                GROUP BY period
                ORDER BY period
                """,
                parameters,
            ).fetchall()
        ]
        recent = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT user_id, username, display_name, channel_id,
                       normalized_channel_name AS channel_name,
                       joined_at, left_at, duration_seconds, source
                FROM ({union}) AS sessions
                WHERE {where}
                ORDER BY left_at DESC
                LIMIT 20
                """,
                parameters,
            ).fetchall()
        ]
        seconds = int(total["seconds"] or 0)
        result.update(
            available=bool(int(total["sessions"] or 0)),
            tables_found=True,
            sessions=int(total["sessions"] or 0),
            seconds=seconds,
            hours=seconds / 3600,
            unique_users=int(total["unique_users"] or 0),
            top_users=top_users,
            top_channels=top_channels,
            daily=daily,
            weekly=weekly,
            recent=recent,
            max_daily=max((int(row["seconds"] or 0) for row in daily), default=0),
        )
        return result
    except (OSError, sqlite3.Error):
        return result
    finally:
        connection.close()


def get_data_freshness() -> dict[str, Any]:
    result = {
        "latest_activity": None,
        "latest_import": None,
        "tracking_started": None,
    }
    connection = _connect()
    if connection is None:
        return result
    try:
        if _activity_available(connection):
            guild_sql, guild_parameters = _guild_filter()
            row = connection.execute(
                f"""
                SELECT MAX(activity_hour)
                FROM stats_message_activity
                WHERE {guild_sql}
                """,
                guild_parameters,
            ).fetchone()
            result["latest_activity"] = row[0] if row else None
        if "imported_at" in _columns(connection, "stats_activity_imports"):
            guild_sql, guild_parameters = _guild_filter()
            row = connection.execute(
                f"""
                SELECT MAX(imported_at)
                FROM stats_activity_imports
                WHERE {guild_sql}
                """,
                guild_parameters,
            ).fetchone()
            result["latest_import"] = row[0] if row else None
        if {"key", "value"}.issubset(_columns(connection, "stats_activity_settings")):
            row = connection.execute(
                """
                SELECT value FROM stats_activity_settings
                WHERE key = 'activity_tracking_started_at'
                """
            ).fetchone()
            result["tracking_started"] = row[0] if row else None
    except (OSError, sqlite3.Error):
        pass
    finally:
        connection.close()
    return result


def get_analytics_overview(range_key: str = "30d") -> dict[str, Any]:
    range_key = validate_range(range_key)
    result = {
        "available": False,
        "range_key": range_key,
        "range_label": RANGES[range_key][0],
        "total_messages": 0,
        "unique_users": 0,
        "channels": 0,
        "first_date": None,
        "last_date": None,
        "messages_7d": 0,
        "messages_30d": 0,
        "selected_messages": 0,
        "top_channels": [],
        "top_members": [],
        "activity": _empty_activity(range_key),
        "heatmap": get_heatmap("30d" if range_key == "7d" else range_key),
        "voice": get_voice_overview(range_key, 10),
        "freshness": get_data_freshness(),
    }
    connection = _connect()
    if connection is None:
        return result
    try:
        if not _activity_available(connection):
            return result
        guild_sql, guild_parameters = _guild_filter()
        summary = connection.execute(
            f"""
            SELECT COALESCE(SUM(message_count), 0) AS total_messages,
                   COUNT(DISTINCT user_id) AS unique_users,
                   COUNT(DISTINCT channel_id) AS channels,
                   MIN(activity_date) AS first_date,
                   MAX(activity_date) AS last_date
            FROM stats_message_activity
            WHERE {guild_sql}
            """,
            guild_parameters,
        ).fetchone()
        today = datetime.now(timezone.utc).date()
        periods = connection.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN activity_date >= ? THEN message_count END), 0),
                COALESCE(SUM(CASE WHEN activity_date >= ? THEN message_count END), 0)
            FROM stats_message_activity
            WHERE {guild_sql}
            """,
            (
                (today - timedelta(days=6)).isoformat(),
                (today - timedelta(days=29)).isoformat(),
                *guild_parameters,
            ),
        ).fetchone()
        activity = get_activity_series(range_key)
        result.update(
            available=bool(int(summary["total_messages"] or 0)),
            total_messages=int(summary["total_messages"] or 0),
            unique_users=int(summary["unique_users"] or 0),
            channels=int(summary["channels"] or 0),
            first_date=summary["first_date"],
            last_date=summary["last_date"],
            messages_7d=int(periods[0] or 0),
            messages_30d=int(periods[1] or 0),
            selected_messages=activity["selected_total"],
            top_channels=get_channel_leaderboard(range_key, 10),
            top_members=get_member_leaderboard(range_key, 10),
            activity=activity,
        )
        return result
    except (OSError, sqlite3.Error):
        return result
    finally:
        connection.close()


def export_analytics_csv(
    range_key: str = "30d",
    export_type: str = "overview",
) -> tuple[str, bytes]:
    range_key = validate_range(range_key, heatmap=export_type == "heatmap")
    export_type = validate_export_type(export_type)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    if export_type == "overview":
        data = get_analytics_overview(range_key)
        writer.writerow(["metric", "value"])
        for key in (
            "range_label",
            "total_messages",
            "unique_users",
            "channels",
            "first_date",
            "last_date",
            "messages_7d",
            "messages_30d",
            "selected_messages",
        ):
            writer.writerow([key, data.get(key)])
    elif export_type == "activity":
        data = get_activity_series(range_key)
        writer.writerow(["granularity", "period", "message_count"])
        for granularity in ("daily", "weekly", "monthly"):
            for row in data[granularity]:
                writer.writerow([granularity, row["period"], row["message_count"]])
    elif export_type == "channels":
        writer.writerow(
            [
                "channel_id",
                "channel_name",
                "category",
                "message_count",
                "unique_users",
                "first_seen",
                "last_seen",
                "percent",
            ]
        )
        for row in get_channel_leaderboard(range_key, 100):
            writer.writerow([
                row["channel_id"],
                row["channel_name"],
                row["category"],
                row["message_count"],
                row["unique_users"],
                row["first_seen"],
                row["last_seen"],
                f"{row['percent']:.2f}",
            ])
    elif export_type == "members":
        writer.writerow(
            [
                "user_id",
                "username",
                "display_name",
                "message_count",
                "active_days",
                "first_seen",
                "last_seen",
                "top_channel_id",
                "top_channel_name",
            ]
        )
        for row in get_member_leaderboard(range_key, 100):
            writer.writerow([row.get(key) for key in (
                "user_id",
                "username",
                "display_name",
                "message_count",
                "active_days",
                "first_seen",
                "last_seen",
                "top_channel_id",
                "top_channel_name",
            )])
    elif export_type == "voice":
        data = get_voice_overview(range_key, 100)
        writer.writerow(["record_type", "id", "name", "sessions", "seconds"])
        writer.writerow(["summary", "", "all voice activity", data["sessions"], data["seconds"]])
        for row in data["top_users"]:
            writer.writerow([
                "user",
                row["user_id"],
                row["display_name"] or row["username"],
                row["sessions"],
                row["seconds"],
            ])
        for row in data["top_channels"]:
            writer.writerow([
                "channel",
                row["channel_id"],
                row["channel_name"],
                row["sessions"],
                row["seconds"],
            ])
    else:
        data = get_heatmap(range_key)
        writer.writerow(["day_of_week", "hour", "message_count"])
        for day in data["days"]:
            for cell in day["cells"]:
                writer.writerow([day["name"], cell["hour"], cell["count"]])
    return (
        f"broeden-analytics-{export_type}-{range_key}.csv",
        output.getvalue().encode("utf-8-sig"),
    )
