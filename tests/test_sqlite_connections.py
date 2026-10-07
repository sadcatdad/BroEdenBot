import sqlite3
import tempfile
import unittest
from pathlib import Path

import aiosqlite

from utils.sqlite import (
    AutoClosingSQLiteConnection,
    BufferedSQLiteConnection,
    configure_connection,
)


class BufferedSQLiteConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_writer_cannot_leave_bot_with_stale_read_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.db"
            connection = BufferedSQLiteConnection(await aiosqlite.connect(path))
            connection.row_factory = aiosqlite.Row
            try:
                await configure_connection(connection)
                await connection.execute("CREATE TABLE example (id INTEGER PRIMARY KEY, value INTEGER)")
                await connection.executemany("INSERT INTO example VALUES (?, ?)", [(1, 10), (2, 20)])
                await connection.commit()
                # Intentionally leave the application's cursor unfetched. The
                # native SELECT must nevertheless be finalized before another
                # connection commits; otherwise the UPDATE raises BUSY_SNAPSHOT.
                cursor = await connection.execute("SELECT * FROM example ORDER BY id")
                with sqlite3.connect(path) as external:
                    external.execute("UPDATE example SET value=11 WHERE id=1")
                update = await connection.execute("UPDATE example SET value=22 WHERE id=2")
                self.assertEqual(update.rowcount, 1)
                await connection.commit()
                self.assertEqual((await cursor.fetchone())["value"], 10)
                self.assertEqual((await cursor.fetchall())[0]["value"], 20)
                self.assertEqual(cursor.description[0][0], "id")
                await cursor.close()
                with sqlite3.connect(path) as external:
                    self.assertEqual(external.execute("SELECT value FROM example ORDER BY id").fetchall(), [(11,), (22,)])
            finally:
                await connection.close()

    async def test_write_transaction_remains_atomic_and_rolls_back(self):
        connection = BufferedSQLiteConnection(await aiosqlite.connect(":memory:"))
        try:
            await connection.execute("CREATE TABLE example (id INTEGER PRIMARY KEY, value INTEGER)")
            first = await connection.execute("INSERT INTO example(value) VALUES (10)")
            self.assertEqual(first.lastrowid, 1)
            self.assertTrue(connection.in_transaction)
            await connection.execute("INSERT INTO example(value) VALUES (20)")
            await connection.rollback()
            cursor = await connection.execute("SELECT * FROM example")
            self.assertEqual(await cursor.fetchall(), [])
            await cursor.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                await cursor.fetchone()
        finally:
            await connection.close()


class AutoClosingSQLiteConnectionTests(unittest.TestCase):
    def test_context_manager_closes_after_committing(self):
        connection = sqlite3.connect(
            ":memory:",
            factory=AutoClosingSQLiteConnection,
        )

        with connection:
            connection.execute("CREATE TABLE example (value INTEGER)")
            connection.execute("INSERT INTO example VALUES (1)")

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT value FROM example")

    def test_context_manager_closes_after_rollback(self):
        connection = sqlite3.connect(
            ":memory:",
            factory=AutoClosingSQLiteConnection,
        )

        with self.assertRaisesRegex(RuntimeError, "stop"):
            with connection:
                connection.execute("CREATE TABLE example (value INTEGER)")
                raise RuntimeError("stop")

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT value FROM example")


if __name__ == "__main__":
    unittest.main()
