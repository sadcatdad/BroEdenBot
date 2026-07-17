import asyncio
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
    parse_recurrence,
    timestamp_codes_embed,
)
from utils.reminder_service import (
    ReminderService,
    parse_offsets,
    recurrence_dates,
    timing_summary,
)


FUTURE = datetime(2035, 7, 14, 20, 0, tzinfo=timezone.utc)


class DummyBot:
    def __init__(self, database):
        self.db = database
        self.user = SimpleNamespace(id=999)
        self.channels = {}
        self.users = {}
        self.views = []

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_user(self, user_id):
        return self.users.get(user_id)

    async def fetch_user(self, user_id):
        return self.users.get(user_id)

    def add_view(self, view, *, message_id=None):
        self.views.append((view, message_id))


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class FakeMember:
    def __init__(self, user_id, *, roles=(), administrator=False, dm_error=None):
        self.id = user_id
        self.roles = list(roles)
        self.guild_permissions = SimpleNamespace(administrator=administrator)
        self.dm_messages = []
        self.dm_error = dm_error

    async def send(self, **kwargs):
        if self.dm_error:
            raise self.dm_error
        self.dm_messages.append(kwargs)
        return SimpleNamespace(id=900 + len(self.dm_messages))


class FakePermissions:
    view_channel = True
    send_messages = True
    send_messages_in_threads = True
    embed_links = True


class FakeChannel:
    def __init__(self, channel_id=456, *, name="main-stage"):
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(id=123, me=object())
        self.guild_id = 123
        self.type = reminder.discord.ChannelType.text
        self.sent_messages = []
        self.messages = {}

    @property
    def mention(self):
        return f"<#{self.id}>"

    def permissions_for(self, _member):
        return FakePermissions()

    async def send(self, **kwargs):
        self.sent_messages.append(kwargs)
        message = FakeMessage(700 + len(self.sent_messages))
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        return self.messages.get(message_id)


class FakeMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


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


class FakeInteraction:
    def __init__(self, user, channel=None):
        self.guild = SimpleNamespace(id=123)
        self.guild_id = 123
        self.user = user
        self.channel = channel or FakeChannel()
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.type = reminder.discord.InteractionType.application_command
        self.data = {}


class ReminderParsingTests(unittest.TestCase):
    def test_parse_supported_phrases_and_formats(self):
        tz = ZoneInfo("America/Chicago")
        now = datetime(2035, 7, 14, 10, 0, tzinfo=tz)
        self.assertEqual(parse_local_datetime("in 2 hours", tz, now=now), datetime(2035, 7, 14, 17, 0, tzinfo=timezone.utc))
        self.assertEqual(parse_local_datetime("tomorrow 9am", tz, now=now), datetime(2035, 7, 15, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(parse_local_datetime("next Friday at 8pm", tz, now=now), datetime(2035, 7, 28, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(parse_local_datetime("2035-07-20 7:30 PM", tz), datetime(2035, 7, 21, 0, 30, tzinfo=timezone.utc))

    def test_date_only_uses_nine_am(self):
        self.assertEqual(
            parse_local_datetime("2035-07-20", ZoneInfo("America/Chicago")),
            datetime(2035, 7, 20, 14, 0, tzinfo=timezone.utc),
        )

    def test_invalid_date_has_useful_error(self):
        with self.assertRaisesRegex(ValueError, "could not understand"):
            parse_local_datetime("the thirteenth of never", ZoneInfo("UTC"))

    def test_offsets_are_deduplicated_limited_and_labeled(self):
        self.assertEqual(parse_offsets("1d, 15m, start, 15m"), (1440, 15, 0))
        self.assertIn("When the event begins", timing_summary((15, 0)))
        with self.assertRaisesRegex(ValueError, "30 days"):
            parse_offsets("31d")

    def test_recurrence_preserves_local_wall_clock_across_dst(self):
        first = datetime(2035, 3, 4, 9, 0, tzinfo=ZoneInfo("America/Chicago"))
        values = recurrence_dates(first, "weekly", count=3)
        local = [value.astimezone(ZoneInfo("America/Chicago")) for value in values]
        self.assertEqual([value.hour for value in local], [9, 9, 9])

    def test_monthly_recurrence_clamps_short_months(self):
        values = recurrence_dates(datetime(2035, 1, 31, 9, tzinfo=timezone.utc), "monthly", count=3)
        self.assertEqual([value.day for value in values], [31, 28, 31])

    def test_recurrence_end_count_and_date_syntax(self):
        kind, interval, count, end_at = parse_recurrence(
            "weekly for 10",
            None,
            ZoneInfo("UTC"),
        )
        self.assertEqual((kind, interval, count, end_at), ("weekly", 1, 10, None))
        kind, interval, count, end_at = parse_recurrence(
            "every 3 days until 2035-08-01",
            None,
            ZoneInfo("UTC"),
        )
        self.assertEqual((kind, interval, count), ("interval", 3, 60))
        self.assertEqual(end_at, datetime(2035, 8, 1, 9, tzinfo=timezone.utc))

    def test_parse_ids_and_timestamp_embed(self):
        self.assertEqual(parse_id_set('123, ["456"]'), {123, 456})
        embed = timestamp_codes_embed(datetime(2035, 7, 14, 16, tzinfo=timezone.utc), "UTC")
        self.assertIn("`UTC`", embed.description)
        self.assertIn(":R>", embed.description)


class ReminderServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = await aiosqlite.connect(":memory:")
        self.database.row_factory = aiosqlite.Row
        self.service = ReminderService(self.database)
        await self.service.initialize()

    async def asyncTearDown(self):
        await self.database.close()

    async def create_event(self, **values):
        defaults = dict(
            reminder_type="event",
            guild_id=123,
            creator_user_id=10,
            title="Movie Night",
            description="Bring popcorn",
            scheduled_at_utc=FUTURE,
            interpretation_timezone="America/Chicago",
            destination_channel_id=456,
            destination_channel_name="main-stage",
            public_channel_id=456,
            default_offsets=(60, 15, 0),
        )
        defaults.update(values)
        return await self.service.create_reminder(**defaults)

    async def test_personal_creation_builds_occurrence_and_delivery(self):
        row = await self.service.create_reminder(
            reminder_type="personal",
            guild_id=123,
            creator_user_id=10,
            target_user_id=10,
            title="Submit plan",
            description="Before review",
            scheduled_at_utc=FUTURE,
            interpretation_timezone="UTC",
        )
        occurrences = await self.service.fetch_all("SELECT * FROM reminder_occurrences WHERE reminder_id = ?", (row["id"],))
        deliveries = await self.service.fetch_all("SELECT * FROM reminder_deliveries")
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(deliveries[0]["delivery_mode"], "dm")
        self.assertEqual(deliveries[0]["trigger_key"], "start")

    async def test_daily_weekly_monthly_and_interval_occurrences(self):
        for recurrence, interval in (("daily", 1), ("weekly", 1), ("monthly", 1), ("interval", 3)):
            row = await self.create_event(
                title=f"{recurrence} event",
                recurrence_type=recurrence,
                recurrence_interval=interval,
                recurrence_end_count=4,
            )
            count = await self.service.fetch_one("SELECT COUNT(*) AS total FROM reminder_occurrences WHERE reminder_id = ?", (row["id"],))
            self.assertEqual(count["total"], 4)

    async def test_list_public_events_returns_future_upcoming_only(self):
        soon = await self.create_event(title="Soon", scheduled_at_utc=FUTURE)
        later = await self.create_event(title="Later", scheduled_at_utc=FUTURE + timedelta(days=2))
        # A past event, a cancelled event, and a personal reminder must all be excluded.
        past = await self.create_event(title="Past")
        await self.database.execute(
            "UPDATE reminder_items SET scheduled_at_utc = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", int(past["id"])),
        )
        await self.database.commit()
        cancelled = await self.create_event(title="Cancelled")
        await self.service.cancel_reminder(int(cancelled["id"]), 10, staff=True)
        await self.service.create_reminder(
            reminder_type="personal", guild_id=123, creator_user_id=10, target_user_id=10,
            title="Personal", description="", scheduled_at_utc=FUTURE, interpretation_timezone="UTC",
        )
        await self.service.subscribe(int(soon["id"]), 42)

        rows = await self.service.list_public_events(123)
        self.assertEqual([row["title"] for row in rows], ["Soon", "Later"])
        self.assertEqual(int(rows[0]["id"]), int(soon["id"]))
        self.assertEqual(rows[0]["subscriber_count"], 1)
        self.assertEqual(int(rows[1]["id"]), int(later["id"]))

    async def test_subscribe_is_idempotent_and_delivery_unique(self):
        event = await self.create_event()
        first, first_created = await self.service.subscribe(int(event["id"]), 42)
        second, second_created = await self.service.subscribe(int(event["id"]), 42)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["id"], second["id"])
        count = await self.service.fetch_one("SELECT COUNT(*) AS total FROM reminder_deliveries WHERE subscription_id = ?", (first["id"],))
        self.assertEqual(count["total"], 3)

    async def test_concurrent_subscriptions_create_one_record(self):
        event = await self.create_event()
        await asyncio.gather(
            self.service.subscribe(int(event["id"]), 42),
            self.service.subscribe(int(event["id"]), 42),
        )
        count = await self.service.fetch_one("SELECT COUNT(*) AS total FROM reminder_subscriptions")
        self.assertEqual(count["total"], 1)

    async def test_customize_restore_defaults_and_unsubscribe(self):
        event = await self.create_event()
        subscription, _ = await self.service.subscribe(int(event["id"]), 42)
        await self.service.update_subscription_offsets(int(subscription["id"]), 42, (1440, 0))
        custom = await self.service.fetch_one("SELECT custom_offsets_json FROM reminder_subscriptions WHERE id = ?", (subscription["id"],))
        self.assertEqual(parse_offsets(custom["custom_offsets_json"]), (1440, 0))
        await self.service.update_subscription_offsets(int(subscription["id"]), 42, None)
        restored = await self.service.fetch_one("SELECT custom_offsets_json FROM reminder_subscriptions WHERE id = ?", (subscription["id"],))
        self.assertIsNone(restored["custom_offsets_json"])
        self.assertTrue(await self.service.unsubscribe(int(subscription["id"]), 42))
        pending = await self.service.fetch_one("SELECT COUNT(*) AS total FROM reminder_deliveries WHERE status = 'pending'")
        self.assertEqual(pending["total"], 0)

    async def test_cancelled_event_rejects_subscriptions_and_cancels_deliveries(self):
        event = await self.create_event()
        await self.service.subscribe(int(event["id"]), 42)
        await self.service.cancel_reminder(int(event["id"]), 10)
        with self.assertRaisesRegex(ValueError, "no longer accepting"):
            await self.service.subscribe(int(event["id"]), 50)
        statuses = {row["status"] for row in await self.service.fetch_all("SELECT status FROM reminder_deliveries")}
        self.assertEqual(statuses, {"cancelled"})

    async def test_customization_can_be_disabled(self):
        event = await self.create_event(allow_custom_timing=False)
        with self.assertRaisesRegex(ValueError, "organizer"):
            await self.service.subscribe(int(event["id"]), 42, offsets=(15,))

    async def test_claim_is_atomic_and_sent_delivery_is_not_reclaimed(self):
        now = datetime(2035, 7, 14, 19, 45, tzinfo=timezone.utc)
        event = await self.create_event(default_offsets=(15,))
        await self.service.subscribe(int(event["id"]), 42)
        claimed = await self.service.claim_due_deliveries(now=now)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(await self.service.claim_due_deliveries(now=now), [])
        await self.service.mark_delivery_sent(int(claimed[0]["id"]))
        self.assertEqual(await self.service.claim_due_deliveries(now=now), [])

    async def test_temporary_failure_retries_and_permanent_failure_disables_subscription(self):
        event = await self.create_event(default_offsets=(15, 0))
        subscription, _ = await self.service.subscribe(int(event["id"]), 42)
        claimed = await self.service.claim_due_deliveries(now=datetime(2035, 7, 14, 19, 45, tzinfo=timezone.utc))
        status = await self.service.mark_delivery_failed(int(claimed[0]["id"]), "discord_temporary", "HTTP 503", permanent=False)
        self.assertEqual(status, "retry")
        await self.database.execute("UPDATE reminder_deliveries SET status = 'claimed' WHERE id = ?", (claimed[0]["id"],))
        await self.database.commit()
        status = await self.service.mark_delivery_failed(int(claimed[0]["id"]), "dm_privacy", "Forbidden", permanent=True)
        self.assertEqual(status, "permanent_failure")
        stored = await self.service.fetch_one("SELECT status FROM reminder_subscriptions WHERE id = ?", (subscription["id"],))
        self.assertEqual(stored["status"], "delivery_unavailable")

    async def test_dashboard_retry_uses_service_and_rejects_permanent_failure(self):
        event = await self.create_event(default_offsets=(15,))
        subscription, _ = await self.service.subscribe(int(event["id"]), 42)
        delivery = await self.service.fetch_one(
            "SELECT * FROM reminder_deliveries WHERE subscription_id = ? LIMIT 1",
            (subscription["id"],),
        )
        await self.database.execute(
            "UPDATE reminder_deliveries SET status = 'failed', attempt_count = 4 WHERE id = ?",
            (delivery["id"],),
        )
        await self.database.commit()
        self.assertTrue(
            await self.service.retry_failed_delivery(
                int(event["id"]), int(delivery["id"]), 99
            )
        )
        retried = await self.service.fetch_one(
            "SELECT status, attempt_count FROM reminder_deliveries WHERE id = ?",
            (delivery["id"],),
        )
        self.assertEqual((retried["status"], retried["attempt_count"]), ("pending", 0))
        await self.database.execute(
            "UPDATE reminder_deliveries SET status = 'permanent_failure' WHERE id = ?",
            (delivery["id"],),
        )
        await self.database.commit()
        self.assertFalse(
            await self.service.retry_failed_delivery(
                int(event["id"]), int(delivery["id"]), 99
            )
        )

    async def test_stale_delivery_is_not_sent_after_grace(self):
        event = await self.create_event(default_offsets=(0,))
        await self.service.subscribe(int(event["id"]), 42)
        with patch("utils.reminder_service.utc_now", return_value=FUTURE + timedelta(hours=5)):
            await self.service.reconcile_deliveries()
        delivery = await self.service.fetch_one("SELECT status FROM reminder_deliveries")
        self.assertEqual(delivery["status"], "stale")

    async def test_expired_claim_is_recovered_after_restart(self):
        event = await self.create_event(default_offsets=(15,))
        await self.service.subscribe(int(event["id"]), 42)
        claimed = await self.service.claim_due_deliveries(now=datetime(2035, 7, 14, 19, 45, tzinfo=timezone.utc))
        await self.database.execute("UPDATE reminder_deliveries SET lease_expires_at_utc = ? WHERE id = ?", ((FUTURE - timedelta(hours=1)).isoformat(), claimed[0]["id"]))
        await self.database.commit()
        with patch("utils.reminder_service.utc_now", return_value=FUTURE):
            await self.service.reconcile_deliveries()
        row = await self.service.fetch_one("SELECT status FROM reminder_deliveries WHERE id = ?", (claimed[0]["id"],))
        self.assertEqual(row["status"], "retry")

    async def test_edit_reschedules_without_duplicate_delivery(self):
        event = await self.create_event(default_offsets=(15,))
        subscription, _ = await self.service.subscribe(int(event["id"]), 42)
        updated, changes = await self.service.update_reminder(
            int(event["id"]), 10, title="New Movie Night", description="New text",
            scheduled_at_utc=FUTURE + timedelta(hours=1), default_offsets=(60, 0),
        )
        self.assertIn("title", changes)
        self.assertIn("scheduled_at_utc", changes)
        rows = await self.service.fetch_all("SELECT * FROM reminder_deliveries WHERE subscription_id = ? AND status = 'pending'", (subscription["id"],))
        self.assertEqual({row["trigger_key"] for row in rows}, {"before:60", "start"})
        self.assertEqual(updated["title"], "New Movie Night")

    async def test_trivial_description_edit_is_audited(self):
        event = await self.create_event()
        _updated, changes = await self.service.update_reminder(int(event["id"]), 10, description="Formatting only")
        self.assertEqual(set(changes), {"description"})
        audit = await self.service.fetch_one("SELECT action FROM reminder_audit WHERE reminder_id = ? ORDER BY id DESC", (event["id"],))
        self.assertEqual(audit["action"], "edited")

    async def test_advance_timing_before_subscription_is_not_scheduled(self):
        event = await self.create_event(
            scheduled_at_utc=datetime.now(timezone.utc) + timedelta(minutes=10),
            default_offsets=(15, 0),
        )
        subscription, _ = await self.service.subscribe(int(event["id"]), 42)
        rows = await self.service.fetch_all(
            "SELECT trigger_key FROM reminder_deliveries WHERE subscription_id = ? AND status = 'pending'",
            (subscription["id"],),
        )
        self.assertEqual([row["trigger_key"] for row in rows], ["start"])

    async def test_reschedule_future_and_cancel_one_occurrence(self):
        event = await self.create_event(
            recurrence_type="daily",
            recurrence_end_count=3,
            default_offsets=(0,),
        )
        await self.service.subscribe(int(event["id"]), 42)
        occurrences = await self.service.fetch_all(
            "SELECT * FROM reminder_occurrences WHERE reminder_id = ? ORDER BY occurrence_index",
            (event["id"],),
        )
        target = datetime.fromisoformat(occurrences[1]["scheduled_at_utc"]) + timedelta(hours=2)
        await self.service.reschedule_occurrence(
            int(occurrences[1]["id"]),
            10,
            target,
            scope="future",
        )
        updated = await self.service.fetch_all(
            "SELECT * FROM reminder_occurrences WHERE reminder_id = ? ORDER BY occurrence_index",
            (event["id"],),
        )
        self.assertEqual(updated[0]["scheduled_at_utc"], occurrences[0]["scheduled_at_utc"])
        self.assertEqual(
            datetime.fromisoformat(updated[2]["scheduled_at_utc"]),
            datetime.fromisoformat(occurrences[2]["scheduled_at_utc"]) + timedelta(hours=2),
        )
        self.assertTrue(await self.service.cancel_occurrence(int(updated[1]["id"]), 10))
        cancelled = await self.service.fetch_one(
            "SELECT status FROM reminder_occurrences WHERE id = ?",
            (updated[1]["id"],),
        )
        self.assertEqual(cancelled["status"], "cancelled")

    async def test_archive_requires_terminal_reminder_and_preserves_history(self):
        event = await self.create_event()
        with self.assertRaisesRegex(ValueError, "Cancel"):
            await self.service.archive_reminder(int(event["id"]), 10)
        await self.service.cancel_reminder(int(event["id"]), 10)
        self.assertTrue(await self.service.archive_reminder(int(event["id"]), 10))
        stored = await self.service.get_reminder(int(event["id"]))
        audit = await self.service.fetch_all(
            "SELECT action FROM reminder_audit WHERE reminder_id = ?",
            (event["id"],),
        )
        self.assertEqual(stored["status"], "deleted")
        self.assertIn("deleted", {row["action"] for row in audit})


class ReminderMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = await aiosqlite.connect(":memory:")
        self.database.row_factory = aiosqlite.Row

    async def asyncTearDown(self):
        await self.database.close()

    async def test_legacy_records_migrate_idempotently_and_completed_stays_completed(self):
        await self.database.executescript(
            """
            CREATE TABLE reminders (id INTEGER PRIMARY KEY, guild_id TEXT, creator_user_id TEXT,
                target_user_id TEXT, channel_id TEXT, message TEXT, scheduled_at_utc TEXT,
                status TEXT, failure_reason TEXT, created_at_utc TEXT, updated_at_utc TEXT,
                sent_at_utc TEXT);
            CREATE TABLE reminder_subscription_posts (id INTEGER PRIMARY KEY, guild_id TEXT,
                channel_id TEXT, message_id TEXT, destination_channel_id TEXT,
                destination_channel_name TEXT, creator_user_id TEXT, message TEXT,
                scheduled_at_utc TEXT, status TEXT, failure_reason TEXT, created_at_utc TEXT,
                completed_at_utc TEXT);
            CREATE TABLE reminder_subscribers (id INTEGER PRIMARY KEY, post_id INTEGER,
                user_id TEXT, status TEXT, subscribed_at_utc TEXT, cancelled_at_utc TEXT,
                processing_at_utc TEXT, sent_at_utc TEXT, dm_confirmation_message_id TEXT,
                dm_reminder_message_id TEXT, attempt_count INTEGER, failure_reason TEXT);
            """
        )
        await self.database.execute(
            "INSERT INTO reminders VALUES (1,'123','10','10','456','Old personal',?,'sent',NULL,?,NULL,?)",
            (FUTURE.isoformat(), FUTURE.isoformat(), FUTURE.isoformat()),
        )
        await self.database.execute(
            "INSERT INTO reminder_subscription_posts VALUES (2,'123','456','700','456','stage','10','Old event',?,'open',NULL,?,NULL)",
            (FUTURE.isoformat(), FUTURE.isoformat()),
        )
        await self.database.execute(
            "INSERT INTO reminder_subscribers VALUES (3,2,'42','subscribed',?,NULL,NULL,NULL,NULL,NULL,0,NULL)",
            (FUTURE.isoformat(),),
        )
        await self.database.commit()
        service = ReminderService(self.database)
        report = await service.initialize()
        self.assertEqual(report.personal_migrated, 1)
        self.assertEqual(report.events_migrated, 1)
        self.assertEqual(report.subscriptions_migrated, 1)
        completed = await service.fetch_one("SELECT status FROM reminder_items WHERE legacy_source = 'reminders'")
        self.assertEqual(completed["status"], "completed")
        await service.initialize()
        count = await service.fetch_one("SELECT COUNT(*) AS total FROM reminder_items")
        self.assertEqual(count["total"], 2)

    async def test_malformed_legacy_record_is_reported_not_scheduled(self):
        await self.database.execute(
            """
            CREATE TABLE reminders (id INTEGER PRIMARY KEY, guild_id TEXT, creator_user_id TEXT,
                target_user_id TEXT, channel_id TEXT, message TEXT, scheduled_at_utc TEXT,
                status TEXT, failure_reason TEXT, created_at_utc TEXT, updated_at_utc TEXT,
                sent_at_utc TEXT)
            """
        )
        await self.database.execute("INSERT INTO reminders VALUES (1,'123','10','10','456','Bad','not-a-date','pending',NULL,?,NULL,NULL)", (FUTURE.isoformat(),))
        await self.database.commit()
        report = await ReminderService(self.database).initialize()
        self.assertEqual(report.malformed, 1)
        cursor = await self.database.execute("SELECT COUNT(*) FROM reminder_items")
        self.assertEqual((await cursor.fetchone())[0], 0)


class ReminderCommandAndRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = await aiosqlite.connect(":memory:")
        self.database.row_factory = aiosqlite.Row
        self.bot = DummyBot(self.database)
        self.cog = ReminderCog(self.bot)
        await self.cog.create_schema()

    async def asyncTearDown(self):
        await self.database.close()

    async def test_personal_is_available_to_regular_member_but_other_target_is_not(self):
        user = FakeMember(10)
        other = FakeMember(20)
        interaction = FakeInteraction(user)
        with patch.object(reminder.discord, "Member", FakeMember):
            with patch("cogs.reminder.get_csv_ids_setting", return_value=[]), patch("cogs.reminder.get_setting", return_value=""):
                self.assertTrue(self.cog.can_target_user(interaction, user))
                self.assertFalse(self.cog.can_target_user(interaction, other))

    async def test_command_role_permissions_are_independent(self):
        configured = {
            "REMINDER_PERSONAL_ALLOWED_ROLE_IDS": [101],
            "REMINDER_EVENT_ALLOWED_ROLE_IDS": [202],
            "REMINDER_MANAGE_ALLOWED_ROLE_IDS": [303],
            "REMINDER_MANAGE_ALL_ROLE_IDS": [505],
            "REMINDER_SUBSCRIPTIONS_ALLOWED_ROLE_IDS": [404],
        }

        def setting(key):
            return configured.get(key, [])

        personal_member = FakeInteraction(FakeMember(10, roles=[FakeRole(101)]))
        manager = FakeInteraction(FakeMember(11, roles=[FakeRole(505)]))
        administrator = FakeInteraction(FakeMember(12, administrator=True))
        with patch("cogs.reminder.get_csv_ids_setting", side_effect=setting), patch("cogs.reminder.get_setting", return_value=""):
            self.assertTrue(self.cog.has_remind_command_access(personal_member, "personal"))
            self.assertFalse(self.cog.has_remind_command_access(personal_member, "event"))
            self.assertFalse(self.cog.has_remind_command_access(personal_member, "manage"))
            self.assertFalse(self.cog.has_remind_command_access(personal_member, "subscriptions"))
            self.assertTrue(self.cog.has_remind_command_access(manager, "manage"))
            self.assertTrue(self.cog.has_manage_all_access(manager))
            for command in ("personal", "event", "manage", "subscriptions"):
                self.assertTrue(self.cog.has_remind_command_access(administrator, command))

    async def test_blank_role_settings_preserve_member_and_staff_defaults(self):
        def setting(key):
            return [55] if key == "REMINDER_ALLOWED_ROLE_IDS" else []

        member = FakeInteraction(FakeMember(10))
        staff = FakeInteraction(FakeMember(11, roles=[FakeRole(55)]))
        with patch("cogs.reminder.get_csv_ids_setting", side_effect=setting), patch("cogs.reminder.get_setting", return_value=""), patch.dict(os.environ, {"REMINDER_ALLOWED_ROLE_IDS": ""}):
            self.assertTrue(self.cog.has_remind_command_access(member, "personal"))
            self.assertTrue(self.cog.has_remind_command_access(member, "manage"))
            self.assertTrue(self.cog.has_remind_command_access(member, "subscriptions"))
            self.assertFalse(self.cog.has_remind_command_access(member, "event"))
            self.assertTrue(self.cog.has_remind_command_access(staff, "event"))
            self.assertTrue(self.cog.has_manage_all_access(staff))

    async def test_restricted_personal_command_is_denied_before_modal(self):
        interaction = FakeInteraction(FakeMember(10))

        def setting(key):
            return [101] if key == "REMINDER_PERSONAL_ALLOWED_ROLE_IDS" else []

        with patch("cogs.reminder.get_csv_ids_setting", side_effect=setting):
            await ReminderCog.remind.get_command("personal").callback(
                self.cog, interaction, None, None
            )
        self.assertIsNone(interaction.response.modal)
        self.assertIn("configured roles", interaction.response.sent_messages[0][0][0])

    async def test_personal_command_opens_modal(self):
        interaction = FakeInteraction(FakeMember(10))
        await ReminderCog.remind.get_command("personal").callback(self.cog, interaction, None, None)
        self.assertIsInstance(interaction.response.modal, reminder.PersonalReminderModal)

    async def test_invalid_and_past_dates_are_rejected_before_preview(self):
        interaction = FakeInteraction(FakeMember(10))
        await self.cog.preview_personal(interaction, title="Test", details="", when="nonsense", recurrence="none", count="", destination=None, target=interaction.user)
        self.assertIn("could not understand", interaction.followup.sent_messages[0][0][0])
        interaction = FakeInteraction(FakeMember(10))
        await self.cog.preview_personal(interaction, title="Test", details="", when="2020-01-01 9am", recurrence="none", count="", destination=None, target=interaction.user)
        self.assertIn("future", interaction.followup.sent_messages[0][0][0])

    async def test_event_card_uses_static_banner_and_name_subheader(self):
        event = await self.cog.service.create_reminder(
            reminder_type="event", guild_id=123, creator_user_id=10,
            title="Movie Night", description="Bottoms", scheduled_at_utc=FUTURE,
            interpretation_timezone="UTC", destination_channel_id=456,
            default_offsets=(15, 0),
        )
        embed = self.cog.event_embed(event)
        view = self.cog.event_view(event)
        self.assertEqual(embed.title, "🎉 EVENT REMINDER")
        self.assertTrue(embed.description.startswith("## Movie Night"))
        self.assertIn("Bottoms", embed.description)
        self.assertEqual(
            [field.name for field in embed.fields],
            ["🗓️ When", "📍 Where", "🎙️ Hosted by", "⏰ Reminders", "👥 Subscribers"],
        )
        self.assertEqual(view.children[0].label, "Remind Me")

    async def test_event_card_title_reflects_status(self):
        event = await self.cog.service.create_reminder(
            reminder_type="event", guild_id=123, creator_user_id=10,
            title="Trivia Night", description="", scheduled_at_utc=FUTURE,
            interpretation_timezone="UTC", destination_channel_id=456,
            default_offsets=(15, 0),
        )
        self.assertEqual(self.cog.event_embed(event).title, "🎉 EVENT REMINDER")
        self.assertEqual(self.cog.event_embed({**event, "status": "cancelled"}).title, "❌ EVENT CANCELLED")
        self.assertEqual(self.cog.event_embed({**event, "status": "completed"}).title, "✅ EVENT COMPLETE")
        # Details are optional: the name subheader still renders on its own.
        self.assertEqual(self.cog.event_embed(event).description, "## Trivia Night")

    async def test_preview_timezone_reinterprets_and_persists(self):
        when_text = "2035-07-20 8:00 PM"
        base_tz = ZoneInfo("America/Chicago")
        draft = {
            "reminder_type": "event",
            "guild_id": 123,
            "creator_user_id": 10,
            "host_user_id": 10,
            "title": "Movie Night",
            "description": "Bottoms",
            "scheduled_at_utc": parse_local_datetime(when_text, base_tz),
            "interpretation_timezone": "America/Chicago",
            "when_text": when_text,
            "recurrence_text": "",
            "destination": None,
            "public_channel": None,
            "default_offsets": (15, 0),
            "allow_custom_timing": True,
            "close_subscriptions_at_start": True,
            "keep_public_card": True,
            "auto_subscribe_creator": True,
            "recurrence_type": "none",
            "recurrence_interval": 1,
            "recurrence_end_count": None,
            "recurrence_end_at_utc": None,
        }
        view = reminder.EventCreationPreviewView(self.cog, 10, draft)
        await self.cog.apply_preview_timezone(FakeInteraction(FakeMember(10)), view, "America/New_York")
        self.assertEqual(draft["interpretation_timezone"], "America/New_York")
        self.assertEqual(draft["scheduled_at_utc"], parse_local_datetime(when_text, ZoneInfo("America/New_York")))
        self.assertNotEqual(draft["scheduled_at_utc"], parse_local_datetime(when_text, base_tz))
        self.assertEqual(await self.cog.user_timezone_name(123, 10), "America/New_York")

    async def test_events_command_lists_events_and_flags_subscribed(self):
        first = await self.cog.service.create_reminder(
            reminder_type="event", guild_id=123, creator_user_id=10,
            title="Movie Night", description="", scheduled_at_utc=FUTURE,
            interpretation_timezone="UTC", destination_channel_id=456, default_offsets=(15, 0),
        )
        second = await self.cog.service.create_reminder(
            reminder_type="event", guild_id=123, creator_user_id=10,
            title="Trivia", description="", scheduled_at_utc=FUTURE + timedelta(days=1),
            interpretation_timezone="UTC", destination_channel_id=456, default_offsets=(15, 0),
        )
        await self.cog.service.subscribe(int(first["id"]), 42)
        interaction = FakeInteraction(FakeMember(42))
        with patch("cogs.reminder.get_setting", return_value=""):
            await ReminderCog.events_command.callback(self.cog, interaction)
        _args, kwargs = interaction.followup.sent_messages[0]
        select = next(child for child in kwargs["view"].children if isinstance(child, reminder.EventBrowseSelect))
        self.assertEqual({option.value for option in select.options}, {str(first["id"]), str(second["id"])})
        flagged = next(option for option in select.options if option.value == str(first["id"]))
        self.assertEqual(str(flagged.emoji), "✅")

    async def test_bulk_subscribe_reports_created_and_existing(self):
        first = await self.cog.service.create_reminder(
            reminder_type="event", guild_id=123, creator_user_id=10,
            title="Movie Night", description="", scheduled_at_utc=FUTURE,
            interpretation_timezone="UTC", destination_channel_id=456, default_offsets=(15, 0),
        )
        second = await self.cog.service.create_reminder(
            reminder_type="event", guild_id=123, creator_user_id=10,
            title="Trivia", description="", scheduled_at_utc=FUTURE + timedelta(days=1),
            interpretation_timezone="UTC", destination_channel_id=456, default_offsets=(15, 0),
        )
        member = FakeMember(42)
        await self.cog.service.subscribe(int(first["id"]), 42)
        interaction = FakeInteraction(member)
        await self.cog.handle_events_subscribe(interaction, [int(first["id"]), int(second["id"])])
        total = await self.cog.service.fetch_one(
            "SELECT COUNT(*) AS total FROM reminder_subscriptions WHERE user_id = '42' AND status = 'active'"
        )
        self.assertEqual(total["total"], 2)
        self.assertEqual(len(member.dm_messages), 1)  # only the newly-created subscription is DMed
        summary = interaction.followup.sent_messages[0][0][0]
        self.assertIn("Subscribed to **Trivia**", summary)
        self.assertIn("Already subscribed to **Movie Night**", summary)

    async def test_duplicate_join_does_not_send_duplicate_confirmation(self):
        event = await self.cog.service.create_reminder(
            reminder_type="event", guild_id=123, creator_user_id=10,
            title="Movie Night", description="", scheduled_at_utc=FUTURE,
            interpretation_timezone="UTC", destination_channel_id=456,
            default_offsets=(15, 0),
        )
        member = FakeMember(42)
        await self.cog.handle_event_join(FakeInteraction(member), int(event["id"]))
        await self.cog.handle_event_join(FakeInteraction(member), int(event["id"]))
        self.assertEqual(len(member.dm_messages), 1)

    async def test_subscription_role_is_checked_before_join(self):
        event = await self.cog.service.create_reminder(
            reminder_type="event", guild_id=123, creator_user_id=10,
            title="Movie Night", description="", scheduled_at_utc=FUTURE,
            interpretation_timezone="UTC", destination_channel_id=456,
            default_offsets=(15,),
        )
        interaction = FakeInteraction(FakeMember(42))

        def setting(key):
            return [404] if key == "REMINDER_SUBSCRIPTIONS_ALLOWED_ROLE_IDS" else []

        with patch("cogs.reminder.get_csv_ids_setting", side_effect=setting):
            await self.cog.handle_event_join(interaction, int(event["id"]))
        self.assertIn("configured roles", interaction.followup.sent_messages[0][0][0])
        stored = await self.cog.service.fetch_one(
            "SELECT COUNT(*) AS total FROM reminder_subscriptions"
        )
        self.assertEqual(stored["total"], 0)

    async def test_dm_disabled_is_reported_and_subscription_marked_unavailable(self):
        class FakeForbidden(Exception):
            pass
        event = await self.cog.service.create_reminder(
            reminder_type="event", guild_id=123, creator_user_id=10,
            title="Movie Night", description="", scheduled_at_utc=FUTURE,
            interpretation_timezone="UTC", destination_channel_id=456,
            default_offsets=(15,),
        )
        member = FakeMember(42, dm_error=FakeForbidden())
        interaction = FakeInteraction(member)
        with patch.object(reminder.discord, "Forbidden", FakeForbidden):
            await self.cog.handle_event_join(interaction, int(event["id"]))
        self.assertIn("could not DM", interaction.followup.sent_messages[0][0][0])
        stored = await self.cog.service.fetch_one("SELECT status FROM reminder_subscriptions")
        self.assertEqual(stored["status"], "delivery_unavailable")

    async def test_legacy_commands_show_transition_notice(self):
        staff = FakeMember(10, roles=[FakeRole(55)])
        interaction = FakeInteraction(staff)
        with patch.object(reminder.discord, "Member", FakeMember):
            with patch("cogs.reminder.get_csv_ids_setting", side_effect=lambda key: [55] if key == "REMINDER_ALLOWED_ROLE_IDS" else []), patch("cogs.reminder.get_setting", return_value=""):
                await ReminderCog.remind.get_command("subscribe").callback(self.cog, interaction)
        self.assertIn("moved", interaction.response.sent_messages[0][0][0])

    async def test_persistent_event_views_restore(self):
        event = await self.cog.service.create_reminder(
            reminder_type="event", guild_id=123, creator_user_id=10,
            title="Movie Night", description="", scheduled_at_utc=FUTURE,
            interpretation_timezone="UTC", destination_channel_id=456,
            default_offsets=(0,),
        )
        await self.cog.service.set_public_message(int(event["id"]), 456, 700)
        await self.cog.restore_persistent_views()
        self.assertEqual(self.bot.views[0][1], 700)


if __name__ == "__main__":
    unittest.main()
