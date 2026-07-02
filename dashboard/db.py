from __future__ import annotations

import os
import json
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
from utils.ai_config import get_ai_config
from utils.ai_kb import get_kb_status
from utils.sqlite import configure_sync_connection

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BANK_DATABASE_CANDIDATES = (Path("brobank.db"),)
MESSAGE_CONTEXT_DATABASE_CANDIDATES = (Path("message_context.db"),)


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


def find_message_context_database_path() -> Path:
    configured = os.getenv("MESSAGE_CONTEXT_DB_PATH", "").strip()
    if configured:
        return _resolve_path(configured)
    for candidate in MESSAGE_CONTEXT_DATABASE_CANDIDATES:
        path = _resolve_path(candidate)
        if path.is_file():
            return path
    return _resolve_path(MESSAGE_CONTEXT_DATABASE_CANDIDATES[0])


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
    minutes_per_pulse = max(1, get_int_setting("VC_XP_PULSE_MINUTES", 30))
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

            if "vc_xp_pulses" in tables:
                result["pulses_table_found"] = True
                day_start = (
                    datetime.now(timezone.utc) - timedelta(days=1)
                ).isoformat()
                result["active_pulses"] = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM vc_xp_pulses
                        WHERE status = ?
                        """,
                        ("added",),
                    ).fetchone()[0]
                    or 0
                )
                result["paid_24h"] = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM vc_xp_pulses
                        WHERE granted_at >= ?
                          AND status = ?
                        """,
                        (day_start, "added"),
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


def ai_dashboard_visible() -> bool:
    return get_ai_config().dashboard_visible


def _current_day_prefix() -> str:
    return datetime.now().astimezone().date().isoformat()


def _current_month_prefix() -> str:
    return datetime.now().astimezone().strftime("%Y-%m")


def ai_usage_overview(
    *,
    limit: int = 50,
    command: str = "",
    model: str = "",
    status_filter: str = "",
) -> dict[str, Any]:
    path = find_database_path()
    ai_config = get_ai_config()
    result: dict[str, Any] = {
        "database": database_status(path),
        "config": {
            "enabled": ai_config.enabled,
            "available": ai_config.available,
            "api_key_present": ai_config.api_key_present,
            "fast_model": ai_config.models.fast,
            "default_model": ai_config.models.default,
            "advanced_model": ai_config.models.advanced,
            "advanced_enabled": ai_config.advanced_enabled,
            "daily_budget_usd": ai_config.budgets.daily_usd,
            "monthly_budget_usd": ai_config.budgets.monthly_usd,
            "max_input_tokens": ai_config.token_limits.max_input_tokens,
            "max_output_tokens": ai_config.token_limits.max_output_tokens,
            "default_temperature": ai_config.default_temperature,
            "member_cooldown_seconds": ai_config.cooldowns.member_seconds,
            "staff_cooldown_seconds": ai_config.cooldowns.staff_seconds,
            "log_prompts": ai_config.logging.log_prompts,
            "log_responses": ai_config.logging.log_responses,
            "dashboard_visible": ai_config.dashboard_visible,
        },
        "tables_found": False,
        "daily_spend_usd": 0.0,
        "monthly_spend_usd": 0.0,
        "daily_requests": 0,
        "monthly_requests": 0,
        "last_error": None,
        "last_success_at": None,
        "recent_logs": [],
        "command_usage": [],
        "top_commands_by_cost": [],
        "recent_failures": [],
        "recent_budget_blocks": [],
        "ask_feedback": {
            "tables_found": False,
            "total": 0,
            "helped": 0,
            "confused": 0,
            "unmarked": 0,
            "recent": [],
        },
        "kb_status": get_kb_status(),
        "filters": {
            "command": command,
            "model": model,
            "status": status_filter,
        },
        "error": None,
    }
    if not path.is_file():
        return result

    try:
        with readonly_connection(path) as connection:
            if "ai_usage_logs" not in table_names(connection):
                usage_tables_found = False
            else:
                usage_tables_found = True
            if "ask_feedback" in table_names(connection):
                result["ask_feedback"]["tables_found"] = True
                totals = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN feedback = 'helped' THEN 1 ELSE 0 END) AS helped,
                        SUM(CASE WHEN feedback = 'confused' THEN 1 ELSE 0 END) AS confused,
                        SUM(CASE WHEN feedback IS NULL OR feedback = '' THEN 1 ELSE 0 END)
                            AS unmarked
                    FROM ask_feedback
                    """
                ).fetchone()
                if totals is not None:
                    result["ask_feedback"]["total"] = int(totals["total"] or 0)
                    result["ask_feedback"]["helped"] = int(totals["helped"] or 0)
                    result["ask_feedback"]["confused"] = int(totals["confused"] or 0)
                    result["ask_feedback"]["unmarked"] = int(totals["unmarked"] or 0)
                recent_feedback = []
                for row in connection.execute(
                    """
                    SELECT
                        created_at, feedback_at, feedback, user_id, channel_id,
                        SUBSTR(question, 1, 240) AS question,
                        SUBSTR(answer, 1, 420) AS answer,
                        kb_sources_json, model_used
                    FROM ask_feedback
                    ORDER BY
                        CASE WHEN feedback = 'confused' THEN 0 ELSE 1 END,
                        COALESCE(feedback_at, updated_at, created_at) DESC,
                        id DESC
                    LIMIT 12
                    """
                ).fetchall():
                    item = dict(row)
                    try:
                        item["kb_sources"] = json.loads(item.pop("kb_sources_json") or "[]")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        item["kb_sources"] = []
                    recent_feedback.append(item)
                result["ask_feedback"]["recent"] = recent_feedback
            if not usage_tables_found:
                return result
            result["tables_found"] = True
            day_prefix = _current_day_prefix()
            month_prefix = _current_month_prefix()
            result["daily_spend_usd"] = float(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(estimated_cost_usd), 0)
                    FROM ai_usage_logs
                    WHERE created_at LIKE ?
                    """,
                    (day_prefix + "%",),
                ).fetchone()[0]
                or 0
            )
            result["monthly_spend_usd"] = float(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(estimated_cost_usd), 0)
                    FROM ai_usage_logs
                    WHERE created_at LIKE ?
                    """,
                    (month_prefix + "%",),
                ).fetchone()[0]
                or 0
            )
            result["daily_requests"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_usage_logs WHERE created_at LIKE ?",
                    (day_prefix + "%",),
                ).fetchone()[0]
                or 0
            )
            result["monthly_requests"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_usage_logs WHERE created_at LIKE ?",
                    (month_prefix + "%",),
                ).fetchone()[0]
                or 0
            )
            last_error = connection.execute(
                """
                SELECT created_at, error_message
                FROM ai_usage_logs
                WHERE success = 0
                  AND error_message IS NOT NULL
                  AND error_message != ''
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if last_error is not None:
                result["last_error"] = dict(last_error)
            last_success = connection.execute(
                """
                SELECT created_at
                FROM ai_usage_logs
                WHERE success = 1
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if last_success is not None:
                result["last_success_at"] = str(last_success["created_at"])

            clauses = []
            params: list[Any] = []
            if command:
                clauses.append("source_command = ?")
                params.append(command)
            if model:
                clauses.append("model_used = ?")
                params.append(model)
            if status_filter == "success":
                clauses.append("success = 1")
            elif status_filter == "failed":
                clauses.append("success = 0 AND blocked_by_budget = 0")
            elif status_filter == "blocked":
                clauses.append("blocked_by_budget = 1")
            where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
            params.append(max(1, min(limit, 200)))
            result["recent_logs"] = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT
                        created_at, source_command, task_type, user_id,
                        model_used, tier_used, input_tokens, output_tokens,
                        total_tokens, estimated_cost_usd, usage_was_estimated,
                        success, blocked_by_budget, error_message
                    FROM ai_usage_logs
                    {where_sql}
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
            ]
            tracked_commands = (
                "/ask",
                "/context channel",
                "/context user",
                "/rulecard draft",
            )
            result["command_usage"] = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT
                        source_command,
                        SUM(created_at LIKE ?) AS today_count,
                        SUM(created_at LIKE ?) AS month_count,
                        COALESCE(SUM(CASE WHEN created_at LIKE ? THEN estimated_cost_usd END), 0)
                            AS today_cost,
                        COALESCE(SUM(CASE WHEN created_at LIKE ? THEN estimated_cost_usd END), 0)
                            AS month_cost
                    FROM ai_usage_logs
                    WHERE source_command IN ({",".join("?" for _ in tracked_commands)})
                    GROUP BY source_command
                    ORDER BY source_command
                    """,
                    (
                        day_prefix + "%",
                        month_prefix + "%",
                        day_prefix + "%",
                        month_prefix + "%",
                        *tracked_commands,
                    ),
                ).fetchall()
            ]
            result["top_commands_by_cost"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT source_command, COUNT(*) AS request_count,
                           COALESCE(SUM(estimated_cost_usd), 0) AS total_cost
                    FROM ai_usage_logs
                    WHERE created_at LIKE ?
                    GROUP BY source_command
                    ORDER BY total_cost DESC, request_count DESC
                    LIMIT 8
                    """,
                    (month_prefix + "%",),
                ).fetchall()
            ]
            result["recent_failures"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT created_at, source_command, task_type, model_used, error_message
                    FROM ai_usage_logs
                    WHERE success = 0 AND blocked_by_budget = 0
                    ORDER BY created_at DESC, id DESC
                    LIMIT 8
                    """
                ).fetchall()
            ]
            result["recent_budget_blocks"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT created_at, source_command, task_type, model_used, estimated_cost_usd
                    FROM ai_usage_logs
                    WHERE blocked_by_budget = 1
                    ORDER BY created_at DESC, id DESC
                    LIMIT 8
                    """
                ).fetchall()
            ]
    except (OSError, sqlite3.Error) as exc:
        result["error"] = str(exc)
    return result


def message_context_overview() -> dict[str, Any]:
    path = find_message_context_database_path()
    result: dict[str, Any] = {
        "enabled": get_bool_setting("MESSAGE_CONTEXT_ENABLED", False),
        "database": database_status(path),
        "tables_found": False,
        "total_messages": 0,
        "oldest": None,
        "newest": None,
        "last_24h": 0,
        "last_7d": 0,
        "top_channels_7d": [],
        "last_error": None,
        "error": None,
    }
    if not path.is_file():
        return result

    try:
        with readonly_connection(path) as connection:
            if "message_context_messages" not in table_names(connection):
                return result
            result["tables_found"] = True
            now = datetime.now(timezone.utc)
            cutoff_24h = (now - timedelta(hours=24)).isoformat()
            cutoff_7d = (now - timedelta(days=7)).isoformat()
            row = connection.execute(
                """
                SELECT COUNT(*) AS total, MIN(timestamp) AS oldest,
                       MAX(timestamp) AS newest,
                       SUM(timestamp >= ?) AS last_24h,
                       SUM(timestamp >= ?) AS last_7d
                FROM message_context_messages
                """,
                (cutoff_24h, cutoff_7d),
            ).fetchone()
            result["total_messages"] = int(row["total"] or 0)
            result["oldest"] = row["oldest"]
            result["newest"] = row["newest"]
            result["last_24h"] = int(row["last_24h"] or 0)
            result["last_7d"] = int(row["last_7d"] or 0)
            result["top_channels_7d"] = [
                dict(item)
                for item in connection.execute(
                    """
                    SELECT COALESCE(channel_name, channel_id) AS channel,
                           COUNT(*) AS total
                    FROM message_context_messages
                    WHERE timestamp >= ?
                    GROUP BY COALESCE(channel_name, channel_id)
                    ORDER BY total DESC
                    LIMIT 5
                    """,
                    (cutoff_7d,),
                ).fetchall()
            ]
    except (OSError, sqlite3.Error) as exc:
        result["error"] = str(exc)
        return result

    # The last /context error, if any, lives in the shared DB's AI usage log
    # rather than message_context.db.
    try:
        main_path = find_database_path()
        if main_path.is_file():
            with readonly_connection(main_path) as connection:
                if "ai_usage_logs" in table_names(connection):
                    last_error = connection.execute(
                        """
                        SELECT created_at, source_command, error_message
                        FROM ai_usage_logs
                        WHERE success = 0
                          AND source_command IN ('/context user', '/context channel')
                          AND error_message IS NOT NULL AND error_message != ''
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1
                        """
                    ).fetchone()
                    if last_error is not None:
                        result["last_error"] = dict(last_error)
    except (OSError, sqlite3.Error):
        pass
    return result
