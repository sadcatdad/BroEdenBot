"""Shared schema helpers for historical voice-channel session imports."""

from __future__ import annotations

import sqlite3
from typing import Iterable

import aiosqlite


VC_IMPORTED_SESSIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS vc_imported_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER,
    user_name TEXT,
    display_name TEXT,
    voice_channel_id INTEGER,
    voice_channel_name TEXT,
    joined_at TEXT NOT NULL,
    left_at TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    counted_seconds INTEGER NOT NULL DEFAULT 0,
    reward_eligible INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'imported_vc_log',
    confidence TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_start_message_id TEXT,
    source_end_message_id TEXT,
    imported_at TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    is_imported INTEGER NOT NULL DEFAULT 1,
    is_estimated INTEGER NOT NULL DEFAULT 0,
    close_reason TEXT
)
"""

VC_IMPORTED_SESSIONS_INDEX_SQL = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_vc_imported_sessions_dedupe
    ON vc_imported_sessions (dedupe_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_vc_imported_sessions_guild_left
    ON vc_imported_sessions (guild_id, left_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_vc_imported_sessions_guild_user_left
    ON vc_imported_sessions (guild_id, user_id, left_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_vc_imported_sessions_guild_channel_left
    ON vc_imported_sessions (guild_id, voice_channel_id, left_at)
    """,
)

VC_IMPORTED_SESSIONS_COLUMNS = {
    "counted_seconds": "INTEGER NOT NULL DEFAULT 0",
    "reward_eligible": "INTEGER NOT NULL DEFAULT 0",
    "source": "TEXT NOT NULL DEFAULT 'imported_vc_log'",
    "confidence": "TEXT NOT NULL DEFAULT 'low'",
    "source_file": "TEXT NOT NULL DEFAULT ''",
    "source_start_message_id": "TEXT",
    "source_end_message_id": "TEXT",
    "imported_at": "TEXT",
    "dedupe_key": "TEXT",
    "is_imported": "INTEGER NOT NULL DEFAULT 1",
    "is_estimated": "INTEGER NOT NULL DEFAULT 0",
    "close_reason": "TEXT",
}


def _sync_columns(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(vc_imported_sessions)"
        ).fetchall()
    }


async def _async_columns(connection: aiosqlite.Connection) -> set[str]:
    cursor = await connection.execute("PRAGMA table_info(vc_imported_sessions)")
    try:
        return {str(row[1]) for row in await cursor.fetchall()}
    finally:
        await cursor.close()


def _missing_columns(existing: Iterable[str]) -> list[tuple[str, str]]:
    existing_set = set(existing)
    return [
        (name, definition)
        for name, definition in VC_IMPORTED_SESSIONS_COLUMNS.items()
        if name not in existing_set
    ]


def ensure_vc_history_schema(connection: sqlite3.Connection) -> None:
    """Create or safely extend the synchronous importer schema."""
    connection.execute(VC_IMPORTED_SESSIONS_TABLE_SQL)
    for name, definition in _missing_columns(_sync_columns(connection)):
        connection.execute(
            f"ALTER TABLE vc_imported_sessions ADD COLUMN {name} {definition}"
        )
    for statement in VC_IMPORTED_SESSIONS_INDEX_SQL:
        connection.execute(statement)
    connection.commit()


async def ensure_vc_history_schema_async(
    connection: aiosqlite.Connection,
) -> None:
    """Create or safely extend the bot's async historical-session schema."""
    await connection.execute(VC_IMPORTED_SESSIONS_TABLE_SQL)
    for name, definition in _missing_columns(await _async_columns(connection)):
        await connection.execute(
            f"ALTER TABLE vc_imported_sessions ADD COLUMN {name} {definition}"
        )
    for statement in VC_IMPORTED_SESSIONS_INDEX_SQL:
        await connection.execute(statement)
    await connection.commit()
