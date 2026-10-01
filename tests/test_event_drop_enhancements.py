"""Behavior and preservation checks for rewards, message text and rare protection."""

import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from fastapi.testclient import TestClient

from cogs.event_drops import EventDropsCog
from utils.event_drop_rewards import validate_reward, render_text
from utils.event_drop_variants import VARIANT_DEFAULTS, migrate_variants
from utils.event_drops import DEFAULTS, SCHEMA, EventDrops, migrate_role_pings


class EnhancementStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.s = EventDrops(Path(self.temp.name) / "test.sqlite")
        self.s.initialize()
        self.c = self.s.save(
            "1",
            "admin",
            dict(DEFAULTS, name="Harvest", plural="Seeds", singular="Seed"),
            ["10", "20"],
        )
        self.s.set_variants_enabled(self.c, "1", True)
        self.default = self.s.variants(self.c)[0]["id"]
        self.rare = self.s.save_variant(
            self.c, "1", dict(VARIANT_DEFAULTS, name="Golden seed", weight=8, points=5)
        )
        self.s.transition(self.c, "1", "start")

    def tearDown(self):
        self.temp.cleanup()

    def post(self, key, variant=None):
        d = self.s.queue_manual(self.c, "1", key, variant_id=variant)
        self.s.take_pending(d)
        self.s.prepare_send(d, "10")
        self.s.sent(d, str(d), time.time())
        return d

    def edit(self, variant, **values):
        if self.s.campaign(self.c)["status"] in ("active", "scheduled"):
            self.s.transition(self.c, "1", "pause")
        row = next(v for v in self.s.variants(self.c) if v["id"] == variant)
        row.update(values)
        self.s.save_variant(self.c, "1", row, variant)
        return row

    def test_random_reward_rolls_once_for_all_members_and_restart(self):
        self.edit(self.rare, reward_mode="random", points_min=2, points_max=7)
        with patch("utils.event_drop_rewards.random.randint", return_value=6) as draw:
            d = self.post("range", self.rare)
            draw.assert_called_once_with(2, 7)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(lambda u: self.s.claim(d, str(u), "1"), range(20, 40))
            )
        self.assertTrue(all(r["ok"] and r["total"] == 6 for r in results))
        self.edit(self.rare, reward_mode="static", points=99)
        self.s = EventDrops(self.s.path)
        self.s.initialize()
        self.assertEqual(self.s.claim(d, "80", "1")["total"], 6)
        self.assertEqual(
            self.s.rows("SELECT DISTINCT points FROM event_drop_claims"),
            [dict(points=6)],
        )

    def test_campaign_random_and_static_rewards(self):
        self.s.transition(self.c, "1", "pause")
        self.s.set_variants_enabled(self.c, "1", False)
        c = self.s.campaign(self.c)
        c.update(reward_mode="random", points_min=3, points_max=9)
        self.s.save("1", "admin", c, c["channels"], self.c)
        with patch("utils.event_drop_rewards.random.randint", return_value=9):
            d = self.post("campaign-range")
        self.assertEqual(self.s.claim(d, "55", "1")["total"], 9)
        c.update(reward_mode="static", points=4)
        self.s.save("1", "admin", c, c["channels"], self.c)
        d = self.post("static")
        self.assertEqual(self.s.claim(d, "55", "1")["total"], 13)

    def test_reward_validation(self):
        for low, high in [(0, 4), (4, 2), (-1, 3), (1, 1_000_001), ("x", 4)]:
            with self.subTest(low=low, high=high), self.assertRaises(ValueError):
                validate_reward(
                    dict(
                        reward_mode="random", points=1, points_min=low, points_max=high
                    )
                )
        self.assertEqual(
            validate_reward(
                dict(reward_mode="random", points=1, points_min=5, points_max=5)
            )["points"],
            5,
        )
        with self.assertRaises(ValueError):
            validate_reward(dict(reward_mode="empty"))
        with self.assertRaises(ValueError):
            validate_reward(dict(points=-1), allow_empty=True)

    def test_empty_claim_reply_zero_awards_duplicates_and_history(self):
        self.edit(
            self.rare,
            reward_mode="empty",
            empty_claim_message="Gotcha, {variant}! Your total stays {total} {currency}.",
        )
        d = self.post("empty", self.rare)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.s.claim(d, "55", "1"), range(12)))
        accepted = [r for r in results if r["ok"]]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["total"], 0)
        self.assertIn(
            "Gotcha, Golden seed! Your total stays 0 Seeds.", accepted[0]["message"]
        )
        self.assertEqual(
            self.s.rows("SELECT points FROM event_drop_claims"), [dict(points=0)]
        )
        self.assertEqual(self.s.leaderboard(self.c), [])
        self.assertEqual(self.s.variant_results(self.c)[0]["total_points"], 0)
        self.edit(self.rare, empty_claim_message="Changed")
        self.assertIn("Gotcha, Golden seed", self.s.claim(d, "56", "1")["message"])
        self.assertFalse(self.s.claim(d, "57", "1", now=time.time() + 99999)["ok"])

    def test_static_zero_and_caps(self):
        self.edit(self.rare, points=0, reward_mode="static")
        self.assertTrue(self.s.claim(self.post("zero", self.rare), "55", "1")["empty"])
        c = self.s.campaign(self.c)
        c["max_user_points"] = 2
        self.s.save("1", "admin", c, c["channels"], self.c)
        self.edit(self.rare, reward_mode="random", points_min=1, points_max=5)
        with patch("utils.event_drop_rewards.random.randint", return_value=5):
            d = self.post("too-big", self.rare)
        self.assertFalse(self.s.claim(d, "55", "1")["ok"])

    def test_post_text_inheritance_overrides_snapshots_and_safe_tokens(self):
        self.s.transition(self.c, "1", "pause")
        c = self.s.campaign(self.c)
        c.update(
            message_text="A {variant}: {points} {currency}! {unknown}",
            ping_role_id="123",
        )
        self.s.save("1", "admin", c, c["channels"], self.c)
        d = self.post("inherit", self.rare)
        row = self.s.rows("SELECT * FROM event_drops WHERE id=?", (d,))[0]
        self.assertEqual(row["message_text"], "A Golden seed: 5 Seeds! {unknown}")
        self.edit(
            self.rare, message_text_override="Custom {campaign} {points}", points=7
        )
        self.assertEqual(
            self.s.rows(
                "SELECT message_text FROM event_drops WHERE id=?",
                (self.post("override", self.rare),),
            )[0]["message_text"],
            "Custom Harvest 7",
        )
        self.edit(self.rare, message_text_override="")
        self.assertEqual(
            self.s.rows(
                "SELECT message_text FROM event_drops WHERE id=?",
                (self.post("clear", self.rare),),
            )[0]["message_text"],
            "",
        )
        self.assertEqual(
            row["message_text"],
            self.s.rows("SELECT message_text FROM event_drops WHERE id=?", (d,))[0][
                "message_text"
            ],
        )
        self.assertEqual(
            render_text("{variant} {points}", variant="{points}", points=5),
            "{points} 5",
        )

    def test_protection_uses_history_and_never_rerolls_request(self):
        self.edit(self.rare, drought_after=3)
        for n in range(3):
            self.post("common" + str(n), self.default)
        health = self.s.variant_health(self.c)["variants"]
        self.assertEqual(
            next(v for v in health if v["id"] == self.rare)["effective_chance"], 100
        )
        self.assertEqual(
            next(v for v in health if v["id"] == self.default)["effective_chance"], 0
        )
        with patch(
            "utils.event_drop_variants.random.random",
            side_effect=AssertionError("No weighted draw when protection is due"),
        ):
            d = self.post("protected")
        row = self.s.rows("SELECT * FROM event_drops WHERE id=?", (d,))[0]
        self.assertEqual(
            (row["variant_id"], row["variant_selection"]), (self.rare, "protected")
        )
        self.assertEqual(
            self.s.queue_manual(self.c, "1", "protected", variant_id=self.default), d
        )
        with patch("utils.event_drop_variants.random.random", return_value=0):
            self.assertEqual(
                self.s.rows(
                    "SELECT variant_id FROM event_drops WHERE id=?",
                    (self.post("next"),),
                )[0]["variant_id"],
                self.default,
            )

    def test_disabled_protection_standard_and_forced_priority(self):
        self.edit(self.rare, drought_after=1)
        self.post("common", self.default)
        forced = self.post("forced", self.default)
        self.assertEqual(
            self.s.rows(
                "SELECT variant_selection FROM event_drops WHERE id=?", (forced,)
            )[0]["variant_selection"],
            "forced",
        )
        self.s.variant_action(self.c, "1", self.rare, "disable")
        with patch("utils.event_drop_variants.random.random", return_value=0):
            self.assertEqual(
                self.s.rows(
                    "SELECT variant_id FROM event_drops WHERE id=?",
                    (self.post("disabled"),),
                )[0]["variant_id"],
                self.default,
            )
        self.s.set_variants_enabled(self.c, "1", False)
        self.assertIsNone(
            self.s.rows(
                "SELECT variant_id FROM event_drops WHERE id=?",
                (self.post("standard"),),
            )[0]["variant_id"]
        )

    def test_health_shows_off_mode_and_observed_counts(self):
        self.post("common", self.default)
        self.post("rare", self.rare)
        health = self.s.variant_health(self.c)
        self.assertEqual(health["week_drops"], 2)
        v = next(v for v in health["variants"] if v["id"] == self.rare)
        self.assertEqual(v["delivered_week"], 1)
        self.assertEqual(v["dry_spell"], 0)
        self.s.transition(self.c, "1", "pause")
        self.s.set_variants_enabled(self.c, "1", False)
        self.assertTrue(
            all(
                v["effective_chance"] == 0
                for v in self.s.variant_health(self.c)["variants"]
            )
        )

    def test_duplicate_preserves_new_fields(self):
        self.edit(
            self.rare,
            reward_mode="random",
            points_min=2,
            points_max=8,
            message_text_override="Hey!",
            empty_claim_message="Nothing",
            drought_after=20,
        )
        copy = self.s.duplicate(self.c, "1", "admin")
        v = next(v for v in self.s.variants(copy) if v["name"] == "Golden seed")
        self.assertEqual(
            (
                v["reward_mode"],
                v["points_min"],
                v["points_max"],
                v["message_text_override"],
                v["drought_after"],
            ),
            ("random", 2, 8, "Hey!", 20),
        )

    def test_v3_migration_preserves_every_existing_column_and_fk(self):
        d = self.post("existing", self.rare)
        self.s.claim(d, "55", "1")
        pending = self.s.queue_manual(self.c, "1", "pending", variant_id=self.rare)
        legacy = Path(self.temp.name) / "legacy.sqlite"
        with sqlite3.connect(legacy) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO event_drop_schema VALUES(1,0)")
            migrate_variants(db)
            migrate_role_pings(db)
            tables = [
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name!='event_drop_schema'"
                )
            ]
            for table in tables:
                columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
                for row in self.s.rows(f"SELECT * FROM {table}"):
                    db.execute(
                        f"INSERT INTO {table}("
                        + ",".join(columns)
                        + ") VALUES("
                        + ",".join("?" for _ in columns)
                        + ")",
                        [row[k] for k in columns],
                    )
            db.execute("CREATE TABLE unrelated(value TEXT)")
            db.execute("INSERT INTO unrelated VALUES('untouched')")
            before = {
                table: [tuple(r) for r in db.execute(f"SELECT * FROM {table}")]
                for table in tables
            }
            original_columns = {
                table: [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
                for table in tables
            }
        backups = Path(self.temp.name) / "backups"
        result = subprocess.run(
            [
                sys.executable,
                "scripts/migrate_event_drops.py",
                "--database",
                str(legacy),
                "--backup-dir",
                str(backups),
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, DATABASE_PATH=str(legacy), DASHBOARD_ENABLED="false"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = list(backups.glob("*.sqlite"))
        self.assertEqual(len(backup), 1)
        with sqlite3.connect(backup[0]) as db:
            self.assertEqual(
                db.execute("SELECT MAX(version) FROM event_drop_schema").fetchone()[0],
                3,
            )
            self.assertEqual(
                db.execute("SELECT SUM(points) FROM event_drop_claims").fetchone()[0], 5
            )
        migrated = EventDrops(legacy)
        migrated.initialize()
        migrated.initialize()
        with sqlite3.connect(legacy) as db:
            for table in tables:
                self.assertEqual(
                    list(
                        db.execute(
                            "SELECT "
                            + ",".join(original_columns[table])
                            + f" FROM {table}"
                        )
                    ),
                    before[table],
                    table,
                )
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                db.execute("SELECT value FROM unrelated").fetchone()[0], "untouched"
            )
        self.assertTrue(migrated.claim(d, "56", "1")["ok"])
        self.assertEqual(migrated.take_pending(pending)[0]["points"], 5)
        from scripts.migrate_event_drops import validate

        validate(legacy)

    def test_initialize_does_not_take_write_lock_when_current(self):
        with sqlite3.connect(self.s.path) as locked:
            locked.execute("BEGIN IMMEDIATE")
            self.s.initialize()  # Would block for 30s / fail if this took a write lock.
            locked.rollback()

    def test_enabling_preconfigured_variants_creates_missing_default(self):
        c = self.s.save("1", "admin", dict(DEFAULTS, name="Preconfigured"), ["10"])
        rare = self.s.save_variant(
            c, "1", dict(VARIANT_DEFAULTS, name="Rare saved first", weight=2, points=5)
        )
        self.s.set_variants_enabled(c, "1", True)
        variants = self.s.variants(c)
        self.assertEqual(len(variants), 2)
        self.assertEqual(sum(v["is_default"] for v in variants), 1)
        self.assertEqual(next(v for v in variants if v["id"] == rare)["points"], 5)

    def test_multiple_protected_variants_take_turns_and_failed_drops_do_not_count(self):
        self.edit(self.rare, drought_after=1)
        other = self.s.save_variant(
            self.c,
            "1",
            dict(
                VARIANT_DEFAULTS,
                name="Other rare",
                weight=1,
                points=10,
                drought_after=1,
            ),
        )
        self.post("start", self.default)
        a = self.post("protected-a")
        b = self.post("protected-b")
        selected = [
            self.s.rows("SELECT variant_id FROM event_drops WHERE id=?", (d,))[0][
                "variant_id"
            ]
            for d in (a, b)
        ]
        self.assertEqual(set(selected), {self.rare, other})
        failed = self.s.queue_manual(self.c, "1", "failed", variant_id=self.default)
        self.s.execute("UPDATE event_drops SET status='failed' WHERE id=?", (failed,))
        health = self.s.variant_health(self.c)
        self.assertEqual(
            next(v for v in health["variants"] if v["id"] == other)["dry_spell"], 0
        )


class EnhancementRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            dict(
                DATABASE_PATH=str(Path(self.temp.name) / "db.sqlite"),
                DASHBOARD_ENABLED="true",
                DASHBOARD_USERNAME="admin",
                DASHBOARD_PASSWORD="test-password",
                DASHBOARD_SECRET_KEY="drop-enhancement-test",
                GUILD_ID="1",
            ),
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
            "INSERT INTO dashboard_discord_channels(id,guild_id,name,type,updated_at) VALUES('10','1','testing','text','now')"
        )
        page = self.client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        self.client.post(
            "/login", data=dict(csrf=csrf, username="admin", password="test-password")
        )
        self.c = self.s.save("1", "admin", dict(DEFAULTS, name="UI Campaign"), ["10"])
        page = self.client.get(f"/events/drops/{self.c}/edit")
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.temp.cleanup()

    def test_off_state_variant_form_empty_and_forced_drop(self):
        page = self.client.get(f"/events/drops/{self.c}/variants")
        self.assertIn("Standard Drops · variants OFF", page.text)
        self.assertIn("Saved variants do not appear", page.text)
        self.s.set_variants_enabled(self.c, "1", True)
        data = dict(
            csrf=self.csrf,
            name="Empty chest",
            enabled="on",
            weight="2",
            reward_mode="empty",
            empty_claim_message="Empty! {total}",
            image_mode="inherit",
            override_message_text="on",
            message_text_override="Try your luck: {variant}",
            drought_after="5",
        )
        response = self.client.post(f"/events/drops/{self.c}/variants/new", data=data)
        self.assertEqual(response.status_code, 200, response.text[:400])
        self.assertIn("Empty · 0 points", response.text)
        self.assertIn("Drop Variants are ON", response.text)
        v = self.s.variants(self.c)[1]
        self.assertEqual(v["points"], 0)
        self.s.transition(self.c, "1", "start")
        self.client.post(
            f"/events/drops/{self.c}/action",
            data=dict(
                csrf=self.csrf,
                action="rare_drop",
                submission_id="empty-manual",
                variant_id=v["id"],
            ),
        )
        row = self.s.rows("SELECT * FROM event_drops")[0]
        self.assertEqual(row["message_text"], "Try your luck: Empty chest")
        self.assertEqual(row["points"], 0)

    def test_random_campaign_form_and_bad_ranges(self):
        data = {
            k: v
            for k, v in dict(
                DEFAULTS,
                name="Range",
                csrf=self.csrf,
                channels="10",
                reward_mode="random",
                points_min="2",
                points_max="8",
                message_text="Collect {points} {currency}",
            ).items()
            if v is not None
        }
        response = self.client.post(f"/events/drops/{self.c}/edit", data=data)
        self.assertEqual(response.status_code, 200, response.text[:400])
        self.assertEqual(self.s.campaign(self.c)["points_max"], 8)
        response = self.client.post(
            f"/events/drops/{self.c}/edit", data=dict(data, points_min="9")
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.s.campaign(self.c)["points_max"], 8)
        page = self.client.get(f"/events/drops/{self.c}/edit")
        self.assertIn('name="message_text"', page.text)
        self.assertIn('value="random" selected', page.text)


class EnhancementDiscordTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_post_content_has_only_allowed_role_ping(self):
        with tempfile.TemporaryDirectory() as temp:
            s = EventDrops(Path(temp) / "db.sqlite")
            s.initialize()
            c = s.save(
                "1",
                "admin",
                dict(
                    DEFAULTS,
                    name="Test",
                    message_text="Hey @everyone <@456>! Collect {points} {currency}.",
                    ping_role_id="123",
                ),
                ["10"],
            )
            s.transition(c, "1", "start")
            d = s.queue_manual(c, "1", "post")
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 10
            channel.guild.get_role.return_value = discord.Object(id=123)
            channel.send = AsyncMock(
                return_value=SimpleNamespace(
                    id=100, created_at=datetime.now(timezone.utc)
                )
            )
            cog = EventDropsCog(MagicMock())
            cog.service = s
            cog.eligible_channels = AsyncMock(return_value=[channel])
            with patch("cogs.event_drops.publish_audit", new=AsyncMock()):
                await cog.send_drop(d)
            sent = channel.send.call_args.kwargs
            self.assertEqual(
                sent["content"], "<@&123>\nHey @everyone <@456>! Collect 1 Point."
            )
            self.assertEqual(sent["allowed_mentions"].to_dict()["roles"], [123])
            self.assertEqual(sent["allowed_mentions"].to_dict()["parse"], [])
