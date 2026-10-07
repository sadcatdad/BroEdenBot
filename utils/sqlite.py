"""Shared SQLite connection tuning for the bot's async databases."""

from __future__ import annotations

import sqlite3
from typing import Any

import aiosqlite


class BufferedSQLiteCursor:
    """Query results whose native cursor has already released its read lock."""

    def __init__(self, rows, description, rowcount, lastrowid):
        self._rows = rows
        self._position = 0
        self._closed = False
        self.description = description
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    async def fetchone(self):
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed cursor.")
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    async def fetchall(self):
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed cursor.")
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    async def close(self):
        self._rows = []
        self._closed = True


class BufferedSQLiteConnection:
    """Release SELECT cursors in the same worker call that executes them.

    Cogs share this connection while dashboard/Event Drops use other writers.
    An open SELECT cursor can pin an old WAL snapshot across coroutine yields;
    a subsequent write then fails with SQLITE_BUSY_SNAPSHOT even when no writer
    holds a lock. Buffering reads removes that window without changing the
    existing commit/rollback boundaries or making writes autocommit.
    """

    def __init__(self, connection: aiosqlite.Connection):
        self._connection = connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._connection.row_factory = value

    async def execute(self, sql, parameters=()):
        def execute_and_release():
            # aiosqlite 0.22.1's worker queue keeps execute/fetch/close atomic
            # relative to other operations on this shared connection. SQLite
            # access remains entirely on its own worker thread.
            cursor = self._connection._conn.execute(sql, parameters)
            try:
                rows = cursor.fetchall() if cursor.description is not None else []
                return BufferedSQLiteCursor(
                    rows, cursor.description, cursor.rowcount, cursor.lastrowid
                )
            finally:
                cursor.close()

        return await self._connection._execute(execute_and_release)


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
