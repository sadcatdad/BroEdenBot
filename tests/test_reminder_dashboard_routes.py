import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.app import app
from utils.reminder_service import initialize_schema_sync
from utils.settings import initialize_settings_from_env
from utils.sqlite import configure_sync_connection


class ReminderDashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.path),
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "owner",
                "DASHBOARD_PASSWORD": "owner-password",
                "DASHBOARD_SECRET_KEY": "reminder-route-test-key",
                "DASHBOARD_AUTH_MODE": "password",
            },
            clear=False,
        )
        self.environment.start()
        initialize_settings_from_env()
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        configure_sync_connection(connection)
        initialize_schema_sync(connection)
        now = datetime(2035, 7, 14, tzinfo=timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO reminder_items (
                reminder_type, guild_id, creator_user_id, title, description,
                scheduled_at_utc, interpretation_timezone, status,
                created_at_utc, updated_at_utc
            ) VALUES ('personal', '123', '10', 'Dashboard reminder', '', ?, 'UTC',
                      'upcoming', ?, ?)
            """,
            (now, now, now),
        )
        self.reminder_id = int(connection.execute("SELECT id FROM reminder_items").fetchone()[0])
        connection.commit()
        connection.close()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.temp.cleanup()

    def login(self):
        page = self.client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/login",
            data={"username": "owner", "password": "owner-password", "csrf": csrf},
        )
        self.assertEqual(response.status_code, 200)

    def test_reminder_pages_require_authentication(self):
        response = self.client.get("/operations/reminders", follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def test_admin_can_view_and_queue_guild_scoped_action(self):
        self.login()
        page = self.client.get("/operations/reminders?guild_id=123")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Dashboard reminder", page.text)
        detail = self.client.get(f"/operations/reminders/{self.reminder_id}?guild_id=123")
        self.assertEqual(detail.status_code, 200)
        csrf = re.search(r'name="csrf" value="([^"]+)"', detail.text).group(1)
        response = self.client.post(
            f"/operations/reminders/{self.reminder_id}/action",
            data={
                "csrf": csrf,
                "guild_id": "123",
                "action": "cancel",
                "reason": "Test cancellation",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        connection = sqlite3.connect(self.path)
        action = connection.execute(
            "SELECT action, status FROM reminder_dashboard_actions"
        ).fetchone()
        connection.close()
        self.assertEqual(action, ("cancel", "pending"))


if __name__ == "__main__":
    unittest.main()
