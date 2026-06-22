import argparse
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.import_vc_logs import archive_file, process_file
from utils.vc_history import ensure_vc_history_schema


GUILD_ID = 1278253523619807233
LOG_CHANNEL_ID = 1278274747913867347


def importer_args(
    root: Path,
    *,
    dry_run: bool = False,
    close_open: bool = True,
    archive_completed: bool = False,
    archive_duplicates: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        guild_id=GUILD_ID,
        log_channel_id=LOG_CHANNEL_ID,
        dry_run=dry_run,
        close_open_at_export_end=close_open,
        min_session_seconds=10,
        max_session_hours=24,
        archive_completed=archive_completed,
        archive_duplicates=archive_duplicates,
        archive_folder=root / "archive",
    )


def event_message(
    message_id: int,
    timestamp: str,
    description: str,
    *,
    user_id: int = 10,
    user_name: str = "Example User",
) -> dict:
    return {
        "id": str(message_id),
        "timestamp": timestamp,
        "author": {
            "id": "999",
            "name": "Logger",
            "isBot": True,
        },
        "content": "",
        "mentions": [
            {
                "id": str(user_id),
                "name": user_name,
            }
        ],
        "embeds": [
            {
                "title": "Voice update",
                "description": description,
                "fields": [],
            }
        ],
    }


def write_export(path: Path, messages: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "channel": {
                    "id": str(LOG_CHANNEL_ID),
                    "name": "vc-log",
                },
                "messages": messages,
            }
        ),
        encoding="utf-8",
    )


class VCLogImporterTests(unittest.TestCase):
    def test_carl_embed_footer_id_and_name_only_channel_are_supported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export = root / "vc-log.json"
            write_export(
                export,
                [
                    {
                        "id": "1",
                        "timestamp": "2025-01-01T00:00:00Z",
                        "author": {"id": "999", "isBot": True},
                        "content": "",
                        "mentions": [],
                        "embeds": [
                            {
                                "title": "Member joined voice channel",
                                "description": "**Example User** joined #🎮・gaming",
                                "fields": [],
                                "footer": {"text": "ID: 10"},
                            }
                        ],
                    },
                    {
                        "id": "2",
                        "timestamp": "2025-01-01T01:00:00Z",
                        "author": {"id": "999", "isBot": True},
                        "content": "",
                        "mentions": [],
                        "embeds": [
                            {
                                "title": "Member left voice channel",
                                "description": "**Example User** left #🎮・gaming",
                                "fields": [],
                                "footer": {"text": "ID: 10"},
                            }
                        ],
                    },
                ],
            )
            connection = sqlite3.connect(":memory:")
            ensure_vc_history_schema(connection)

            result = process_file(
                connection,
                export,
                importer_args(root),
            )

            self.assertEqual(result.sessions_imported, 1)
            self.assertEqual(
                connection.execute(
                    """
                    SELECT user_id, voice_channel_name, confidence
                    FROM vc_imported_sessions
                    """
                ).fetchone(),
                (10, "🎮・gaming", "medium"),
            )
            connection.close()

    def test_join_then_leave_reconstructs_one_session(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export = root / "vc-log.json"
            write_export(
                export,
                [
                    event_message(
                        1,
                        "2025-01-01T00:00:00Z",
                        "<@10> joined voice channel <#20>",
                    ),
                    event_message(
                        2,
                        "2025-01-01T01:00:00Z",
                        "<@10> left voice channel <#20>",
                    ),
                ],
            )
            connection = sqlite3.connect(":memory:")
            ensure_vc_history_schema(connection)

            result = process_file(
                connection,
                export,
                importer_args(root),
            )

            self.assertFalse(result.failed)
            self.assertEqual(result.sessions_reconstructed, 1)
            self.assertEqual(result.sessions_imported, 1)
            self.assertEqual(
                connection.execute(
                    """
                    SELECT user_id, voice_channel_id, duration_seconds,
                           confidence
                    FROM vc_imported_sessions
                    """
                ).fetchone(),
                (10, 20, 3600, "high"),
            )
            connection.close()

    def test_join_move_leave_reconstructs_two_sessions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export = root / "vc-log.json"
            write_export(
                export,
                [
                    event_message(
                        1,
                        "2025-01-01T00:00:00Z",
                        "<@10> joined voice channel <#20>",
                    ),
                    event_message(
                        2,
                        "2025-01-01T01:00:00Z",
                        "<@10> moved from <#20> to <#21>",
                    ),
                    event_message(
                        3,
                        "2025-01-01T02:00:00Z",
                        "<@10> left voice channel <#21>",
                    ),
                ],
            )
            connection = sqlite3.connect(":memory:")
            ensure_vc_history_schema(connection)

            result = process_file(
                connection,
                export,
                importer_args(root),
            )

            self.assertEqual(result.sessions_reconstructed, 2)
            self.assertEqual(result.sessions_imported, 2)
            self.assertEqual(
                connection.execute(
                    """
                    SELECT voice_channel_id, duration_seconds
                    FROM vc_imported_sessions
                    ORDER BY joined_at
                    """
                ).fetchall(),
                [(20, 3600), (21, 3600)],
            )
            connection.close()

    def test_leave_without_join_is_unmatched(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export = root / "vc-log.json"
            write_export(
                export,
                [
                    event_message(
                        1,
                        "2025-01-01T01:00:00Z",
                        "<@10> left voice channel <#20>",
                    )
                ],
            )
            connection = sqlite3.connect(":memory:")
            ensure_vc_history_schema(connection)

            result = process_file(
                connection,
                export,
                importer_args(root),
            )

            self.assertEqual(result.unmatched_leaves, 1)
            self.assertEqual(result.sessions_imported, 0)
            connection.close()

    def test_open_join_closes_at_export_end_when_enabled(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export = root / "vc-log.json"
            write_export(
                export,
                [
                    event_message(
                        1,
                        "2025-01-01T00:00:00Z",
                        "<@10> joined voice channel <#20>",
                    ),
                    event_message(
                        2,
                        "2025-01-01T01:00:00Z",
                        "<@11> left voice channel <#21>",
                        user_id=11,
                        user_name="Other User",
                    ),
                ],
            )
            connection = sqlite3.connect(":memory:")
            ensure_vc_history_schema(connection)

            result = process_file(
                connection,
                export,
                importer_args(root, close_open=True),
            )

            self.assertEqual(result.open_sessions_closed, 1)
            self.assertEqual(result.sessions_imported, 1)
            self.assertEqual(
                connection.execute(
                    """
                    SELECT duration_seconds, is_estimated, close_reason
                    FROM vc_imported_sessions
                    """
                ).fetchone(),
                (3600, 1, "closed_at_export_end"),
            )
            connection.close()

    def test_open_join_is_not_imported_when_close_disabled(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export = root / "vc-log.json"
            write_export(
                export,
                [
                    event_message(
                        1,
                        "2025-01-01T00:00:00Z",
                        "<@10> joined voice channel <#20>",
                    )
                ],
            )
            connection = sqlite3.connect(":memory:")
            ensure_vc_history_schema(connection)

            result = process_file(
                connection,
                export,
                importer_args(root, close_open=False),
            )

            self.assertEqual(result.open_sessions_unclosed, 1)
            self.assertEqual(result.sessions_imported, 0)
            connection.close()

    def test_reimport_skips_duplicate_session(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export = root / "vc-log.json"
            write_export(
                export,
                [
                    event_message(
                        1,
                        "2025-01-01T00:00:00Z",
                        "<@10> joined voice channel <#20>",
                    ),
                    event_message(
                        2,
                        "2025-01-01T01:00:00Z",
                        "<@10> left voice channel <#20>",
                    ),
                ],
            )
            connection = sqlite3.connect(":memory:")
            ensure_vc_history_schema(connection)
            args = importer_args(root)

            first = process_file(connection, export, args)
            second = process_file(connection, export, args)

            self.assertEqual(first.sessions_imported, 1)
            self.assertEqual(second.sessions_imported, 0)
            self.assertEqual(second.duplicates_skipped, 1)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM vc_imported_sessions"
                ).fetchone()[0],
                1,
            )
            connection.close()

    def test_dry_run_does_not_write_or_archive(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export = root / "vc-log.json"
            write_export(
                export,
                [
                    event_message(
                        1,
                        "2025-01-01T00:00:00Z",
                        "<@10> joined voice channel <#20>",
                    ),
                    event_message(
                        2,
                        "2025-01-01T01:00:00Z",
                        "<@10> left voice channel <#20>",
                    ),
                ],
            )
            connection = sqlite3.connect(":memory:")
            ensure_vc_history_schema(connection)
            args = importer_args(
                root,
                dry_run=True,
                archive_completed=True,
                archive_duplicates=True,
            )

            result = process_file(connection, export, args)
            archived = archive_file(export, result, args)

            self.assertEqual(result.would_import, 1)
            self.assertEqual(result.sessions_imported, 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM vc_imported_sessions"
                ).fetchone()[0],
                0,
            )
            self.assertIsNone(archived)
            self.assertTrue(export.exists())
            self.assertFalse((root / "archive").exists())
            connection.close()

    def test_duplicate_only_file_archives_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export = root / "vc-log.json"
            write_export(
                export,
                [
                    event_message(
                        1,
                        "2025-01-01T00:00:00Z",
                        "<@10> joined voice channel <#20>",
                    ),
                    event_message(
                        2,
                        "2025-01-01T01:00:00Z",
                        "<@10> left voice channel <#20>",
                    ),
                ],
            )
            connection = sqlite3.connect(":memory:")
            ensure_vc_history_schema(connection)
            first_args = importer_args(root)
            process_file(connection, export, first_args)

            duplicate_args = importer_args(
                root,
                archive_completed=True,
                archive_duplicates=False,
            )
            duplicate = process_file(connection, export, duplicate_args)
            self.assertIsNone(
                archive_file(export, duplicate, duplicate_args)
            )
            self.assertTrue(export.exists())

            duplicate_args.archive_duplicates = True
            archived = archive_file(export, duplicate, duplicate_args)
            self.assertIsNotNone(archived)
            self.assertFalse(export.exists())
            self.assertTrue(archived.exists())
            connection.close()


if __name__ == "__main__":
    unittest.main()
