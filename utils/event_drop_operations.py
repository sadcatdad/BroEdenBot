"""Immutable staff operations and campaign scores, separate from drop claims."""

import time

from utils.event_drop_rewards import roll_reward


def migrate_operations(db):
    db.execute("""CREATE TABLE IF NOT EXISTS event_drop_operations (
        id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES event_drop_campaigns(id),
        action TEXT NOT NULL CHECK(action IN ('now','give','remove')),
        actor_id TEXT, user_id TEXT, points INTEGER NOT NULL DEFAULT 0,
        variant_id INTEGER REFERENCES event_drop_variants(id), variant_name TEXT NOT NULL DEFAULT '',
        rarity TEXT NOT NULL DEFAULT '', drop_id INTEGER REFERENCES event_drops(id),
        before_total INTEGER, after_total INTEGER, reason TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL, request_key TEXT NOT NULL UNIQUE, created_at REAL NOT NULL,
        CHECK((action='now' AND points=0 AND drop_id IS NOT NULL AND user_id IS NULL)
           OR (action='give' AND points>=0 AND user_id IS NOT NULL)
           OR (action='remove' AND points<0 AND user_id IS NOT NULL))
    )""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS event_drop_operations_campaign ON event_drop_operations(campaign_id,user_id,id)"
    )
    db.execute("INSERT OR IGNORE INTO event_drop_schema VALUES(5,unixepoch())")


SCORES_SQL = """WITH entries AS (
    SELECT user_id,points,1 AS claim FROM event_drop_claims WHERE campaign_id=?
    UNION ALL SELECT user_id,points,0 FROM event_drop_operations
    WHERE campaign_id=? AND action IN ('give','remove')
), totals AS (
    SELECT user_id,SUM(points) AS total_points,SUM(claim) AS drops_claimed
    FROM entries GROUP BY user_id
)
SELECT t.*, CASE WHEN total_points>0 THEN RANK() OVER(ORDER BY total_points DESC) END AS rank,
m.display_name FROM totals t JOIN event_drop_campaigns c ON c.id=?
LEFT JOIN event_drop_members m ON m.guild_id=c.guild_id AND m.user_id=t.user_id
ORDER BY total_points DESC,t.user_id"""


class OperationsMixin:
    @staticmethod
    def _score_total(db, campaign_id, user_id):
        return db.execute(
            """SELECT
            (SELECT COALESCE(SUM(points),0) FROM event_drop_claims WHERE campaign_id=? AND user_id=?) +
            (SELECT COALESCE(SUM(points),0) FROM event_drop_operations WHERE campaign_id=? AND user_id=? AND action IN ('give','remove'))""",
            (campaign_id, str(user_id), campaign_id, str(user_id)),
        ).fetchone()[0]

    def participant_scores(self, campaign_id):
        return self.rows(SCORES_SQL, (campaign_id, campaign_id, campaign_id))

    def operations(self, campaign_id, user_id=None, limit=50, offset=0):
        where = " AND o.user_id=?" if user_id is not None else ""
        args = (campaign_id, str(user_id)) if user_id is not None else (campaign_id,)
        return self.rows(
            """SELECT o.*,d.status AS drop_status,d.variant_name AS drop_variant_name,
            d.points AS drop_points,d.channel_id,d.error AS drop_error,m.display_name
            FROM event_drop_operations o JOIN event_drop_campaigns c ON c.id=o.campaign_id
            LEFT JOIN event_drops d ON d.id=o.drop_id
            LEFT JOIN event_drop_members m ON m.guild_id=c.guild_id AND m.user_id=o.user_id
            WHERE o.campaign_id=?""" + where + " ORDER BY o.id DESC LIMIT ? OFFSET ?",
            args + (limit, offset),
        )

    def adjust_points(
        self,
        campaign_id,
        guild_id,
        key,
        actor_id,
        user_id,
        action,
        points=None,
        variant_id=None,
        reason="",
        display_name="",
    ):
        if action not in ("give", "remove"):
            raise ValueError("Choose give or remove.")
        if (points is None) == (variant_id is None) or (
            action == "remove" and variant_id is not None
        ):
            raise ValueError(
                "Choose exactly one of points or drop; removals require points."
            )
        if points is not None and (
            type(points) is not int or not 1 <= points <= 1000000
        ):
            raise ValueError("Points must be a whole number between 1 and 1,000,000.")
        if len(reason) > 500:
            raise ValueError("Reasons must be 500 characters or fewer.")
        now = time.time()
        with self.connect(True) as db:
            c = self._campaign(db, campaign_id, guild_id)
            existing = db.execute(
                "SELECT * FROM event_drop_operations WHERE request_key=?", (key,)
            ).fetchone()
            if existing:
                if (
                    existing["campaign_id"],
                    existing["actor_id"],
                    existing["user_id"],
                    existing["action"],
                ) != (campaign_id, str(actor_id), str(user_id), action):
                    raise ValueError(
                        "This request was already used for another operation."
                    )
                return dict(existing, duplicate=True)
            if (
                c["status"] not in ("active", "paused")
                or (c["start_at"] and now < c["start_at"])
                or (c["end_at"] and now >= c["end_at"])
            ):
                raise ValueError(
                    "Choose a currently running campaign (active or paused)."
                )
            variant = None
            if variant_id is not None:
                if not c["variants_enabled"]:
                    raise ValueError("Drop Variants are disabled for this campaign.")
                variant = next(
                    (
                        v
                        for v in self._variants(db, campaign_id)
                        if v["id"] == int(variant_id) and v["enabled"]
                    ),
                    None,
                )
                if variant is None:
                    raise ValueError("Choose an enabled variant from this campaign.")
                points = roll_reward(variant)
            delta = points if action == "give" else -points
            before = self._score_total(db, campaign_id, user_id)
            after = before + delta
            if after < 0:
                raise ValueError(
                    f"Cannot remove {points} points; this member has {before}."
                )
            if (
                action == "give"
                and c["max_user_points"] is not None
                and after > c["max_user_points"]
            ):
                raise ValueError(
                    "This award would exceed the member’s campaign points limit."
                )
            cursor = db.execute(
                """INSERT INTO event_drop_operations
                (campaign_id,action,actor_id,user_id,points,variant_id,variant_name,rarity,before_total,after_total,reason,source,request_key,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    campaign_id,
                    action,
                    str(actor_id),
                    str(user_id),
                    delta,
                    variant["id"] if variant else None,
                    variant["name"] if variant else "",
                    variant["rarity"] if variant else "",
                    before,
                    after,
                    reason,
                    "discord",
                    key,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO event_drop_members VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET display_name=CASE WHEN excluded.display_name='' THEN event_drop_members.display_name ELSE excluded.display_name END,updated_at=excluded.updated_at",
                (str(guild_id), str(user_id), str(display_name)[:100], now),
            )
            return dict(
                db.execute(
                    "SELECT * FROM event_drop_operations WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone(),
                duplicate=False,
            )
