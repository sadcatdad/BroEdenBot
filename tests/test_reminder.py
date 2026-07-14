import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import aiosqlite

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from cogs import reminder
from cogs.reminder import (
    ReminderCog,
    parse_id_set,
    parse_local_datetime,
    timestamp_codes_embed,
)


class DummyBot:
    def __init__(self, database):
        self.db = database
        self.user = SimpleNamespace(id=999)
        self.channels = {}
        self.users = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_user(self, user_id):
        return self.users.get(user_id)

    async def fetch_user(self, user_id):
        return self.users.get(user_id)


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class FakeMember:
    def __init__(self, user_id, *, roles=(), administrator=False):
        self.id = user_id
        self.roles = list(roles)
        self.guild_permissions = SimpleNamespace(administrator=administrator)
        self.dm_messages = []

    async def send(self, **kwargs):
        self.dm_messages.append(kwargs)
        return SimpleNamespace(id=900 + len(self.dm_messages))


class FakeInteraction:
    def __init__(self, user):
        self.guild = object()
        self.guild_id = 123
        self.user = user


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.sent_messages = []
        self.modal = None
        self.edited_message = None

    def is_done(self):
        return self.deferred or bool(self.sent_messages)

    async def defer(self, *, ephemeral=False, thinking=False):
        self.deferred = True
        self.defer_ephemeral = ephemeral
        self.defer_thinking = thinking

    async def send_message(self, *args, **kwargs):
        self.sent_messages.append((args, kwargs))

    async def send_modal(self, modal):
        self.modal = modal

    async def edit_message(self, **kwargs):
        self.edited_message = kwargs


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
        self.channel = None


class FakeContext:
    def __init__(self, author):
        self.guild = SimpleNamespace(id=123)
        self.author = author
        self.sent_messages = []

    async def send(self, *args, **kwargs):
        self.sent_messages.append((args, kwargs))


class FakeChannel:
    id = 456
    name = "main-stage"
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
        return SimpleNamespace(id=700 + len(self.sent_messages))


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

    def test_parse_local_datetime_accepts_relative_and_conversational_phrases(self):
        timezone_value = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)  # 8:00 AM PDT

        in_two_hours = parse_local_datetime(
            "in 2 hours",
            timezone_value,
            now=now,
        )
        tomorrow_morning = parse_local_datetime(
            "tomorrow at 9am",
            timezone_value,
            now=now,
        )
        combined = parse_local_datetime(
            "in 1 day and 30 minutes",
            timezone_value,
            now=now,
        )

        self.assertEqual(in_two_hours.isoformat(), "2026-07-13T17:00:00+00:00")
        self.assertEqual(tomorrow_morning.isoformat(), "2026-07-14T16:00:00+00:00")
        self.assertEqual(combined.isoformat(), "2026-07-14T15:30:00+00:00")

    def test_parse_local_datetime_does_not_require_at_before_clock_time(self):
        timezone_value = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)  # Monday, 8 AM

        today = parse_local_datetime("today 9am", timezone_value, now=now)
        tomorrow = parse_local_datetime("tomorrow 6:30pm", timezone_value, now=now)
        friday = parse_local_datetime("Friday 7pm", timezone_value, now=now)

        self.assertEqual(today.isoformat(), "2026-07-13T16:00:00+00:00")
        self.assertEqual(tomorrow.isoformat(), "2026-07-15T01:30:00+00:00")
        self.assertEqual(friday.isoformat(), "2026-07-18T02:00:00+00:00")

    def test_parse_local_datetime_accepts_weekday_phrase(self):
        timezone_value = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)  # Monday

        parsed = parse_local_datetime(
            "Friday at 7:30pm",
            timezone_value,
            now=now,
        )

        self.assertEqual(parsed.isoformat(), "2026-07-18T02:30:00+00:00")

    def test_parse_id_set_accepts_csv_and_dashboard_json(self):
        self.assertEqual(
            parse_id_set('123, 456 ["789"] nope'),
            {123, 456, 789},
        )

    def test_timestamp_codes_embed_identifies_parser_timezone(self):
        value = datetime(2026, 7, 14, 16, 0, tzinfo=timezone.utc)

        embed = timestamp_codes_embed(value, "America/New_York")

        self.assertEqual(embed.title, "TIME CODES")
        self.assertIn("`America/New_York`", embed.description)
        self.assertIn("`<t:1784044800:F>`", embed.description)
        self.assertIn("<t:1784044800:R>", embed.description)


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

    async def test_personal_timezone_overrides_server_fallback(self):
        with patch("cogs.reminder.get_setting", return_value="America/Los_Angeles"):
            self.assertEqual(
                await self.cog.user_timezone_name(123, 10),
                "America/Los_Angeles",
            )

            await self.cog.save_user_timezone(123, 10, "Europe/London")

            self.assertEqual(
                await self.cog.user_timezone_name(123, 10),
                "Europe/London",
            )
            self.assertEqual(
                await self.cog.user_timezone(123, 10),
                ZoneInfo("Europe/London"),
            )

    async def test_time_slash_command_is_private_and_uses_personal_timezone(self):
        staff = FakeMember(10, roles=[FakeRole(55)])
        interaction = FakeCommandInteraction(staff)
        await self.cog.save_user_timezone(123, 10, "Europe/London")

        with patch.object(reminder.discord, "Member", FakeMember):
            with patch(
                "cogs.reminder.get_csv_ids_setting",
                side_effect=lambda key: [55] if key == "REMINDER_ALLOWED_ROLE_IDS" else [],
            ):
                with patch("cogs.reminder.get_setting", return_value="America/Los_Angeles"):
                    await ReminderCog.time_command.callback(
                        self.cog,
                        interaction,
                        "tomorrow at 9am",
                    )

        _args, kwargs = interaction.response.sent_messages[0]
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn("`Europe/London`", kwargs["embed"].description)

    async def test_time_prefix_command_is_public_for_staff(self):
        staff = FakeMember(10, roles=[FakeRole(55)])
        context = FakeContext(staff)
        await self.cog.save_user_timezone(123, 10, "Asia/Tokyo")

        with patch.object(reminder.discord, "Member", FakeMember):
            with patch(
                "cogs.reminder.get_csv_ids_setting",
                side_effect=lambda key: [55] if key == "REMINDER_ALLOWED_ROLE_IDS" else [],
            ):
                with patch("cogs.reminder.get_setting", return_value="America/Los_Angeles"):
                    await ReminderCog.time_prefix.callback(
                        self.cog,
                        context,
                        when="tomorrow at 9am",
                    )

        _args, kwargs = context.sent_messages[0]
        self.assertIn("`Asia/Tokyo`", kwargs["embed"].description)
        self.assertNotIn("ephemeral", kwargs)

    async def test_subscription_schema_adds_destination_columns_to_existing_database(self):
        database = await aiosqlite.connect(":memory:")
        try:
            await database.execute(
                """
                CREATE TABLE reminder_subscription_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT,
                    creator_user_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    scheduled_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    failure_reason TEXT,
                    created_at_utc TEXT NOT NULL,
                    completed_at_utc TEXT
                )
                """
            )
            await database.commit()
            cog = ReminderCog(DummyBot(database))

            await cog.create_schema()

            cursor = await database.execute(
                "PRAGMA table_info(reminder_subscription_posts)"
            )
            columns = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
            self.assertIn("destination_channel_id", columns)
            self.assertIn("destination_channel_name", columns)
        finally:
            await database.close()

    async def test_add_command_opens_creation_modal(self):
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
                        channel,
                    )

        self.assertIsInstance(interaction.response.modal, reminder.ReminderCreateModal)
        self.assertIsNone(interaction.response.modal.target)

    async def test_modal_creation_defaults_to_no_automatic_ping(self):
        staff = FakeMember(10, roles=[FakeRole(55)])
        interaction = FakeCommandInteraction(staff)
        channel = FakeChannel()

        with patch.object(reminder.discord, "Member", FakeMember):
            with patch(
                "cogs.reminder.get_csv_ids_setting",
                side_effect=lambda key: [55] if key == "REMINDER_ALLOWED_ROLE_IDS" else [],
            ):
                with patch("cogs.reminder.get_setting", return_value="America/Chicago"):
                    await self.cog.create_from_modal(
                        interaction,
                        channel=channel,
                        target=None,
                        message="# Event reminder\n- Bring your notes",
                        date_time="in 2 hours",
                    )

        self.assertTrue(interaction.response.deferred)
        _args, kwargs = interaction.followup.sent_messages[0]
        self.assertEqual(kwargs["embed"].title, "Reminder Scheduled")
        automatic_ping = next(
            field for field in kwargs["embed"].fields if field.name == "Ping:"
        )
        self.assertEqual(automatic_ping.value, "Nobody")
        self.assertEqual(
            [field.name for field in kwargs["embed"].fields],
            ["Ping:", "Channel:", "Scheduled For:"],
        )
        self.assertRegex(kwargs["embed"].fields[2].value, r"^<t:\d+:f>$")
        stored = await self.cog.fetch_one("SELECT target_user_id FROM reminders")
        self.assertEqual(stored["target_user_id"], "")

    async def test_remind_subscribe_command_opens_modal_in_current_channel(self):
        staff = FakeMember(10, roles=[FakeRole(55)])
        interaction = FakeCommandInteraction(staff)
        interaction.channel = FakeChannel()

        with patch.object(reminder.discord, "Member", FakeMember):
            with patch(
                "cogs.reminder.get_csv_ids_setting",
                side_effect=lambda key: [55] if key == "REMINDER_ALLOWED_ROLE_IDS" else [],
            ):
                with patch("cogs.reminder.get_setting", return_value="America/Chicago"):
                    await ReminderCog.remind.get_command("subscribe").callback(
                        self.cog,
                        interaction,
                    )

        self.assertIsInstance(interaction.response.modal, reminder.RemindSubscribeModal)
        self.assertIs(interaction.response.modal.channel, interaction.channel)
        destination_labels = [
            item for item in interaction.response.modal.children
            if isinstance(item, reminder.discord.ui.Label) and item.text == "WHERE:"
        ]
        self.assertEqual(len(destination_labels), 1)
        self.assertIs(
            destination_labels[0].component,
            interaction.response.modal.destination,
        )

    async def test_subscription_modal_posts_public_bell_embed(self):
        staff = FakeMember(10, roles=[FakeRole(55)])
        interaction = FakeCommandInteraction(staff)
        channel = FakeChannel()

        with patch.object(reminder.discord, "Member", FakeMember):
            with patch(
                "cogs.reminder.get_csv_ids_setting",
                side_effect=lambda key: [55] if key == "REMINDER_ALLOWED_ROLE_IDS" else [],
            ):
                with patch("cogs.reminder.get_setting", return_value="America/Chicago"):
                    await self.cog.create_subscription_post(
                        interaction,
                        channel=channel,
                        destination=channel,
                        message="# Shuffle Storm\n- Join the stage",
                        date_time="in 1 hour",
                    )

        sent = channel.sent_messages[0]
        self.assertEqual(sent["embed"].footer.text, "🔔 Subscribe to DM Reminder")
        self.assertEqual(sent["embed"].fields[0].name, "WHEN:")
        self.assertEqual(sent["embed"].fields[1].name, "WHERE:")
        self.assertEqual(sent["embed"].fields[1].value, "<#456>")
        self.assertEqual(sent["view"].children[0].emoji.name, "🔔")
        row = await self.cog.fetch_one("SELECT * FROM reminder_subscription_posts")
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["message_id"], "701")
        self.assertEqual(row["destination_channel_id"], "456")
        self.assertEqual(row["destination_channel_name"], "main-stage")

    async def test_member_can_subscribe_receive_confirmation_and_cancel(self):
        scheduled_at = datetime.now(timezone.utc) + timedelta(hours=1)
        cursor = await self.database.execute(
            """
            INSERT INTO reminder_subscription_posts (
                guild_id, channel_id, message_id, destination_channel_id,
                destination_channel_name, creator_user_id,
                message, scheduled_at_utc, status, created_at_utc
            ) VALUES ('123', '456', '700', '456', 'main-stage', '10', ?, ?, 'open', ?)
            """,
            ("Shuffle Storm starts soon", scheduled_at.isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        post_id = cursor.lastrowid
        await cursor.close()
        await self.database.commit()
        member = FakeMember(42)
        subscribe_interaction = FakeCommandInteraction(member)

        await self.cog.handle_subscription_join(subscribe_interaction, int(post_id))

        self.assertEqual(member.dm_messages[0]["embed"].title, "🔔 Reminder Confirmation")
        self.assertIn(
            "[Open #main-stage](https://discord.com/channels/123/456)",
            member.dm_messages[0]["embed"].description,
        )
        cancel_button = member.dm_messages[0]["view"].children[0]
        self.assertEqual(cancel_button.label, "Cancel Reminder")
        subscriber = await self.cog.fetch_one("SELECT * FROM reminder_subscribers")
        self.assertEqual(subscriber["status"], "subscribed")
        self.assertEqual(subscriber["dm_confirmation_message_id"], "901")

        cancel_interaction = FakeCommandInteraction(member)
        await self.cog.handle_subscription_cancel(
            cancel_interaction,
            int(subscriber["id"]),
        )

        subscriber = await self.cog.fetch_one("SELECT * FROM reminder_subscribers")
        self.assertEqual(subscriber["status"], "cancelled")
        self.assertEqual(
            cancel_interaction.response.edited_message["embed"].title,
            "🔕 Reminder Cancelled",
        )
        self.assertTrue(
            cancel_interaction.response.edited_message["view"].children[0].disabled
        )

    async def test_due_subscription_is_persistently_delivered_by_dm(self):
        scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        cursor = await self.database.execute(
            """
            INSERT INTO reminder_subscription_posts (
                guild_id, channel_id, message_id, destination_channel_id,
                destination_channel_name, creator_user_id,
                message, scheduled_at_utc, status, created_at_utc
            ) VALUES ('123', '456', '700', '456', 'main-stage', '10', ?, ?, 'open', ?)
            """,
            ("Shuffle Storm starts now", scheduled_at.isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        post_id = int(cursor.lastrowid)
        await cursor.close()
        await self.database.execute(
            """
            INSERT INTO reminder_subscribers (
                post_id, user_id, status, subscribed_at_utc
            ) VALUES (?, '42', 'subscribed', ?)
            """,
            (post_id, datetime.now(timezone.utc).isoformat()),
        )
        await self.database.commit()
        member = FakeMember(42)
        self.bot.users[42] = member

        await self.cog.send_due_subscription_reminders()

        self.assertEqual(member.dm_messages[0]["embed"].title, "🔔 Reminder")
        self.assertIn(
            "[Open #main-stage](https://discord.com/channels/123/456)",
            member.dm_messages[0]["embed"].description,
        )
        subscriber = await self.cog.fetch_one("SELECT * FROM reminder_subscribers")
        post = await self.cog.fetch_one("SELECT * FROM reminder_subscription_posts")
        self.assertEqual(subscriber["status"], "sent")
        self.assertEqual(subscriber["attempt_count"], 1)
        self.assertEqual(post["status"], "completed")

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
        posted_embed = channel.sent_messages[0]["embed"]
        self.assertIsNone(posted_embed.title)
        self.assertEqual(posted_embed.description, "Submit the event plan")
        self.assertEqual(len(posted_embed.fields), 0)
        self.assertIsNone(posted_embed.footer.text)
        stored = await self.cog.fetch_one(
            "SELECT status, sent_at_utc FROM reminders WHERE id = ?",
            (row["id"],),
        )
        self.assertEqual(stored["status"], "sent")
        self.assertIsNotNone(stored["sent_at_utc"])

    async def test_send_one_reminder_without_target_has_no_automatic_ping(self):
        scheduled_at = parse_local_datetime(
            "2026-07-01 7:30 PM",
            ZoneInfo("America/Chicago"),
        )
        row = await self.cog.insert_reminder(
            guild_id=123,
            creator_user_id=10,
            target_user_id=None,
            channel_id=456,
            message="Event starts soon",
            scheduled_at_utc=scheduled_at,
        )
        channel = FakeChannel()
        self.bot.channels[456] = channel

        await self.cog.send_one_reminder(row)

        self.assertIsNone(channel.sent_messages[0]["content"])

    async def test_explicit_message_user_and_role_mentions_are_pinged_once(self):
        scheduled_at = parse_local_datetime(
            "2026-07-01 7:30 PM",
            ZoneInfo("America/Chicago"),
        )
        row = await self.cog.insert_reminder(
            guild_id=123,
            creator_user_id=10,
            target_user_id=None,
            channel_id=456,
            message="Please join <@20> and <@&30>. <@20>",
            scheduled_at_utc=scheduled_at,
        )
        channel = FakeChannel()
        self.bot.channels[456] = channel

        await self.cog.send_one_reminder(row)

        self.assertEqual(channel.sent_messages[0]["content"], "<@20> <@&30>")

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
