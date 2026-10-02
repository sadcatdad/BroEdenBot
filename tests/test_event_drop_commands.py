import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.event_drops import EventDropsCog
from dashboard import rbac
from dashboard.users import initialize_dashboard_users
from tests import test_event_drops as fixtures
from utils.event_drops import DEFAULTS
from utils.event_drop_variants import VARIANT_DEFAULTS


class AdjustmentTests(unittest.TestCase):
    setUp = fixtures.EventDropsStorageTests.setUp
    tearDown = fixtures.EventDropsStorageTests.tearDown
    drop = fixtures.EventDropsStorageTests.drop

    def award(self, key="award", points=10, **kwargs):
        return self.s.adjust_points(
            self.c, "1", key, "admin", "55", "give", points, **kwargs
        )

    def test_claims_and_adjustments_share_totals_without_rewriting_history(self):
        d = self.drop()
        self.s.claim(d, "55", "1")
        claims = self.s.rows("SELECT * FROM event_drop_claims")
        result = self.award()
        self.assertEqual((result["before_total"], result["after_total"]), (1, 11))
        self.s.adjust_points(self.c, "1", "remove", "admin", "55", "remove", 4)
        score = self.s.leaderboard(self.c)[0]
        self.assertEqual((score["total_points"], score["drops_claimed"]), (7, 1))
        self.assertEqual(self.s.rows("SELECT * FROM event_drop_claims"), claims)
        self.assertEqual(self.s.claim(self.drop("second"), "55", "1")["total"], 8)

    def test_concurrent_duplicate_awards_are_one_operation(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.award(), range(24)))
        self.assertEqual(sum(not r["duplicate"] for r in results), 1)
        self.assertEqual(len(self.s.operations(self.c, user_id="55")), 1)
        self.assertEqual(self.s.leaderboard(self.c)[0]["total_points"], 10)

    def test_concurrent_removals_cannot_make_negative_balance(self):
        self.award()

        def remove(i):
            try:
                return self.s.adjust_points(
                    self.c, "1", f"remove:{i}", "admin", "55", "remove", 7
                )
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(remove, range(8)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.s.leaderboard(self.c)[0]["total_points"], 3)

    def test_campaign_cap_includes_staff_awards_and_claims(self):
        self.s.execute(
            "UPDATE event_drop_campaigns SET max_user_points=10 WHERE id=?", (self.c,)
        )
        self.award()
        self.assertFalse(self.s.claim(self.drop(), "55", "1")["ok"])
        with self.assertRaisesRegex(ValueError, "limit"):
            self.award("too-many", 1)
        self.s.adjust_points(self.c, "1", "deduct", "admin", "55", "remove", 1)
        self.assertEqual(self.s.claim(self.drop("another"), "55", "1")["total"], 10)

    def variant(self, **values):
        self.s.transition(self.c, "1", "pause")
        self.s.set_variants_enabled(self.c, "1", True)
        return self.s.save_variant(
            self.c, "1", dict(VARIANT_DEFAULTS, name="Rare", points=8, **values)
        )

    def test_variant_roll_and_identity_are_frozen_on_retry(self):
        v = self.variant(reward_mode="random", points_min=5, points_max=15)
        with patch("utils.event_drop_rewards.random.randint", return_value=12) as roll:
            a = self.award(points=None, variant_id=v)
            b = self.award(points=None, variant_id=v)
            roll.assert_called_once_with(5, 15)
        self.assertEqual((a["points"], b["points"], b["duplicate"]), (12, 12, True))
        values = dict(self.s.variants(self.c)[1], name="Renamed")
        self.s.save_variant(self.c, "1", values, v)
        self.assertEqual(self.s.operations(self.c)[0]["variant_name"], "Rare")
        self.assertEqual(len(self.s.rows("SELECT * FROM event_drops")), 0)

    def test_empty_variant_award_is_logged_without_rank_or_claim(self):
        v = self.variant(reward_mode="empty")
        result = self.award(points=None, variant_id=v)
        self.assertEqual(result["after_total"], 0)
        self.assertEqual(self.s.leaderboard(self.c), [])
        self.assertEqual(self.s.participant_scores(self.c)[0]["total_points"], 0)
        self.assertEqual(self.s.campaigns("1")[0]["participants"], 1)

    def test_awarded_variant_is_retained_when_deleted(self):
        v = self.variant()
        self.award(points=None, variant_id=v)
        self.s.variant_action(self.c, "1", v, "delete")
        self.assertNotIn(v, [r["id"] for r in self.s.variants(self.c)])
        self.assertIsNotNone(
            self.s.rows("SELECT deleted_at FROM event_drop_variants WHERE id=?", (v,))[
                0
            ]["deleted_at"]
        )
        self.assertEqual(self.s.operations(self.c)[0]["variant_name"], "Rare")
        self.assertEqual(self.s.leaderboard(self.c)[0]["total_points"], 8)

    def test_input_scope_and_campaign_validation(self):
        for amount in (0, -1, 1.5, True, 1000001):
            with self.assertRaises(ValueError):
                self.award(points=amount)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.award(points=None)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.award(variant_id=999)
        with self.assertRaisesRegex(ValueError, "not found"):
            self.s.adjust_points(self.c, "2", "wrong", "admin", "55", "give", 1)
        self.s.transition(self.c, "1", "end")
        with self.assertRaisesRegex(ValueError, "running"):
            self.award()
        self.assertEqual(self.s.operations(self.c), [])

    def test_disabled_and_foreign_variants_rejected(self):
        v = self.variant(enabled=False)
        with self.assertRaisesRegex(ValueError, "enabled variant"):
            self.award(points=None, variant_id=v)
        with self.assertRaisesRegex(ValueError, "enabled variant"):
            self.award(points=None, variant_id=999)
        self.s.set_variants_enabled(self.c, "1", False)
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.award(points=None, variant_id=v)

    def test_manual_send_operation_is_atomic_and_idempotent(self):
        d = self.s.queue_manual(self.c, "1", "now", actor_id="77", source="discord")
        self.assertEqual(
            self.s.queue_manual(self.c, "1", "now", actor_id="77", source="discord"), d
        )
        rows = self.s.operations(self.c)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["action"], rows[0]["actor_id"], rows[0]["drop_id"]),
            ("now", "77", d),
        )
        self.assertEqual(self.s.leaderboard(self.c), [])

    def test_v4_migration_preserves_all_existing_rows(self):
        self.s.claim(self.drop(), "55", "1")
        self.s.execute("DROP TABLE event_drop_operations")
        self.s.execute("DELETE FROM event_drop_schema WHERE version=5")
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
            self.assertEqual(
                self.s.rows(f"SELECT * FROM {table}"), before[table], table
            )
        self.assertEqual(self.s.leaderboard(self.c)[0]["total_points"], 1)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.EventDropsDiscordTests.asyncSetUp
    asyncTearDown = fixtures.EventDropsDiscordTests.asyncTearDown

    def interaction(self):
        user = SimpleNamespace(
            id=77,
            bot=False,
            roles=[],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        return SimpleNamespace(
            id=700,
            guild_id=1,
            user=user,
            guild=MagicMock(),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_denied_command_cannot_change_points(self):
        i = self.interaction()
        recipient = SimpleNamespace(
            id=55, bot=False, guild=SimpleNamespace(id=1), display_name="Recipient"
        )
        with patch.object(self.cog, "command_allowed", AsyncMock(return_value=False)):
            await self.cog.adjust_command(i, self.c, recipient, "give", 10, None, "")
        self.assertEqual(self.s.leaderboard(self.c), [])
        i.response.defer.assert_awaited_once_with(ephemeral=True)
        i.followup.send.assert_awaited_once()

    async def test_drop_now_attempts_delivery_and_records_actor(self):
        i = self.interaction()
        with patch.object(
            self.cog, "command_allowed", AsyncMock(return_value=True)
        ), patch.object(self.cog, "send_drop", AsyncMock()) as send:
            await EventDropsCog.drop_now.callback(self.cog, i, self.c)
        operation = self.s.operations(self.c)[0]
        send.assert_awaited_once_with(operation["drop_id"])
        self.assertEqual(
            (operation["actor_id"], operation["source"]), ("77", "discord")
        )
        self.assertIn("pending", i.followup.send.await_args.args[0])

    async def test_award_notification_only_pings_recipient(self):
        i = self.interaction()
        recipient = SimpleNamespace(
            id=55, bot=False, guild=SimpleNamespace(id=1), display_name="Recipient"
        )
        with patch.object(
            self.cog, "command_allowed", AsyncMock(return_value=True)
        ), patch("cogs.event_drops.publish_audit", AsyncMock()):
            await self.cog.adjust_command(
                i, self.c, recipient, "give", 10, None, "For helping"
            )
            await self.cog.adjust_command(
                i, self.c, recipient, "give", 10, None, "For helping"
            )
        public = [
            call
            for call in i.followup.send.await_args_list
            if call.kwargs.get("ephemeral") is False
        ]
        self.assertEqual(len(public), 1)
        self.assertIn("<@55> received 10", public[0].args[0])
        mentions = public[0].kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertEqual([user.id for user in mentions.users], [55])
        self.assertEqual(self.s.operations(self.c)[0]["reason"], "For helping")

    async def test_status_uses_local_discord_timestamps(self):
        i = self.interaction()
        self.s.execute(
            "UPDATE event_drop_campaigns SET end_at=? WHERE id=?",
            (time.time() + 3600, self.c),
        )
        await EventDropsCog.drop_status.callback(self.cog, i)
        embed = i.followup.send.await_args.kwargs["embed"]
        self.assertIn(":F>", embed.fields[0].value)
        self.assertIn(":R>", embed.fields[0].value)
        self.assertIn("Next drop:", embed.fields[0].value)

    async def test_autocomplete_scopes_campaigns_and_variants(self):
        i = self.interaction()
        i.command = SimpleNamespace(name="drop-give")
        i.namespace = SimpleNamespace(campaign=self.c)
        self.s.transition(self.c, "1", "pause")
        self.s.set_variants_enabled(self.c, "1", True)
        foreign = self.s.save("2", "admin", dict(DEFAULTS, name="Foreign"), ["20"])
        self.s.transition(foreign, "2", "start")
        with patch.object(self.cog, "command_allowed", AsyncMock(return_value=True)):
            choices = await self.cog.active_campaign_choices(i, "")
            self.assertEqual([c.value for c in choices], [self.c])
            self.assertTrue(await self.cog.award_variant_choices(i, ""))
            i.namespace.campaign = foreign
            self.assertEqual(await self.cog.award_variant_choices(i, ""), [])


class LiveRoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "db.sqlite"
        self.env = patch.dict(
            os.environ,
            dict(
                DATABASE_PATH=str(self.path),
                DASHBOARD_USERNAME="owner",
                DASHBOARD_PASSWORD="test-password",
            ),
        )
        self.env.start()
        initialize_dashboard_users()
        rbac.initialize_rbac_schema()
        with sqlite3.connect(self.path) as db:
            self.role_id = db.execute(
                "INSERT INTO dashboard_roles(role_key,name) VALUES('drop_staff','Drop staff')"
            ).lastrowid
            db.execute(
                "INSERT INTO dashboard_role_permissions(role_id,permission_key) VALUES(?,'event_drops.give')",
                (self.role_id,),
            )
            db.execute(
                "INSERT INTO dashboard_discord_role_mappings(discord_role_id,role_id) VALUES('88',?)",
                (self.role_id,),
            )

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_live_role_works_before_first_dashboard_login_and_revokes_immediately(self):
        self.assertEqual(
            rbac.permissions_for_discord_member(55, [88]), {"event_drops.give"}
        )
        self.assertEqual(rbac.permissions_for_discord_member(55, []), set())

    def test_cached_discord_roles_never_grant_permissions(self):
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO dashboard_users(id,username,role,status,access_source) VALUES(55,'staff','viewer','active','discord_role')"
            )
            db.execute(
                "INSERT INTO dashboard_user_role_assignments(user_id,role_id,source) VALUES(55,?,'discord')",
                (self.role_id,),
            )
        self.assertEqual(rbac.permissions_for_discord_member(55, []), set())
        self.assertEqual(
            rbac.permissions_for_discord_member(55, [88]), {"event_drops.give"}
        )
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO dashboard_user_permission_overrides(user_id,permission_key,allowed) VALUES(55,'event_drops.give',0)"
            )
        self.assertEqual(rbac.permissions_for_discord_member(55, [88]), set())
        with sqlite3.connect(self.path) as db:
            db.execute(
                "DELETE FROM dashboard_user_permission_overrides WHERE user_id=55"
            )
            db.execute("UPDATE dashboard_users SET status='blocked' WHERE id=55")
        self.assertEqual(rbac.permissions_for_discord_member(55, [88]), set())


class OperationsRouteTests(unittest.TestCase):
    setUp = fixtures.EventDropsRouteTests.setUp
    tearDown = fixtures.EventDropsRouteTests.tearDown
    login = fixtures.EventDropsRouteTests.login

    def test_operation_history_and_export_and_zero_balance_participant(self):
        self.login()
        c = self.s.save("1", "staff", dict(DEFAULTS, name="Awards"), ["10"])
        self.s.transition(c, "1", "start")
        self.s.adjust_points(
            c,
            "1",
            "gift",
            "77",
            "55",
            "give",
            10,
            reason="=unsafe formula",
            display_name="Recipient",
        )
        self.s.adjust_points(c, "1", "remove", "77", "55", "remove", 10)
        page = self.client.get(f"/events/drops/{c}")
        self.assertEqual(page.status_code, 200, page.text[:500])
        self.assertIn("Staff operations", page.text)
        self.assertIn("=unsafe formula", page.text)
        participant = self.client.get(f"/events/drops/{c}/participants/55")
        self.assertEqual(participant.status_code, 200)
        self.assertIn("Not ranked", participant.text)
        self.assertIn("0 Points", participant.text)
        export = self.client.get(f"/events/drops/{c}/exports/operations.csv")
        self.assertEqual(export.status_code, 200)
        self.assertIn("'=unsafe formula", export.text)
        self.assertIn("actor_id", export.text)
        self.assertEqual(
            self.client.get(
                f"/events/drops/{c}/exports/operations.csv?operations_page=2"
            ).text,
            export.text,
        )
