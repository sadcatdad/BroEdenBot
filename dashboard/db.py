from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHARED_DATABASE_CANDIDATES = (
    Path("data.db"),
    Path("data/broeden.sqlite"),
    Path("data/bot.sqlite"),
    Path("bot.db"),
    Path("broeden.sqlite"),
)
BANK_DATABASE_CANDIDATES = (Path("brobank.db"),)


def _resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def find_database_path() -> Path:
    configured = os.getenv("DATABASE_PATH", "").strip()
    if configured:
        return _resolve_path(configured)
    for candidate in SHARED_DATABASE_CANDIDATES:
        path = _resolve_path(candidate)
        if path.is_file():
            return path
    return _resolve_path(SHARED_DATABASE_CANDIDATES[0])


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
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


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
