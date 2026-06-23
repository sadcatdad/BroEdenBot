import csv
import io
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.app import app
from utils.analytics import (
    export_analytics_csv,
    get_activity_series,
    get_analytics_overview,
    get_channel_leaderboard,
    get_heatmap,
    get_member_leaderboard,
    get_voice_overview,
    validate_export_type,
    validate_limit,
    validate_range,
)


def create_analytics_schema(database: Path) -> None:
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE stats_message_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            channel_name TEXT,
            user_id INTEGER NOT NULL,
            display_name TEXT,
            username TEXT,
            activity_date TEXT NOT NULL,
            activity_hour TEXT NOT NULL,
            message_count INTEGER DEFAULT 0,
            source TEXT DEFAULT 'live',
            imported_at TEXT,
            import_batch_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE stats_activity_imports (
            id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            imported_at TEXT
        );
        CREATE TABLE stats_activity_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE vc_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            display_name TEXT,
            username TEXT,
            channel_id INTEGER,
            channel_name TEXT,
            joined_at TEXT NOT NULL,
            left_at TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()


class AnalyticsFixture(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        create_analytics_schema(self.database)
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database),
                "GUILD_ID": "123",
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "admin",
                "DASHBOARD_PASSWORD": "test-password",
                "DASHBOARD_SECRET_KEY": "test-session-signing-key",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    def insert_fixtures(self):
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        rows = [
            (123, 10, "general", 1, "Alpha", "alpha", now - timedelta(days=1), 5),
            (123, 10, "general", 2, "Beta", "beta", now - timedelta(days=1, hours=-1), 3),
            (123, 20, "events", 1, "Alpha", "alpha", now - timedelta(days=8), 7),
            (123, 20, "events", 3, "Gamma", "gamma", now - timedelta(days=40), 11),
            (999, 99, "other-guild", 99, "Other", "other", now - timedelta(days=1), 50),
        ]
        connection = sqlite3.connect(self.database)
        for guild_id, channel_id, channel_name, user_id, display_name, username, moment, count in rows:
            connection.execute(
                """
                INSERT INTO stats_message_activity (
                    guild_id, channel_id, channel_name, user_id, display_name,
                    username, activity_date, activity_hour, message_count,
                    source, import_batch_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', 'live', ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    channel_name,
                    user_id,
                    display_name,
                    username,
                    moment.date().isoformat(),
                    moment.isoformat(),
                    count,
                    moment.isoformat(),
                    moment.isoformat(),
                ),
            )
        connection.execute(
            "INSERT INTO stats_activity_imports VALUES (1, 123, ?)",
            ((now - timedelta(hours=2)).isoformat(),),
        )
        connection.execute(
            "INSERT INTO stats_activity_settings VALUES ('activity_tracking_started_at', ?)",
            ((now - timedelta(days=100)).isoformat(),),
        )
        connection.execute(
            """
            INSERT INTO vc_sessions (
                guild_id, user_id, display_name, username, channel_id,
                channel_name, joined_at, left_at, duration_seconds
            ) VALUES (123, 1, 'Alpha', 'alpha', 30, 'Lounge', ?, ?, 3600)
            """,
            (
                (now - timedelta(days=1, hours=1)).isoformat(),
                (now - timedelta(days=1)).isoformat(),
            ),
        )
        connection.commit()
        connection.close()


class AnalyticsHelperTests(AnalyticsFixture):
    def test_overview_counts_and_guild_scope(self):
        self.insert_fixtures()
        overview = get_analytics_overview("all")
        self.assertTrue(overview["available"])
        self.assertEqual(overview["total_messages"], 26)
        self.assertEqual(overview["unique_users"], 3)
        self.assertEqual(overview["channels"], 2)
        self.assertEqual(overview["messages_7d"], 8)
        self.assertEqual(overview["messages_30d"], 15)
        self.assertIsNotNone(overview["first_date"])
        self.assertIsNotNone(overview["last_date"])

    def test_activity_aggregations_and_range_allowlist(self):
        self.insert_fixtures()
        seven = get_activity_series("7d")
        all_time = get_activity_series("all")
        self.assertEqual(seven["selected_total"], 8)
        self.assertEqual(len(seven["daily"]), 7)
        self.assertEqual(all_time["selected_total"], 26)
        self.assertTrue(all_time["weekly"])
        self.assertTrue(all_time["monthly"])
        with self.assertRaises(ValueError):
            validate_range("30d; DROP TABLE stats_message_activity")

    def test_channel_and_member_leaderboards(self):
        self.insert_fixtures()
        channels = get_channel_leaderboard("all", 25)
        members = get_member_leaderboard("all", 25)
        self.assertEqual(channels[0]["channel_name"], "events")
        self.assertEqual(channels[0]["message_count"], 18)
        general = next(row for row in channels if row["channel_name"] == "general")
        self.assertEqual(general["unique_users"], 2)
        self.assertEqual(members[0]["display_name"], "Alpha")
        self.assertEqual(members[0]["message_count"], 12)
        self.assertEqual(members[0]["active_days"], 2)
        self.assertEqual(members[0]["top_channel_name"], "events")
        with self.assertRaises(ValueError):
            validate_limit("25 UNION SELECT 1")

    def test_heatmap_and_voice_aggregations(self):
        self.insert_fixtures()
        heatmap = get_heatmap("all")
        voice = get_voice_overview("30d", 25)
        self.assertTrue(heatmap["available"])
        self.assertIsNotNone(heatmap["busiest"])
        self.assertEqual(sum(cell["count"] for day in heatmap["days"] for cell in day["cells"]), 26)
        self.assertTrue(voice["available"])
        self.assertEqual(voice["sessions"], 1)
        self.assertEqual(voice["seconds"], 3600)
        self.assertEqual(voice["top_users"][0]["display_name"], "Alpha")

    def test_missing_tables_and_empty_tables_are_friendly(self):
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            DROP TABLE stats_message_activity;
            DROP TABLE vc_sessions;
            """
        )
        connection.commit()
        connection.close()
        self.assertFalse(get_analytics_overview()["available"])
        self.assertFalse(get_voice_overview()["available"])
        create_path = Path(self.temporary_directory.name) / "empty.db"
        create_analytics_schema(create_path)
        with patch.dict(os.environ, {"DATABASE_PATH": str(create_path)}):
            overview = get_analytics_overview()
            self.assertFalse(overview["available"])
            self.assertEqual(overview["total_messages"], 0)

    def test_all_csv_exports_are_aggregated_and_content_free(self):
        self.insert_fixtures()
        for export_type in (
            "overview",
            "activity",
            "channels",
            "members",
            "voice",
            "heatmap",
        ):
            filename, data = export_analytics_csv(
                "30d" if export_type != "heatmap" else "90d",
                export_type,
            )
            self.assertIn(export_type, filename)
            text = data.decode("utf-8-sig")
            self.assertNotIn("message body", text.casefold())
            self.assertNotIn("content", text.casefold())
            self.assertNotIn("DISCORD_TOKEN", text)
            self.assertGreater(len(list(csv.reader(io.StringIO(text)))), 1)
        with self.assertRaises(ValueError):
            validate_export_type("messages")


class AnalyticsRouteTests(AnalyticsFixture):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        super().tearDown()

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

    def test_auth_required_for_pages_and_export(self):
        for path in (
            "/analytics",
            "/analytics/activity",
            "/analytics/channels",
            "/analytics/members",
            "/analytics/voice",
            "/analytics/heatmap",
            "/analytics/export.csv",
        ):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 303)

    def test_pages_render_fixture_and_empty_states(self):
        self.insert_fixtures()
        self.login()
        for path in (
            "/analytics?range=all",
            "/analytics/activity?range=30d",
            "/analytics/channels?range=all&limit=10",
            "/analytics/members?range=all&limit=25",
            "/analytics/voice?range=30d&limit=25",
            "/analytics/heatmap?range=90d",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        overview = self.client.get("/analytics?range=all")
        self.assertIn("26", overview.text)
        self.assertIn("Message contents are not shown", overview.text)
        members = self.client.get("/analytics/members?range=all")
        self.assertNotIn("message body", members.text.casefold())

        connection = sqlite3.connect(self.database)
        connection.execute("DROP TABLE stats_message_activity")
        connection.execute("DROP TABLE vc_sessions")
        connection.commit()
        connection.close()
        self.assertIn("No stored message activity", self.client.get("/analytics").text)
        self.assertIn("No VC history", self.client.get("/analytics/voice").text)

    def test_invalid_query_values_are_rejected(self):
        self.login()
        self.assertEqual(
            self.client.get("/analytics?range=all%27%20OR%201=1").status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/analytics/channels?limit=11").status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/analytics/heatmap?range=7d").status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/analytics/export.csv?type=raw_messages").status_code,
            400,
        )

    def test_exports_require_allowlisted_types_and_have_no_raw_content(self):
        self.insert_fixtures()
        self.login()
        for export_type in (
            "overview",
            "activity",
            "channels",
            "members",
            "voice",
            "heatmap",
        ):
            range_key = "90d" if export_type == "heatmap" else "30d"
            response = self.client.get(
                f"/analytics/export.csv?range={range_key}&type={export_type}"
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
            self.assertNotIn("message body", response.text.casefold())
            self.assertNotIn("discord-super-secret", response.text)

    def test_existing_dashboard_pages_and_cogs_still_import(self):
        self.login()
        for path in (
            "/",
            "/settings",
            "/stats",
            "/knowledge",
            "/bank",
            "/imports",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
        from cogs.message_context import MessageContext
        from cogs.staff_ai import StaffAI
        from cogs.stats import Stats
        from cogs.vc_stats import VCStats

        self.assertTrue(all((MessageContext, StaffAI, Stats, VCStats)))


if __name__ == "__main__":
    unittest.main()
