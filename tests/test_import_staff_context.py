import argparse
import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

import aiosqlite

from cogs.staff_ai import StaffAI
from scripts.import_staff_context import ensure_schema, input_files, process_file
from utils.staff_context import (
    STAFF_CONTEXT_TABLE_SQL,
    fts_query,
    has_staff_ai_access,
    infer_channel,
    short_excerpt,
)


GUILD_ID = 1278253523619807233


def importer_args(root: Path, *, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        folder=root,
        file=None,
        database=root / "staff_context.db",
        guild_id=GUILD_ID,
        dry_run=dry_run,
        archive_completed=False,
        archive_folder=root / "archive",
        channel_id=None,
        channel_name=None,
    )


class StaffContextImporterTests(unittest.TestCase):
    def test_imports_content_into_separate_schema_and_dedupes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "Bro Eden - staff-chat [12345].csv"
            path.write_text(
                "AuthorID,Author,Date,Content,Attachments,Reactions\n"
                "100,Staff One,2026-06-20T01:02:03Z,"
                "\"Private staff context\",,\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(":memory:")
            fts_available = ensure_schema(connection)
            args = importer_args(root)

            first = process_file(connection, path, args)
            second = process_file(connection, path, args)

            self.assertEqual(first.imported, 1)
            self.assertEqual(second.duplicates, 1)
            row = connection.execute(
                """
                SELECT guild_id, channel_id, channel_name, author_id, content,
                       message_id, source, stored_at
                FROM staff_context_messages
                """
            ).fetchone()
            self.assertEqual(
                row,
                (
                    GUILD_ID,
                    12345,
                    "staff-chat",
                    100,
                    "Private staff context",
                    None,
                    "imported_csv",
                    row[7],
                ),
            )
            self.assertTrue(row[7])
            activity_table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'stats_message_activity'
                """
            ).fetchone()
            self.assertIsNone(activity_table)
            if fts_available:
                fts_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM staff_context_fts
                    WHERE staff_context_fts MATCH 'private'
                    """
                ).fetchone()[0]
                self.assertEqual(fts_count, 1)
            connection.close()

    def test_dry_run_does_not_insert_content(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "staff.csv"
            path.write_text(
                "AuthorID,Author,Date,Content,Attachments,Reactions\n"
                "100,Staff One,06/20/2026 01:02 PM,Private text,,\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(":memory:")
            ensure_schema(connection)

            result = process_file(connection, path, importer_args(root, dry_run=True))

            self.assertEqual(result.imported, 1)
            count = connection.execute(
                "SELECT COUNT(*) FROM staff_context_messages"
            ).fetchone()[0]
            self.assertEqual(count, 0)
            connection.close()

    def test_archive_and_broken_folders_are_skipped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "archive").mkdir()
            (root / "archive" / "old.csv").write_text("", encoding="utf-8")
            (root / "broken").mkdir()
            (root / "broken" / "bad.csv").write_text("", encoding="utf-8")
            active = root / "active.csv"
            active.write_text("", encoding="utf-8")

            files = input_files(importer_args(root))

            self.assertEqual(files, [active])

    def test_channel_metadata_is_inferred_from_export_filename(self):
        channel_id, channel_name = infer_channel(
            Path("Bro Eden - leadership [987654].csv")
        )
        self.assertEqual(channel_id, 987654)
        self.assertEqual(channel_name, "leadership")

    def test_headquarters_exports_are_normalized_to_staff_channel(self):
        channel_id, channel_name = infer_channel(
            Path("Bro Eden - Headquarters Chat [123456].csv")
        )
        self.assertEqual(channel_id, 123456)
        self.assertEqual(channel_name, "staff")

    def test_logs_exports_are_normalized_to_staff_channel(self):
        channel_id, channel_name = infer_channel(
            Path("Bro Eden - Moderation Logs [654321].csv")
        )
        self.assertEqual(channel_id, 654321)
        self.assertEqual(channel_name, "staff")

    def test_hoarders_island_exports_are_normalized_to_archived_channel(self):
        filenames = (
            "Bro Eden - Hoarders Island - old-staff [777888].csv",
            "Bro Eden - Hoarder's Island - old-staff [777888].csv",
            "Bro Eden - Hoarder’s Island - old-staff [777888].csv",
        )
        for filename in filenames:
            with self.subTest(filename=filename):
                channel_id, channel_name = infer_channel(Path(filename))
                self.assertEqual(channel_id, 777888)
                self.assertEqual(channel_name, "archived")

    def test_search_helpers_drop_question_filler_and_redact_secrets(self):
        self.assertEqual(
            fts_query("What did we decide about verification?"),
            '"decide" OR "verification"',
        )
        excerpt = short_excerpt(
            "GEMINI_API_KEY=do-not-show Authorization: Bearer secret-token"
        )
        self.assertNotIn("do-not-show", excerpt)
        self.assertNotIn("secret-token", excerpt)
        self.assertIn("[REDACTED]", excerpt)

    def test_access_is_limited_to_configured_roles_or_owner_ids(self):
        self.assertTrue(has_staff_ai_access(10, [], set(), {10}))
        self.assertTrue(has_staff_ai_access(20, [200], {200}, set()))
        self.assertFalse(has_staff_ai_access(30, [300], {200}, {10}))

    def test_legacy_import_schema_migrates_to_shared_source_table(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            """
            CREATE TABLE staff_context_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER,
                channel_name TEXT NOT NULL,
                author_id INTEGER NOT NULL,
                author_name TEXT,
                timestamp TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                source_file TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                dedupe_key TEXT NOT NULL,
                imported_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO staff_context_messages (
                guild_id, channel_id, channel_name, author_id, author_name,
                timestamp, content, content_hash, source_file, row_number,
                dedupe_key, imported_at
            ) VALUES (1, 2, 'staff', 3, 'Staff', '2026-06-20T00:00:00+00:00',
                      'context', 'hash', 'staff.csv', 2, 'dedupe',
                      '2026-06-21T00:00:00+00:00')
            """
        )

        ensure_schema(connection)

        row = connection.execute(
            """
            SELECT source, message_id, source_file, row_number, stored_at
            FROM staff_context_messages
            """
        ).fetchone()
        self.assertEqual(
            row,
            (
                "imported_csv",
                None,
                "staff.csv",
                2,
                "2026-06-21T00:00:00+00:00",
            ),
        )
        connection.close()


class StaffContextLiveTrackingTests(unittest.IsolatedAsyncioTestCase):
    class Dummy:
        def __init__(self, **values):
            self.__dict__.update(values)

        def __str__(self):
            return str(getattr(self, "name", "dummy"))

    async def asyncSetUp(self):
        self.connection = await aiosqlite.connect(":memory:")
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute(STAFF_CONTEXT_TABLE_SQL)
        await self.connection.commit()
        self.cog = StaffAI.__new__(StaffAI)
        self.cog.db = self.connection
        self.cog.live_tracking_enabled = True
        self.cog.track_deletes = True
        self.cog.tracked_channel_ids = {200}
        self.cog.fts_available = False

    async def asyncTearDown(self):
        await self.connection.close()

    def message(self, content="live context", *, message_id=300):
        return self.Dummy(
            guild=self.Dummy(id=100),
            channel=self.Dummy(id=200, name="staff-chat"),
            author=self.Dummy(id=400, display_name="Staff One", bot=False),
            webhook_id=None,
            content=content,
            attachments=[],
            id=message_id,
            created_at=dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc),
            edited_at=None,
        )

    async def test_live_message_is_deduped_edited_and_soft_deleted(self):
        message = self.message("TOKEN=secret-value live context")

        await self.cog.on_message(message)
        await self.cog.on_message(message)

        row = (
            await self.connection.execute_fetchall(
                """
                SELECT message_id, source, content, deleted
                FROM staff_context_messages
                """
            )
        )[0]
        self.assertEqual(row["message_id"], 300)
        self.assertEqual(row["source"], "live_discord")
        self.assertNotIn("secret-value", row["content"])
        self.assertEqual(row["deleted"], 0)

        edited = self.message("updated live context")
        edited.edited_at = dt.datetime(
            2026,
            6,
            22,
            1,
            tzinfo=dt.timezone.utc,
        )
        await self.cog.on_message_edit(message, edited)
        await self.cog.on_message_delete(edited)

        updated = (
            await self.connection.execute_fetchall(
                """
                SELECT content, edited_at, deleted, deleted_at
                FROM staff_context_messages
                """
            )
        )[0]
        self.assertEqual(updated["content"], "updated live context")
        self.assertTrue(updated["edited_at"])
        self.assertEqual(updated["deleted"], 1)
        self.assertTrue(updated["deleted_at"])

    async def test_untracked_and_webhook_messages_are_ignored(self):
        untracked = self.message(message_id=301)
        untracked.channel.id = 999
        webhook = self.message(message_id=302)
        webhook.webhook_id = 55

        await self.cog.on_message(untracked)
        await self.cog.on_message(webhook)

        count = (
            await self.connection.execute_fetchall(
                "SELECT COUNT(*) AS count FROM staff_context_messages"
            )
        )[0]["count"]
        self.assertEqual(count, 0)

    async def test_configured_parent_channel_tracks_thread_messages(self):
        parent = self.Dummy(id=200, name="staff-chat")
        thread = self.Dummy(id=201, name="staff-thread", parent=parent)
        message = self.message("thread context", message_id=304)
        message.channel = thread

        await self.cog.on_message(message)

        count = (
            await self.connection.execute_fetchall(
                "SELECT COUNT(*) AS count FROM staff_context_messages"
            )
        )[0]["count"]
        self.assertEqual(count, 1)

    async def test_search_can_filter_imported_and_live_sources(self):
        await self.cog.on_message(self.message("shared topic", message_id=303))
        await self.connection.execute(
            """
            INSERT INTO staff_context_messages (
                guild_id, channel_id, channel_name, author_id, author_name,
                timestamp, content, content_hash, source, source_file,
                row_number, dedupe_key, stored_at
            ) VALUES (
                100, 200, 'staff-chat', 401, 'Staff Two',
                '2026-06-21T00:00:00+00:00', 'shared topic', 'hash',
                'imported_csv', 'staff.csv', 2, 'imported-dedupe',
                '2026-06-22T00:00:00+00:00'
            )
            """
        )
        await self.connection.commit()

        imported = await self.cog._search(
            100,
            "shared topic",
            limit=10,
            source="imported_csv",
        )
        live = await self.cog._search(
            100,
            "shared topic",
            limit=10,
            source="live_discord",
        )

        self.assertEqual([row["source"] for row in imported], ["imported_csv"])
        self.assertEqual([row["source"] for row in live], ["live_discord"])


if __name__ == "__main__":
    unittest.main()
