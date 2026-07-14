"""Shared SQLite connection tuning for the bot's async databases."""

from __future__ import annotations

import sqlite3

import aiosqlite


class AutoClosingSQLiteConnection(sqlite3.Connection):
    """Commit or roll back a context-managed connection, then close it."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


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


def configure_sync_connection(
    connection: sqlite3.Connection,
    *,
    readonly: bool = False,
) -> sqlite3.Connection:
    """Apply the shared timeout/query settings to sqlite3 connections."""
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    if readonly:
        connection.execute("PRAGMA query_only = ON")
    return connection
