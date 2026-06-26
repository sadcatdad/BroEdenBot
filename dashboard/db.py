from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from utils.settings import (
    get_bool_setting,
    get_csv_ids_setting,
    get_int_setting,
    get_setting,
    settings_database_path,
)
from utils.sqlite import configure_sync_connection

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BANK_DATABASE_CANDIDATES = (Path("brobank.db"),)


def _resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def find_database_path() -> Path:
    return settings_database_path()


def find_bank_database_path() -> Path:
    configured = os.getenv("BANK_DATABASE_PATH", "").strip()
    if configured:
        return _resolve_path(configured)
    for candidate in BANK_DATABASE_CANDIDATES:
        path = _resolve_path(candidate)
        if path.is_file():
            return path
    return _resolve_path(BANK_DATABASE_CANDIDATES[0])


def database_status(path: Path) -> dict[str, Any]:
    status: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "readable": False,
        "size_bytes": None,
        "error": None,
    }
    if not status["exists"]:
        return status
    try:
        status["size_bytes"] = path.stat().st_size
        with readonly_connection(path) as connection:
            connection.execute("SELECT 1").fetchone()
        status["readable"] = True
    except (OSError, sqlite3.Error) as exc:
        status["error"] = str(exc)
    return status


def readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=5,
    )
    return configure_sync_connection(connection, readonly=True)


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if table not in table_names(connection):
        return set()
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(row["name"]) for row in rows}


def bank_overview(limit: int = 20) -> dict[str, Any]:
    path = find_bank_database_path()
    result: dict[str, Any] = {
        "database": database_status(path),
        "tables_found": False,
        "totals": None,
        "transactions": [],
        "donors": [],
        "error": None,
    }
    if not path.is_file():
        return result

    try:
        with readonly_connection(path) as connection:
            tables = table_names(connection)
            if "bank_transactions" not in tables:
                return result
            columns = table_columns(connection, "bank_transactions")
            required = {"type", "amount"}
            if not required.issubset(columns):
                return result
            result["tables_found"] = True
            totals = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN type = 'contribution' THEN amount END), 0)
                        AS contributions,
                    COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0)
                        AS expenses,
                    COALESCE(SUM(
                        CASE
                            WHEN type = 'contribution' THEN amount
                            WHEN type = 'expense' THEN -amount
                            WHEN type = 'adjustment' THEN amount
                            ELSE 0
                        END
                    ), 0) AS balance
                FROM bank_transactions
                """
            ).fetchone()
            result["totals"] = dict(totals)
            transaction_columns = [
                column
                for column in (
                    "type",
                    "display_name",
                    "amount",
                    "note",
                    "is_public",
                    "created_at",
                )
                if column in columns
            ]
            order_columns = [
                column for column in ("created_at", "id") if column in columns
            ]
            order_sql = ", ".join(f"{column} DESC" for column in order_columns)
            if not order_sql:
                order_sql = "rowid DESC"
            result["transactions"] = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT {", ".join(transaction_columns)}
                    FROM bank_transactions
                    ORDER BY {order_sql}
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
            donor_columns = {"display_name", "discord_user_id", "is_public"}
            if donor_columns.issubset(columns):
                result["donors"] = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT
                            COALESCE(NULLIF(display_name, ''), 'Anonymous') AS donor,
                            SUM(amount) AS total
                        FROM bank_transactions
                        WHERE type = 'contribution' AND is_public = 1
                        GROUP BY discord_user_id, donor
                        ORDER BY total DESC
                        LIMIT 10
                        """
                    ).fetchall()
                ]
    except (OSError, sqlite3.Error) as exc:
        result["error"] = str(exc)
    return result


def import_history(limit: int = 50) -> dict[str, Any]:
    path = find_database_path()
    result: dict[str, Any] = {
        "database": database_status(path),
        "tables_found": False,
        "imports": [],
        "error": None,
    }
    if not path.is_file():
        return result

    try:
        with readonly_connection(path) as connection:
            table = "stats_activity_imports"
            columns = table_columns(connection, table)
            if not columns:
                return result
            result["tables_found"] = True
            wanted = (
                "filename",
                "source_file",
                "source_format",
                "status",
                "messages_seen",
                "messages_imported",
                "messages_skipped",
                "duplicates_skipped",
                "imported_at",
                "channel_name",
            )
            selected = [column for column in wanted if column in columns]
            if not selected:
                return result
            order_column = "imported_at" if "imported_at" in columns else "id"
            query = (
                f"SELECT {', '.join(selected)} FROM {table} "
                f"ORDER BY {order_column} DESC LIMIT ?"
            )
            result["imports"] = [
                dict(row) for row in connection.execute(query, (limit,)).fetchall()
            ]
    except (OSError, sqlite3.Error) as exc:
        result["error"] = str(exc)
    return result


def vcxp_overview(limit: int = 5) -> dict[str, Any]:
    path = find_database_path()
    trigger_role_ids = get_csv_ids_setting("VCXP_TRIGGER_ROLE_ID")
    trigger_role_id = trigger_role_ids[0] if trigger_role_ids else 0
    enabled = get_bool_setting("VCXP_ENABLED", False)
    minutes_per_pulse = max(1, get_int_setting("VCXP_MINUTES_PER_PULSE", 30))
    remove_delay = max(0, get_int_setting("VCXP_ROLE_REMOVE_DELAY_SECONDS", 30))
    daily_cap = max(0, get_int_setting("VCXP_DAILY_PULSE_CAP", 4))
    weekly_cap = max(0, get_int_setting("VCXP_WEEKLY_PULSE_CAP", 20))
    xp_excluded_role_ids = get_csv_ids_setting("VCXP_EXCLUDED_ROLE_IDS")
    reward_start_at = get_setting("VCXP_REWARD_START_AT", "") or ""
    result: dict[str, Any] = {
        "database": database_status(path),
        "enabled": enabled,
        "trigger_role_id": str(trigger_role_id) if trigger_role_id else "",
        "trigger_role_name": "",
        "trigger_role_managed": False,
        "role_snapshot_available": False,
        "role_snapshot_found": None,
        "minutes_per_pulse": minutes_per_pulse,
        "remove_delay_seconds": remove_delay,
        "daily_cap": daily_cap,
        "weekly_cap": weekly_cap,
        "xp_excluded_role_count": len(xp_excluded_role_ids),
        "reward_start_at": reward_start_at,
        "state_table_found": False,
        "pulses_table_found": False,
        "unpaid_users": 0,
        "unpaid_pulses": 0,
        "active_pulses": 0,
        "paid_24h": 0,
        "recent_statuses": [],
        "issues": [],
        "status": "Not configured",
        "status_class": "warning-text",
        "error": None,
    }
    if not path.is_file():
        result["issues"].append("Shared database was not found.")
        result["status"] = "Database missing"
        return result

    try:
        with readonly_connection(path) as connection:
            tables = table_names(connection)
            if "dashboard_discord_roles" in tables:
                result["role_snapshot_available"] = True
                if trigger_role_id:
                    role = connection.execute(
                        """
                        SELECT name, managed
                        FROM dashboard_discord_roles
                        WHERE id = ?
                        """,
                        (str(trigger_role_id),),
                    ).fetchone()
                    result["role_snapshot_found"] = role is not None
                    if role is not None:
                        result["trigger_role_name"] = str(role["name"] or "")
                        result["trigger_role_managed"] = bool(role["managed"])

            if "vc_xp_user_state" in tables:
                result["state_table_found"] = True
                if "vc_sessions" in tables and reward_start_at:
                    row = connection.execute(
                        """
                        WITH earned AS (
                            SELECT
                                guild_id,
                                user_id,
                                CAST(
                                    COALESCE(SUM(counted_seconds), 0) / ? AS INTEGER
                                ) AS pulses_earned
                            FROM vc_sessions
                            WHERE reward_eligible = 1
                              AND left_at >= ?
                            GROUP BY user_id
                        ),
                        unpaid AS (
                            SELECT
                                earned.user_id,
                                MAX(
                                    0,
                                    earned.pulses_earned
                                        - COALESCE(state.pulses_paid, 0)
                                ) AS unpaid_pulses
                            FROM earned
                            LEFT JOIN vc_xp_user_state AS state
                              ON state.guild_id = earned.guild_id
                             AND state.user_id = earned.user_id
                        )
                        SELECT
                            COUNT(*) AS users,
                            COALESCE(SUM(unpaid_pulses), 0) AS pulses
                        FROM unpaid
                        WHERE unpaid_pulses > 0
                        """,
                        (minutes_per_pulse * 60, reward_start_at),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT
                            COUNT(*) AS users,
                            COALESCE(SUM(pulses_earned - pulses_paid), 0) AS pulses
                        FROM vc_xp_user_state
                        WHERE pulses_earned > pulses_paid
                        """
                    ).fetchone()
                result["unpaid_users"] = int(row["users"] or 0)
                result["unpaid_pulses"] = int(row["pulses"] or 0)

            if "vc_xp_pulses" in tables:
                result["pulses_table_found"] = True
                paid_statuses = (
                    "paid",
                    "remove_failed_assumed_paid",
                    "stale_assumed_paid",
                    "marked_paid",
                )
                active_statuses = ("pending", "granted")
                day_start = (
                    datetime.now(timezone.utc) - timedelta(days=1)
                ).isoformat()
                result["active_pulses"] = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM vc_xp_pulses
                        WHERE status IN (?, ?)
                        """,
                        active_statuses,
                    ).fetchone()[0]
                    or 0
                )
                result["paid_24h"] = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM vc_xp_pulses
                        WHERE granted_at >= ?
                          AND status IN (?, ?, ?, ?)
                        """,
                        (day_start, *paid_statuses),
                    ).fetchone()[0]
                    or 0
                )
                result["recent_statuses"] = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT status, error, granted_at
                        FROM vc_xp_pulses
                        ORDER BY granted_at DESC, id DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                ]
    except (OSError, sqlite3.Error) as exc:
        result["error"] = str(exc)
        result["issues"].append("VCXP database details could not be read.")

    if not trigger_role_id:
        result["issues"].append("Set VCXP_TRIGGER_ROLE_ID to the pulse role ID.")
    elif result["role_snapshot_available"] and result["role_snapshot_found"] is False:
        result["issues"].append(
            "The latest Discord metadata snapshot does not include the trigger role."
        )
    elif result["trigger_role_managed"]:
        result["issues"].append("The trigger role is managed by an integration.")
    elif not result["role_snapshot_available"]:
        result["issues"].append(
            "Refresh Discord metadata to show the trigger role name here."
        )

    if remove_delay == 0:
        result["issues"].append(
            "Use a nonzero removal delay unless the MEE6 automation has been tested."
        )
    if not result["state_table_found"] or not result["pulses_table_found"]:
        result["issues"].append(
            "Restart the bot once so the VC XP accounting tables are created."
        )

    blocking_issues = [
        issue
        for issue in result["issues"]
        if not issue.startswith("Refresh Discord metadata")
        and not issue.startswith("Use a nonzero removal delay")
    ]
    if blocking_issues:
        result["status"] = "Needs setup"
    elif enabled:
        result["status"] = "Enabled"
        result["status_class"] = "good"
    else:
        result["status"] = "Ready to test"
        result["status_class"] = "warning-text"

    return result
