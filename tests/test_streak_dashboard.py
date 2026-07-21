import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.streaks_manager import initialize_streak_dashboard_schema
from dashboard.users import initialize_dashboard_users
from utils.settings import initialize_settings_from_env


class StreakDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database),
                "GUILD_ID": "123",
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "admin",
                "DASHBOARD_PASSWORD": "test-password",
                "DASHBOARD_SECRET_KEY": "test-session-signing-key",
                "STREAK_TIMEZONE": "America/Chicago",
            },
            clear=False,
        )
        self.environment.start()
        initialize_settings_from_env()
        initialize_dashboard_users()
        initialize_streak_dashboard_schema()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def login(self):
        page = self.client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": "test-password",
                "csrf": token,
            },
        )
        self.assertEqual(response.status_code, 200)

    def csrf(self) -> str:
        page = self.client.get("/streaks")
        return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def today(self) -> str:
        return datetime.now(ZoneInfo("America/Chicago")).date().isoformat()

    def test_streaks_page_requires_auth_and_is_top_level_navigation(self):
        response = self.client.get("/streaks", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.login()
        response = self.client.get("/streaks")
        self.assertEqual(response.status_code, 200)
        self.assertIn('<span>Streaks</span>', response.text)
        self.assertIn('href="http://testserver/streaks" aria-current="page"', response.text)
        self.assertIn("Restore Streaks", response.text)
        self.assertIn("Manual adjustment", response.text)
        self.assertIn("Recent restore requests", response.text)

    def test_manual_add_and_remove_recompute_from_source_days(self):
        self.login()
        token = self.csrf()
        response = self.client.post(
            "/streaks/adjust",
            data={
                "csrf": token,
                "guild_id": "123",
                "user_id": "42",
                "activity_date": self.today(),
                "action": "add",
                "reason": "Bot missed a verified qualifying message",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT current_streak, longest_streak FROM member_streaks"
                ).fetchone(),
                (1, 1),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM streak_adjustments").fetchone()[0],
                1,
            )

        token = self.csrf()
        response = self.client.post(
            "/streaks/adjust",
            data={
                "csrf": token,
                "guild_id": "123",
                "user_id": "42",
                "activity_date": self.today(),
                "action": "remove",
                "reason": "Correction was entered for the wrong member",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT current_streak, longest_streak FROM member_streaks"
                ).fetchone(),
                (0, 0),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM streak_adjustments").fetchone()[0],
                2,
            )

    def test_restore_button_queues_persistent_request(self):
        self.login()
        response = self.client.post(
            "/streaks/restore",
            data={
                "csrf": self.csrf(),
                "guild_id": "123",
                "start_date": self.today(),
                "end_date": self.today(),
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT request_source, status, requested_by
                    FROM streak_restore_requests
                    """
                ).fetchone(),
                ("dashboard", "pending", "admin"),
            )
        page = self.client.get("/streaks")
        self.assertIn("Restore request #1 queued", page.text)
        self.assertIn("pending", page.text)

    def test_streak_writes_require_csrf(self):
        self.login()
        for path in ("/streaks/restore", "/streaks/adjust"):
            response = self.client.post(path, data={"csrf": "invalid"})
            self.assertEqual(response.status_code, 400)

    def test_analyst_viewer_cannot_open_or_mutate_streaks(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE dashboard_users SET role = 'viewer'")
            connection.commit()
        self.login()
        page = self.client.get("/streaks")
        self.assertEqual(page.status_code, 403)
        token = re.search(
            r'name="csrf" value="([^"]+)"', self.client.get("/").text
        ).group(1)
        response = self.client.post(
            "/streaks/restore",
            data={"csrf": token},
        )
        self.assertEqual(response.status_code, 403)

    def test_feature_settings_consolidate_transferred_configuration(self):
        self.login()
        response = self.client.get("/features/streaks")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Activity Streaks", response.text)
        self.assertIn("DISBOARD Bumps", self.client.get("/features/bumps").text)
        self.assertIn("Reminders", self.client.get("/features/reminders").text)
        self.assertIn("Analytics &amp; Stats", self.client.get("/features/analytics").text)
        for key in (
            "STREAK_RESTORE_ENABLED",
            "STREAK_RESTORE_MAX_DAYS",
            "STREAK_EXCLUDED_CATEGORY_IDS",
            "STREAK_MILESTONE_CHANNEL_ID",
            "STREAK_MILESTONE_ASSET_ID",
        ):
            self.assertIn(key, response.text)
        self.assertIn('setting-key="STREAK_EXCLUDED_CATEGORY_IDS"', response.text)
        self.assertIn('setting-key="STREAK_MILESTONE_CHANNEL_ID"', response.text)
        self.assertIn('name="setting__STREAK_MILESTONE_ASSET_ID"', response.text)
        self.assertNotIn('name="setting__STREAK_MILESTONE_MESSAGE"', response.text)
        token = re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)
        saved = self.client.post(
            "/features/streaks/save",
            data={
                "csrf": token,
                "setting__STREAK_RESTORE_MAX_DAYS": "7",
            },
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(
            saved.headers["location"],
            "http://testserver/features/streaks",
        )


if __name__ == "__main__":
    unittest.main()
