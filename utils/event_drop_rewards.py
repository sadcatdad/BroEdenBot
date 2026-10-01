"""Reward configuration, safe text substitution, and the v4 Event Drops upgrade."""

from __future__ import annotations

import random

REWARD_DEFAULTS = dict(reward_mode="static", points_min=None, points_max=None)
MESSAGE_LIMIT = 1800  # Leave room for the separately controlled role mention.
EMPTY_MESSAGE = "Gotcha! This drop is empty. No {currency} this time."


def validate_reward(values, allow_empty=False):
    mode = values.get("reward_mode", "static")
    if mode not in (
        ("static", "random", "empty") if allow_empty else ("static", "random")
    ):
        raise ValueError("Choose a valid reward type.")
    try:
        points = int(str(values.get("points", 1)))
        low = int(
            str(
                values.get("points_min")
                if values.get("points_min") not in (None, "")
                else points
            )
        )
        high = int(
            str(
                values.get("points_max")
                if values.get("points_max") not in (None, "")
                else points
            )
        )
    except (ValueError, TypeError):
        raise ValueError("Reward values must be whole numbers.") from None
    if mode == "empty":
        points = low = high = 0
    elif mode == "random":
        if not 1 <= low <= high <= 1_000_000:
            raise ValueError(
                "Random reward must have 1 ≤ minimum ≤ maximum ≤ 1,000,000."
            )
        points = low  # Legacy readers retain a valid representative value.
    elif not (0 if allow_empty else 1) <= points <= 1_000_000:
        raise ValueError("Static reward is outside the supported range.")
    else:
        low = high = points
    return dict(points=points, reward_mode=mode, points_min=low, points_max=high)


def roll_reward(config):
    if config.get("reward_mode") == "empty":
        return 0
    if config.get("reward_mode") == "random":
        return random.randint(config["points_min"], config["points_max"])
    return config["points"]


def reward_label(config):
    if config.get("reward_mode") == "empty" or config["points"] == 0:
        return "Empty · 0 points"
    if config.get("reward_mode") == "random":
        return f"{config['points_min']}–{config['points_max']} points"
    return f"{config['points']} points"


def render_text(template, **values):
    # Substitute known placeholders in one pass; arbitrary braces/Discord markdown
    # are preserved. Replacements cannot be interpreted as new placeholders.
    import re

    return re.sub(
        r"\{(campaign|variant|points|currency|total)\}",
        lambda match: str(values.get(match[1], match[0])),
        template,
    )


def migrate_rewards(db):
    if db.execute("SELECT MAX(version) FROM event_drop_schema").fetchone()[0] >= 4:
        return
    # Caller disables FK enforcement before BEGIN; validation runs before commit.
    # Rebuild only the two CHECK(points>0) tables. Keep every column, ID, row,
    # index and FK target intact, then add defaulted columns below.
    for table in ("event_drop_variants", "event_drop_claims"):
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        indexes = [
            r[0]
            for r in db.execute(
                "SELECT sql FROM sqlite_master WHERE type IN ('index','trigger') AND tbl_name=? AND sql IS NOT NULL ORDER BY type,name",
                (table,),
            )
        ]
        temp = table + "_v4"
        db.execute(
            sql.replace(table, temp, 1).replace("CHECK(points>0)", "CHECK(points>=0)")
        )
        columns = ",".join(
            '"' + r[1] + '"' for r in db.execute(f"PRAGMA table_info({table})")
        )
        db.execute(f"INSERT INTO {temp}({columns}) SELECT {columns} FROM {table}")
        db.execute(f"DROP TABLE {table}")
        db.execute(f"ALTER TABLE {temp} RENAME TO {table}")
        for index in indexes:
            db.execute(index)
    for table, columns in {
        "event_drop_campaigns": {
            "reward_mode": "TEXT NOT NULL DEFAULT 'static'",
            "points_min": "INTEGER",
            "points_max": "INTEGER",
            "message_text": "TEXT NOT NULL DEFAULT ''",
        },
        "event_drop_variants": {
            "reward_mode": "TEXT NOT NULL DEFAULT 'static'",
            "points_min": "INTEGER",
            "points_max": "INTEGER",
            "empty_claim_message": "TEXT NOT NULL DEFAULT ''",
            "message_text_override": "TEXT",
            "drought_after": "INTEGER NOT NULL DEFAULT 0",
        },
        "event_drops": {
            "message_text": "TEXT NOT NULL DEFAULT ''",
            "empty_claim_message": "TEXT NOT NULL DEFAULT ''",
        },
    }.items():
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    for table in ("event_drop_campaigns", "event_drop_variants"):
        db.execute(f"UPDATE {table} SET points_min=points WHERE points_min IS NULL")
        db.execute(f"UPDATE {table} SET points_max=points WHERE points_max IS NULL")
    db.execute("INSERT INTO event_drop_schema VALUES(4,unixepoch())")
