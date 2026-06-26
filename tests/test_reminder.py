import os
import unittest
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import aiosqlite

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from cogs import reminder
from cogs.reminder import ReminderCog, parse_id_set, parse_local_datetime


class DummyBot:
    def __init__(self, database):
        self.db = database
        self.user = SimpleNamespace(id=999)
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self.channels.get(channel_id)


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class FakeMember:
    def __init__(self, user_id, *, roles=(), administrator=False):
        self.id = user_id
        self.roles = list(roles)
        self.guild_permissions = SimpleNamespace(administrator=administrator)


class FakeInteraction:
    def __init__(self, user):
        self.guild = object()
        self.guild_id = 123
        self.user = user


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.sent_messages = []

    def is_done(self):
        return self.deferred or bool(self.sent_messages)

    async def defer(self, *, ephemeral=False, thinking=False):
        self.deferred = True
        self.defer_ephemeral = ephemeral
        self.defer_thinking = thinking

    async def send_message(self, *args, **kwargs):
        self.sent_messages.append((args, kwargs))


class FakeFollowup:
    def __init__(self):
        self.sent_messages = []

    async def send(self, *args, **kwargs):
        self.sent_messages.append((args, kwargs))


class FakeCommandInteraction(FakeInteraction):
    def __init__(self, user):
        super().__init__(user)
        self.guild = SimpleNamespace(id=123)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakeChannel:
    id = 456
    type = reminder.discord.ChannelType.text

    def __init__(self):
        self.sent_messages = []
        self.guild = SimpleNamespace(
            id=123,
            me=object(),
            get_member=lambda _user_id: object(),
        )

    def permissions_for(self, _member):
        return SimpleNamespace(
            view_channel=True,
            send_messages=True,
            send_messages_in_threads=True,
            embed_links=True,
        )

    async def send(self, **kwargs):
        self.sent_messages.append(kwargs)


class ReminderParsingTests(unittest.TestCase):
    def test_parse_local_datetime_accepts_required_formats(self):
        timezone_value = ZoneInfo("America/Chicago")

        iso_style = parse_local_datetime("2026-07-01 7:30 PM", timezone_value)
        slash_style = parse_local_datetime("07/01/2026 7:30 PM", timezone_value)
        compact_meridiem = parse_local_datetime("2026-07-01 7:30PM", timezone_value)
        lowercase_meridiem = parse_local_datetime("2026-07-01 7:30 pm", timezone_value)

        self.assertEqual(iso_style, slash_style)
        self.assertEqual(iso_style, compact_meridiem)
        self.assertEqual(iso_style, lowercase_meridiem)
        self.assertEqual(
            iso_style.astimezone(timezone.utc).isoformat(),
            "2026-07-02T00:30:00+00:00",
        )

    def test_parse_local_datetime_accepts_date_only_at_default_time(self):
        timezone_value = ZoneInfo("America/Chicago")

        parsed = parse_local_datetime("2026-07-01", timezone_value)

        self.assertEqual(
            parsed.astimezone(timezone.utc).isoformat(),
            "2026-07-01T14:00:00+00:00",
        )

    def test_parse_id_set_accepts_csv_and_dashboard_json(self):
        self.assertEqual(
            parse_id_set('123, 456 ["789"] nope'),
            {123, 456, 789},
        )


class ReminderPermissionTests(unittest.TestCase):
    def test_regular_users_cannot_target_themselves(self):
        cog = ReminderCog(DummyBot(None))
        user = FakeMember(10)
        interaction = FakeInteraction(user)

        with patch.object(reminder.discord, "Member", FakeMember):
            with patch("cogs.reminder.get_csv_ids_setting", return_value=[]):
                with patch("cogs.reminder.get_setting", return_value=""):
                    self.assertFalse(cog.can_target_user(interaction, user))

    def test_staff_role_can_target_self_or_other_members(self):
        cog = ReminderCog(DummyBot(None))
        user = FakeMember(10, roles=[FakeRole(55)])
        target = FakeMember(20)
        interaction = FakeInteraction(user)

        with patch.object(reminder.discord, "Member", FakeMember):
            with patch(
                "cogs.reminder.get_csv_ids_setting",
                side_effect=lambda key: [55] if key == "REMINDER_ALLOWED_ROLE_IDS" else [],
            ):
                with patch("cogs.reminder.get_setting", return_value=""):
                    self.assertTrue(cog.can_target_user(interaction, user))
                    self.assertTrue(cog.can_target_user(interaction, target))


class ReminderDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = await aiosqlite.connect(":memory:")
        self.bot = DummyBot(self.database)
        self.cog = ReminderCog(self.bot)
        await self.cog.create_schema()

    async def asyncTearDown(self):
        await self.database.close()

    async def test_insert_and_delete_pending_reminder(self):
        scheduled_at = parse_local_datetime(
            "2026-07-01 7:30 PM",
            ZoneInfo("America/Chicago"),
        )

        row = await self.cog.insert_reminder(
            guild_id=123,
            creator_user_id=10,
            target_user_id=10,
            channel_id=456,
            message="Submit the event plan",
            scheduled_at_utc=scheduled_at,
        )

        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["target_user_id"], "10")
        self.assertEqual(row["scheduled_at_utc"], "2026-07-02T00:30:00+00:00")

        deleted = await self.cog.soft_delete_reminder(123, 10, int(row["id"]))
        self.assertTrue(deleted)

        stored = await self.cog.fetch_one(
            "SELECT status FROM reminders WHERE id = ?",
            (row["id"],),
        )
        self.assertEqual(stored["status"], "deleted")

    async def test_add_command_defers_before_private_confirmation(self):
        staff = FakeMember(10, roles=[FakeRole(55)])
        interaction = FakeCommandInteraction(staff)
        channel = FakeChannel()

        with patch.object(reminder.discord, "Member", FakeMember):
            with patch(
                "cogs.reminder.get_csv_ids_setting",
                side_effect=lambda key: [55] if key == "REMINDER_ALLOWED_ROLE_IDS" else [],
            ):
                with patch("cogs.reminder.get_setting", return_value="America/Chicago"):
                    await ReminderCog.reminder.get_command("add").callback(
                        self.cog,
                        interaction,
                        "Submit the event plan",
                        "2026-07-01 7:30 PM",
                        channel,
                    )

        self.assertTrue(interaction.response.deferred)
        self.assertTrue(interaction.response.defer_ephemeral)
        self.assertTrue(interaction.followup.sent_messages)
        _args, kwargs = interaction.followup.sent_messages[0]
        self.assertEqual(kwargs["embed"].title, "Reminder Scheduled")

    async def test_send_one_reminder_mentions_target_and_marks_sent(self):
        scheduled_at = parse_local_datetime(
            "2026-07-01 7:30 PM",
            ZoneInfo("America/Chicago"),
        )
        row = await self.cog.insert_reminder(
            guild_id=123,
            creator_user_id=10,
            target_user_id=20,
            channel_id=456,
            message="Submit the event plan",
            scheduled_at_utc=scheduled_at,
        )
        channel = FakeChannel()
        self.bot.channels[456] = channel

        await self.cog.send_one_reminder(row)

        self.assertEqual(channel.sent_messages[0]["content"], "<@20>")
        self.assertEqual(channel.sent_messages[0]["embed"].title, "Reminder")
        stored = await self.cog.fetch_one(
            "SELECT status, sent_at_utc FROM reminders WHERE id = ?",
            (row["id"],),
        )
        self.assertEqual(stored["status"], "sent")
        self.assertIsNotNone(stored["sent_at_utc"])

    async def test_unsendable_channel_marks_reminder_failed(self):
        scheduled_at = parse_local_datetime(
            "2026-07-01 7:30 PM",
            ZoneInfo("America/Chicago"),
        )
        row = await self.cog.insert_reminder(
            guild_id=123,
            creator_user_id=10,
            target_user_id=20,
            channel_id=456,
            message="Submit the event plan",
            scheduled_at_utc=scheduled_at,
        )
        self.bot.channels[456] = object()

        with patch("cogs.reminder.logger.warning"):
            await self.cog.send_one_reminder(row)

        stored = await self.cog.fetch_one(
            "SELECT status, failure_reason FROM reminders WHERE id = ?",
            (row["id"],),
        )
        self.assertEqual(stored["status"], "failed")
        self.assertIn("cannot receive messages", stored["failure_reason"])


if __name__ == "__main__":
    unittest.main()
