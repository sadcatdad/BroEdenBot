import csv
import os
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiosqlite

from cogs.message_context import MessageContext
from scripts.import_message_context import ensure_schema, input_files, process_file
from utils.message_context import (
    MESSAGE_CONTEXT_TABLE_SQL,
    deterministic_import_id,
    has_message_context_access,
    parse_date_boundary,
    parse_retention_days,
)


class MessageContextHelperTests(unittest.TestCase):
    def test_access_requires_owner_or_allowed_role(self):
        self.assertTrue(has_message_context_access(10, [], set(), {10}))
        self.assertTrue(has_message_context_access(20, [30], {30}, set()))
        self.assertFalse(has_message_context_access(20, [31], {30}, {10}))

    def test_retention_is_optional_and_positive(self):
        self.assertIsNone(parse_retention_days(""))
        self.assertIsNone(parse_retention_days("0"))
        self.assertIsNone(parse_retention_days("invalid"))
        self.assertEqual(parse_retention_days("30"), 30)

    def test_date_boundaries_support_relative_dates(self):
        self.assertIsNotNone(parse_date_boundary("yesterday"))
        self.assertIsNotNone(parse_date_boundary("today"))
        self.assertEqual(
            parse_date_boundary("2026-06-21"),
            "2026-06-21T00:00:00+00:00",
        )

    def test_fallback_message_id_is_deterministic(self):
        first = deterministic_import_id("chat.csv", 2, "123", "abc")
        second = deterministic_import_id("chat.csv", 2, "123", "abc")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("imported_csv:chat.csv:2::123:"))


class MessageContextImporterTests(unittest.TestCase):
    def _args(self, root: Path, **overrides) -> Namespace:
        values = {
            "file": None,
            "folder": root,
            "database": root / "message_context.db",
            "guild_id": "42",
            "channel_id": "99",
            "channel_name": "general",
            "dry_run": False,
            "archive_completed": False,
            "archive_duplicates": False,
            "archive_folder": root / "archive",
        }
        values.update(overrides)
        return Namespace(**values)

    @staticmethod
    def _write_csv(path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "AuthorID",
                    "Author",
                    "Date",
                    "Content",
                    "Attachments",
                    "Reactions",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "AuthorID": "123",
                    "Author": "Alice",
                    "Date": "2026-06-21 20:00:00",
                    "Content": "A stored message",
                    "Attachments": "",
                    "Reactions": "",
                }
            )

    def test_import_and_dedupe_share_the_archive_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "general.csv"
            self._write_csv(source)
            connection = sqlite3.connect(root / "context.db")
            ensure_schema(connection)
            args = self._args(root)

            first = process_file(connection, source, args)
            second = process_file(connection, source, args)

            self.assertEqual(first.imported, 1)
            self.assertEqual(second.duplicates, 1)
            row = connection.execute(
                """
                SELECT guild_id, channel_id, author_id, content, source,
                       source_file, row_number, imported_at
                FROM message_context_messages
                """
            ).fetchone()
            self.assertEqual(row[:7], (
                "42",
                "99",
                "123",
                "A stored message",
                "imported_csv",
                "general.csv",
                2,
            ))
            self.assertIsNotNone(row[7])
            connection.close()

    def test_dry_run_does_not_need_a_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "general.csv"
            self._write_csv(source)
            result = process_file(
                None,
                source,
                self._args(root, dry_run=True),
            )
            self.assertEqual(result.imported, 1)
            self.assertFalse((root / "message_context.db").exists())

    def test_skipped_archive_folders_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            active = root / "active.csv"
            archived = root / "archive" / "old.csv"
            archived.parent.mkdir()
            self._write_csv(active)
            self._write_csv(archived)
            self.assertEqual(input_files(self._args(root)), [active])


class MessageContextLiveTrackingTests(unittest.IsolatedAsyncioTestCase):
    class FakeChannel:
        def __init__(self, channel_id, name="general", parent=None):
            self.id = channel_id
            self.name = name
            self.parent = parent

    class FakeAuthor:
        def __init__(self, user_id=123, *, bot=False):
            self.id = user_id
            self.bot = bot
            self.display_name = "Alice"

        def __str__(self):
            return "alice"

    class FakeMessage:
        def __init__(self, channel, *, message_id=777, content="hello"):
            self.guild = SimpleNamespace(id=42)
            self.channel = channel
            self.author = MessageContextLiveTrackingTests.FakeAuthor()
            self.webhook_id = None
            self.content = content
            self.attachments = []
            self.embeds = []
            self.stickers = []
            self.id = message_id
            self.created_at = datetime.now(timezone.utc)
            self.edited_at = None
            self.jump_url = "https://discord.com/channels/42/99/777"

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "context.db"
        self.channel = self.FakeChannel(99)
        self.bot = SimpleNamespace(
            intents=SimpleNamespace(message_content=True),
            get_channel=lambda channel_id: (
                self.channel if channel_id == self.channel.id else None
            ),
        )

    async def _cog(self, **env):
        values = {
            "MESSAGE_CONTEXT_ENABLED": "true",
            "MESSAGE_CONTEXT_DB_PATH": str(self.db_path),
            "MESSAGE_CONTEXT_CHANNEL_IDS": "",
            "MESSAGE_CONTEXT_EXCLUDED_CHANNEL_IDS": "",
            "MESSAGE_CONTEXT_TRACK_EDITS": "true",
            "MESSAGE_CONTEXT_TRACK_DELETES": "true",
        }
        values.update(env)
        with patch.dict(os.environ, values, clear=False):
            cog = MessageContext(self.bot)
        cog.db = await aiosqlite.connect(self.db_path)
        cog.db.row_factory = aiosqlite.Row
        await cog.db.execute(MESSAGE_CONTEXT_TABLE_SQL)
        await cog.db.commit()
        self.addAsyncCleanup(cog.db.close)
        return cog

    async def test_empty_include_list_tracks_visible_channels_and_updates_rows(self):
        cog = await self._cog()
        message = self.FakeMessage(self.channel)
        await cog.on_message(message)

        edited = self.FakeMessage(self.channel, content="edited")
        edited.edited_at = datetime.now(timezone.utc)
        await cog.on_message_edit(message, edited)
        await cog._mark_deleted(42, 99, [777])

        cursor = await cog.db.execute(
            """
            SELECT content, edited_at, is_deleted, deleted_at
            FROM message_context_messages WHERE message_id = '777'
            """
        )
        row = await cursor.fetchone()
        await cursor.close()
        self.assertEqual(row["content"], "edited")
        self.assertIsNotNone(row["edited_at"])
        self.assertEqual(row["is_deleted"], 1)
        self.assertIsNotNone(row["deleted_at"])

    async def test_excluded_channels_and_bots_are_not_stored(self):
        cog = await self._cog(MESSAGE_CONTEXT_EXCLUDED_CHANNEL_IDS="99")
        await cog.on_message(self.FakeMessage(self.channel))
        bot_message = self.FakeMessage(self.FakeChannel(100), message_id=778)
        bot_message.author = self.FakeAuthor(bot=True)
        await cog.on_message(bot_message)
        cursor = await cog.db.execute(
            "SELECT COUNT(*) FROM message_context_messages"
        )
        self.assertEqual((await cursor.fetchone())[0], 0)
        await cursor.close()


if __name__ == "__main__":
    unittest.main()
