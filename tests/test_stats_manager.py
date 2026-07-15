import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
from fastapi.testclient import TestClient

from dashboard.app import app
from utils.settings import initialize_settings_from_env
from utils.stats_manager import (
    archive_stat,
    export_stat_csv,
    get_stat,
    initialize_stats_manager_schema,
    list_stats,
    queue_stat_refresh,
    replace_member_snapshot,
    update_stat,
)


def create_stats_tables(database: Path) -> None:
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE role_stat_embeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            image_url TEXT,
            image_data BLOB,
            graphic_enabled INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE tracked_stats_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            report_type TEXT NOT NULL,
            role_1_id INTEGER,
            role_2_id INTEGER,
            has_role_id INTEGER,
            missing_role_id INTEGER,
            title TEXT,
            body TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE tracked_activity_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            report_type TEXT NOT NULL,
            config_json TEXT NOT NULL,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()


class StatsManagerDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        create_stats_tables(self.database)
        self.environment = patch.dict(
            os.environ,
            {"DATABASE_PATH": str(self.database)},
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    def insert_roster(self, title="Rangers"):
        connection = sqlite3.connect(self.database)
        cursor = connection.execute(
            """
            INSERT INTO role_stat_embeds (
                guild_id, channel_id, message_id, role_id, title, body,
                created_at, updated_at
            ) VALUES (1, 2, 3, 12345678901234567, ?, 'Body', 'now', 'now')
            """,
            (title,),
        )
        connection.commit()
        record_id = cursor.lastrowid
        connection.close()
        return f"roster-{record_id}"

    def test_empty_and_existing_listing_with_old_schema(self):
        self.assertEqual(list_stats(), [])
        stat_id = self.insert_roster()
        records = list_stats()
        self.assertEqual(records[0]["stat_id"], stat_id)
        self.assertEqual(records[0]["status"], "active")

    def test_safe_edit_and_unsafe_fields_are_ignored(self):
        stat_id = self.insert_roster()
        update_stat(
            stat_id,
            title="Updated title",
            body="Updated body",
        )
        record = get_stat(stat_id)
        self.assertEqual(record["title"], "Updated title")
        self.assertEqual(record["channel_id"], 2)
        with self.assertRaisesRegex(ValueError, "Title is required"):
            update_stat(stat_id, title="", body="")

    def test_refresh_creates_pending_action_and_rejects_invalid_ids(self):
        stat_id = self.insert_roster()
        action_id = queue_stat_refresh(stat_id, "admin")
        connection = sqlite3.connect(self.database)
        row = connection.execute(
            "SELECT action_type, payload_json, status FROM dashboard_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], "refresh_stat")
        self.assertEqual(json.loads(row[1]), {"stat_id": stat_id})
        self.assertEqual(row[2], "pending")
        with self.assertRaisesRegex(ValueError, "Invalid stat ID"):
            queue_stat_refresh("roster-1; DROP TABLE role_stat_embeds", "admin")

    def test_archive_is_non_destructive_and_remains_listed(self):
        stat_id = self.insert_roster()
        archive_stat(stat_id)
        record = get_stat(stat_id)
        self.assertEqual(record["status"], "archived")
        self.assertEqual(len(list_stats()), 1)
        with self.assertRaisesRegex(ValueError, "Archived"):
            queue_stat_refresh(stat_id, "admin")

    def test_csv_export_uses_stored_snapshot(self):
        stat_id = self.insert_roster()
        replace_member_snapshot(
            stat_id,
            [
                {
                    "discord_user_id": 111,
                    "username": "member",
                    "display_name": "Member",
                    "role_id": 12345678901234567,
                    "joined_at": "2026-01-01T00:00:00+00:00",
                    "category": "member",
                }
            ],
        )
        data = export_stat_csv(stat_id)
        self.assertIn(b"discord_user_id", data)
        self.assertIn(b"Member", data)

    def test_csv_export_without_snapshot_is_friendly(self):
        stat_id = self.insert_roster()
        self.assertIsNone(export_stat_csv(stat_id))


class StatsManagerRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        create_stats_tables(self.database)
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database),
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "admin",
                "DASHBOARD_PASSWORD": "test-password",
                "DASHBOARD_SECRET_KEY": "test-session-signing-key",
            },
            clear=False,
        )
        self.environment.start()
        initialize_settings_from_env()
        initialize_stats_manager_schema()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def login(self):
        page = self.client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": "test-password",
                "csrf": token,
            },
        )

    def csrf(self, path="/stats"):
        page = self.client.get(path)
        return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def insert_roster(self):
        connection = sqlite3.connect(self.database)
        cursor = connection.execute(
            """
            INSERT INTO role_stat_embeds (
                guild_id, channel_id, message_id, role_id, title, body,
                created_at, updated_at, status
            ) VALUES (1, 2, 3, 12345678901234567, 'Rangers', 'Body',
                      'now', 'now', 'active')
            """
        )
        connection.commit()
        record_id = cursor.lastrowid
        connection.close()
        return f"roster-{record_id}"

    def test_auth_required_for_pages_posts_and_export(self):
        paths = ["/stats", "/stats/roster-1", "/stats/roster-1/edit", "/stats/roster-1/export.csv"]
        for path in paths:
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
        for path in (
            "/stats/roster-1/edit",
            "/stats/roster-1/refresh",
            "/stats/roster-1/archive",
        ):
            response = self.client.post(path, data={"csrf": "bad"}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)

    def test_stats_page_empty_and_existing(self):
        self.login()
        response = self.client.get("/stats")
        self.assertIn("No tracked stats graphics found yet", response.text)
        stat_id = self.insert_roster()
        response = self.client.get("/stats")
        self.assertIn(stat_id, response.text)
        self.assertIn("Rangers", response.text)

    def test_posts_require_csrf(self):
        stat_id = self.insert_roster()
        self.login()
        for suffix in ("edit", "refresh", "archive"):
            response = self.client.post(
                f"/stats/{stat_id}/{suffix}",
                data={"csrf": "bad"},
            )
            self.assertEqual(response.status_code, 400)

    def test_valid_edit_saves_only_safe_fields(self):
        stat_id = self.insert_roster()
        self.login()
        token = self.csrf(f"/stats/{stat_id}/edit")
        response = self.client.post(
            f"/stats/{stat_id}/edit",
            data={
                "csrf": token,
                "title": "Updated",
                "body": "New body",
                "image_url": "https://example.com/banner.png",
                "channel_id": "999",
                "message_id": "888",
                "sql": "DROP TABLE role_stat_embeds",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        record = get_stat(stat_id)
        self.assertEqual(record["title"], "Updated")
        self.assertEqual(record["channel_id"], 2)
        self.assertEqual(record["message_id"], 3)
        self.assertIsNone(record["image_url"])

    def test_refresh_queues_action(self):
        stat_id = self.insert_roster()
        self.login()
        token = self.csrf(f"/stats/{stat_id}")
        response = self.client.post(
            f"/stats/{stat_id}/refresh",
            data={"csrf": token, "command": "whoami"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        connection = sqlite3.connect(self.database)
        count = connection.execute(
            "SELECT COUNT(*) FROM dashboard_actions WHERE status = 'pending'"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)

    def test_archive_preserves_record(self):
        stat_id = self.insert_roster()
        self.login()
        token = self.csrf(f"/stats/{stat_id}")
        self.client.post(
            f"/stats/{stat_id}/archive",
            data={"csrf": token},
            follow_redirects=False,
        )
        self.assertEqual(get_stat(stat_id)["status"], "archived")

    def test_csv_export_and_missing_snapshot(self):
        stat_id = self.insert_roster()
        self.login()
        response = self.client.get(
            f"/stats/{stat_id}/export.csv",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        replace_member_snapshot(
            stat_id,
            [
                {
                    "discord_user_id": 111,
                    "username": "member",
                    "display_name": "Member",
                    "role_id": 12345678901234567,
                    "joined_at": None,
                    "category": "member",
                }
            ],
        )
        response = self.client.get(f"/stats/{stat_id}/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        self.assertIn("Member", response.text)

    def test_existing_dashboard_pages_still_render(self):
        self.login()
        for path in ("/", "/settings", "/operations", "/bank", "/imports"):
            with (
                patch("dashboard.app.service_status", return_value={"name": "service", "state": "unavailable", "detail": "none"}),
                patch("dashboard.app.service_logs", return_value={"name": "service", "output": "none"}),
            ):
                response = self.client.get(path)
            self.assertEqual(response.status_code, 200)


class StatsCogCompatibilityTests(unittest.TestCase):
    def test_stats_cog_imports(self):
        from cogs.stats import Stats

        self.assertTrue(hasattr(Stats, "dashboard_action_worker"))

    def test_discord_metadata_emojis_preserve_names_and_animation(self):
        from cogs.stats import Stats

        emoji = type(
            "Emoji",
            (),
            {
                "id": 1334088283587874826,
                "name": "p_freakout",
                "animated": True,
                "available": True,
                "managed": False,
            },
        )()
        guild = type("Guild", (), {"emojis": [emoji]})()

        self.assertEqual(
            Stats._discord_metadata_emojis(guild),
            [
                {
                    "id": "1334088283587874826",
                    "name": "p_freakout",
                    "animated": True,
                    "available": True,
                    "managed": False,
                }
            ],
        )

    def test_bot_enables_discord_emoji_gateway_intent(self):
        from main import intents

        self.assertTrue(intents.emojis_and_stickers)


class StatsCogEmojiRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_refresh_fetches_emojis_when_gateway_cache_is_empty(self):
        from cogs.stats import Stats

        emoji = type(
            "Emoji",
            (),
            {
                "id": 1334088283587874826,
                "name": "p_freakout",
                "animated": True,
                "available": True,
                "managed": False,
            },
        )()

        class Guild:
            id = 1278253523619807233
            emojis = []

            async def fetch_emojis(self):
                return [emoji]

        self.assertEqual(
            await Stats._fetch_discord_metadata_emojis(Guild()),
            [
                {
                    "id": "1334088283587874826",
                    "name": "p_freakout",
                    "animated": True,
                    "available": True,
                    "managed": False,
                }
            ],
        )


class StatsCogSchemaCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_cog_async_migration_adds_manager_columns(self):
        from cogs.stats import Stats

        database = await aiosqlite.connect(":memory:")
        for table in (
            "role_stat_embeds",
            "tracked_stats_reports",
            "tracked_activity_reports",
        ):
            await database.execute(
                f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)'
            )
        bot = type("Bot", (), {"db": database})()
        cog = Stats(bot)
        await cog._ensure_dashboard_manager_columns()
        for table in (
            "role_stat_embeds",
            "tracked_stats_reports",
            "tracked_activity_reports",
        ):
            cursor = await database.execute(f'PRAGMA table_info("{table}")')
            columns = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
            self.assertIn("status", columns)
            self.assertIn("last_error", columns)
        await database.close()

    async def test_replace_roster_banner_persists_bytes_and_refreshes_same_row(self):
        from cogs.stats import Stats

        database = await aiosqlite.connect(":memory:")
        await database.execute(
            """
            CREATE TABLE role_stat_embeds (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                image_url TEXT,
                image_data BLOB,
                graphic_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
            """
        )
        await database.execute(
            """
            INSERT INTO role_stat_embeds (
                id, guild_id, channel_id, message_id, role_id, title, body,
                created_at, updated_at
            ) VALUES (1, 10, 20, 30, 40, 'Roster', 'Body', 'now', 'now')
            """
        )
        await database.commit()
        cog = Stats(type("Bot", (), {"db": database})())
        cog._refresh_row = AsyncMock(return_value=True)

        result = await cog._replace_roster_banner(
            guild_id=10,
            message_id=30,
            image_url="https://example.com/banner.png",
            image_data=b"replacement-image",
        )

        self.assertTrue(result)
        cursor = await database.execute(
            "SELECT image_url, image_data FROM role_stat_embeds WHERE id = 1"
        )
        stored = await cursor.fetchone()
        await cursor.close()
        self.assertEqual(stored[0], "https://example.com/banner.png")
        self.assertEqual(stored[1], b"replacement-image")
        cog._refresh_row.assert_awaited_once()
        self.assertEqual(cog._refresh_row.await_args.args[0][8], b"replacement-image")
        await database.close()


if __name__ == "__main__":
    unittest.main()
