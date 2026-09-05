"""Durable Event Drops domain service shared by the bot and Garden.

Each write uses its own connection and BEGIN IMMEDIATE: unrelated cogs cannot
accidentally commit a claim or reservation on the bot's shared connection.
Discord sends are at-most-once attempts. Ambiguous attempts are reconciled by
message marker, never resent. Claims, rather than counters, are authoritative.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from utils.event_drop_variants import (
    APPEARANCE_FIELDS,
    VARIANT_DEFAULTS,
    SNAPSHOT_FIELDS,
    migrate_variants,
    resolve_appearance,
    select_drop_variant,
    valid_weight,
)
from utils.settings import settings_database_path
from utils.sqlite import AutoClosingSQLiteConnection, configure_sync_connection

log = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS event_drop_schema(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS event_drop_campaigns(
 id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','scheduled','active','paused','completed')),
 singular TEXT NOT NULL, plural TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT '', points INTEGER NOT NULL CHECK(points>0),
 title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '#5865f2', thumbnail TEXT NOT NULL DEFAULT '',
 button_label TEXT NOT NULL, button_emoji TEXT NOT NULL DEFAULT '', button_style TEXT NOT NULL DEFAULT 'primary',
 schedule_mode TEXT NOT NULL DEFAULT 'fixed', fixed_minutes INTEGER NOT NULL DEFAULT 60,
 random_min INTEGER NOT NULL DEFAULT 45, random_max INTEGER NOT NULL DEFAULT 90,
 claim_minutes INTEGER NOT NULL DEFAULT 10, start_at REAL, end_at REAL, next_drop_at REAL, last_drop_at REAL, last_scheduled_at REAL,
 avoid_recent INTEGER NOT NULL DEFAULT 3, max_drops INTEGER, max_user_points INTEGER,
 created_by TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, warning TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_drop_schedule ON event_drop_campaigns(status,next_drop_at);
CREATE TABLE IF NOT EXISTS event_drop_channels(
 campaign_id INTEGER NOT NULL REFERENCES event_drop_campaigns(id) ON DELETE CASCADE,
 channel_id TEXT NOT NULL, PRIMARY KEY(campaign_id,channel_id)
);
CREATE TABLE IF NOT EXISTS event_drop_roles(
 campaign_id INTEGER NOT NULL REFERENCES event_drop_campaigns(id) ON DELETE CASCADE,
 role_id TEXT NOT NULL, rule TEXT NOT NULL CHECK(rule IN ('eligible','excluded')), PRIMARY KEY(campaign_id,role_id,rule)
);
CREATE TABLE IF NOT EXISTS event_drop_assets(
 id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES event_drop_campaigns(id) ON DELETE CASCADE,
 image_bytes BLOB NOT NULL, content_type TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS event_drops(
 id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES event_drop_campaigns(id),
 channel_id TEXT, message_id TEXT, kind TEXT NOT NULL CHECK(kind IN ('automatic','manual')),
 scheduled_at REAL, posted_at REAL, expires_at REAL, created_at REAL NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','sending','active','expired','deleted','failed','missed')),
 asset_id INTEGER REFERENCES event_drop_assets(id), points INTEGER NOT NULL,
 claim_count INTEGER NOT NULL DEFAULT 0, error TEXT, cleanup_after REAL NOT NULL DEFAULT 0, reconcile_after_id TEXT,
 request_key TEXT UNIQUE, UNIQUE(campaign_id,scheduled_at)
);
CREATE INDEX IF NOT EXISTS idx_event_drops_campaign ON event_drops(campaign_id,id);
CREATE INDEX IF NOT EXISTS idx_event_drops_expiry ON event_drops(status,expires_at,cleanup_after);
CREATE TABLE IF NOT EXISTS event_drop_claims(
 id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES event_drop_campaigns(id),
 drop_id INTEGER NOT NULL REFERENCES event_drops(id), user_id TEXT NOT NULL,
 points INTEGER NOT NULL CHECK(points>0), claimed_at REAL NOT NULL, UNIQUE(drop_id,user_id)
);
CREATE INDEX IF NOT EXISTS idx_event_drop_totals ON event_drop_claims(campaign_id,user_id);
CREATE TABLE IF NOT EXISTS event_drop_members(
 guild_id TEXT NOT NULL,user_id TEXT NOT NULL,display_name TEXT NOT NULL,updated_at REAL NOT NULL,
 PRIMARY KEY(guild_id,user_id)
);
CREATE TABLE IF NOT EXISTS event_drop_worker(id INTEGER PRIMARY KEY CHECK(id=1),heartbeat REAL NOT NULL);
"""
DEFAULTS = dict(
    name="",
    singular="Point",
    plural="Points",
    emoji="",
    points=1,
    title="Event Drop",
    description="Collect a point before this drop expires.",
    color="#5865f2",
    thumbnail="",
    button_label="Collect",
    button_emoji="",
    button_style="primary",
    schedule_mode="fixed",
    fixed_minutes=60,
    random_min=45,
    random_max=90,
    claim_minutes=10,
    start_at=None,
    end_at=None,
    avoid_recent=3,
    max_drops=None,
    max_user_points=None,
)


class EventDrops:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else settings_database_path()

    @contextmanager
    def connect(self, write=False):
        with sqlite3.connect(
            self.path, timeout=30, factory=AutoClosingSQLiteConnection
        ) as db:
            configure_sync_connection(db)
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            # Additive, idempotent schema migration, matching the Events Hub.
            db.executescript(
                "BEGIN IMMEDIATE;\n"
                + SCHEMA
                + "\nINSERT OR IGNORE INTO event_drop_schema VALUES(1,unixepoch());\nCOMMIT;"
            )

        with self.connect(True) as db:
            migrate_variants(db)

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect(True) as db:
            return db.execute(sql, args).rowcount

    @staticmethod
    def _campaign(db, campaign_id, guild_id=None):
        row = db.execute(
            "SELECT * FROM event_drop_campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        if not row or (guild_id is not None and row["guild_id"] != str(guild_id)):
            raise ValueError("Campaign not found.")
        result = dict(row)
        result["channels"] = [
            r[0]
            for r in db.execute(
                "SELECT channel_id FROM event_drop_channels WHERE campaign_id=?",
                (campaign_id,),
            )
        ]
        for rule in ("eligible", "excluded"):
            result[rule + "_roles"] = [
                r[0]
                for r in db.execute(
                    "SELECT role_id FROM event_drop_roles WHERE campaign_id=? AND rule=?",
                    (campaign_id, rule),
                )
            ]
        return result

    def campaign(self, campaign_id, guild_id=None):
        with self.connect() as db:
            return self._campaign(db, campaign_id, guild_id)

    @staticmethod
    def _editable(campaign):
        if campaign["status"] not in ("draft", "paused"):
            raise ValueError(
                "Pause the campaign before editing Drop Variants. Completed campaigns can be duplicated."
            )

    @staticmethod
    def _variants(db, campaign_id):
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM event_drop_variants WHERE campaign_id=? AND deleted_at IS NULL ORDER BY sort_order,id",
                (campaign_id,),
            )
        ]
        total = sum(row["weight"] for row in rows if row["enabled"])
        for row in rows:
            row["asset_ids"] = [
                r[0]
                for r in db.execute(
                    "SELECT asset_id FROM event_drop_variant_assets WHERE variant_id=? ORDER BY asset_id",
                    (row["id"],),
                )
            ]
            row["chance"] = (
                row["weight"] / total * 100 if row["enabled"] and total else 0
            )
        return rows

    def variants(self, campaign_id, guild_id=None):
        with self.connect() as db:
            self._campaign(db, campaign_id, guild_id)
            return self._variants(db, campaign_id)

    @staticmethod
    def _validate_variant_set(db, campaign):
        if not campaign["variants_enabled"]:
            return
        variants = EventDrops._variants(db, campaign["id"])
        enabled = [v for v in variants if v["enabled"]]
        if not enabled:
            raise ValueError(
                "At least one enabled Drop Variant must have a selection weight greater than 0."
            )
        for variant in enabled:
            valid_weight(variant["weight"])
        if len([v for v in enabled if v["is_default"]]) != 1:
            raise ValueError(
                "Choose one enabled Default Variant before disabling or deleting the current default."
            )

    def set_variants_enabled(self, campaign_id, guild_id, enabled):
        with self.connect(True) as db:
            c = self._campaign(db, campaign_id, guild_id)
            self._editable(c)
            if enabled and not self._variants(db, campaign_id):
                now = time.time()
                db.execute(
                    "INSERT INTO event_drop_variants(campaign_id,name,weight,points,is_default,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                    (campaign_id, "Standard Drop", 100, c["points"], now, now),
                )
            c["variants_enabled"] = int(bool(enabled))
            self._validate_variant_set(db, c)
            db.execute(
                "UPDATE event_drop_campaigns SET variants_enabled=?,updated_at=? WHERE id=?",
                (c["variants_enabled"], time.time(), campaign_id),
            )

    def validate_variant(self, campaign, values):
        data = {key: values.get(key, value) for key, value in VARIANT_DEFAULTS.items()}
        for key in ("enabled", "is_default", "show_reward", "show_rarity"):
            data[key] = int(data[key] in (True, 1, "1", "on"))
        data["name"] = str(data["name"] or "").strip()
        data["rarity"] = str(data["rarity"] or "").strip()
        if not data["name"] or len(data["name"]) > 100 or len(data["rarity"]) > 60:
            raise ValueError(
                "Variant name is required (up to 100 characters); rarity may be up to 60 characters."
            )
        data["weight"] = valid_weight(data["weight"], bool(data["enabled"]))
        try:
            data["points"] = int(str(data["points"]))
            data["sort_order"] = int(str(data["sort_order"]))
        except (ValueError, TypeError):
            raise ValueError(
                "Reward and display order must be whole numbers."
            ) from None
        if not 1 <= data["points"] <= 1_000_000 or not 0 <= data["sort_order"] <= 10000:
            raise ValueError(
                "Reward must be 1–1,000,000 points; display order must be 0–10,000."
            )
        if data["image_mode"] not in ("inherit", "single", "pool", "none"):
            raise ValueError("Choose a valid image behavior.")
        for field in APPEARANCE_FIELDS:
            key = field + "_override"
            if data[key] is not None:
                data[key] = str(data[key]).strip()
        appearance = resolve_appearance(campaign, data)
        self.validate(
            dict(campaign, **appearance, points=data["points"], max_user_points=None),
            campaign["channels"],
        )
        return data

    def save_variant(
        self, campaign_id, guild_id, values, variant_id=None, asset_ids=(), assets=()
    ):
        with self.connect(True) as db:
            c = self._campaign(db, campaign_id, guild_id)
            self._editable(c)
            data = self.validate_variant(c, values)
            variants = self._variants(db, campaign_id)
            if variant_id and not any(v["id"] == int(variant_id) for v in variants):
                raise ValueError("Drop Variant not found in this campaign.")
            if not variant_id and len(variants) >= 30:
                raise ValueError("Use at most 30 Drop Variants per campaign.")
            ids = {int(a) for a in asset_ids}
            known = {
                r[0]
                for r in db.execute(
                    "SELECT id FROM event_drop_assets WHERE campaign_id=?",
                    (campaign_id,),
                )
            }
            if ids - known:
                raise ValueError("Choose images belonging to this campaign.")
            if len(ids) + len(assets) > 10:
                raise ValueError("Use at most 10 images in a variant pool.")
            if data["image_mode"] == "single" and len(ids) + len(assets) != 1:
                raise ValueError(
                    "Select or upload exactly one image for Variant Image."
                )
            if data["image_mode"] == "pool" and not (ids or assets):
                raise ValueError(
                    "Select or upload at least one image for Variant Image Pool."
                )
            if data["is_default"]:
                db.execute(
                    "UPDATE event_drop_variants SET is_default=0 WHERE campaign_id=?",
                    (campaign_id,),
                )
            now = time.time()
            if variant_id:
                db.execute(
                    "UPDATE event_drop_variants SET "
                    + ",".join(k + "=?" for k in data)
                    + ",updated_at=? WHERE id=?",
                    (*data.values(), now, variant_id),
                )
            else:
                variant_id = db.execute(
                    "INSERT INTO event_drop_variants(campaign_id,created_at,updated_at,"
                    + ",".join(data)
                    + ") VALUES("
                    + ",".join("?" for _ in range(len(data) + 3))
                    + ")",
                    (campaign_id, now, now, *data.values()),
                ).lastrowid
            for blob, mime in assets:
                asset_id = db.execute(
                    "INSERT INTO event_drop_assets(campaign_id,image_bytes,content_type,created_at,campaign_pool) VALUES(?,?,?,?,0)",
                    (campaign_id, blob, mime, now),
                ).lastrowid
                ids.add(asset_id)
            db.execute(
                "DELETE FROM event_drop_variant_assets WHERE variant_id=?",
                (variant_id,),
            )
            db.executemany(
                "INSERT INTO event_drop_variant_assets VALUES(?,?)",
                [(variant_id, a) for a in ids],
            )
            self._validate_variant_set(db, c)
            db.execute(
                "UPDATE event_drop_campaigns SET updated_at=? WHERE id=?",
                (now, campaign_id),
            )
        log.info(
            "Event Drop variant saved campaign=%s variant=%s", campaign_id, variant_id
        )
        return variant_id

    def variant_action(self, campaign_id, guild_id, variant_id, action):
        with self.connect(True) as db:
            c = self._campaign(db, campaign_id, guild_id)
            self._editable(c)
            variants = self._variants(db, campaign_id)
            variant = next((v for v in variants if v["id"] == int(variant_id)), None)
            if variant is None:
                raise ValueError("Drop Variant not found in this campaign.")
            if action == "duplicate":
                if len(variants) >= 30:
                    raise ValueError("Use at most 30 Drop Variants per campaign.")
                values = {key: variant[key] for key in VARIANT_DEFAULTS}
                values.update(name=variant["name"][:93] + " (copy)", is_default=0)
                variant_id = db.execute(
                    "INSERT INTO event_drop_variants(campaign_id,created_at,updated_at,"
                    + ",".join(values)
                    + ") VALUES("
                    + ",".join("?" for _ in range(len(values) + 3))
                    + ")",
                    (campaign_id, time.time(), time.time(), *values.values()),
                ).lastrowid
                db.executemany(
                    "INSERT INTO event_drop_variant_assets VALUES(?,?)",
                    [(variant_id, a) for a in variant["asset_ids"]],
                )
            elif action == "default":
                if not variant["enabled"]:
                    raise ValueError(
                        "Enable this variant before making it the default."
                    )
                db.execute(
                    "UPDATE event_drop_variants SET is_default=0 WHERE campaign_id=?",
                    (campaign_id,),
                )
                db.execute(
                    "UPDATE event_drop_variants SET is_default=1,updated_at=? WHERE id=?",
                    (time.time(), variant_id),
                )
            elif action in ("enable", "disable"):
                valid_weight(variant["weight"], enabled=action == "enable")
                db.execute(
                    "UPDATE event_drop_variants SET enabled=?,updated_at=? WHERE id=?",
                    (int(action == "enable"), time.time(), variant_id),
                )
            elif action == "delete":
                if db.execute(
                    "SELECT 1 FROM event_drops WHERE variant_id=?", (variant_id,)
                ).fetchone():
                    db.execute(
                        "UPDATE event_drop_variants SET enabled=0,is_default=0,deleted_at=?,updated_at=? WHERE id=?",
                        (time.time(), time.time(), variant_id),
                    )
                else:
                    db.execute(
                        "DELETE FROM event_drop_variants WHERE id=?", (variant_id,)
                    )
            else:
                raise ValueError("Unknown Drop Variant action.")
            self._validate_variant_set(db, c)
            db.execute(
                "UPDATE event_drop_campaigns SET updated_at=? WHERE id=?",
                (time.time(), campaign_id),
            )
        return variant_id

    def drop_appearance(self, drop_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT s.*,d.points,d.variant_name,d.rarity,d.variant_id FROM event_drop_snapshots s JOIN event_drops d ON d.id=s.drop_id WHERE drop_id=?",
                (drop_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Drop snapshot not found.")
            return dict(row)

    def variant_results(self, campaign_id, user_id=None):
        if user_id is not None:
            return self.rows(
                """SELECT d.variant_id,d.variant_name,d.rarity,COUNT(*) AS claims,SUM(q.points) AS total_points
                FROM event_drop_claims q JOIN event_drops d ON d.id=q.drop_id
                WHERE q.campaign_id=? AND q.user_id=? GROUP BY d.variant_id,d.variant_name,d.rarity ORDER BY total_points DESC""",
                (campaign_id, str(user_id)),
            )
        return self.rows(
            """SELECT d.variant_id,d.variant_name,d.rarity,COUNT(*) AS drops,
            COALESCE(SUM(q.claims),0) AS claims,COALESCE(SUM(q.points),0) AS total_points,
            AVG(COALESCE(q.claims,0)) AS average_claims FROM event_drops d LEFT JOIN
            (SELECT drop_id,COUNT(*) AS claims,SUM(points) AS points FROM event_drop_claims GROUP BY drop_id) q ON q.drop_id=d.id
            WHERE d.campaign_id=? AND d.posted_at IS NOT NULL GROUP BY d.variant_id,d.variant_name,d.rarity ORDER BY drops DESC""",
            (campaign_id,),
        )

    @staticmethod
    def validate(values, channels, starting=False):
        data = {key: values.get(key, default) for key, default in DEFAULTS.items()}
        for key, maximum in [
            ("name", 100),
            ("singular", 50),
            ("plural", 50),
            ("title", 256),
            ("description", 3500),
            ("button_label", 80),
            ("emoji", 100),
            ("button_emoji", 100),
            ("thumbnail", 1000),
        ]:
            data[key] = str(data[key] or "").strip()
            if len(data[key]) > maximum or (
                key in ("name", "singular", "plural", "title", "button_label")
                and not data[key]
            ):
                raise ValueError(
                    f'{key.replace("_", " ").title()} is required and must be at most {maximum} characters.'
                )
        for key in (
            "points",
            "fixed_minutes",
            "random_min",
            "random_max",
            "claim_minutes",
        ):
            try:
                data[key] = int(data[key])
            except (ValueError, TypeError):
                raise ValueError(
                    f'{key.replace("_", " ")} must be a positive whole number.'
                ) from None
            if not 1 <= data[key] <= (1000000 if key == "points" else 525600):
                raise ValueError(
                    f'{key.replace("_", " ")} is outside the supported range.'
                )
        data["avoid_recent"] = int(data["avoid_recent"])
        if not 0 <= data["avoid_recent"] <= 100:
            raise ValueError("Avoid Last X Channels must be between 0 and 100.")
        for key in ("max_drops", "max_user_points"):
            data[key] = int(data[key]) if data[key] not in (None, "") else None
            if data[key] is not None and not 1 <= data[key] <= 1000000000:
                raise ValueError(
                    "Maximums must be positive or left empty for unlimited."
                )
        if (
            data["max_user_points"] is not None
            and data["max_user_points"] < data["points"]
        ):
            raise ValueError("User maximum must allow at least one full claim.")
        if data["schedule_mode"] not in ("fixed", "random") or data[
            "button_style"
        ] not in ("primary", "secondary", "success", "danger"):
            raise ValueError("Choose a valid schedule and button style.")
        if data["random_min"] > data["random_max"]:
            raise ValueError(
                "Random maximum interval must be greater than or equal to the minimum."
            )
        import re

        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(data["color"])):
            raise ValueError("Embed color must be a six-digit hex color.")
        if data["thumbnail"]:
            url = urlsplit(data["thumbnail"])
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
            ):
                raise ValueError("Thumbnail must be a public HTTPS image URL.")
        for key in ("start_at", "end_at"):
            data[key] = float(data[key]) if data[key] not in (None, "") else None
            if data[key] is not None and not 0 < data[key] < 253402300799:
                raise ValueError("Campaign date is invalid.")
        if (
            data["end_at"] is not None
            and data["start_at"] is not None
            and data["end_at"] <= data["start_at"]
        ):
            raise ValueError("Campaign end time must be later than its start time.")
        if starting and not channels:
            raise ValueError("Select at least one channel where drops can appear.")
        return data

    def save(
        self,
        guild_id,
        actor,
        values,
        channels,
        campaign_id=None,
        assets=(),
        remove_assets=(),
        eligible_roles=(),
        excluded_roles=(),
    ):
        data = self.validate(values, channels)
        ids = {str(c) for c in channels}
        if not str(guild_id).isdigit() or any(not c.isdigit() for c in ids):
            raise ValueError(
                "A configured server and valid individual channels are required."
            )
        now = time.time()
        with self.connect(True) as db:
            if campaign_id:
                old = self._campaign(db, campaign_id, guild_id)
                if old["status"] not in ("draft", "paused"):
                    raise ValueError(
                        "Pause the campaign before editing. Completed campaigns can be duplicated."
                    )
                db.execute(
                    "UPDATE event_drop_campaigns SET "
                    + ",".join(k + "=?" for k in data)
                    + ",updated_at=? WHERE id=?",
                    (*data.values(), now, campaign_id),
                )
            else:
                cur = db.execute(
                    "INSERT INTO event_drop_campaigns(guild_id,created_by,created_at,updated_at,"
                    + ",".join(data)
                    + ") VALUES("
                    + ",".join("?" for _ in range(len(data) + 4))
                    + ")",
                    (str(guild_id), str(actor), now, now, *data.values()),
                )
                campaign_id = cur.lastrowid
            db.execute(
                "DELETE FROM event_drop_channels WHERE campaign_id=?", (campaign_id,)
            )
            db.executemany(
                "INSERT INTO event_drop_channels VALUES(?,?)",
                [(campaign_id, c) for c in ids],
            )
            db.execute(
                "DELETE FROM event_drop_roles WHERE campaign_id=?", (campaign_id,)
            )
            for rule, roles in [
                ("eligible", eligible_roles),
                ("excluded", excluded_roles),
            ]:
                if any(not str(r).isdigit() for r in roles):
                    raise ValueError("Choose valid Discord roles.")
                db.executemany(
                    "INSERT INTO event_drop_roles VALUES(?,?,?)",
                    [(campaign_id, str(r), rule) for r in set(roles)],
                )
            # Referenced images remain historical assets, but are removed from future selection.
            for asset_id in remove_assets:
                db.execute(
                    "UPDATE event_drop_assets SET created_at=-ABS(created_at) WHERE id=? AND campaign_id=?",
                    (asset_id, campaign_id),
                )
            existing = db.execute(
                "SELECT COUNT(*) FROM event_drop_assets WHERE campaign_id=? AND created_at>0 AND campaign_pool=1",
                (campaign_id,),
            ).fetchone()[0]
            if existing + len(assets) > 10:
                raise ValueError("Use at most 10 campaign images.")
            db.executemany(
                "INSERT INTO event_drop_assets(campaign_id,image_bytes,content_type,created_at) VALUES(?,?,?,?)",
                [(campaign_id, blob, mime, now) for blob, mime in assets],
            )
        log.info("Event Drops campaign saved id=%s actor=%s", campaign_id, actor)
        return campaign_id

    def duplicate(self, campaign_id, guild_id, actor):
        # Copy the complete configuration in one transaction; retain references
        # only within the new campaign and never copy drops, claims or schedules.
        with self.connect(True) as db:
            source = self._campaign(db, campaign_id, guild_id)
            values = {key: source[key] for key in DEFAULTS}
            values.update(
                name=source["name"][:93] + " (copy)", start_at=None, end_at=None
            )
            now = time.time()
            new_id = db.execute(
                "INSERT INTO event_drop_campaigns(guild_id,created_by,created_at,updated_at,variants_enabled,"
                + ",".join(values)
                + ") VALUES("
                + ",".join("?" for _ in range(len(values) + 5))
                + ")",
                (
                    str(guild_id),
                    str(actor),
                    now,
                    now,
                    source["variants_enabled"],
                    *values.values(),
                ),
            ).lastrowid
            db.execute(
                "INSERT INTO event_drop_channels SELECT ?,channel_id FROM event_drop_channels WHERE campaign_id=?",
                (new_id, campaign_id),
            )
            db.execute(
                "INSERT INTO event_drop_roles SELECT ?,role_id,rule FROM event_drop_roles WHERE campaign_id=?",
                (new_id, campaign_id),
            )
            variants = self._variants(db, campaign_id)
            asset_map = {}
            needed = {a for v in variants for a in v["asset_ids"]}
            for asset in db.execute(
                "SELECT * FROM event_drop_assets WHERE campaign_id=?", (campaign_id,)
            ).fetchall():
                if asset["id"] not in needed and not (
                    asset["campaign_pool"] and asset["created_at"] > 0
                ):
                    continue
                asset_map[asset["id"]] = db.execute(
                    "INSERT INTO event_drop_assets(campaign_id,image_bytes,content_type,created_at,campaign_pool) VALUES(?,?,?,?,?)",
                    (
                        new_id,
                        asset["image_bytes"],
                        asset["content_type"],
                        now if asset["created_at"] > 0 else -now,
                        asset["campaign_pool"],
                    ),
                ).lastrowid
            for variant in variants:
                data = {key: variant[key] for key in VARIANT_DEFAULTS}
                new_variant = db.execute(
                    "INSERT INTO event_drop_variants(campaign_id,created_at,updated_at,"
                    + ",".join(data)
                    + ") VALUES("
                    + ",".join("?" for _ in range(len(data) + 3))
                    + ")",
                    (new_id, now, now, *data.values()),
                ).lastrowid
                db.executemany(
                    "INSERT INTO event_drop_variant_assets VALUES(?,?)",
                    [(new_variant, asset_map[a]) for a in variant["asset_ids"]],
                )
            return new_id

    def delete(self, campaign_id, guild_id):
        with self.connect(True) as db:
            campaign = self._campaign(db, campaign_id, guild_id)
            if (
                campaign["status"] != "draft"
                or db.execute(
                    "SELECT 1 FROM event_drops WHERE campaign_id=?", (campaign_id,)
                ).fetchone()
            ):
                raise ValueError(
                    "Only unused drafts can be deleted. End a running campaign to preserve results."
                )
            db.execute("DELETE FROM event_drop_campaigns WHERE id=?", (campaign_id,))

    @staticmethod
    def next_time(campaign, now):
        minutes = (
            campaign["fixed_minutes"]
            if campaign["schedule_mode"] == "fixed"
            else random.randint(campaign["random_min"], campaign["random_max"])
        )
        return now + minutes * 60

    def transition(self, campaign_id, guild_id, action, now=None):
        now = time.time() if now is None else now
        with self.connect(True) as db:
            c = self._campaign(db, campaign_id, guild_id)
            allowed = {
                "start": ("draft",),
                "pause": ("active", "scheduled"),
                "resume": ("paused",),
                "end": ("active", "paused", "scheduled", "draft"),
            }
            if action not in allowed or c["status"] not in allowed[action]:
                raise ValueError("This action is not available for the campaign state.")
            next_at = None
            if action in ("start", "resume"):
                self.validate(c, c["channels"], starting=True)
                self._validate_variant_set(db, c)
                if c["end_at"] is not None and c["end_at"] <= now:
                    raise ValueError("Campaign end time is in the past.")
                state = (
                    "scheduled" if c["start_at"] and c["start_at"] > now else "active"
                )
                next_at = (
                    c["start_at"] if state == "scheduled" else self.next_time(c, now)
                )
            else:
                state = "paused" if action == "pause" else "completed"
            db.execute(
                "UPDATE event_drop_campaigns SET status=?,next_drop_at=?,updated_at=? WHERE id=?",
                (state, next_at, now, campaign_id),
            )
            if state == "completed":
                db.execute(
                    "UPDATE event_drops SET status='expired' WHERE campaign_id=? AND status='active'",
                    (campaign_id,),
                )
            if state in ("completed", "paused"):
                db.execute(
                    "UPDATE event_drops SET status='failed',error='Campaign stopped before delivery' WHERE campaign_id=? AND status='pending'",
                    (campaign_id,),
                )
        log.info("Event Drops campaign %s id=%s", action, campaign_id)

    def queue_manual(
        self, campaign_id, guild_id, key, channel_id=None, variant_id=None
    ):
        now = time.time()
        with self.connect(True) as db:
            c = self._campaign(db, campaign_id, guild_id)
            existing = db.execute(
                "SELECT id FROM event_drops WHERE request_key=? AND campaign_id=?",
                (key, campaign_id),
            ).fetchone()
            if existing:
                return existing[0]
            self._can_send(db, c, now, manual=True)
            if channel_id and str(channel_id) not in c["channels"]:
                raise ValueError("Choose a channel from this campaign’s allowlist.")
            return self._reserve(
                db,
                c,
                now,
                "manual",
                channel_id=str(channel_id) if channel_id else None,
                key=key,
                forced_variant_id=variant_id,
            )

    @staticmethod
    def _can_send(db, c, now, manual=False):
        if (
            c["status"] not in (("active", "paused") if manual else ("active",))
            or (c["start_at"] and now < c["start_at"])
            or (c["end_at"] and now >= c["end_at"])
        ):
            raise ValueError("Campaign is not currently available for drops.")
        total = db.execute(
            "SELECT COUNT(*) FROM event_drops WHERE campaign_id=? AND (posted_at IS NOT NULL OR status IN ('pending','sending'))",
            (c["id"],),
        ).fetchone()[0]
        if c["max_drops"] is not None and total >= c["max_drops"]:
            raise ValueError("Maximum campaign drops reached.")

    @staticmethod
    def _reserve(
        db,
        c,
        now,
        kind,
        scheduled=None,
        channel_id=None,
        key=None,
        forced_variant_id=None,
    ):
        variant = None
        if c.get("variants_enabled"):
            EventDrops._validate_variant_set(db, c)
            variant = select_drop_variant(
                EventDrops._variants(db, c["id"]), forced_variant_id
            )
        elif forced_variant_id is not None:
            raise ValueError("Enable Drop Variants before forcing a variant.")
        mode = variant["image_mode"] if variant else "inherit"
        if mode == "inherit":
            assets = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM event_drop_assets WHERE campaign_id=? AND created_at>0 AND campaign_pool=1",
                    (c["id"],),
                )
            ]
        elif mode == "none":
            assets = []
        else:
            assets = variant["asset_ids"]
            if not assets:
                raise ValueError(
                    "Variant images are unavailable. Edit its image settings."
                )
        appearance = resolve_appearance(c, variant)
        cur = db.execute(
            "INSERT INTO event_drops(campaign_id,channel_id,kind,scheduled_at,created_at,status,asset_id,points,request_key,variant_id,variant_name,rarity,variant_selection) VALUES(?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
            (
                c["id"],
                channel_id,
                kind,
                scheduled,
                now,
                random.choice(assets) if assets else None,
                variant["points"] if variant else c["points"],
                key,
                variant["id"] if variant else None,
                variant["name"] if variant else "Standard Drop",
                variant["rarity"] if variant else "",
                (
                    "forced"
                    if forced_variant_id is not None
                    else ("random" if variant else "standard")
                ),
            ),
        )
        drop_id = cur.lastrowid
        db.execute(
            "INSERT INTO event_drop_snapshots(drop_id,"
            + ",".join(SNAPSHOT_FIELDS)
            + ") VALUES("
            + ",".join("?" for _ in range(len(SNAPSHOT_FIELDS) + 1))
            + ")",
            (drop_id, *(appearance[field] for field in SNAPSHOT_FIELDS)),
        )
        return drop_id

    def tick(self, now=None, recover=False):
        now = time.time() if now is None else now
        with self.connect(True) as db:
            db.execute(
                "INSERT INTO event_drop_worker VALUES(1,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat",
                (now,),
            )
            db.execute(
                "UPDATE event_drop_campaigns SET status='completed',next_drop_at=NULL,updated_at=? WHERE status IN ('active','paused','scheduled') AND end_at<=?",
                (now, now),
            )
            db.execute(
                "UPDATE event_drops SET status='expired' WHERE status='active' AND (expires_at<=? OR campaign_id IN (SELECT id FROM event_drop_campaigns WHERE status='completed'))",
                (now,),
            )
            db.execute(
                "UPDATE event_drops SET status='failed',error='Campaign ended before delivery' WHERE status='pending' AND campaign_id IN (SELECT id FROM event_drop_campaigns WHERE status='completed')"
            )
            db.execute(
                "UPDATE event_drops SET status='missed',error='Reserved occurrence skipped after downtime' WHERE status='pending' AND kind='automatic' AND scheduled_at<?",
                (now - 90,),
            )
            db.execute(
                "UPDATE event_drop_campaigns SET status='active',updated_at=? WHERE status='scheduled' AND start_at<=?",
                (now, now),
            )
            for row in db.execute(
                "SELECT * FROM event_drop_campaigns WHERE status='active' AND next_drop_at<=?",
                (now,),
            ).fetchall():
                c = dict(row)
                scheduled = c["next_drop_at"]
                # Skip downtime in constant time. Ticks delayed over 90 seconds also skip.
                missed = recover or now - scheduled > 90
                try:
                    self._can_send(db, c, now)
                    drop_id = self._reserve(db, c, now, "automatic", scheduled)
                    if missed:
                        db.execute(
                            "UPDATE event_drops SET status='missed',error='Scheduled occurrence skipped after downtime' WHERE id=?",
                            (drop_id,),
                        )
                except (ValueError, sqlite3.IntegrityError) as exc:
                    db.execute(
                        "UPDATE event_drop_campaigns SET warning=? WHERE id=?",
                        (str(exc), c["id"]),
                    )
                if missed and c["schedule_mode"] == "fixed":
                    interval = c["fixed_minutes"] * 60
                    next_at = (
                        scheduled + (int((now - scheduled) // interval) + 1) * interval
                    )
                else:
                    next_at = self.next_time(c, now)
                db.execute(
                    "UPDATE event_drop_campaigns SET next_drop_at=?,updated_at=? WHERE id=?",
                    (next_at, now, c["id"]),
                )

    def take_pending(self, drop_id, now=None):
        now = time.time() if now is None else now
        with self.connect(True) as db:
            row = db.execute(
                "SELECT * FROM event_drops WHERE id=? AND status='pending'", (drop_id,)
            ).fetchone()
            if not row:
                return None
            c = self._campaign(db, row["campaign_id"])
            allowed = c["status"] == "active" or (
                row["kind"] == "manual" and c["status"] == "paused"
            )
            if (
                not allowed
                or (c["end_at"] and now >= c["end_at"])
                or (c["start_at"] and now < c["start_at"])
            ):
                db.execute(
                    "UPDATE event_drops SET status='failed',error='Campaign unavailable' WHERE id=?",
                    (drop_id,),
                )
                return None
            db.execute(
                "UPDATE event_drops SET status='sending',created_at=? WHERE id=?",
                (now, drop_id),
            )
            return dict(row), c

    def prepare_send(self, drop_id, channel_id, now=None):
        now = time.time() if now is None else now
        with self.connect(True) as db:
            row = db.execute(
                "SELECT * FROM event_drops WHERE id=? AND status='sending'", (drop_id,)
            ).fetchone()
            if not row:
                raise ValueError("Drop no longer reserved.")
            c = self._campaign(db, row["campaign_id"])
            allowed = c["status"] == "active" or (
                row["kind"] == "manual" and c["status"] == "paused"
            )
            if not allowed or (c["end_at"] and now >= c["end_at"]):
                raise ValueError("Campaign stopped before delivery.")
            if str(channel_id) not in c["channels"]:
                raise ValueError("Channel is no longer allowlisted.")
            snapshot = db.execute(
                "SELECT claim_minutes FROM event_drop_snapshots WHERE drop_id=?",
                (drop_id,),
            ).fetchone()
            expires = (
                row["expires_at"]
                if row["expires_at"] is not None
                else min(
                    now + snapshot["claim_minutes"] * 60, c["end_at"] or float("inf")
                )
            )
            db.execute(
                "UPDATE event_drops SET channel_id=?,expires_at=? WHERE id=?",
                (str(channel_id), expires, drop_id),
            )
            return expires

    def sent(self, drop_id, message_id, posted_at=None):
        now = time.time() if posted_at is None else posted_at
        with self.connect(True) as db:
            d = db.execute(
                "SELECT * FROM event_drops WHERE id=?", (drop_id,)
            ).fetchone()
            if d is None or d["status"] != "sending":
                return
            c = self._campaign(db, d["campaign_id"])
            state = (
                "expired"
                if c["status"] == "completed" or d["expires_at"] <= time.time()
                else "active"
            )
            db.execute(
                "UPDATE event_drops SET message_id=?,posted_at=?,status=?,error=NULL WHERE id=?",
                (str(message_id), now, state, drop_id),
            )
            db.execute(
                "UPDATE event_drop_campaigns SET last_drop_at=MAX(COALESCE(last_drop_at,0),?),warning=NULL WHERE id=?",
                (now, c["id"]),
            )
            if d["kind"] == "automatic":
                db.execute(
                    "UPDATE event_drop_campaigns SET last_scheduled_at=MAX(COALESCE(last_scheduled_at,0),?) WHERE id=?",
                    (d["scheduled_at"], c["id"]),
                )
                newer = db.execute(
                    "SELECT 1 FROM event_drops WHERE campaign_id=? AND kind='automatic' AND scheduled_at>?",
                    (c["id"], d["scheduled_at"]),
                ).fetchone()
                # Fresh delivery determines the interval. Historical receipt
                # recovery must not replace a schedule advanced during downtime.
                if c["status"] == "active" and now >= time.time() - 90 and not newer:
                    db.execute(
                        "UPDATE event_drop_campaigns SET next_drop_at=? WHERE id=?",
                        (self.next_time(c, now), c["id"]),
                    )

    def claim(
        self,
        drop_id,
        user_id,
        guild_id,
        *,
        bot=False,
        role_ids=(),
        display_name="",
        now=None,
    ):
        with self.connect(True) as db:
            # Sample time after acquiring the write lock, so lock contention cannot
            # allow a request that waited beyond expiration to sneak through.
            now = time.time() if now is None else now
            d = db.execute(
                "SELECT * FROM event_drops WHERE id=?", (drop_id,)
            ).fetchone()
            if not d:
                return {"ok": False, "message": "This drop no longer exists."}
            c = self._campaign(db, d["campaign_id"])
            if bot or str(guild_id) != c["guild_id"]:
                return {
                    "ok": False,
                    "message": "Only human members of this server can collect this drop.",
                }
            if (
                d["status"] != "active"
                or d["expires_at"] <= now
                or c["status"] == "completed"
                or (c["end_at"] and c["end_at"] <= now)
            ):
                return {"ok": False, "message": "This drop has expired."}
            roles = {str(r) for r in role_ids}
            if roles.intersection(c["excluded_roles"]) or (
                c["eligible_roles"] and not roles.intersection(c["eligible_roles"])
            ):
                return {
                    "ok": False,
                    "message": "Your roles are not eligible for this campaign.",
                }
            if db.execute(
                "SELECT 1 FROM event_drop_claims WHERE drop_id=? AND user_id=?",
                (drop_id, str(user_id)),
            ).fetchone():
                return {
                    "ok": False,
                    "message": f'You have already collected this {c["singular"]}! {c["emoji"]}',
                }
            total = db.execute(
                "SELECT COALESCE(SUM(points),0) FROM event_drop_claims WHERE campaign_id=? AND user_id=?",
                (c["id"], str(user_id)),
            ).fetchone()[0]
            if (
                c["max_user_points"] is not None
                and total + d["points"] > c["max_user_points"]
            ):
                return {
                    "ok": False,
                    "message": "You have reached the campaign limit for full claims.",
                }
            db.execute(
                "INSERT INTO event_drop_claims(campaign_id,drop_id,user_id,points,claimed_at) VALUES(?,?,?,?,?)",
                (c["id"], drop_id, str(user_id), d["points"], now),
            )
            db.execute(
                "UPDATE event_drops SET claim_count=claim_count+1 WHERE id=?",
                (drop_id,),
            )
            db.execute(
                "INSERT INTO event_drop_members VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET display_name=excluded.display_name,updated_at=excluded.updated_at",
                (str(guild_id), str(user_id), str(display_name)[:100], now),
            )
            total += d["points"]
            snapshot = dict(
                db.execute(
                    "SELECT * FROM event_drop_snapshots WHERE drop_id=?", (drop_id,)
                ).fetchone()
            )
            earned_name = (
                snapshot["singular"] if d["points"] == 1 else snapshot["plural"]
            )
            total_name = snapshot["singular"] if total == 1 else snapshot["plural"]
            confirmation = (
                f"{snapshot['emoji']} Collected {d['variant_name']}!"
                if d["variant_id"]
                else f"{snapshot['emoji']} Collected!"
            )
            confirmation += f" You earned {d['points']} {earned_name}. You now have {total} {total_name}."
            return {
                "ok": True,
                "total": total,
                "message": confirmation,
            }

    def leaderboard(self, campaign_id):
        return self.rows(
            """SELECT t.*, RANK() OVER(ORDER BY total_points DESC) AS rank,m.display_name FROM
          (SELECT user_id,SUM(points) AS total_points,COUNT(*) AS drops_claimed FROM event_drop_claims WHERE campaign_id=? GROUP BY user_id) t
          JOIN event_drop_campaigns c ON c.id=? LEFT JOIN event_drop_members m ON m.guild_id=c.guild_id AND m.user_id=t.user_id
          ORDER BY total_points DESC,t.user_id""",
            (campaign_id, campaign_id),
        )

    def campaigns(self, guild_id):
        return self.rows(
            """SELECT c.*,
         (SELECT COUNT(*) FROM event_drops d WHERE d.campaign_id=c.id AND d.posted_at IS NOT NULL) total_drops,
         (SELECT COUNT(*) FROM event_drop_claims q WHERE q.campaign_id=c.id) total_claims,
         (SELECT COUNT(DISTINCT user_id) FROM event_drop_claims q WHERE q.campaign_id=c.id) participants
         FROM event_drop_campaigns c WHERE guild_id=? ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'scheduled' THEN 1 WHEN 'paused' THEN 2 WHEN 'draft' THEN 3 ELSE 4 END,c.id DESC""",
            (str(guild_id),),
        )
