import io
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
from fastapi.testclient import TestClient
from PIL import Image

from utils.event_drops import DEFAULTS, SCHEMA, EventDrops
from utils.event_drop_variants import (
    VARIANT_DEFAULTS,
    select_drop_variant,
    valid_weight,
)
from cogs.event_drops import ClaimButton, EventDropsCog, drop_embed


class VariantStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.s = EventDrops(Path(self.temp.name) / "variants.sqlite")
        self.s.initialize()
        self.c = self.s.save(
            "1",
            "admin",
            dict(
                DEFAULTS,
                name="Fat Bear Week",
                singular="Fish",
                plural="Fish",
                emoji="🐟",
            ),
            ["10", "20"],
        )
        self.s.set_variants_enabled(self.c, "1", True)
        normal = self.s.variants(self.c)[0]
        normal.update(name="Normal Fish", weight=90)
        self.normal = self.s.save_variant(self.c, "1", normal, normal["id"])
        self.gold = self.s.save_variant(
            self.c,
            "1",
            dict(
                VARIANT_DEFAULTS,
                name="Golden Fish",
                rarity="Rare",
                weight=8,
                points=5,
                title_override="Golden!",
                color_override="#f1c40f",
                emoji_override="✨",
            ),
        )
        self.legend = self.s.save_variant(
            self.c,
            "1",
            dict(
                VARIANT_DEFAULTS,
                name="Legendary Salmon",
                rarity="Absolute Unit",
                weight=2,
                points=10,
            ),
        )

    def tearDown(self):
        self.temp.cleanup()

    def begin(self):
        self.s.transition(self.c, "1", "start")

    def drop(self, variant=None, key=None):
        d = self.s.queue_manual(
            self.c, "1", key or str(time.time_ns()), variant_id=variant
        )
        self.s.take_pending(d)
        self.s.prepare_send(d, "10")
        self.s.sent(d, 1000 + d)
        return d

    def test_weighted_boundaries_and_non_100_totals(self):
        variants = self.s.variants(self.c)
        for draw, expected in [
            (0, self.normal),
            (0.899999, self.normal),
            (0.90, self.gold),
            (0.97999, self.gold),
            (0.98, self.legend),
            (0.999999, self.legend),
        ]:
            with self.subTest(draw=draw), patch(
                "utils.event_drop_variants.random.random", return_value=draw
            ):
                self.assertEqual(select_drop_variant(variants)["id"], expected)
        for v, w in zip(variants, [50, 5, 1]):
            v["weight"] = w
        with patch("utils.event_drop_variants.random.random", return_value=0.95):
            self.assertEqual(select_drop_variant(variants)["id"], self.gold)
        self.assertAlmostEqual(self.s.variants(self.c)[1]["chance"], 8)

    def test_disabled_never_selected_single_enabled_always_wins(self):
        rows = self.s.variants(self.c)
        rows[0]["enabled"] = False
        rows[1]["enabled"] = False
        for draw in [0, 0.5, 0.999999]:
            with patch("utils.event_drop_variants.random.random", return_value=draw):
                self.assertEqual(select_drop_variant(rows)["id"], self.legend)
        with self.assertRaises(ValueError):
            select_drop_variant(rows, self.gold)

    def test_weight_validation_rejects_invalid_values(self):
        for weight in [
            0,
            -1,
            float("nan"),
            float("inf"),
            -float("inf"),
            "nonsense",
            1e20,
        ]:
            with self.subTest(weight=weight), self.assertRaises(ValueError):
                self.s.save_variant(
                    self.c, "1", dict(VARIANT_DEFAULTS, name="Invalid", weight=weight)
                )
        self.assertEqual(valid_weight(0, enabled=False), 0)
        with self.assertRaises(ValueError):
            select_drop_variant([])

    def test_default_and_last_enabled_are_protected(self):
        with self.assertRaises(ValueError):
            self.s.variant_action(self.c, "1", self.normal, "disable")
        with self.assertRaises(ValueError):
            self.s.variant_action(self.c, "1", self.normal, "delete")
        self.s.variant_action(self.c, "1", self.gold, "default")
        self.s.variant_action(self.c, "1", self.normal, "default")
        self.assertEqual(
            [v["id"] for v in self.s.variants(self.c) if v["is_default"]], [self.normal]
        )
        self.s.set_variants_enabled(self.c, "1", False)
        for v in [self.normal, self.gold, self.legend]:
            self.s.variant_action(self.c, "1", v, "disable")
        with self.assertRaises(ValueError):
            self.s.set_variants_enabled(self.c, "1", True)

    def test_awards_1_5_10_and_total_16(self):
        self.begin()
        for v, points in [(self.normal, 1), (self.gold, 5), (self.legend, 10)]:
            d = self.drop(v)
            result = self.s.claim(d, "55", "1")
            self.assertTrue(result["ok"])
            self.assertIn(f"earned {points} Fish", result["message"])
            self.assertFalse(self.s.claim(d, "55", "1")["ok"])
        self.assertEqual(self.s.leaderboard(self.c)[0]["total_points"], 16)
        self.assertEqual(self.s.leaderboard(self.c)[0]["drops_claimed"], 3)
        self.assertEqual(
            [
                r["points"]
                for r in self.s.rows("SELECT points FROM event_drop_claims ORDER BY id")
            ],
            [1, 5, 10],
        )
        self.assertEqual(
            sum(r["total_points"] for r in self.s.variant_results(self.c, "55")), 16
        )

    def test_high_value_claims_remain_atomic_and_unlimited_claimers(self):
        self.begin()
        d = self.drop(self.gold)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: EventDrops(self.s.path).claim(d, "55", "1"), range(16)
                )
            )
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertTrue(self.s.claim(d, "56", "1")["ok"])
        self.assertEqual(sum(r["total_points"] for r in self.s.leaderboard(self.c)), 10)

    def test_point_and_name_edits_leave_active_history_unchanged(self):
        self.begin()
        old = self.drop(self.gold)
        self.s.transition(self.c, "1", "pause")
        gold = next(v for v in self.s.variants(self.c) if v["id"] == self.gold)
        gold.update(
            points=10, name="New Name", rarity="New Rarity", title_override="New title"
        )
        self.s.save_variant(self.c, "1", gold, self.gold)
        new = self.drop(self.gold)
        self.assertEqual(self.s.claim(old, "55", "1")["total"], 5)
        self.assertEqual(self.s.claim(new, "55", "1")["total"], 15)
        before = self.s.rows("SELECT * FROM event_drops WHERE id=?", (old,))[0]
        self.assertEqual(
            (before["points"], before["variant_name"], before["rarity"]),
            (5, "Golden Fish", "Rare"),
        )
        self.assertEqual(self.s.drop_appearance(old)["title"], "Golden!")
        self.assertEqual(self.s.drop_appearance(new)["title"], "New title")

    def test_queued_appearance_and_expiration_survive_campaign_edits(self):
        self.begin()
        self.s.transition(self.c, "1", "pause")
        d = self.s.queue_manual(self.c, "1", "queued", variant_id=self.normal)
        c = self.s.campaign(self.c)
        c.update(
            title="New campaign title", button_label="Changed button", claim_minutes=30
        )
        self.s.save("1", "admin", c, c["channels"], self.c)
        self.s.take_pending(d)
        now = time.time()
        expires = self.s.prepare_send(d, "10", now=now)
        self.assertEqual(expires, now + 600)
        self.assertEqual(self.s.prepare_send(d, "20", now=now + 30), expires)
        snapshot = self.s.drop_appearance(d)
        self.assertEqual(snapshot["title"], DEFAULTS["title"])
        self.assertEqual(snapshot["button_label"], "Collect")

    def test_active_configuration_lock_is_preserved(self):
        self.begin()
        with self.assertRaises(ValueError):
            self.s.save_variant(self.c, "1", dict(VARIANT_DEFAULTS, name="Locked"))
        with self.assertRaises(ValueError):
            self.s.variant_action(self.c, "1", self.gold, "disable")
        with self.assertRaises(ValueError):
            self.s.set_variants_enabled(self.c, "1", False)

    def test_inheritance_overrides_and_visibility_toggles(self):
        self.begin()
        d = self.drop(self.gold)
        c = self.s.drop_appearance(d)
        self.assertEqual(c["description"], DEFAULTS["description"])
        self.assertEqual(c["button_label"], DEFAULTS["button_label"])
        self.assertEqual(c["emoji"], "✨")
        self.assertEqual(c["color"], "#f1c40f")
        embed = drop_embed(c, d, time.time() + 600)
        self.assertIn("Worth: 5 Fish", embed.description)
        self.assertNotIn("Rarity:", embed.description)
        c.update(show_rarity=True, rarity="Anything", show_reward=False)
        embed = drop_embed(c, d, time.time() + 600)
        self.assertIn("Rarity: Anything", embed.description)
        self.assertNotIn("Worth:", embed.description)

    def test_standard_mode_is_backward_compatible(self):
        self.s.set_variants_enabled(self.c, "1", False)
        self.begin()
        d = self.drop()
        row = self.s.rows("SELECT * FROM event_drops WHERE id=?", (d,))[0]
        self.assertIsNone(row["variant_id"])
        self.assertEqual(row["points"], 1)
        self.assertEqual(row["variant_name"], "Standard Drop")
        self.assertFalse(self.s.drop_appearance(d)["show_reward"])
        self.assertEqual(self.s.claim(d, "55", "1")["total"], 1)
        with self.assertRaises(ValueError):
            self.s.queue_manual(self.c, "1", "forced", variant_id=self.gold)

    def test_variant_images_do_not_leak_to_campaign_pool(self):
        c = self.s.campaign(self.c)
        self.s.save(
            "1", "admin", c, c["channels"], self.c, assets=[(b"campaign", "image/webp")]
        )
        golden = next(v for v in self.s.variants(self.c) if v["id"] == self.gold)
        golden["image_mode"] = "single"
        self.s.save_variant(
            self.c, "1", golden, self.gold, assets=[(b"golden", "image/webp")]
        )
        self.begin()
        a = self.drop(self.normal)
        b = self.drop(self.gold)
        images = self.s.rows(
            "SELECT d.id,a.image_bytes,a.campaign_pool FROM event_drops d JOIN event_drop_assets a ON a.id=d.asset_id ORDER BY d.id"
        )
        self.assertEqual([r["image_bytes"] for r in images], [b"campaign", b"golden"])
        self.assertEqual([r["campaign_pool"] for r in images], [1, 0])
        self.assertNotEqual(a, b)

    def test_pool_none_and_image_validation(self):
        values = dict(VARIANT_DEFAULTS, name="Pool", image_mode="pool")
        with self.assertRaises(ValueError):
            self.s.save_variant(self.c, "1", values)
        v = self.s.save_variant(
            self.c, "1", values, assets=[(b"one", "image/webp"), (b"two", "image/webp")]
        )
        none = self.s.save_variant(
            self.c, "1", dict(VARIANT_DEFAULTS, name="No image", image_mode="none")
        )
        self.begin()
        a = self.drop(v)
        b = self.drop(none)
        self.assertIn(
            self.s.rows("SELECT asset_id FROM event_drops WHERE id=?", (a,))[0][
                "asset_id"
            ],
            next(r for r in self.s.variants(self.c) if r["id"] == v)["asset_ids"],
        )
        self.assertIsNone(
            self.s.rows("SELECT asset_id FROM event_drops WHERE id=?", (b,))[0][
                "asset_id"
            ]
        )

    def test_foreign_variants_and_assets_are_rejected(self):
        other = self.s.save(
            "2",
            "admin",
            dict(DEFAULTS, name="Other"),
            ["30"],
            assets=[(b"foreign", "image/webp")],
        )
        asset = self.s.rows(
            "SELECT id FROM event_drop_assets WHERE campaign_id=?", (other,)
        )[0]["id"]
        with self.assertRaises(ValueError):
            self.s.save_variant(
                self.c,
                "1",
                dict(VARIANT_DEFAULTS, name="Foreign", image_mode="single"),
                asset_ids=[asset],
            )
        self.s.set_variants_enabled(other, "2", True)
        foreign = self.s.variants(other)[0]["id"]
        self.begin()
        with self.assertRaises(ValueError):
            self.s.queue_manual(self.c, "1", "foreign", variant_id=foreign)

    def test_disable_reenable_soft_delete_preserve_claims(self):
        self.begin()
        d = self.drop(self.gold)
        self.s.transition(self.c, "1", "pause")
        self.s.variant_action(self.c, "1", self.gold, "disable")
        self.assertTrue(self.s.claim(d, "55", "1")["ok"])
        with self.assertRaises(ValueError):
            self.s.queue_manual(self.c, "1", "disabled", variant_id=self.gold)
        self.s.variant_action(self.c, "1", self.gold, "enable")
        self.s.variant_action(self.c, "1", self.gold, "delete")
        self.assertTrue(
            self.s.rows(
                "SELECT deleted_at FROM event_drop_variants WHERE id=?", (self.gold,)
            )[0]["deleted_at"]
        )
        self.assertEqual(
            self.s.variant_results(self.c, "55")[0]["variant_name"], "Golden Fish"
        )
        self.assertTrue(self.s.claim(d, "56", "1")["ok"])
        self.s.variant_action(self.c, "1", self.legend, "delete")
        self.assertEqual(
            self.s.rows(
                "SELECT id FROM event_drop_variants WHERE id=?", (self.legend,)
            ),
            [],
        )

    def test_duplicate_variant_and_campaign_copy_configuration_only(self):
        gold = next(v for v in self.s.variants(self.c) if v["id"] == self.gold)
        gold["image_mode"] = "single"
        self.s.save_variant(
            self.c, "1", gold, self.gold, assets=[(b"gold", "image/webp")]
        )
        copy = self.s.variant_action(self.c, "1", self.gold, "duplicate")
        duplicated = next(v for v in self.s.variants(self.c) if v["id"] == copy)
        self.assertFalse(duplicated["is_default"])
        self.assertEqual(duplicated["points"], 5)
        self.assertEqual(duplicated["title_override"], "Golden!")
        new = self.s.duplicate(self.c, "1", "admin")
        self.assertEqual(self.s.campaign(new)["status"], "draft")
        self.assertTrue(self.s.campaign(new)["variants_enabled"])
        self.assertEqual(len(self.s.variants(new)), 4)
        self.assertEqual(self.s.leaderboard(new), [])
        for v in self.s.variants(new):
            for asset in v["asset_ids"]:
                self.assertEqual(
                    self.s.rows(
                        "SELECT campaign_id FROM event_drop_assets WHERE id=?", (asset,)
                    )[0]["campaign_id"],
                    new,
                )

    def test_automatic_selects_once_and_manual_force_is_recorded(self):
        self.begin()
        now = time.time()
        self.s.execute(
            "UPDATE event_drop_campaigns SET next_drop_at=? WHERE id=?", (now, self.c)
        )
        with patch(
            "utils.event_drop_variants.random.random", return_value=0.95
        ) as draw:
            self.s.tick(now=now)
            self.s.tick(now=now)
        draw.assert_called_once()
        auto = self.s.rows("SELECT * FROM event_drops WHERE kind='automatic'")[0]
        self.assertEqual(
            (auto["variant_id"], auto["variant_selection"]), (self.gold, "random")
        )
        manual = self.s.queue_manual(self.c, "1", "forced", variant_id=self.legend)
        self.assertEqual(
            self.s.rows(
                "SELECT variant_selection FROM event_drops WHERE id=?", (manual,)
            )[0]["variant_selection"],
            "forced",
        )
        self.assertEqual(
            self.s.queue_manual(self.c, "1", "forced", variant_id=self.normal), manual
        )

    def test_variant_results_include_zero_claim_drops_and_actual_points(self):
        self.begin()
        self.drop(self.gold)
        b = self.drop(self.gold)
        self.s.claim(b, "55", "1")
        stat = self.s.variant_results(self.c)[0]
        self.assertEqual(
            (
                stat["drops"],
                stat["claims"],
                stat["total_points"],
                stat["average_claims"],
            ),
            (2, 1, 5, 0.5),
        )

    def test_maximum_user_points_still_blocks_full_rare_award(self):
        self.s.execute(
            "UPDATE event_drop_campaigns SET max_user_points=6 WHERE id=?", (self.c,)
        )
        self.begin()
        self.s.claim(self.drop(self.gold), "55", "1")
        self.assertFalse(self.s.claim(self.drop(self.gold), "55", "1")["ok"])
        self.assertTrue(self.s.claim(self.drop(self.normal), "55", "1")["ok"])
        self.assertEqual(self.s.leaderboard(self.c)[0]["total_points"], 6)

    def test_v1_migration_preserves_points_assets_and_restart_claims(self):
        legacy = Path(self.temp.name) / "legacy.sqlite"
        now = time.time()
        with sqlite3.connect(legacy) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO event_drop_schema VALUES(1,?)", (now,))
            db.execute(
                "INSERT INTO event_drop_campaigns(id,guild_id,name,status,singular,plural,points,title,button_label,created_by,created_at,updated_at) VALUES(1,'1','Legacy','active','Fish','Fish',1,'Original','Catch','admin',?,?)",
                (now, now),
            )
            db.execute(
                "INSERT INTO event_drop_assets VALUES(1,1,?,'image/webp',?)",
                (b"old image", now),
            )
            db.execute(
                "INSERT INTO event_drops(id,campaign_id,channel_id,message_id,kind,posted_at,expires_at,created_at,status,asset_id,points,claim_count) VALUES(1,1,'10','20','manual',?,?,?,'active',1,5,1)",
                (now, now + 600, now),
            )
            db.execute("INSERT INTO event_drop_claims VALUES(1,1,1,'55',5,?)", (now,))
        migrated = EventDrops(legacy)
        migrated.initialize()
        migrated.initialize()
        self.assertFalse(migrated.campaign(1)["variants_enabled"])
        self.assertEqual(migrated.variants(1), [])
        self.assertEqual(
            migrated.rows("SELECT points,asset_id,variant_name FROM event_drops")[0],
            dict(points=5, asset_id=1, variant_name="Standard Drop"),
        )
        self.assertFalse(migrated.claim(1, "55", "1")["ok"])
        self.assertEqual(migrated.claim(1, "56", "1")["total"], 5)
        self.assertEqual(migrated.drop_appearance(1)["title"], "Original")
        self.assertEqual(
            migrated.rows("SELECT MAX(version) AS version FROM event_drop_schema")[0][
                "version"
            ],
            2,
        )
        from scripts.migrate_event_drops import validate

        validate(legacy)


class VariantDiscordTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_sends_snapshot_after_queued_variant_edit_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            service = EventDrops(Path(folder) / "db.sqlite")
            service.initialize()
            c = service.save("1", "admin", dict(DEFAULTS, name="Test"), ["10"])
            service.set_variants_enabled(c, "1", True)
            v = service.save_variant(
                c,
                "1",
                dict(
                    VARIANT_DEFAULTS,
                    name="Rare",
                    points=5,
                    title_override="Original rare",
                    button_label_override="Rare button",
                ),
            )
            service.transition(c, "1", "start")
            service.transition(c, "1", "pause")
            d = service.queue_manual(c, "1", "queued", variant_id=v)
            values = next(x for x in service.variants(c) if x["id"] == v)
            values.update(
                title_override="Changed",
                button_label_override="Changed button",
                points=10,
            )
            service.save_variant(c, "1", values, v)
            bot = MagicMock()
            cog = EventDropsCog(bot)
            cog.service = EventDrops(service.path)
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 10
            from datetime import datetime, timezone

            channel.send = AsyncMock(
                return_value=SimpleNamespace(
                    id=123, created_at=datetime.now(timezone.utc)
                )
            )
            cog.eligible_channels = AsyncMock(return_value=[channel])
            with patch("cogs.event_drops.publish_audit", new=AsyncMock()):
                await cog.send_drop(d)
            args = channel.send.call_args.kwargs
            self.assertEqual(args["embed"].title, "Original rare")
            self.assertIn("Worth: 5 Points", args["embed"].description)
            self.assertEqual(args["view"].children[0].item.label, "Rare button")
            self.assertTrue(args["view"].is_persistent())
            restored = await ClaimButton.from_custom_id(
                None,
                None,
                re.match(
                    r"eventdrop:claim:(?P<drop_id>[0-9]+)", f"eventdrop:claim:{d}"
                ),
            )
            self.assertEqual(restored.drop_id, d)
            self.assertEqual(cog.service.claim(d, "55", "1")["total"], 5)


class VariantRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(Path(self.temp.name) / "db.sqlite"),
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "admin",
                "DASHBOARD_PASSWORD": "test-password",
                "DASHBOARD_SECRET_KEY": "variant-test-key",
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
        self.c = self.s.save(
            "1", "admin", dict(DEFAULTS, name="Route Campaign"), ["10"]
        )
        login = self.client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', login.text)[1]
        self.client.post(
            "/login",
            data={"username": "admin", "password": "test-password", "csrf": token},
        )
        page = self.client.get(f"/events/drops/{self.c}/variants")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.temp.cleanup()

    def test_enable_edit_probability_preview_and_manual_force(self):
        response = self.client.post(
            f"/events/drops/{self.c}/variants/mode",
            data={"csrf": self.csrf, "mode": "enabled"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Default", response.text)
        payload = dict(
            name="Golden Fish",
            rarity="Rare",
            weight="8",
            points="5",
            enabled="on",
            show_reward="on",
            image_mode="inherit",
            sort_order="1",
            csrf=self.csrf,
            override_title="on",
            title_override="Golden!",
        )
        response = self.client.post(
            f"/events/drops/{self.c}/variants/new", data=payload
        )
        self.assertEqual(response.status_code, 200, response.text[:400])
        self.assertIn("Golden Fish", response.text)
        v = self.s.variants(self.c)[1]
        self.assertAlmostEqual(v["chance"], 8 / 108 * 100)
        page = self.client.get(f'/events/drops/{self.c}/variants/{v["id"]}/edit')
        self.assertIn("Editing variant", page.text)
        self.assertIn("data-campaign", page.text)
        self.s.transition(self.c, "1", "start")
        page = self.client.get(f"/events/drops/{self.c}")
        self.assertIn("Rare Drop Now", page.text)
        self.assertIn('name="variant_id" required', page.text)
        forms = re.findall(r"<form\b.*?</form>", page.text, re.S)
        rare_form = next(f for f in forms if 'value="rare_drop"' in f)
        random_form = next(f for f in forms if 'value="drop"' in f)
        token = re.search(r'name="submission_id" value="([^"]+)"', rare_form)[1]
        random_token = re.search(r'name="submission_id" value="([^"]+)"', random_form)[
            1
        ]
        self.assertNotEqual(token, random_token)
        next_drop = self.s.campaign(self.c)["next_drop_at"]
        data = {
            "csrf": self.csrf,
            "action": "rare_drop",
            "submission_id": token,
            "variant_id": v["id"],
            "channel_id": "10",
        }
        with patch(
            "utils.event_drop_variants.random.random",
            side_effect=AssertionError(
                "A forced variant must bypass the weighted draw"
            ),
        ):
            response = self.client.post(f"/events/drops/{self.c}/action", data=data)
            self.client.post(f"/events/drops/{self.c}/action", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Rare drop #", response.text)
        self.assertEqual(len(self.s.rows("SELECT * FROM event_drops")), 1)
        self.assertEqual(self.s.campaign(self.c)["next_drop_at"], next_drop)
        d = self.s.rows("SELECT * FROM event_drops")[0]
        self.assertEqual(
            (d["variant_id"], d["points"], d["variant_selection"]),
            (v["id"], 5, "forced"),
        )
        self.assertIn("Forced Variant", response.text)

    def test_rare_drop_requires_an_enabled_variant(self):
        self.s.set_variants_enabled(self.c, "1", True)
        disabled = self.s.save_variant(
            self.c,
            "1",
            dict(VARIANT_DEFAULTS, name="Disabled rare", enabled=False),
        )
        self.s.transition(self.c, "1", "start")
        self.s.transition(self.c, "1", "pause")
        page = self.client.get(f"/events/drops/{self.c}")
        self.assertIn("Rare Drop Now", page.text)
        rare_form = next(
            f
            for f in re.findall(r"<form\b.*?</form>", page.text, re.S)
            if 'value="rare_drop"' in f
        )
        self.assertNotIn("Disabled rare", rare_form)
        data = dict(csrf=self.csrf, action="rare_drop", submission_id="missing")
        response = self.client.post(f"/events/drops/{self.c}/action", data=data)
        self.assertIn("Choose a variant before using Rare Drop Now", response.text)
        response = self.client.post(
            f"/events/drops/{self.c}/action",
            data=dict(data, variant_id=disabled),
        )
        self.assertIn("Choose an enabled Drop Variant", response.text)
        self.assertEqual(self.s.rows("SELECT * FROM event_drops"), [])
        self.s.set_variants_enabled(self.c, "1", False)
        page = self.client.get(f"/events/drops/{self.c}")
        self.assertIn("Configure Drop Variants", page.text)
        self.assertNotIn('value="rare_drop"', page.text)

    def test_invalid_weights_and_active_edit_lock(self):
        self.s.set_variants_enabled(self.c, "1", True)
        data = dict(
            name="Broken", weight="nan", points="5", enabled="on", csrf=self.csrf
        )
        response = self.client.post(f"/events/drops/{self.c}/variants/new", data=data)
        self.assertEqual(response.status_code, 400)
        self.assertIn("finite", response.text)
        self.s.transition(self.c, "1", "start")
        data["weight"] = "10"
        self.assertEqual(
            self.client.post(
                f"/events/drops/{self.c}/variants/new", data=data
            ).status_code,
            400,
        )

    def test_permissions_csrf_and_foreign_variant_ids(self):
        self.assertEqual(
            self.client.post(
                f"/events/drops/{self.c}/variants/mode",
                data={"csrf": "bad", "mode": "enabled"},
            ).status_code,
            400,
        )
        with patch("dashboard.app.has_permission", return_value=False):
            for suffix in [
                "/variants",
                "/variants/new",
                "/exports/claims.csv",
                "/exports/drops.csv",
                "/participants/55",
            ]:
                self.assertEqual(
                    self.client.get(f"/events/drops/{self.c}" + suffix).status_code, 403
                )
        other = self.s.save("2", "admin", dict(DEFAULTS, name="Private"), ["20"])
        self.s.set_variants_enabled(other, "2", True)
        v = self.s.variants(other)[0]["id"]
        self.assertEqual(
            self.client.get(f"/events/drops/{self.c}/variants/{v}/edit").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/events/drops/{other}/variants").status_code, 404
        )

    def test_variant_uploads_and_detailed_exports_keep_summary_compatible(self):
        self.s.set_variants_enabled(self.c, "1", True)
        image = io.BytesIO()
        Image.new("RGB", (300, 200), "#ddba33").save(image, "PNG")
        data = dict(
            name="=Golden",
            rarity="Rare",
            weight="8",
            points="5",
            enabled="on",
            show_reward="on",
            image_mode="single",
            csrf=self.csrf,
        )
        response = self.client.post(
            f"/events/drops/{self.c}/variants/new",
            data=data,
            files={"images": ("gold.png", image.getvalue(), "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text[:400])
        v = self.s.variants(self.c)[1]
        self.assertEqual(
            self.s.rows("SELECT campaign_pool FROM event_drop_assets")[0][
                "campaign_pool"
            ],
            0,
        )
        self.s.transition(self.c, "1", "start")
        d = self.s.queue_manual(self.c, "1", "claim", variant_id=v["id"])
        self.s.take_pending(d)
        self.s.prepare_send(d, "10")
        self.s.sent(d, 100)
        self.s.claim(d, "55", "1", display_name="Joe")
        summary = self.client.get(f"/events/drops/{self.c}/export.csv")
        self.assertEqual(
            summary.text.splitlines()[0],
            "user_id,display_name,total_points,drops_claimed,rank",
        )
        claims = self.client.get(f"/events/drops/{self.c}/exports/claims.csv")
        self.assertIn("variant_id,variant_name,rarity", claims.text)
        self.assertIn("'=Golden", claims.text)
        drops = self.client.get(f"/events/drops/{self.c}/exports/drops.csv")
        self.assertIn("points_per_claim", drops.text)
        member = self.client.get(f"/events/drops/{self.c}/participants/55")
        self.assertEqual(member.status_code, 200)
        self.assertIn("=Golden", member.text)
        self.assertEqual(
            self.client.get(f"/events/drops/{self.c}/exports/unknown.csv").status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
