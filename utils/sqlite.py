"""Shared SQLite connection tuning for the bot's async databases."""

from __future__ import annotations

import aiosqlite


async def configure_connection(
    connection: aiosqlite.Connection,
    *,
    foreign_keys: bool = False,
) -> str:
    """Apply consistent contention and durability settings.

    Returns the journal mode SQLite actually selected. In-memory or restricted
    databases may legitimately return a mode other than WAL.
    """
    await connection.execute("PRAGMA busy_timeout = 30000")
    if foreign_keys:
        await connection.execute("PRAGMA foreign_keys = ON")
    cursor = await connection.execute("PRAGMA journal_mode = WAL")
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    await connection.execute("PRAGMA synchronous = NORMAL")
    return str(row[0]).casefold() if row else "unknown"
