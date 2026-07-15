import json
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
from utils.discord_metadata import save_discord_metadata_snapshot


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
        save_discord_metadata_snapshot(
            guild_id="123456789012345678",
            guild_name="Bro Eden",
            roles=[
                {
                    "id": "111111111111111111",
                    "name": "Staff",
                    "color": "#ff00ff",
                    "position": 10,
                    "managed": False,
                    "mentionable": True,
                    "hoist": True,
                    "member_count": 7,
                    "is_bot_role": False,
                }
            ],
            categories=[
                {
                    "id": "222222222222222222",
                    "name": "Tickets",
                    "position": 2,
                    "child_channel_ids": ["333333333333333333"],
                }
            ],
            channels=[
                {
                    "id": "333333333333333333",
                    "name": "help-desk",
                    "type": "text",
                    "parent_id": "222222222222222222",
                    "parent_name": "Tickets",
                    "position": 3,
                    "nsfw": False,
                    "archived": False,
                    "is_thread": False,
                }
            ],
        )

    def test_top_level_nav_and_settings_sidebar_labels_render(self):
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for label in (
            "Overview",
            "Operations",
            "AI",
            "Knowledge",
            "Analytics",
            "Streaks",
            "Embed Editor",
            "Bank",
            "Settings",
        ):
            self.assertIn(label, response.text)
        self.assertNotIn(">Stats</a>", response.text)
        self.assertNotIn(">Users</a>", response.text)

        settings = self.client.get("/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertIn('class="settings-layout"', settings.text)
        self.assertIn('class="settings-sidebar"', settings.text)
        self.assertIn('class="settings-menu"', settings.text)
        self.assertIn('class="settings-menu-item active"', settings.text)
        self.assertIn('aria-current="page"', settings.text)
        for label in (
            "Bot Configuration",
            "Permissions &amp; Access",
            "Discord Roles &amp; Channels",
            "Feature Settings",
            "Imports",
            "Dashboard Users",
            "Advanced",
        ):
            self.assertIn(label, settings.text)

    def test_embed_editor_create_search_edit_and_feature_picker(self):
        self.login()
        editor = self.client.get("/embeds/new")
        self.assertEqual(editor.status_code, 200)
        self.assertIn("Live Discord preview", editor.text)
        self.assertIn("+ Add button", editor.text)
        self.assertIn("role-single-select", editor.text)
        token = re.search(r'name="csrf" value="([^"]+)"', editor.text).group(1)
        payload = {
            "content": "{role}",
            "embed": {
                "title": "Bump time",
                "description": "Please use `/bump`.",
                "color": "#25b8b8",
                "fields": [],
            },
            "buttons": [],
        }
        saved = self.client.post(
            "/embeds/save",
            data={
                "csrf": token,
                "template_id": "",
                "name": "Bump Reminder",
                "payload_json": json.dumps(payload),
            },
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 303)
        self.assertRegex(saved.headers["location"], r"/embeds/\d+/edit$")

        listing = self.client.get("/embeds?q=Bump&sort=name&order=asc")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("Bump Reminder", listing.text)
        self.assertIn("Date Modified", listing.text)
        self.assertIn("Feature(s)", listing.text)

        template_id = saved.headers["location"].split("/")[-2]
        settings = self.client.get("/settings/features")
        self.assertIn(f'<option value="{template_id}"', settings.text)
        self.assertIn("Bump Reminder", settings.text)

    def test_moved_pages_have_old_url_redirects(self):
        self.login()
        redirects = {
            "/stats": "http://testserver/analytics/stats",
            "/settings/knowledge": "http://testserver/knowledge",
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
        self.assertEqual(roles.json()[0]["color"], "#ff00ff")
        self.assertEqual(roles.json()[0]["member_count"], 7)
        self.assertEqual(channels.json()[0]["parent_id"], "222222222222222222")
        self.assertEqual(categories.json()[0]["child_channel_ids"], ["333333333333333333"])
        self.assertEqual(structure.json()["categories"][0]["channels"][0]["name"], "help-desk")

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
        self.assertEqual(categories, [])
        settings = self.client.get("/settings/discord")
        self.assertIn("analytics_excluded_category_ids", settings.text)

    def test_imported_channels_are_not_selector_options(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            CREATE TABLE stats_message_activity (
                guild_id INTEGER,
                channel_id INTEGER,
                channel_name TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO stats_message_activity VALUES (1, 555555555555555555, 'import-only')"
        )
        connection.commit()
        connection.close()

        self.login()
        channels = self.client.get("/api/discord/channels").json()
        self.assertEqual(channels, [])

    def test_refresh_discord_metadata_queues_fixed_action(self):
        self.login()
        page = self.client.get("/settings/discord")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/settings/discord/refresh",
            data={"csrf": token, "action_type": "whoami"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        connection = sqlite3.connect(self.database)
        row = connection.execute(
            "SELECT action_type, payload_json, status FROM dashboard_actions"
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], "refresh_discord_metadata")
        self.assertEqual(row[1], "{}")
        self.assertEqual(row[2], "pending")

    def test_picker_assets_use_collapsed_compact_panel_pattern(self):
        root = Path(__file__).resolve().parent.parent
        script = (root / "dashboard/static/discord_pickers.js").read_text()
        styles = (root / "dashboard/static/styles.css").read_text()
        self.assertIn("this.panelOpen = false", script)
        self.assertIn("Browse ${label}", script)
        self.assertIn("this.panelOpen || Boolean(query)", script)
        self.assertIn("slice(0, max)", script)
        self.assertIn("const csvValues = () => raw.split", script)
        self.assertIn("return csvValues();", script)
        self.assertIn("discord-picker-panel", script)
        self.assertIn(".discord-picker-panel[hidden] { display: none; }", styles)
        self.assertIn(".discord-picker-option {\n  display: flex;", styles)
        self.assertIn(".discord-picker-category-row", styles)
        self.assertIn(".settings-sidebar", styles)
        self.assertIn(".settings-menu-item", styles)
        self.assertIn("text-decoration: none", styles)
        base_template = (root / "dashboard/templates/base.html").read_text()
        self.assertIn("styles.css') }}?v=embed-editor1", base_template)
        self.assertIn("discord_pickers.js') }}?v=picker-single-values2", base_template)

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
