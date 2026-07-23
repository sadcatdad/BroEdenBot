import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.app import app, required_permission
from dashboard.features import FEATURES_BY_KEY, feature_snapshot
from dashboard.rbac import initialize_rbac_schema
from dashboard.users import hash_password, initialize_dashboard_users
from utils.settings import initialize_settings_from_env
from utils.settings import get_setting
from utils.events import initialize_events_schema


class DashboardFeatureAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database),
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "owner",
                "DASHBOARD_PASSWORD": "owner-password",
                "DASHBOARD_SECRET_KEY": "feature-access-test-key",
                "DASHBOARD_AUTH_MODE": "password",
                "GUILD_ID": "999999999999999999",
            },
            clear=False,
        )
        self.environment.start()
        initialize_settings_from_env()
        initialize_dashboard_users()
        initialize_rbac_schema()
        initialize_events_schema()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def add_password_user(self, username, password, role):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                INSERT INTO dashboard_users(username, password_hash, role, status, auth_provider)
                VALUES (?, ?, ?, 'active', 'password')
                """,
                (username, hash_password(password), role),
            )
            connection.commit()

    def login(self, username, password):
        page = self.client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/login", data={"username": username, "password": password, "csrf": token}
        )
        self.assertEqual(response.status_code, 200)

    def test_feature_enabled_state_matches_enabled_modules(self):
        with patch.dict(os.environ, {"ENABLED_MODULES": "reminders,events"}):
            self.assertFalse(feature_snapshot(FEATURES_BY_KEY["streaks"])["enabled"])
            self.assertTrue(feature_snapshot(FEATURES_BY_KEY["events"])["enabled"])
        with patch.dict(os.environ, {"ENABLED_MODULES": ""}):
            self.assertTrue(feature_snapshot(FEATURES_BY_KEY["streaks"])["enabled"])

    def test_every_mutating_route_has_an_explicit_permission_policy(self):
        unmapped = []
        for route in app.routes:
            path = getattr(route, "path", "")
            for method in getattr(route, "methods", set()) or set():
                if method in {"POST", "PUT", "PATCH", "DELETE"} and required_permission(path, method) == "dashboard.unmapped":
                    unmapped.append("{} {}".format(method, path))
        self.assertEqual(unmapped, [])

    def test_party_captain_navigation_and_routes_are_limited_to_events(self):
        self.add_password_user("captain", "captain-password", "party_captain")
        self.login("captain", "captain-password")
        home = self.client.get("/")
        self.assertIn("Features", home.text)
        self.assertNotIn('<span>Bank</span>', home.text)
        self.assertNotIn('<span>Settings</span>', home.text)
        features = self.client.get("/features")
        self.assertEqual(features.status_code, 200)
        self.assertIn("Events", features.text)
        self.assertNotIn("Staff &amp; Moderation Tools", features.text)
        self.assertEqual(self.client.get("/features/ask").status_code, 403)
        self.assertEqual(self.client.get("/features/events").status_code, 200)

    def test_party_captain_cannot_edit_another_organizers_event(self):
        self.add_password_user("captain", "captain-password", "party_captain")
        self.login("captain", "captain-password")
        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """INSERT INTO dashboard_scheduled_events
                (scheduled_event_id,guild_id,name,entity_type,location,scheduled_at_utc,event_url,status,updated_at_utc)
                VALUES ('700', '999999999999999999', 'Another Event', 'external', 'Park', ?, 'https://discord.com/events/1/700', 'scheduled', ?)""",
                (future, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                """INSERT INTO dashboard_event_ownership
                (scheduled_event_id,dashboard_user_id,organizer_name,created_at_utc,updated_at_utc)
                VALUES ('700', 999, 'Another Captain', ?, ?)""",
                (future, future),
            )
            connection.commit()
        self.assertEqual(self.client.get("/events/700/edit").status_code, 403)

    def test_owner_can_queue_external_event_with_csrf(self):
        self.login("owner", "owner-password")
        page = self.client.get("/events/new")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        submission = re.search(r'name="submission_id" value="([^"]+)"', page.text).group(1)
        start = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        end = (datetime.now() + timedelta(days=3, hours=2)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            "/events/new",
            data={"csrf": token, "submission_id": submission, "entity_type": "external", "name": "Pride Picnic", "description": "Welcome", "start_time": start, "end_time": end, "location": "River Park"},
        )
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.database) as connection:
            action = connection.execute("SELECT action, status FROM event_dashboard_actions").fetchone()
        self.assertEqual(action, ("create", "pending"))

    def test_owner_events_settings_include_discord_artwork_storage_picker(self):
        self.login("owner", "owner-password")
        events_page = self.client.get("/events")
        self.assertIn("Event settings", events_page.text)
        self.assertIn('/features/events', events_page.text)
        response = self.client.get("/features/events")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Event Artwork Storage", response.text)
        self.assertIn("EVENTS_ARTWORK_STORAGE_CHANNEL_ID", response.text)
        self.assertIn("channel-single-select", response.text)

    def test_viewer_cannot_fetch_discord_metadata_or_settings(self):
        self.add_password_user("viewer", "viewer-password", "viewer")
        self.login("viewer", "viewer-password")
        home = self.client.get("/")
        self.assertIn("Analytics", home.text)
        self.assertNotIn('<span>Settings</span>', home.text)
        response = self.client.get("/api/discord/roles")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            "You do not have permission to access this resource.",
        )
        denied = self.client.get("/settings")
        self.assertEqual(denied.status_code, 403)
        self.assertIn("Nothing from the requested page was loaded", denied.text)

    def test_verified_events_member_is_limited_to_schedule_and_subscriptions(self):
        self.add_password_user("verified", "verified-password", "verified_events_member")
        self.login("verified", "verified-password")
        events = self.client.get("/events")
        self.assertEqual(events.status_code, 200)
        self.assertIn("<h1>Events</h1>", events.text)
        self.assertIn("The Garden", events.text)
        self.assertNotIn("Create an event", events.text)
        self.assertEqual(self.client.get("/").status_code, 403)
        self.assertEqual(self.client.get("/events/new").status_code, 403)

    def test_expired_discord_verification_clears_existing_session(self):
        self.login("owner", "owner-password")
        expired = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                UPDATE dashboard_users
                SET auth_provider = 'discord', discord_user_id = ?,
                    discord_verification_status = 'verified', discord_verified_at = ?
                WHERE username = 'owner'
                """,
                ("555555555555555555", expired),
            )
            connection.commit()
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "http://testserver/login")

    def test_unsaved_change_script_has_dirty_discard_and_navigation_guards(self):
        script = (Path(__file__).resolve().parents[1] / "dashboard/static/settings_form.js").read_text()
        self.assertIn('form.addEventListener("input", markDirty)', script)
        self.assertIn('form.addEventListener("discord-picker-change", markDirty)', script)
        self.assertIn('window.addEventListener("beforeunload"', script)
        self.assertIn("window.location.reload()", script)
        self.assertIn('save.textContent = "Saving…"', script)

    def test_feature_form_validation_is_atomic(self):
        self.login("owner", "owner-password")
        page = self.client.get("/features/streaks")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/features/streaks/save",
            data={
                "csrf": token,
                "setting__STREAK_RESTORE_MAX_DAYS": "7",
                "setting__STREAK_RESTORE_MAX_MESSAGES": "not-a-number",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(get_setting("STREAK_RESTORE_MAX_DAYS"), "14")
        self.assertEqual(get_setting("STREAK_RESTORE_MAX_MESSAGES"), "50000")
        error_page = self.client.get(response.headers["location"])
        self.assertIn("Value must be an integer", error_page.text)


if __name__ == "__main__":
    unittest.main()
