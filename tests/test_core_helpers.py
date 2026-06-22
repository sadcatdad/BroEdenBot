import unittest
from os import environ
from unittest.mock import patch

import aiosqlite

from cogs.bot_admin import is_bot_manager, parse_user_ids, sanitize_logs
from cogs.leaderboards import normalize_leaderboard_name, parse_points
from cogs.poll import (
    deserialize_options,
    parse_duration,
    parse_poll_options,
    serialize_options,
)
from utils.ui import progress_bar, truncate
from utils.sqlite import configure_connection


class PollHelperTests(unittest.TestCase):
    def test_duration_parser_accepts_compound_values(self):
        self.assertEqual(parse_duration("1h 30m"), 5_400)

    def test_duration_parser_rejects_partial_garbage(self):
        self.assertEqual(parse_duration("1h later"), 0)

    def test_options_are_unique_and_bounded(self):
        self.assertEqual(parse_poll_options("Yes, No"), ["Yes", "No"])
        with self.assertRaises(ValueError):
            parse_poll_options("Yes, yes")

    def test_legacy_options_are_read_without_eval(self):
        self.assertEqual(deserialize_options("['A', 'B']"), ["A", "B"])
        encoded = serialize_options(["A", "B"])
        self.assertEqual(deserialize_options(encoded), ["A", "B"])
        with self.assertRaises((ValueError, SyntaxError)):
            deserialize_options("__import__('os').system('echo unsafe')")


class LeaderboardHelperTests(unittest.TestCase):
    def test_points_are_finite_positive_and_rounded(self):
        self.assertEqual(parse_points("2.345"), 2.35)
        self.assertIsNone(parse_points("nan"))
        self.assertIsNone(parse_points("0"))

    def test_names_are_compacted(self):
        self.assertEqual(
            normalize_leaderboard_name("  Weekly   Wins "),
            "Weekly Wins",
        )


class UIHelperTests(unittest.TestCase):
    def test_progress_bar(self):
        self.assertEqual(progress_bar(5, 10, width=4), "▰▰▱▱")

    def test_truncate(self):
        self.assertEqual(truncate("abcdef", 4), "abc…")


class BotAdminHelperTests(unittest.TestCase):
    class DummyPermissions:
        def __init__(self, administrator=False):
            self.administrator = administrator

    class DummyUser:
        def __init__(self, user_id, administrator=False):
            self.id = user_id
            self.guild_permissions = BotAdminHelperTests.DummyPermissions(
                administrator
            )

    def test_owner_ids_ignore_invalid_values(self):
        self.assertEqual(parse_user_ids("12, nope 34, -1"), {12, 34})

    def test_admins_are_denied_by_default(self):
        with patch.dict(
            environ,
            {
                "BOT_OWNER_USER_IDS": "123",
                "BOT_OWNER_ALLOW_ADMINS": "false",
            },
            clear=False,
        ):
            self.assertTrue(is_bot_manager(self.DummyUser(123)))
            self.assertFalse(is_bot_manager(self.DummyUser(456, True)))

    def test_admin_access_can_be_enabled(self):
        with patch.dict(
            environ,
            {
                "BOT_OWNER_USER_IDS": "",
                "BOT_OWNER_ALLOW_ADMINS": "true",
            },
            clear=False,
        ):
            self.assertTrue(is_bot_manager(self.DummyUser(456, True)))

    def test_logs_are_redacted(self):
        sanitized = sanitize_logs(
            "API_KEY=super-secret\n"
            "Authorization: Bearer abc.def.ghi\n"
            "Traceback (most recent call last):\n"
            '  File "/srv/main.py", line 12, in main'
        )
        self.assertNotIn("super-secret", sanitized)
        self.assertNotIn("/srv/main.py", sanitized)
        self.assertIn("[REDACTED]", sanitized)
        self.assertIn("[traceback detail omitted]", sanitized)


class SQLiteHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_configuration(self):
        connection = await aiosqlite.connect(":memory:")
        try:
            mode = await configure_connection(
                connection,
                foreign_keys=True,
            )
            cursor = await connection.execute("PRAGMA foreign_keys")
            foreign_keys = (await cursor.fetchone())[0]
            await cursor.close()
            cursor = await connection.execute("PRAGMA busy_timeout")
            timeout = (await cursor.fetchone())[0]
            await cursor.close()
        finally:
            await connection.close()

        self.assertIn(mode, {"memory", "wal"})
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(timeout, 30_000)


if __name__ == "__main__":
    unittest.main()
