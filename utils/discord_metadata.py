"""Dashboard-safe Discord guild metadata snapshots.

The dashboard process does not start a Discord client. It reads the latest
snapshot written by the live bot process and can queue a fixed refresh action.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from utils.settings import settings_database_path
from utils.sqlite import AutoClosingSQLiteConnection, configure_sync_connection


def _connect() -> sqlite3.Connection:
    path = settings_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path,
        timeout=30,
        factory=AutoClosingSQLiteConnection,
    )
    return configure_sync_connection(connection)


def _readonly_connect() -> sqlite3.Connection:
    path = settings_database_path()
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=5,
        factory=AutoClosingSQLiteConnection,
    )
    return configure_sync_connection(connection, readonly=True)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def initialize_discord_metadata_schema() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_discord_roles (
                id TEXT PRIMARY KEY,
                guild_id TEXT,
                name TEXT NOT NULL,
                color TEXT,
                position INTEGER,
                managed INTEGER NOT NULL DEFAULT 0,
                mentionable INTEGER NOT NULL DEFAULT 0,
                hoist INTEGER NOT NULL DEFAULT 0,
                member_count INTEGER,
                is_bot_role INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_discord_categories (
                id TEXT PRIMARY KEY,
                guild_id TEXT,
                name TEXT NOT NULL,
                position INTEGER,
                child_channel_ids TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_discord_channels (
                id TEXT PRIMARY KEY,
                guild_id TEXT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                position INTEGER,
                parent_id TEXT,
                parent_name TEXT,
                nsfw INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                is_thread INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_discord_emojis (
                id TEXT PRIMARY KEY,
                guild_id TEXT,
                name TEXT NOT NULL,
                animated INTEGER NOT NULL DEFAULT 0,
                available INTEGER NOT NULL DEFAULT 1,
                managed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_discord_metadata_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                guild_id TEXT,
                guild_name TEXT,
                roles_count INTEGER NOT NULL DEFAULT 0,
                categories_count INTEGER NOT NULL DEFAULT 0,
                channels_count INTEGER NOT NULL DEFAULT 0,
                emojis_count INTEGER NOT NULL DEFAULT 0,
                last_refreshed_at TEXT,
                last_error TEXT
            )
            """
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
            CREATE INDEX IF NOT EXISTS idx_dashboard_actions_pending
            ON dashboard_actions (status, action_type, id)
            """
        )
        _ensure_column(connection, "dashboard_discord_roles", "guild_id", "TEXT")
        _ensure_column(connection, "dashboard_discord_roles", "color", "TEXT")
        _ensure_column(connection, "dashboard_discord_roles", "managed", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "dashboard_discord_roles", "mentionable", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "dashboard_discord_roles", "hoist", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "dashboard_discord_roles", "member_count", "INTEGER")
        _ensure_column(connection, "dashboard_discord_roles", "is_bot_role", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "dashboard_discord_roles", "updated_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "dashboard_discord_categories", "guild_id", "TEXT")
        _ensure_column(connection, "dashboard_discord_categories", "child_channel_ids", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(connection, "dashboard_discord_categories", "updated_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "dashboard_discord_channels", "guild_id", "TEXT")
        _ensure_column(connection, "dashboard_discord_channels", "parent_name", "TEXT")
        _ensure_column(connection, "dashboard_discord_channels", "nsfw", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "dashboard_discord_channels", "archived", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "dashboard_discord_channels", "is_thread", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "dashboard_discord_channels", "updated_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "dashboard_discord_emojis", "guild_id", "TEXT")
        _ensure_column(connection, "dashboard_discord_emojis", "animated", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "dashboard_discord_emojis", "available", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(connection, "dashboard_discord_emojis", "managed", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "dashboard_discord_emojis", "updated_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "dashboard_discord_metadata_status", "emojis_count", "INTEGER NOT NULL DEFAULT 0")
        connection.commit()


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }
    if column not in columns:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')


def queue_discord_metadata_refresh(requested_by: str = "dashboard") -> int:
    initialize_discord_metadata_schema()
    payload_json = "{}"
    with _connect() as connection:
        existing = connection.execute(
            """
            SELECT id FROM dashboard_actions
            WHERE action_type = 'refresh_discord_metadata'
              AND status IN ('pending', 'processing')
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if existing:
            return int(existing["id"])
        cursor = connection.execute(
            """
            INSERT INTO dashboard_actions (
                action_type, payload_json, status, requested_by
            ) VALUES ('refresh_discord_metadata', ?, 'pending', ?)
            """,
            (payload_json, requested_by),
        )
        connection.commit()
        return int(cursor.lastrowid)


def save_discord_metadata_snapshot(
    *,
    guild_id: str,
    guild_name: str,
    roles: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    channels: list[dict[str, Any]],
    emojis: Optional[list[dict[str, Any]]] = None,
) -> None:
    initialize_discord_metadata_schema()
    now = datetime.now(timezone.utc).isoformat()
    emoji_rows = emojis or []
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM dashboard_discord_roles")
        connection.execute("DELETE FROM dashboard_discord_categories")
        connection.execute("DELETE FROM dashboard_discord_channels")
        connection.execute("DELETE FROM dashboard_discord_emojis")
        connection.executemany(
            """
            INSERT INTO dashboard_discord_roles (
                id, guild_id, name, color, position, managed, mentionable,
                hoist, member_count, is_bot_role, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(role["id"]),
                    str(guild_id),
                    str(role.get("name") or ""),
                    role.get("color"),
                    _int_or_none(role.get("position")),
                    int(bool(role.get("managed"))),
                    int(bool(role.get("mentionable"))),
                    int(bool(role.get("hoist"))),
                    _int_or_none(role.get("member_count")),
                    int(bool(role.get("is_bot_role"))),
                    now,
                )
                for role in roles
            ],
        )
        connection.executemany(
            """
            INSERT INTO dashboard_discord_categories (
                id, guild_id, name, position, child_channel_ids, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(category["id"]),
                    str(guild_id),
                    str(category.get("name") or ""),
                    _int_or_none(category.get("position")),
                    json.dumps([str(item) for item in category.get("child_channel_ids", [])]),
                    now,
                )
                for category in categories
            ],
        )
        connection.executemany(
            """
            INSERT INTO dashboard_discord_channels (
                id, guild_id, name, type, position, parent_id, parent_name,
                nsfw, archived, is_thread, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(channel["id"]),
                    str(guild_id),
                    str(channel.get("name") or ""),
                    str(channel.get("type") or "unknown"),
                    _int_or_none(channel.get("position")),
                    _optional_string(channel.get("parent_id")),
                    _optional_string(channel.get("parent_name")),
                    int(bool(channel.get("nsfw"))),
                    int(bool(channel.get("archived"))),
                    int(bool(channel.get("is_thread"))),
                    now,
                )
                for channel in channels
            ],
        )
        connection.executemany(
            """
            INSERT INTO dashboard_discord_emojis (
                id, guild_id, name, animated, available, managed, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(emoji["id"]),
                    str(guild_id),
                    str(emoji.get("name") or ""),
                    int(bool(emoji.get("animated"))),
                    int(bool(emoji.get("available", True))),
                    int(bool(emoji.get("managed"))),
                    now,
                )
                for emoji in emoji_rows
            ],
        )
        connection.execute(
            """
            INSERT INTO dashboard_discord_metadata_status (
                id, guild_id, guild_name, roles_count, categories_count,
                channels_count, emojis_count, last_refreshed_at, last_error
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                guild_id = excluded.guild_id,
                guild_name = excluded.guild_name,
                roles_count = excluded.roles_count,
                categories_count = excluded.categories_count,
                channels_count = excluded.channels_count,
                emojis_count = excluded.emojis_count,
                last_refreshed_at = excluded.last_refreshed_at,
                last_error = NULL
            """,
            (
                str(guild_id),
                str(guild_name or ""),
                len(roles),
                len(categories),
                len(channels),
                len(emoji_rows),
                now,
            ),
        )
        connection.commit()


def record_discord_metadata_error(message: str) -> None:
    initialize_discord_metadata_schema()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO dashboard_discord_metadata_status (
                id, roles_count, categories_count, channels_count, emojis_count,
                last_error
            ) VALUES (1, 0, 0, 0, 0, ?)
            ON CONFLICT(id) DO UPDATE SET last_error = excluded.last_error
            """,
            (str(message)[:1_000],),
        )
        connection.commit()


def metadata_status() -> dict[str, Any]:
    initialize_discord_metadata_schema()
    try:
        with _readonly_connect() as connection:
            if not _table_exists(connection, "dashboard_discord_metadata_status"):
                return _empty_status()
            row = connection.execute(
                "SELECT * FROM dashboard_discord_metadata_status WHERE id = 1"
            ).fetchone()
            return dict(row) if row else _empty_status()
    except (OSError, sqlite3.Error):
        return _empty_status()


def roles_snapshot() -> list[dict[str, Any]]:
    initialize_discord_metadata_schema()
    return _snapshot_rows(
        "dashboard_discord_roles",
        """
        SELECT id, name, color, position, managed, mentionable, hoist,
               member_count, is_bot_role
        FROM dashboard_discord_roles
        ORDER BY COALESCE(position, -1) DESC, lower(name)
        """,
    )


def categories_snapshot() -> list[dict[str, Any]]:
    initialize_discord_metadata_schema()
    rows = _snapshot_rows(
        "dashboard_discord_categories",
        """
        SELECT id, name, position, child_channel_ids
        FROM dashboard_discord_categories
        ORDER BY COALESCE(position, 999999), lower(name)
        """,
    )
    for row in rows:
        try:
            row["child_channel_ids"] = json.loads(row.get("child_channel_ids") or "[]")
        except json.JSONDecodeError:
            row["child_channel_ids"] = []
    return rows


def channels_snapshot() -> list[dict[str, Any]]:
    initialize_discord_metadata_schema()
    return _snapshot_rows(
        "dashboard_discord_channels",
        """
        SELECT id, name, type, position, parent_id, parent_name,
               nsfw, archived, is_thread
        FROM dashboard_discord_channels
        WHERE is_thread = 0
        ORDER BY COALESCE(position, 999999), lower(name)
        """,
    )


def emojis_snapshot() -> list[dict[str, Any]]:
    initialize_discord_metadata_schema()
    return _snapshot_rows(
        "dashboard_discord_emojis",
        """
        SELECT id, name, animated, available, managed
        FROM dashboard_discord_emojis
        ORDER BY lower(name), id
        """,
    )


def guild_structure_snapshot() -> dict[str, Any]:
    categories = categories_snapshot()
    channels = channels_snapshot()
    by_parent: dict[Optional[str], list[dict[str, Any]]] = {}
    for channel in channels:
        by_parent.setdefault(channel.get("parent_id"), []).append(channel)
    grouped_categories = []
    for category in categories:
        children = by_parent.pop(str(category["id"]), [])
        grouped_categories.append({**category, "channels": children})
    return {
        "status": metadata_status(),
        "roles": roles_snapshot(),
        "emojis": emojis_snapshot(),
        "categories": grouped_categories,
        "uncategorized": by_parent.pop(None, []) + by_parent.pop("", []),
    }


def _snapshot_rows(table: str, query: str) -> list[dict[str, Any]]:
    try:
        with _readonly_connect() as connection:
            if not _table_exists(connection, table):
                return []
            rows = connection.execute(query).fetchall()
            return [_normalize_booleans(dict(row)) for row in rows]
    except (OSError, sqlite3.Error):
        return []


def _normalize_booleans(row: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "managed",
        "mentionable",
        "hoist",
        "is_bot_role",
        "nsfw",
        "archived",
        "is_thread",
        "animated",
        "available",
    ):
        if key in row:
            row[key] = bool(row[key])
    return row


def _empty_status() -> dict[str, Any]:
    return {
        "guild_id": None,
        "guild_name": None,
        "roles_count": 0,
        "categories_count": 0,
        "channels_count": 0,
        "emojis_count": 0,
        "last_refreshed_at": None,
        "last_error": None,
    }


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_string(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None
