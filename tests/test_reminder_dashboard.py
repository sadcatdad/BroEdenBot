import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from dashboard.reminders_manager import (
    list_reminders,
    queue_reminder_action,
    reminder_detail,
    reminder_overview,
)
from utils.reminder_service import initialize_schema_sync
from utils.sqlite import configure_sync_connection


class ReminderDashboardManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "data.db"
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        configure_sync_connection(connection)
        initialize_schema_sync(connection)
        now = datetime(2035, 7, 14, tzinfo=timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO reminder_items (
                reminder_type, guild_id, creator_user_id, title, description,
                destination_channel_id, scheduled_at_utc, interpretation_timezone,
                status, created_at_utc, updated_at_utc
            ) VALUES ('event', '123', '10', 'Movie Night', 'Bottoms', '456', ?,
                      'America/Chicago', 'upcoming', ?, ?)
            """,
            (now, now, now),
        )
        self.reminder_id = int(connection.execute("SELECT id FROM reminder_items").fetchone()[0])
        connection.execute(
            """
            INSERT INTO reminder_occurrences (
                reminder_id, occurrence_index, scheduled_at_utc, status, created_at_utc
            ) VALUES (?, 0, ?, 'upcoming', ?)
            """,
            (self.reminder_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO reminder_subscriptions (
                reminder_id, user_id, status, created_at_utc, updated_at_utc
            ) VALUES (?, '42', 'active', ?, ?)
            """,
            (self.reminder_id, now, now),
        )
        connection.commit()
        connection.close()
        self.path_patch = patch("dashboard.reminders_manager.find_database_path", return_value=self.path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def test_overview_and_filters_are_guild_scoped(self):
        overview = reminder_overview(guild_id="123")
        self.assertEqual(overview["upcoming"], 1)
        self.assertEqual(overview["active_subscriptions"], 1)
        self.assertEqual(list_reminders(guild_id="999"), [])
        rows = list_reminders(guild_id="123", reminder_type="event", recurrence="once")
        self.assertEqual(rows[0]["title"], "Movie Night")

    def test_detail_includes_occurrences_subscriptions_deliveries_and_audit(self):
        detail = reminder_detail(self.reminder_id, guild_id="123")
        self.assertEqual(detail["reminder"]["title"], "Movie Night")
        self.assertEqual(len(detail["occurrences"]), 1)
        self.assertEqual(len(detail["subscriptions"]), 1)
        self.assertEqual(detail["deliveries"], [])

    def test_actions_are_queued_not_applied_directly(self):
        action_id = queue_reminder_action(
            self.reminder_id,
            action="cancel",
            requested_by="dashboard-admin",
            guild_id="123",
            payload={"reason": "Weather"},
        )
        connection = sqlite3.connect(self.path)
        action = connection.execute(
            "SELECT status, action FROM reminder_dashboard_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
        status = connection.execute(
            "SELECT status FROM reminder_items WHERE id = ?",
            (self.reminder_id,),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(action, ("pending", "cancel"))
        self.assertEqual(status, "upcoming")

    def test_action_rejects_cross_guild_and_invalid_mutation(self):
        with self.assertRaisesRegex(ValueError, "selected guild"):
            queue_reminder_action(
                self.reminder_id,
                action="cancel",
                requested_by="admin",
                guild_id="999",
            )
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            queue_reminder_action(
                self.reminder_id,
                action="delete-everything",
                requested_by="admin",
                guild_id="123",
            )


if __name__ == "__main__":
    unittest.main()
