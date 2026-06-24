import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.discord_metadata import channel_matches_selection
from dashboard.users import initialize_dashboard_users
from utils.settings import get_setting, initialize_settings_from_env


class DashboardNavigationMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database),
                "GUILD_ID": "123456789012345678",
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "admin",
                "DASHBOARD_PASSWORD": "test-password",
                "DASHBOARD_SECRET_KEY": "test-session-signing-key",
            },
            clear=False,
        )
        self.environment.start()
        initialize_settings_from_env()
        initialize_dashboard_users()
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

    def create_discord_snapshot(self):
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE dashboard_discord_roles (
                id TEXT PRIMARY KEY,
                name TEXT,
                position INTEGER
            );
            CREATE TABLE dashboard_discord_categories (
                id TEXT PRIMARY KEY,
                name TEXT,
                position INTEGER
            );
            CREATE TABLE dashboard_discord_channels (
                id TEXT PRIMARY KEY,
                name TEXT,
                type TEXT,
                parent_id TEXT,
                position INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO dashboard_discord_roles VALUES (?, ?, ?)",
            ("111111111111111111", "Staff", 1),
        )
        connection.execute(
            "INSERT INTO dashboard_discord_categories VALUES (?, ?, ?)",
            ("222222222222222222", "Tickets", 2),
        )
        connection.execute(
            "INSERT INTO dashboard_discord_channels VALUES (?, ?, ?, ?, ?)",
            (
                "333333333333333333",
                "help-desk",
                "text",
                "222222222222222222",
                3,
            ),
        )
        connection.commit()
        connection.close()

    def test_top_level_nav_and_settings_sidebar_labels_render(self):
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for label in ("Overview", "Operations", "Analytics", "Bank", "Settings"):
            self.assertIn(label, response.text)
        self.assertNotIn(">Stats</a>", response.text)
        self.assertNotIn(">Knowledge</a>", response.text)
        self.assertNotIn(">Users</a>", response.text)

        settings = self.client.get("/settings")
        self.assertEqual(settings.status_code, 200)
        for label in (
            "Bot Configuration",
            "Permissions &amp; Access",
            "Discord Roles &amp; Channels",
            "Knowledge Base",
            "Imports",
            "Dashboard Users",
            "Advanced",
        ):
            self.assertIn(label, settings.text)

    def test_moved_pages_have_old_url_redirects(self):
        self.login()
        redirects = {
            "/stats": "http://testserver/analytics/stats",
            "/knowledge": "http://testserver/settings/knowledge",
            "/imports": "http://testserver/settings/imports",
            "/users": "http://testserver/settings/users",
        }
        for old_path, new_path in redirects.items():
            response = self.client.get(old_path, follow_redirects=False)
            self.assertEqual(response.status_code, 303, old_path)
            self.assertEqual(response.headers["location"], new_path)

    def test_analytics_sidebar_includes_requested_sections(self):
        self.login()
        response = self.client.get("/analytics")
        self.assertEqual(response.status_code, 200)
        for label in (
            "Overview",
            "Activity Analytics",
            "Stats Graphics",
            "VC Analytics",
            "Exports",
        ):
            self.assertIn(label, response.text)

    def test_discord_metadata_api_uses_local_snapshot_and_requires_auth(self):
        self.create_discord_snapshot()
        unauthenticated = self.client.get("/api/discord/guild-structure", follow_redirects=False)
        self.assertEqual(unauthenticated.status_code, 303)

        self.login()
        roles = self.client.get("/api/discord/roles")
        channels = self.client.get("/api/discord/channels")
        categories = self.client.get("/api/discord/categories")
        structure = self.client.get("/api/discord/guild-structure")
        self.assertEqual(roles.status_code, 200)
        self.assertEqual(channels.status_code, 200)
        self.assertEqual(categories.status_code, 200)
        self.assertEqual(structure.status_code, 200)
        self.assertEqual(roles.json()[0]["name"], "Staff")
        self.assertEqual(channels.json()[0]["parent_id"], "222222222222222222")
        self.assertEqual(categories.json()[0]["type"], "category")
        self.assertIn("roles", structure.json())

    def test_json_settings_save_and_stale_ids_are_preserved(self):
        self.login()
        page = self.client.get("/settings/discord")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/settings/update",
            data={
                "key": "analytics_excluded_category_ids",
                "value": '["444444444444444444"]',
                "csrf": token,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(get_setting("analytics_excluded_category_ids"), '["444444444444444444"]')

        categories = self.client.get("/api/discord/categories").json()
        self.assertEqual(categories[0]["name"], "Missing: 444444444444444444")
        self.assertTrue(categories[0]["missing"])

    def test_category_selection_matches_child_channels(self):
        self.assertTrue(
            channel_matches_selection(
                "333333333333333333",
                "222222222222222222",
                channel_ids=[],
                category_ids=["222222222222222222"],
            )
        )
        self.assertTrue(
            channel_matches_selection(
                "333333333333333333",
                None,
                channel_ids=["333333333333333333"],
                category_ids=[],
            )
        )
        self.assertFalse(
            channel_matches_selection(
                "333333333333333333",
                "222222222222222222",
                channel_ids=[],
                category_ids=["555555555555555555"],
            )
        )


if __name__ == "__main__":
    unittest.main()
