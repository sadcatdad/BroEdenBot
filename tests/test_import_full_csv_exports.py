import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.import_discord_history import ensure_schema as ensure_activity_schema
from scripts.import_full_csv_exports import main
from utils.import_helpers import get_json_activity_imported_channel_ids


GUILD_ID = 1278253523619807233


def write_csv(path: Path, *, author_id: int = 10) -> None:
    path.write_text(
        "AuthorID,Author,Date,Content,Attachments,Reactions\n"
        f"{author_id},Alice,2026-06-20T01:02:03Z,Stored context,,\n",
        encoding="utf-8",
    )


class FullCSVImportTests(unittest.TestCase):
    def _run(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with patch(
            "sys.argv",
            ["import_full_csv_exports.py", *arguments],
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            return main(), output.getvalue()

    def test_json_channel_gets_context_but_skips_activity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            imports = root / "imports"
            imports.mkdir()
            source = imports / "Server - general [111].csv"
            write_csv(source)
            activity_db = root / "data.db"
            context_db = root / "message_context.db"

            connection = sqlite3.connect(activity_db)
            ensure_activity_schema(connection)
            connection.execute(
                """
                INSERT INTO stats_activity_imports (
                    guild_id, import_batch_id, filename, channel_id,
                    channel_name, imported_at, messages_imported, status,
                    source_file, source_format, imported_for_activity
                ) VALUES (?, 'json-batch', ?, 111, 'general', ?, 10,
                          'completed', ?, 'json', 1)
                """,
                (
                    GUILD_ID,
                    "Server - general [111].json",
                    "2026-06-20T00:00:00+00:00",
                    "Server - general [111].json",
                ),
            )
            connection.commit()
            connection.close()

            code, output = self._run(
                "--folder",
                str(imports),
                "--guild-id",
                str(GUILD_ID),
                "--activity-database",
                str(activity_db),
                "--context-database",
                str(context_db),
            )

            self.assertEqual(code, 0)
            self.assertIn("skipped_json_already_imported", output)
            connection = sqlite3.connect(context_db)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM message_context_messages"
                ).fetchone()[0],
                1,
            )
            connection.close()
            connection = sqlite3.connect(activity_db)
            self.assertIsNone(
                connection.execute(
                    "SELECT SUM(message_count) FROM stats_message_activity"
                ).fetchone()[0]
            )
            connection.close()

    def test_new_channel_backfills_activity_and_rerun_dedupes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            imports = root / "imports"
            imports.mkdir()
            source = imports / "Server - new-channel [222].csv"
            write_csv(source)
            activity_db = root / "data.db"
            context_db = root / "message_context.db"
            arguments = (
                "--folder",
                str(imports),
                "--guild-id",
                str(GUILD_ID),
                "--activity-database",
                str(activity_db),
                "--context-database",
                str(context_db),
            )

            first_code, first_output = self._run(*arguments)
            second_code, second_output = self._run(*arguments)

            self.assertEqual((first_code, second_code), (0, 0))
            self.assertIn("Activity status: imported", first_output)
            self.assertIn("counted=1", first_output)
            self.assertIn("Context: seen=1 imported=0 duplicates=1", second_output)
            self.assertIn("counted=0 duplicates=1", second_output)
            connection = sqlite3.connect(activity_db)
            row = connection.execute(
                """
                SELECT SUM(message_count), MIN(source)
                FROM stats_message_activity
                """
            ).fetchone()
            self.assertEqual(row, (1, "csv_backfill"))
            import_row = connection.execute(
                """
                SELECT source_format, imported_for_activity,
                       imported_for_context
                FROM stats_activity_imports
                ORDER BY id ASC LIMIT 1
                """
            ).fetchone()
            self.assertEqual(import_row, ("csv", 1, 1))
            connection.close()

    def test_unknown_channel_context_imports_and_activity_skips(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            imports = root / "imports"
            imports.mkdir()
            write_csv(imports / "channel-without-id.csv")
            code, output = self._run(
                "--folder",
                str(imports),
                "--guild-id",
                str(GUILD_ID),
                "--activity-database",
                str(root / "data.db"),
                "--context-database",
                str(root / "message_context.db"),
            )
            self.assertEqual(code, 0)
            self.assertIn("skipped_no_channel_id", output)
            connection = sqlite3.connect(root / "message_context.db")
            self.assertEqual(
                connection.execute(
                    "SELECT channel_id FROM message_context_messages"
                ).fetchone()[0],
                "unknown",
            )
            connection.close()

    def test_dry_run_writes_nothing_and_detector_supports_legacy_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            imports = root / "imports"
            imports.mkdir()
            write_csv(imports / "Server - dry [333].csv")
            activity_db = root / "data.db"

            connection = sqlite3.connect(activity_db)
            connection.execute(
                """
                CREATE TABLE stats_activity_imports (
                    guild_id INTEGER,
                    filename TEXT,
                    channel_id INTEGER,
                    messages_imported INTEGER,
                    status TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO stats_activity_imports
                VALUES (?, 'Server - legacy [444].json', NULL, 5, 'completed')
                """,
                (GUILD_ID,),
            )
            connection.commit()
            connection.close()
            self.assertEqual(
                get_json_activity_imported_channel_ids(
                    activity_db,
                    guild_id=GUILD_ID,
                ),
                {"444"},
            )

            context_db = root / "message_context.db"
            code, output = self._run(
                "--folder",
                str(imports),
                "--guild-id",
                str(GUILD_ID),
                "--activity-database",
                str(activity_db),
                "--context-database",
                str(context_db),
                "--dry-run",
                "--archive-completed",
            )
            self.assertEqual(code, 0)
            self.assertIn("No database writes or file moves will occur.", output)
            self.assertFalse(context_db.exists())
            self.assertTrue((imports / "Server - dry [333].csv").exists())


if __name__ == "__main__":
    unittest.main()
