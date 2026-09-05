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
                "SELECT COUNT(*) FROM event_drop_assets WHERE campaign_id=? AND created_at>0",
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
        campaign = self.campaign(campaign_id, guild_id)
        campaign.update(
            name=(campaign["name"][:93] + " (copy)"), start_at=None, end_at=None
        )
        assets = self.rows(
            "SELECT image_bytes,content_type FROM event_drop_assets WHERE campaign_id=? AND created_at>0",
            (campaign_id,),
        )
        return self.save(
            guild_id,
            actor,
            campaign,
            campaign["channels"],
            assets=[(a["image_bytes"], a["content_type"]) for a in assets],
            eligible_roles=campaign["eligible_roles"],
            excluded_roles=campaign["excluded_roles"],
        )

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

    def queue_manual(self, campaign_id, guild_id, key, channel_id=None):
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
    def _reserve(db, c, now, kind, scheduled=None, channel_id=None, key=None):
        assets = [
            r[0]
            for r in db.execute(
                "SELECT id FROM event_drop_assets WHERE campaign_id=? AND created_at>0",
                (c["id"],),
            )
        ]
        cur = db.execute(
            "INSERT INTO event_drops(campaign_id,channel_id,kind,scheduled_at,created_at,status,asset_id,points,request_key) VALUES(?,?,?,?,?,'pending',?,?,?)",
            (
                c["id"],
                channel_id,
                kind,
                scheduled,
                now,
                random.choice(assets) if assets else None,
                c["points"],
                key,
            ),
        )
        return cur.lastrowid

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
            expires = min(now + c["claim_minutes"] * 60, c["end_at"] or float("inf"))
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
            return {
                "ok": True,
                "total": total,
                "message": f'{c["emoji"]} Collected! You now have {total} {c["singular"] if total==1 else c["plural"]}.',
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
