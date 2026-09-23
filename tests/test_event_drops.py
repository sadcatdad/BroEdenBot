import os
import re
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.ext import commands
from fastapi.testclient import TestClient

from utils.event_drops import DEFAULTS, EventDrops
from cogs.event_drops import ClaimButton, EventDropsCog, channel_order, drop_view


class EventDropsStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.s = EventDrops(Path(self.temp.name) / "data.db")
        self.s.initialize()
        self.c = self.s.save(
            "1",
            "admin",
            dict(DEFAULTS, name="Test", emoji="🐟"),
            ["10", "20", "30", "40"],
        )
        self.s.transition(self.c, "1", "start")

    def tearDown(self):
        self.temp.cleanup()

    def test_role_ping_migration_preserves_all_existing_data(self):
        from utils.event_drop_variants import VARIANT_DEFAULTS
        from scripts.migrate_event_drops import validate

        self.s.transition(self.c, "1", "pause")
        self.s.set_variants_enabled(self.c, "1", True)
        variant = self.s.save_variant(
            self.c, "1", dict(VARIANT_DEFAULTS, name="Golden", points=5)
        )
        self.s.execute(
            "INSERT INTO event_drop_assets(campaign_id,image_bytes,content_type,created_at) VALUES(?,?,?,?)",
            (self.c, b"preserved-image", "image/webp", time.time()),
        )
        self.s.execute(
            "INSERT INTO event_drop_roles VALUES(?, '88', 'excluded')", (self.c,)
        )
        self.s.transition(self.c, "1", "resume")
        live = self.drop("live")
        self.assertTrue(self.s.claim(live, "55", "1")["ok"])
        pending = self.s.queue_manual(self.c, "1", "pending", variant_id=variant)
        self.s.execute("CREATE TABLE unrelated_data(value TEXT)")
        self.s.execute("INSERT INTO unrelated_data VALUES('keep me')")
        # Recreate the exact v2 shape in this temporary, populated database.
        self.s.execute("ALTER TABLE event_drop_campaigns DROP COLUMN ping_role_id")
        self.s.execute("ALTER TABLE event_drops DROP COLUMN ping_role_id")
        self.s.execute("DELETE FROM event_drop_schema WHERE version=3")
        tables = [
            r["name"]
            for r in self.s.rows(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='event_drop_schema'"
            )
        ]
        before = {table: self.s.rows(f"SELECT * FROM {table}") for table in tables}
        self.s.initialize()
        self.s.initialize()
        for table in tables:
            after = self.s.rows(f"SELECT * FROM {table}")
            if table in ("event_drop_campaigns", "event_drops"):
                for row in after:
                    self.assertEqual(row.pop("ping_role_id"), "")
            self.assertEqual(after, before[table], table)
        validate(self.s.path)
        self.assertTrue(self.s.claim(live, "56", "1")["ok"])
        self.assertEqual(self.s.take_pending(pending)[0]["ping_role_id"], "")

    def test_role_ping_snapshot_duplicate_and_validation(self):
        self.s.transition(self.c, "1", "pause")
        values = dict(self.s.campaign(self.c), ping_role_id="123")
        self.s.save("1", "admin", values, values["channels"], self.c)
        d = self.s.queue_manual(self.c, "1", "ping")
        self.s.set_variants_enabled(self.c, "1", True)
        variant = self.s.variants(self.c)[0]["id"]
        rare = self.s.queue_manual(self.c, "1", "rare-ping", variant_id=variant)
        self.assertEqual(
            self.s.rows("SELECT ping_role_id FROM event_drops WHERE id=?", (rare,))[0][
                "ping_role_id"
            ],
            "123",
        )
        values["ping_role_id"] = "456"
        self.s.save("1", "admin", values, values["channels"], self.c)
        self.assertEqual(
            self.s.rows("SELECT ping_role_id FROM event_drops WHERE id=?", (d,))[0][
                "ping_role_id"
            ],
            "123",
        )
        copied = self.s.duplicate(self.c, "1", "admin")
        self.assertEqual(self.s.campaign(copied)["ping_role_id"], "456")
        del values["ping_role_id"]
        self.s.save("1", "admin", values, values["channels"], self.c)
        self.assertEqual(self.s.campaign(self.c)["ping_role_id"], "456")
        self.s.transition(self.c, "1", "resume")
        due = self.s.campaign(self.c)["next_drop_at"]
        self.s.tick(now=due)
        self.assertEqual(
            self.s.rows("SELECT ping_role_id FROM event_drops WHERE kind='automatic'")[
                0
            ]["ping_role_id"],
            "456",
        )
        self.s.transition(self.c, "1", "pause")
        for bad in ("@everyone", "<@&123>", "-1", "0", "1", str(2**64), "１２３"):
            with self.subTest(role=bad), self.assertRaises(ValueError):
                self.s.save(
                    "1",
                    "admin",
                    dict(values, ping_role_id=bad),
                    values["channels"],
                    self.c,
                )
        self.s.save(
            "1", "admin", dict(values, ping_role_id=""), values["channels"], self.c
        )
        self.assertEqual(self.s.campaign(self.c)["ping_role_id"], "")

    def drop(self, key="first"):
        d = self.s.queue_manual(self.c, "1", key)
        self.s.take_pending(d)
        self.s.prepare_send(d, "10")
        self.s.sent(d, 100 + d)
        return d

    def test_first_duplicate_and_unlimited_different_members(self):
        d = self.drop()
        self.assertTrue(self.s.claim(d, 1, "1")["ok"])
        self.assertFalse(self.s.claim(d, 1, "1")["ok"])
        for u in range(2, 52):
            self.assertTrue(self.s.claim(d, u, "1")["ok"])
        self.assertEqual(len(self.s.leaderboard(self.c)), 51)
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.execute(
                "INSERT INTO event_drop_claims(campaign_id,drop_id,user_id,points,claimed_at) VALUES(?,?,?,?,?)",
                (self.c, d, "1", 1, time.time()),
            )

    def test_concurrent_claims_across_connections(self):
        d = self.drop()
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(
                pool.map(lambda _: EventDrops(self.s.path).claim(d, 5, "1"), range(24))
            )
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertEqual(self.s.leaderboard(self.c)[0]["total_points"], 1)

    def test_expiry_bot_guild_and_closed_claims(self):
        d = self.drop()
        self.assertFalse(self.s.claim(d, 5, "1", bot=True)["ok"])
        self.assertFalse(self.s.claim(d, 5, "2")["ok"])
        row = self.s.rows("SELECT * FROM event_drops WHERE id=?", (d,))[0]
        self.assertFalse(self.s.claim(d, 5, "1", now=row["expires_at"])["ok"])
        self.s.tick(now=row["expires_at"])
        self.assertFalse(self.s.claim(d, 5, "1")["ok"])
        self.assertEqual(
            self.s.rows("SELECT status FROM event_drops WHERE id=?", (d,))[0]["status"],
            "expired",
        )

    def test_limit_across_simultaneous_different_drops(self):
        self.s.execute(
            "UPDATE event_drop_campaigns SET max_user_points=1 WHERE id=?", (self.c,)
        )
        drops = [self.drop("a"), self.drop("b")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda d: self.s.claim(d, 8, "1"), drops))
        self.assertEqual(sum(r["ok"] for r in result), 1)

    def test_roles_exclusion_takes_precedence(self):
        d = self.drop()
        self.s.execute(
            "INSERT INTO event_drop_roles VALUES(?,?,'eligible')", (self.c, "5")
        )
        self.s.execute(
            "INSERT INTO event_drop_roles VALUES(?,?,'excluded')", (self.c, "6")
        )
        self.assertFalse(self.s.claim(d, 1, "1")["ok"])
        self.assertFalse(self.s.claim(d, 1, "1", role_ids=[5, 6])["ok"])
        self.assertTrue(self.s.claim(d, 1, "1", role_ids=[5])["ok"])

    def test_fixed_random_and_missed_recovery(self):
        c = self.s.campaign(self.c)
        self.assertEqual(self.s.next_time(c, 100), 3700)
        c.update(schedule_mode="random")
        for _ in range(100):
            self.assertTrue(2800 <= self.s.next_time(c, 100) <= 5500)
        now = time.time()
        self.s.execute(
            "UPDATE event_drop_campaigns SET next_drop_at=? WHERE id=?",
            (now - 18000, self.c),
        )
        self.s.tick(now=now, recover=True)
        self.s.tick(now=now)
        drops = self.s.rows("SELECT * FROM event_drops")
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["status"], "missed")
        self.assertGreater(self.s.campaign(self.c)["next_drop_at"], now)

    def test_racing_schedulers_reserve_once(self):
        now = time.time()
        self.s.execute(
            "UPDATE event_drop_campaigns SET next_drop_at=? WHERE id=?",
            (now - 1, self.c),
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: EventDrops(self.s.path).tick(now=now), range(16)))
        drops = self.s.rows("SELECT * FROM event_drops WHERE kind='automatic'")
        self.assertEqual(len(drops), 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(lambda _: self.s.take_pending(drops[0]["id"]), range(16))
            )
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_pending_automatic_after_crash_is_missed(self):
        now = time.time()
        self.s.execute(
            "UPDATE event_drop_campaigns SET next_drop_at=? WHERE id=?", (now, self.c)
        )
        self.s.tick(now=now)
        self.s.tick(now=now + 1000, recover=True)
        self.assertEqual(
            self.s.rows("SELECT status FROM event_drops")[0]["status"], "missed"
        )

    def test_outside_dates_pause_and_completed_do_not_post(self):
        now = time.time()
        self.s.execute(
            "UPDATE event_drop_campaigns SET start_at=?,next_drop_at=? WHERE id=?",
            (now + 1000, now, self.c),
        )
        self.s.tick(now=now)
        self.assertEqual(self.s.rows("SELECT * FROM event_drops"), [])
        self.s.execute(
            "UPDATE event_drop_campaigns SET start_at=NULL WHERE id=?", (self.c,)
        )
        self.s.transition(self.c, "1", "pause")
        self.s.tick(now=now + 9999)
        self.assertEqual(self.s.rows("SELECT * FROM event_drops"), [])
        self.s.transition(self.c, "1", "end")
        self.s.tick(now=now + 99999)
        self.assertEqual(self.s.rows("SELECT * FROM event_drops"), [])

    def test_scheduled_start_end_and_live_drop_closure(self):
        now = time.time()
        c = self.s.save(
            "1",
            "admin",
            dict(DEFAULTS, name="Scheduled", start_at=now + 100, end_at=now + 300),
            ["10"],
        )
        self.s.transition(c, "1", "start", now=now)
        self.s.tick(now=now + 99)
        self.assertEqual(self.s.campaign(c)["status"], "scheduled")
        self.s.tick(now=now + 100)
        drops = self.s.rows("SELECT * FROM event_drops WHERE campaign_id=?", (c,))
        self.assertEqual(len(drops), 1)
        self.s.tick(now=now + 300)
        self.assertEqual(self.s.campaign(c)["status"], "completed")

    def test_pause_allows_live_claims_end_closes_them(self):
        d = self.drop()
        self.s.transition(self.c, "1", "pause")
        self.assertTrue(self.s.claim(d, 1, "1")["ok"])
        self.s.transition(self.c, "1", "end")
        self.assertFalse(self.s.claim(d, 2, "1")["ok"])
        self.assertEqual(self.s.leaderboard(self.c)[0]["total_points"], 1)
        with self.assertRaises(ValueError):
            self.s.transition(self.c, "1", "resume")

    def test_manual_idempotency_allowlist_and_schedule(self):
        before = self.s.campaign(self.c)["next_drop_at"]
        a = self.s.queue_manual(self.c, "1", "request")
        b = self.s.queue_manual(self.c, "1", "request")
        self.assertEqual(a, b)
        with self.assertRaises(ValueError):
            self.s.queue_manual(self.c, "1", "other", "999")
        self.drop("sent")
        self.assertEqual(self.s.campaign(self.c)["next_drop_at"], before)

    def test_campaign_max_counts_reserved_drops(self):
        self.s.execute(
            "UPDATE event_drop_campaigns SET max_drops=1 WHERE id=?", (self.c,)
        )
        self.s.queue_manual(self.c, "1", "a")
        with self.assertRaises(ValueError):
            self.s.queue_manual(self.c, "1", "b")

    def test_edit_points_does_not_change_existing_drop_award(self):
        d = self.drop()
        self.s.transition(self.c, "1", "pause")
        c = self.s.campaign(self.c)
        c["points"] = 5
        self.s.save("1", "admin", c, c["channels"], self.c)
        self.assertEqual(self.s.claim(d, 1, "1")["total"], 1)

    def test_validation_and_duplicate_without_results(self):
        for changes in [
            dict(random_min=90, random_max=10),
            dict(claim_minutes=0),
            dict(end_at=2, start_at=3),
            dict(thumbnail="javascript:alert(1)"),
            dict(button_label=""),
            dict(color="#bad"),
        ]:
            with self.assertRaises(ValueError):
                self.s.validate(dict(DEFAULTS, name="x", **changes), ["10"])
        c = self.s.save("1", "admin", dict(DEFAULTS, name="Empty"), [])
        with self.assertRaises(ValueError):
            self.s.transition(c, "1", "start")
        self.s.delete(c, "1")
        self.drop()
        duplicate = self.s.duplicate(self.c, "1", "admin")
        self.assertEqual(self.s.campaign(duplicate)["status"], "draft")
        self.assertEqual(self.s.leaderboard(duplicate), [])
        with self.assertRaises(ValueError):
            self.s.delete(self.c, "1")

    def test_additive_migration_preserves_unrelated_data(self):
        self.s.execute("CREATE TABLE unrelated(value TEXT)")
        self.s.execute("INSERT INTO unrelated VALUES('keep')")
        self.s.initialize()
        self.s.initialize()
        self.assertEqual(self.s.rows("SELECT * FROM unrelated"), [{"value": "keep"}])
        from scripts.migrate_event_drops import validate

        validate(self.s.path)


class EventDropsDiscordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.s = EventDrops(Path(self.temp.name) / "test.db")
        self.s.initialize()
        self.c = self.s.save(
            "1", "admin", dict(DEFAULTS, name="Discord test"), ["10", "20"]
        )
        self.s.transition(self.c, "1", "start")
        self.bot = MagicMock()
        self.bot.user = SimpleNamespace(id=999)
        self.cog = EventDropsCog(self.bot)
        self.cog.service = self.s

    async def asyncTearDown(self):
        self.temp.cleanup()

    def channel(self, cid, allowed=True):
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = cid
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=allowed,
            send_messages=allowed,
            embed_links=True,
            read_message_history=True,
            attach_files=True,
        )
        channel.send = AsyncMock(
            return_value=SimpleNamespace(
                id=9999,
                created_at=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ),
            )
        )
        return channel

    async def test_allowlist_and_inaccessible_channels(self):
        a = self.channel(10, False)
        b = self.channel(20)
        extra = self.channel(30)
        guild = MagicMock()
        guild.get_channel.side_effect = lambda cid: {10: a, 20: b, 30: extra}.get(cid)
        self.bot.get_guild.return_value = guild
        channels = await self.cog.eligible_channels(self.s.campaign(self.c))
        self.assertEqual([c.id for c in channels], [20])
        a.permissions_for.assert_called_once()
        extra.permissions_for.assert_not_called()

    async def test_drop_pings_only_frozen_role_and_missing_role_still_sends(self):
        self.s.transition(self.c, "1", "pause")
        values = dict(self.s.campaign(self.c), ping_role_id="123")
        self.s.save("1", "admin", values, values["channels"], self.c)
        d = self.s.queue_manual(self.c, "1", "ping")
        self.s.save(
            "1", "admin", dict(values, ping_role_id="456"), values["channels"], self.c
        )
        channel = self.channel(10)
        channel.guild.get_role.return_value = discord.Object(id=123)
        self.cog.eligible_channels = AsyncMock(return_value=[channel])
        with patch("cogs.event_drops.publish_audit", new=AsyncMock()):
            await self.cog.send_drop(d)
        channel.guild.get_role.assert_called_with(123)
        sent = channel.send.call_args.kwargs
        self.assertEqual(sent["content"], "<@&123>")
        mentions = sent["allowed_mentions"].to_dict()
        self.assertEqual(mentions["roles"], [123])
        self.assertEqual(mentions["parse"], [])
        self.assertFalse(sent["allowed_mentions"].replied_user)
        channel.guild.get_role.return_value = None
        missing = self.s.queue_manual(self.c, "1", "missing-role")
        with patch("cogs.event_drops.publish_audit", new=AsyncMock()):
            await self.cog.send_drop(missing)
        sent = channel.send.call_args.kwargs
        self.assertNotIn("content", sent)
        self.assertEqual(
            sent["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict()
        )
        self.assertEqual(
            self.s.rows("SELECT status FROM event_drops WHERE id=?", (missing,))[0][
                "status"
            ],
            "active",
        )

    async def test_avoidance_and_small_pool(self):
        pool = [SimpleNamespace(id=i) for i in [10, 20, 30, 40]]
        self.assertEqual(channel_order(pool, ["10", "20", "30"], 3)[0].id, 40)
        self.assertEqual(channel_order(pool[:2], ["10", "20"], 3)[0].id, 20)

    async def test_zero_channels_failure_keeps_scheduler_alive(self):
        self.cog.eligible_channels = AsyncMock(return_value=[])
        d = self.s.queue_manual(self.c, "1", "x")
        await self.cog.send_drop(d)
        self.assertEqual(
            self.s.rows("SELECT status FROM event_drops")[0]["status"], "failed"
        )
        self.assertIn("No eligible", self.s.campaign(self.c)["warning"])
        with patch.object(
            self.s, "tick", side_effect=RuntimeError("database temporarily unavailable")
        ):
            await self.cog.worker.coro(self.cog)

    async def test_send_fallback_expiration_and_history_preservation(self):
        a = self.channel(10)
        b = self.channel(20)
        a.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "no"
        )
        self.cog.eligible_channels = AsyncMock(return_value=[a, b])
        d = self.s.queue_manual(self.c, "1", "x")
        with patch("cogs.event_drops.publish_audit", new=AsyncMock()):
            await self.cog.send_drop(d)
        drop = self.s.rows("SELECT * FROM event_drops")[0]
        self.assertEqual(drop["channel_id"], "20")
        self.assertEqual(drop["status"], "active")
        self.s.tick(now=drop["expires_at"])
        drop = self.s.rows("SELECT * FROM event_drops")[0]
        message = SimpleNamespace(delete=AsyncMock())
        b.get_partial_message.return_value = message
        self.bot.get_channel.return_value = b
        await self.cog.cleanup(drop)
        message.delete.assert_awaited_once()
        self.assertEqual(
            self.s.rows("SELECT status FROM event_drops")[0]["status"], "deleted"
        )

    async def test_failed_deletion_keeps_record_and_retries_later(self):
        d = self.s.queue_manual(self.c, "1", "x")
        self.s.take_pending(d)
        self.s.prepare_send(d, "10")
        self.s.sent(d, 9)
        self.s.transition(self.c, "1", "end")
        drop = self.s.rows("SELECT * FROM event_drops")[0]
        message = SimpleNamespace(
            delete=AsyncMock(side_effect=RuntimeError("permission error"))
        )
        self.bot.get_channel.return_value.get_partial_message.return_value = message
        await self.cog.cleanup(drop)
        stored = self.s.rows("SELECT * FROM event_drops")[0]
        self.assertEqual(stored["status"], "expired")
        self.assertGreater(stored["cleanup_after"], time.time())
        self.assertIn("cleanup failed", stored["error"])

    async def test_persistent_components_and_command_collision_safety(self):
        self.assertTrue(drop_view(1, self.s.campaign(self.c)).is_persistent())
        self.assertRegex(
            "eventdrop:claim:42", ClaimButton.__discord_ui_compiled_template__
        )
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        with patch.object(EventDropsCog, "cog_load", new=AsyncMock()):
            await bot.add_cog(EventDropsCog(bot))
        self.assertEqual(
            {c.name for c in bot.tree.get_commands()}, {"event", "eventdrop"}
        )
        self.assertEqual(
            {c.name for c in bot.tree.get_command("event").commands},
            {"score", "leaderboard"},
        )
        await bot.close()

    async def test_uncertain_send_is_never_retried(self):
        a = self.channel(10)
        a.send.side_effect = OSError("connection lost after sending")
        self.cog.eligible_channels = AsyncMock(return_value=[a])
        d = self.s.queue_manual(self.c, "1", "x")
        await self.cog.send_drop(d)
        await self.cog.send_drop(d)
        a.send.assert_awaited_once()
        self.assertEqual(
            self.s.rows("SELECT status FROM event_drops")[0]["status"], "sending"
        )

    async def test_crash_receipt_reconciles_without_resending(self):
        d = self.s.queue_manual(self.c, "1", "x")
        self.s.take_pending(d)
        self.s.prepare_send(d, "10")
        channel = self.channel(10)
        self.bot.get_channel.return_value = channel

        async def history(**kwargs):
            yield SimpleNamespace(
                id=100,
                author=self.bot.user,
                embeds=[
                    SimpleNamespace(footer=SimpleNamespace(text=f"Event Drop #{d}"))
                ],
                created_at=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ),
            )

        channel.history = history
        await self.cog.reconcile(self.s.rows("SELECT * FROM event_drops")[0])
        self.assertEqual(
            self.s.rows("SELECT status FROM event_drops")[0]["status"], "active"
        )
        channel.send.assert_not_awaited()

    async def test_cog_load_registers_restart_handler_before_worker(self):
        with patch.object(self.cog.worker, "start") as start:
            await self.cog.cog_load()
        self.bot.add_dynamic_items.assert_called_once_with(ClaimButton)
        start.assert_called_once()
        button = await ClaimButton.from_custom_id(
            None,
            None,
            re.match(r"eventdrop:claim:(?P<drop_id>[0-9]+)", "eventdrop:claim:321"),
        )
        self.assertEqual(button.drop_id, 321)

    async def test_invalid_discord_payload_fails_without_endless_reconciliation(self):
        channel = self.channel(10)
        channel.send.side_effect = discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request"), "Invalid emoji"
        )
        self.cog.eligible_channels = AsyncMock(return_value=[channel])
        drop_id = self.s.queue_manual(self.c, "1", "bad-emoji")
        await self.cog.send_drop(drop_id)
        self.assertEqual(
            self.s.rows("SELECT status FROM event_drops")[0]["status"], "failed"
        )

    async def test_manually_deleted_message_is_successful_cleanup(self):
        drop_id = self.s.queue_manual(self.c, "1", "deleted")
        self.s.take_pending(drop_id)
        self.s.prepare_send(drop_id, "10")
        self.s.sent(drop_id, 100)
        self.s.transition(self.c, "1", "end")
        message = SimpleNamespace(
            delete=AsyncMock(
                side_effect=discord.NotFound(
                    SimpleNamespace(status=404, reason="Not Found"), "Unknown Message"
                )
            )
        )
        self.bot.get_channel.return_value.get_partial_message.return_value = message
        await self.cog.cleanup(self.s.rows("SELECT * FROM event_drops")[0])
        self.assertEqual(
            self.s.rows("SELECT status FROM event_drops")[0]["status"], "deleted"
        )


class EventDropsRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(Path(self.temp.name) / "db.sqlite"),
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "admin",
                "DASHBOARD_PASSWORD": "test-password",
                "DASHBOARD_SECRET_KEY": "test-session-signing-key",
                "GUILD_ID": "1",
            },
        )
        self.env.start()
        from utils.settings import initialize_settings_from_env

        initialize_settings_from_env()
        from dashboard.app import app

        self.client = TestClient(app)
        self.s = EventDrops()
        self.s.initialize()
        from utils.discord_metadata import initialize_discord_metadata_schema

        initialize_discord_metadata_schema()
        self.s.execute(
            "INSERT INTO dashboard_discord_channels(id,guild_id,name,type,updated_at) VALUES('10','1','general','text','now')"
        )

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.temp.cleanup()

    def login(self):
        page = self.client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        self.client.post(
            "/login",
            data={"csrf": csrf, "username": "admin", "password": "test-password"},
        )
        page = self.client.get("/events/drops/new")
        self.assertEqual(page.status_code, 200, page.text[:500])
        return re.search(r'name="csrf" value="([^"]+)"', page.text)[1]

    def test_auth_permission_and_csrf(self):
        self.assertEqual(
            self.client.get("/events/drops", follow_redirects=False).status_code, 303
        )
        csrf = self.login()
        self.assertEqual(
            self.client.post("/events/drops/new", data={"csrf": "invalid"}).status_code,
            400,
        )
        with patch("dashboard.app.has_permission", return_value=False):
            for path in [
                "/events/drops",
                "/events/drops/1/export.csv",
                "/events/drops/1/assets/1",
                "/events/drops/1/claims/1",
            ]:
                self.assertEqual(self.client.get(path).status_code, 403)
        self.assertTrue(csrf)

    def test_role_ping_picker_save_preserve_and_clear(self):
        for role_id, guild, name in [
            ("1", "1", "@everyone"),
            ("123", "1", "Drop fans"),
            ("999", "2", "Other server"),
        ]:
            self.s.execute(
                "INSERT INTO dashboard_discord_roles(id,guild_id,name,updated_at) VALUES(?,?,?,'now')",
                (role_id, guild, name),
            )
        csrf = self.login()
        page = self.client.get("/events/drops/new")
        picker = re.search(
            r'<select name="ping_role_id">(.*?)</select>', page.text, re.S
        )[1]
        self.assertIn("No role ping", picker)
        self.assertIn("Drop fans", picker)
        self.assertNotIn("@everyone", picker)
        self.assertNotIn("Other server", picker)
        values = {
            k: v
            for k, v in dict(
                DEFAULTS,
                name="Ping campaign",
                csrf=csrf,
                channels="10",
                ping_role_id="123",
            ).items()
            if v is not None
        }
        response = self.client.post("/events/drops/new", data=values)
        self.assertEqual(response.status_code, 200)
        campaign = self.s.campaigns("1")[0]
        self.assertEqual(campaign["ping_role_id"], "123")
        cid = campaign["id"]
        for bad in ("1", "999", "789"):
            response = self.client.post(
                f"/events/drops/{cid}/edit", data=dict(values, ping_role_id=bad)
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.s.campaign(cid)["ping_role_id"], "123")
        self.s.execute("DELETE FROM dashboard_discord_roles WHERE id='123'")
        page = self.client.get(f"/events/drops/{cid}/edit")
        self.assertIn('value="123" selected>Unavailable role (123)', page.text)
        del values[
            "ping_role_id"
        ]  # A form opened before the upgrade must preserve the setting.
        response = self.client.post(f"/events/drops/{cid}/edit", data=values)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.s.campaign(cid)["ping_role_id"], "123")
        response = self.client.post(
            f"/events/drops/{cid}/edit", data=dict(values, ping_role_id="")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.s.campaign(cid)["ping_role_id"], "")

    def test_crud_actions_preview_export_and_confirmation(self):
        csrf = self.login()
        values = dict(
            DEFAULTS,
            name="Fat Bear Week",
            singular="Fish",
            plural="Fish",
            emoji="🐟",
            button_label="Catch Fish",
            button_emoji="🐟",
            csrf=csrf,
            channels="10",
        )
        values = {k: v for k, v in values.items() if v is not None}
        response = self.client.post("/events/drops/new", data=values)
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertIn("Fat Bear Week", response.text)
        c = self.s.campaigns("1")[0]["id"]
        self.assertIn("drop-preview", self.client.get(f"/events/drops/{c}/edit").text)
        self.client.post(
            f"/events/drops/{c}/action", data={"csrf": csrf, "action": "start"}
        )
        self.assertEqual(self.s.campaign(c)["status"], "active")
        self.client.post(
            f"/events/drops/{c}/action",
            data={"csrf": csrf, "action": "drop", "submission_id": "same"},
        )
        self.client.post(
            f"/events/drops/{c}/action",
            data={"csrf": csrf, "action": "drop", "submission_id": "same"},
        )
        self.assertEqual(len(self.s.rows("SELECT * FROM event_drops")), 1)
        self.client.post(
            f"/events/drops/{c}/action", data={"csrf": csrf, "action": "end"}
        )
        self.assertEqual(self.s.campaign(c)["status"], "active")
        self.client.post(
            f"/events/drops/{c}/action",
            data={"csrf": csrf, "action": "end", "confirmation": "END"},
        )
        self.assertEqual(self.s.campaign(c)["status"], "completed")
        response = self.client.get(f"/events/drops/{c}/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "user_id,display_name,total_points,drops_claimed,rank", response.text
        )

    def test_invalid_channel_and_form_return_helpful_errors(self):
        csrf = self.login()
        data = {
            k: v
            for k, v in dict(DEFAULTS, name="x", csrf=csrf, channels="999").items()
            if v is not None
        }
        response = self.client.post("/events/drops/new", data=data)
        self.assertEqual(response.status_code, 400)
        self.assertIn("individual text channels", response.text)
        self.assertEqual(self.s.campaigns("1"), [])

    def test_uploaded_image_is_validated_protected_and_duplicated(self):
        from io import BytesIO
        from PIL import Image

        csrf = self.login()
        image = BytesIO()
        Image.new("RGB", (400, 300), "#204b44").save(image, "PNG")
        values = {
            k: v
            for k, v in dict(DEFAULTS, name="Images", csrf=csrf, channels="10").items()
            if v is not None
        }
        response = self.client.post(
            "/events/drops/new",
            data=values,
            files={"images": ("photo.png", image.getvalue(), "image/png")},
        )
        self.assertEqual(response.status_code, 200)
        campaign = self.s.campaigns("1")[0]
        assets = self.s.rows("SELECT * FROM event_drop_assets")
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["content_type"], "image/webp")
        url = f'/events/drops/{campaign["id"]}/assets/{assets[0]["id"]}'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Image.open(BytesIO(response.content)).size, (1600, 900))
        duplicate = self.s.duplicate(campaign["id"], "1", "admin")
        self.assertEqual(
            len(
                self.s.rows(
                    "SELECT id FROM event_drop_assets WHERE campaign_id=?", (duplicate,)
                )
            ),
            1,
        )
        invalid = self.client.post(
            "/events/drops/new",
            data=values,
            files={"images": ("fake.png", b"invalid image", "image/png")},
        )
        self.assertEqual(invalid.status_code, 400)

    def test_cross_guild_campaign_is_hidden_and_csv_names_are_safe(self):
        self.login()
        other = self.s.save("2", "admin", dict(DEFAULTS, name="Private"), ["10"])
        self.assertEqual(self.client.get(f"/events/drops/{other}").status_code, 404)
        from utils.csv_export import safe_cell as csv_cell

        self.assertEqual(csv_cell("=SUM(A1:A3)"), "'=SUM(A1:A3)")
        self.assertEqual(csv_cell("Joe"), "Joe")


if __name__ == "__main__":
    unittest.main()
