import os
import sqlite3
import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import aiosqlite

from PIL import Image

from dashboard.events_manager import normalize_event_image, parse_event_form
from utils.events import (
    event_is_owned_by,
    get_event_action,
    initialize_events_schema,
    list_events,
    parse_offsets,
    queue_event_action,
    record_event_action_storage,
    save_event_artwork,
    subscribe_to_event,
    unsubscribe_from_event,
    update_event_subscription,
)
from cogs.events import EventsSync
from utils.sqlite import configure_connection


class EventsHubStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "events.db"
        self.environment = patch.dict(os.environ, {"DATABASE_PATH": str(self.database), "SERVER_TIMEZONE": "America/Chicago"})
        self.environment.start()
        initialize_events_schema()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_schema_and_action_queue_are_repeat_safe(self):
        initialize_events_schema()
        values = dict(
            action="create", guild_id="1", scheduled_event_id=None,
            requested_by_dashboard_user_id=4, requested_by_discord_user_id="99",
            requested_by_name="Captain", payload={"name": "Pride Picnic"},
            idempotency_key="same-browser-submit",
        )
        first = queue_event_action(**values)
        second = queue_event_action(**values)
        self.assertEqual(first, second)
        action = get_event_action(first)
        self.assertEqual(action["status"], "pending")
        self.assertEqual(action["requested_by_dashboard_user_id"], 4)
        self.assertEqual(action["attempt_count"], 0)

    def test_snapshot_ownership_is_preserved_and_scoped(self):
        now = datetime.now(timezone.utc)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """INSERT INTO dashboard_scheduled_events
                (scheduled_event_id,guild_id,name,entity_type,location,scheduled_at_utc,event_url,status,updated_at_utc)
                VALUES ('55','1','Community Night','external','The Center',?,'https://discord.com/events/1/55','scheduled',?)""",
                ((now + timedelta(days=2)).isoformat(), now.isoformat()),
            )
            connection.execute(
                """INSERT INTO dashboard_event_ownership
                (scheduled_event_id,dashboard_user_id,discord_user_id,organizer_name,created_at_utc,updated_at_utc)
                VALUES ('55',4,'99','Captain Alex',?,?)""",
                (now.isoformat(), now.isoformat()),
            )
            connection.commit()
        event = list_events("1", user_id="99")[0]
        self.assertEqual(event["organizer_name"], "Captain Alex")
        self.assertTrue(event_is_owned_by(event, 4, "99"))
        self.assertFalse(event_is_owned_by(event, 8, "100"))

    def test_discord_artwork_reference_overrides_event_cover_without_storing_bytes(self):
        now = datetime.now(timezone.utc)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """INSERT INTO dashboard_scheduled_events
                (scheduled_event_id,guild_id,name,entity_type,location,scheduled_at_utc,event_url,image_url,status,updated_at_utc)
                VALUES ('56','1','Art Night','external','Studio',?,'https://discord.com/events/1/56','https://discord.example/event-cover','scheduled',?)""",
                ((now + timedelta(days=3)).isoformat(), now.isoformat()),
            )
            connection.commit()
        save_event_artwork(
            scheduled_event_id="56", guild_id="1", storage_channel_id="70",
            storage_thread_id="71", storage_message_id="72",
            attachment_url="https://cdn.discordapp.com/attachments/70/72/cover.webp",
        )
        event = list_events("1")[0]
        self.assertEqual(event["image_url"], "https://cdn.discordapp.com/attachments/70/72/cover.webp")
        self.assertEqual(event["discord_cover_url"], "https://discord.example/event-cover")
        with sqlite3.connect(self.database) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(dashboard_event_artwork)")}
        self.assertNotIn("image_bytes", columns)

    def test_action_storage_receipt_is_reusable_across_retries(self):
        action_id = queue_event_action(
            action="create", guild_id="1", scheduled_event_id=None,
            requested_by_dashboard_user_id=4, requested_by_discord_user_id="99",
            requested_by_name="Captain", payload={"name": "Pride Picnic"},
            image_bytes=b"image", image_content_type="image/webp",
        )
        record_event_action_storage(
            action_id, storage_channel_id="70", storage_thread_id="71",
            storage_message_id="72", attachment_url="https://cdn.discordapp.com/cover.webp",
        )
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM event_dashboard_actions WHERE id = ?", (action_id,)).fetchone()
        self.assertEqual(row["storage_message_id"], "72")
        self.assertEqual(row["storage_attachment_url"], "https://cdn.discordapp.com/cover.webp")

    def test_form_validation_enforces_discord_type_rules(self):
        start = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        end = (datetime.now() + timedelta(days=2, hours=2)).strftime("%Y-%m-%dT%H:%M")
        external = parse_event_form({"entity_type": "external", "name": "Park Pride", "description": "", "start_time": start, "end_time": end, "location": "River Park"})
        self.assertEqual(external["location"], "River Park")
        with self.assertRaisesRegex(ValueError, "location and end time"):
            parse_event_form({"entity_type": "external", "name": "Park Pride", "start_time": start})
        with self.assertRaisesRegex(ValueError, "eligible Discord channel"):
            parse_event_form({"entity_type": "stage", "name": "Town Hall", "start_time": start})

    def test_artwork_is_bounded_webp_and_timings_are_restricted(self):
        source = BytesIO()
        Image.new("RGB", (2400, 1200), "#f03b9f").save(source, "PNG")
        output, content_type = normalize_event_image(source.getvalue(), "image/png")
        self.assertEqual(content_type, "image/webp")
        with Image.open(BytesIO(output)) as rendered:
            self.assertEqual(rendered.size, (1600, 900))
        self.assertEqual(parse_offsets(["0", "15", "15", "360"]), (360, 15, 0))
        with self.assertRaises(ValueError):
            parse_offsets([30])


class EventsDiscordActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock()
        self.bot.db.execute = AsyncMock()
        self.bot.db.commit = AsyncMock()
        self.guild = MagicMock()
        self.guild.create_scheduled_event = AsyncMock(return_value=MagicMock(id=700))
        self.bot.get_guild.return_value = self.guild
        self.cog = EventsSync(self.bot)
        self.cog.refresh_guild = AsyncMock()

    def action(self, entity_type, **changes):
        start = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        payload = {
            "entity_type": entity_type, "name": "Gathering", "description": "Welcome",
            "scheduled_at_utc": start, "end_at_utc": None, "channel_id": "88", "location": "",
        }
        payload.update(changes)
        return {
            "action": "create", "guild_id": "1", "scheduled_event_id": None,
            "requested_by_name": "Captain Alex", "requested_by_dashboard_user_id": 4,
            "requested_by_discord_user_id": "99", "payload_json": json.dumps(payload),
            "image_bytes": None,
        }

    async def test_stage_create_omits_external_location(self):
        channel = MagicMock()
        self.cog._event_channel = AsyncMock(return_value=channel)
        await self.cog._process_event_action(self.action("stage"))
        kwargs = self.guild.create_scheduled_event.await_args.kwargs
        self.assertIs(kwargs["channel"], channel)
        self.assertNotIn("location", kwargs)
        self.assertNotIn("end_time", kwargs)

    async def test_external_create_omits_channel_and_requires_end(self):
        end = (datetime.now(timezone.utc) + timedelta(days=2, hours=2)).isoformat()
        self.cog._event_channel = AsyncMock(return_value=None)
        await self.cog._process_event_action(self.action("external", channel_id=None, location="River Park", end_at_utc=end))
        kwargs = self.guild.create_scheduled_event.await_args.kwargs
        self.assertEqual(kwargs["location"], "River Park")
        self.assertNotIn("channel", kwargs)
        self.assertIn("end_time", kwargs)

    async def test_forum_artwork_upload_records_attachment_receipt(self):
        forum = MagicMock(spec=__import__("discord").ForumChannel)
        forum.flags.require_tag = False
        forum.available_tags = []
        attachment = SimpleNamespace(url="https://cdn.discordapp.com/attachments/70/72/cover.webp")
        message = SimpleNamespace(id=72, attachments=[attachment])
        forum.create_thread = AsyncMock(return_value=SimpleNamespace(thread=SimpleNamespace(id=71), message=message))
        self.bot.get_channel.return_value = forum
        action = self.action("external", channel_id=None, location="River Park", end_at_utc=(datetime.now(timezone.utc) + timedelta(days=2, hours=2)).isoformat())
        action.update({"id": 8, "image_bytes": b"normalized-webp", "image_content_type": "image/webp"})
        with patch("cogs.events.get_setting", return_value="70"):
            receipt = await self.cog._store_event_artwork(action, self.guild, json.loads(action["payload_json"]))
        self.assertEqual(receipt["thread_id"], "71")
        self.assertEqual(receipt["attachment_url"], attachment.url)
        forum.create_thread.assert_awaited_once()
        self.bot.db.execute.assert_awaited()

    async def test_existing_action_receipt_prevents_duplicate_storage_post(self):
        action = self.action("stage")
        action.update({
            "id": 9, "image_bytes": b"normalized-webp",
            "storage_channel_id": "70", "storage_thread_id": "71",
            "storage_message_id": "72",
            "storage_attachment_url": "https://cdn.discordapp.com/cover.webp",
        })
        receipt = await self.cog._store_event_artwork(action, self.guild, json.loads(action["payload_json"]))
        self.assertEqual(receipt["message_id"], "72")
        self.bot.get_channel.assert_not_called()


class EventsReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "sync.db"
        self.environment = patch.dict(os.environ, {"DATABASE_PATH": str(self.database_path), "SERVER_TIMEZONE": "America/Chicago"})
        self.environment.start()
        self.database = await aiosqlite.connect(self.database_path)
        self.database.row_factory = aiosqlite.Row
        await configure_connection(self.database, foreign_keys=True)
        self.bot = MagicMock()
        self.bot.db = self.database
        self.bot.user = SimpleNamespace(id=111)
        self.guild = MagicMock()
        self.guild.id = 123
        self.guild.channels = []
        self.guild.get_channel.return_value = None
        self.guild.me.guild_permissions.create_events = True
        self.guild.me.guild_permissions.manage_events = True
        self.state = MagicMock()
        self.state._get_guild.return_value = self.guild
        self.state.get_user.return_value = None
        self.guild._state = self.state
        self.bot.get_guild.return_value = self.guild
        self.raw_event = {
            "id": "900", "guild_id": "123", "name": "Community Night",
            "description": "Welcome", "entity_type": 3, "entity_id": None,
            "scheduled_start_time": (datetime.now(timezone.utc) + timedelta(days=4)).isoformat(),
            "scheduled_end_time": (datetime.now(timezone.utc) + timedelta(days=4, hours=2)).isoformat(),
            "privacy_level": 2, "status": 1, "image": None, "user_count": 12,
            "creator_id": "222", "channel_id": None,
            "entity_metadata": {"location": "River Park"},
            "recurrence_rule": {"frequency": 2, "interval": 1},
        }
        self.state.http.get_scheduled_events = AsyncMock(return_value=[self.raw_event])
        self.cog = EventsSync(self.bot)
        await self.cog.cog_load()

    async def asyncTearDown(self):
        await self.database.close()
        self.environment.stop()
        self.temp.cleanup()

    async def test_repeated_reconciliation_reschedules_and_cancels_without_duplicates(self):
        await self.cog.refresh_guild(self.guild)
        await self.cog.refresh_guild(self.guild)
        cursor = await self.database.execute(
            "SELECT COUNT(*), interested_count, recurrence_json FROM dashboard_scheduled_events"
        )
        event_count, interested, recurrence = await cursor.fetchone()
        await cursor.close()
        self.assertEqual(event_count, 1)
        self.assertEqual(interested, 12)
        self.assertIn('"frequency":2', recurrence)
        cursor = await self.database.execute(
            "SELECT id FROM reminder_items WHERE legacy_source='discord_scheduled_event' AND legacy_id='900'"
        )
        reminder_id = int((await cursor.fetchone())[0])
        await cursor.close()
        subscription, created = await subscribe_to_event(reminder_id=reminder_id, user_id=333, offsets=(15, 0))
        self.assertTrue(created)
        updated = await update_event_subscription(subscription_id=int(subscription["id"]), user_id=333, offsets=(360, 60, 0))
        self.assertEqual(updated["custom_offsets_json"], "[360,60,0]")
        self.assertTrue(await unsubscribe_from_event(subscription_id=int(subscription["id"]), user_id=333))
        restored, restored_created = await subscribe_to_event(reminder_id=reminder_id, user_id=333, offsets=(15, 0))
        self.assertTrue(restored_created)

        moved = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        self.raw_event["scheduled_start_time"] = moved
        self.raw_event["user_count"] = 19
        await self.cog.refresh_guild(self.guild)
        cursor = await self.database.execute("SELECT scheduled_at_utc FROM reminder_items WHERE legacy_id='900'")
        self.assertEqual((await cursor.fetchone())[0], moved)
        await cursor.close()

        self.state.http.get_scheduled_events.return_value = []
        await self.cog.refresh_guild(self.guild)
        cursor = await self.database.execute("SELECT status FROM reminder_items WHERE legacy_id='900'")
        self.assertEqual((await cursor.fetchone())[0], "cancelled")
        await cursor.close()
        cursor = await self.database.execute("SELECT status, last_sync_status FROM dashboard_scheduled_events WHERE scheduled_event_id='900'")
        self.assertEqual(tuple(await cursor.fetchone()), ("cancelled", "removed"))
        await cursor.close()


if __name__ == "__main__":
    unittest.main()
