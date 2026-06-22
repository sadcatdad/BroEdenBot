import argparse
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Optional

from scripts.import_discord_history import (
    archive_completed_file,
    csv_messages,
    ensure_schema,
    message_fields,
    process_file,
)


GUILD_ID = 1278253523619807233


def importer_args(
    database: Path,
    *,
    dry_run: bool = False,
    channel_id: Optional[int] = None,
    channel_name: Optional[str] = None,
    archive_completed: bool = False,
    archive_duplicates: bool = False,
    archive_folder: Optional[Path] = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        file=None,
        folder=None,
        guild_id=GUILD_ID,
        database=database,
        channel_id=channel_id,
        channel_name=channel_name,
        dry_run=dry_run,
        source="imported",
        archive_completed=archive_completed,
        archive_duplicates=archive_duplicates,
        archive_folder=archive_folder or database.parent / "archive",
    )


class DiscordHistoryCSVTests(unittest.TestCase):
    def test_filename_channel_id_is_used_when_csv_has_no_channel_column(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = (
                Path(temporary_directory)
                / "Server - general [300].csv"
            )
            path.write_text(
                "ID,Date,User ID,Author\n"
                "1,2026-06-20T01:00:00Z,10,First\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(":memory:")
            ensure_schema(connection)
            result = process_file(
                connection,
                path,
                importer_args(Path(temporary_directory) / "data.db"),
                str(uuid.uuid4()),
            )
            self.assertEqual(result.channel_id, 300)
            connection.close()

    def test_csv_aliases_are_normalized_without_retaining_content(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "channel.csv"
            path.write_text(
                "Message ID,Created At,Author ID,Display Name,"
                "Channel ID,Channel Name,Is Bot,Content\n"
                "100,2026-06-20T01:02:03Z,200,Example User,"
                "300,example-channel,false,private message text\n",
                encoding="utf-8",
            )

            messages, metadata = csv_messages(path)
            message = next(messages)
            parsed = message_fields(message, 0, "fallback")

            self.assertEqual(metadata, {})
            self.assertNotIn("private message text", repr(message))
            self.assertEqual(
                parsed,
                (
                    "100",
                    parsed[1],
                    200,
                    "Example User",
                    "Example User",
                    300,
                    "example-channel",
                ),
            )
            self.assertEqual(parsed[1].isoformat(), "2026-06-20T01:02:03+00:00")

    def test_missing_message_id_uses_hashed_deterministic_id(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "vent-summit.csv"
            path.write_text(
                "Timestamp,User ID,Username,Message\n"
                "2026-06-20T02:00:00Z,200,Example,secret words\n",
                encoding="utf-8",
            )

            messages, _ = csv_messages(path)
            message = next(messages)
            first = message_fields(message, 300, "vent-summit")
            second = message_fields(message, 300, "vent-summit")

            self.assertEqual(first[0], second[0])
            self.assertTrue(
                first[0].startswith(
                    "activity_csv:300:vent-summit.csv:2::200:"
                )
            )
            self.assertNotIn("secret", first[0])

    def test_dotted_and_camel_case_csv_aliases_are_supported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "aliases.csv"
            path.write_text(
                "MessageId,Timestamp,user.id,author.username,"
                "channelId,channelName,user.isBot\n"
                "100,2026-06-20T01:02:03Z,200,Example,"
                "300,example-channel,false\n",
                encoding="utf-8",
            )

            messages, _ = csv_messages(path)
            parsed = message_fields(next(messages), 0, "fallback")

            self.assertEqual(parsed[0], "100")
            self.assertEqual(parsed[2], 200)
            self.assertEqual(parsed[3], "Example")
            self.assertEqual(parsed[5], 300)
            self.assertEqual(parsed[6], "example-channel")

    def test_malformed_rows_are_skipped_and_later_rows_continue(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "mixed.csv"
            database = root / "data.db"
            path.write_text(
                "ID,Date,User ID,Author,Channel ID,Channel\n"
                "1,2026-06-20T01:00:00Z,10,First,20,general\n"
                "malformed,row,with,too,many,columns,here\n"
                "2,2026-06-20T02:00:00Z,11,Second,20,general\n"
                "3,2026-06-20T03:00:00Z,,Missing ID,20,general\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(database)
            ensure_schema(connection)

            result = process_file(
                connection,
                path,
                importer_args(database),
                str(uuid.uuid4()),
            )

            self.assertEqual(result.messages_seen, 4)
            self.assertEqual(result.messages_imported, 2)
            self.assertEqual(result.messages_skipped, 2)
            self.assertEqual(
                connection.execute(
                    "SELECT SUM(message_count) FROM stats_message_activity"
                ).fetchone()[0],
                2,
            )
            connection.close()

    def test_csv_dry_run_does_not_write_or_move_the_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "dry-run.csv"
            database = root / "missing.db"
            archive_folder = root / "archive"
            path.write_text(
                "ID,Date,User ID,Author\n"
                "1,2026-06-20T01:00:00Z,10,First\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(":memory:")
            args = importer_args(
                database,
                dry_run=True,
                archive_completed=True,
                archive_duplicates=True,
                archive_folder=archive_folder,
            )

            result = process_file(
                connection,
                path,
                args,
                str(uuid.uuid4()),
            )

            self.assertEqual(result.messages_imported, 1)
            self.assertTrue(
                archive_completed_file(path, result, args)
            )
            self.assertTrue(path.exists())
            self.assertFalse(archive_folder.exists())
            self.assertFalse(database.exists())
            connection.close()

    def test_csv_import_dedupes_ids_and_fallback_ids_on_rerun(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "dedupe.csv"
            database = root / "data.db"
            path.write_text(
                "messageId,Timestamp,author.id,author.name,"
                "channel.id,channel.name,Bot,Content\n"
                "500,2026-06-20T01:05:00Z,10,First,20,general,false,one\n"
                ",2026-06-20T01:15:00Z,11,Second,20,general,false,two\n"
                "501,2026-06-20T01:25:00Z,12,Bot User,20,general,true,three\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(database)
            ensure_schema(connection)
            args = importer_args(database)

            first = process_file(
                connection,
                path,
                args,
                str(uuid.uuid4()),
            )
            second = process_file(
                connection,
                path,
                args,
                str(uuid.uuid4()),
            )

            self.assertEqual(first.messages_imported, 2)
            self.assertEqual(first.messages_skipped, 1)
            self.assertEqual(second.messages_imported, 0)
            self.assertEqual(second.duplicates_skipped, 2)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM stats_activity_imported_messages"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT SUM(message_count) FROM stats_message_activity"
                ).fetchone()[0],
                2,
            )
            connection.close()

    def test_csv_archive_rules_match_completed_and_duplicate_imports(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive_folder = root / "archive"
            database = root / "data.db"
            args = importer_args(
                database,
                archive_completed=True,
                archive_duplicates=True,
                archive_folder=archive_folder,
            )

            imported_path = root / "imported.csv"
            imported_path.write_text("header\n", encoding="utf-8")
            imported_result = type(
                "Result",
                (),
                {
                    "status": "completed",
                    "messages_imported": 1,
                    "duplicates_skipped": 0,
                },
            )()
            self.assertTrue(
                archive_completed_file(imported_path, imported_result, args)
            )
            self.assertFalse(imported_path.exists())
            self.assertTrue((archive_folder / "imported.csv").exists())

            duplicate_path = root / "duplicate.csv"
            duplicate_path.write_text("header\n", encoding="utf-8")
            duplicate_result = type(
                "Result",
                (),
                {
                    "status": "completed",
                    "messages_imported": 0,
                    "duplicates_skipped": 2,
                },
            )()
            self.assertTrue(
                archive_completed_file(duplicate_path, duplicate_result, args)
            )
            self.assertFalse(duplicate_path.exists())
            self.assertTrue((archive_folder / "duplicate.csv").exists())


if __name__ == "__main__":
    unittest.main()
