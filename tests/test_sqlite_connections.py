import sqlite3
import unittest

from utils.sqlite import AutoClosingSQLiteConnection


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
