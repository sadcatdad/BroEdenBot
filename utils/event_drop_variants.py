"""Drop Variant selection and appearance resolution, used by EventDrops.

Rarity is metadata only. Selection uses positive relative weights and never
consults channels, schedules, or rarity names.
"""

from __future__ import annotations

import math
import random

APPEARANCE_FIELDS = (
    "emoji",
    "title",
    "description",
    "color",
    "thumbnail",
    "button_label",
    "button_emoji",
    "button_style",
)
VARIANT_DEFAULTS = dict(
    name="",
    rarity="",
    weight=1.0,
    points=1,
    enabled=True,
    is_default=False,
    image_mode="inherit",
    sort_order=0,
    show_reward=True,
    show_rarity=False,
    **{field + "_override": None for field in APPEARANCE_FIELDS},
)
SNAPSHOT_FIELDS = (
    *APPEARANCE_FIELDS,
    "singular",
    "plural",
    "claim_minutes",
    "show_reward",
    "show_rarity",
)


def valid_weight(value, enabled=True):
    try:
        weight = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Selection weight must be a finite number.") from None
    if not math.isfinite(weight) or weight < 0 or weight > 1_000_000_000:
        raise ValueError(
            "Selection weight must be finite and between 0 and 1,000,000,000."
        )
    if enabled and weight <= 0:
        raise ValueError(
            "Every enabled Drop Variant must have a selection weight greater than 0."
        )
    return weight


def select_drop_variant(variants, forced_id=None):
    eligible = [v for v in variants if v["enabled"] and not v.get("deleted_at")]
    for v in eligible:
        valid_weight(v["weight"])
    if not eligible:
        raise ValueError(
            "At least one enabled Drop Variant must have a selection weight greater than 0."
        )
    if forced_id is not None:
        selected = next((v for v in eligible if str(v["id"]) == str(forced_id)), None)
        if selected is None:
            raise ValueError("Choose an enabled Drop Variant from this campaign.")
        return selected
    total = math.fsum(float(v["weight"]) for v in eligible)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Total variant weight must be a finite positive number.")
    draw = random.random() * total
    for variant in eligible:
        draw -= float(variant["weight"])
        if draw < 0:
            return variant
    return eligible[-1]  # Floating-point rounding at the upper boundary.


def resolve_appearance(campaign, variant=None):
    result = {
        field: campaign[field]
        for field in (*APPEARANCE_FIELDS, "singular", "plural", "claim_minutes")
    }
    result.update(show_reward=0, show_rarity=0)
    if variant is not None:
        for field in APPEARANCE_FIELDS:
            override = variant.get(field + "_override")
            if override is not None:
                result[field] = override
        result.update(
            show_reward=int(variant["show_reward"]),
            show_rarity=int(variant["show_rarity"]),
        )
    return result


VARIANTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_drop_variants(
 id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES event_drop_campaigns(id) ON DELETE CASCADE,
 name TEXT NOT NULL, rarity TEXT NOT NULL DEFAULT '', weight REAL NOT NULL CHECK(weight>=0),
 points INTEGER NOT NULL CHECK(points>0), enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
 emoji_override TEXT, title_override TEXT, description_override TEXT, color_override TEXT, thumbnail_override TEXT,
 button_label_override TEXT, button_emoji_override TEXT, button_style_override TEXT,
 image_mode TEXT NOT NULL DEFAULT 'inherit' CHECK(image_mode IN ('inherit','single','pool','none')),
 sort_order INTEGER NOT NULL DEFAULT 0, show_reward INTEGER NOT NULL DEFAULT 1, show_rarity INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, deleted_at REAL,
 CHECK(enabled=0 OR weight>0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_drop_variant_default ON event_drop_variants(campaign_id) WHERE is_default=1 AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_event_drop_variants_campaign ON event_drop_variants(campaign_id,enabled,sort_order);
CREATE TABLE IF NOT EXISTS event_drop_variant_assets(
 variant_id INTEGER NOT NULL REFERENCES event_drop_variants(id) ON DELETE CASCADE,
 asset_id INTEGER NOT NULL REFERENCES event_drop_assets(id), PRIMARY KEY(variant_id,asset_id)
);
CREATE TABLE IF NOT EXISTS event_drop_snapshots(
 drop_id INTEGER PRIMARY KEY REFERENCES event_drops(id),
 emoji TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, color TEXT NOT NULL, thumbnail TEXT NOT NULL,
 button_label TEXT NOT NULL, button_emoji TEXT NOT NULL, button_style TEXT NOT NULL,
 singular TEXT NOT NULL, plural TEXT NOT NULL, claim_minutes INTEGER NOT NULL,
 show_reward INTEGER NOT NULL DEFAULT 0, show_rarity INTEGER NOT NULL DEFAULT 0
);
"""


def migrate_variants(db):
    """Version 2: additive transaction, preserving V1 drops/claims and assets."""
    if db.execute("SELECT MAX(version) FROM event_drop_schema").fetchone()[0] >= 2:
        return
    # Execute individual statements so executescript cannot commit the caller's
    # transaction before the ALTER/backfill statements have finished.
    for statement in VARIANTS_SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    additions = {
        "event_drop_campaigns": {"variants_enabled": "INTEGER NOT NULL DEFAULT 0"},
        "event_drop_assets": {"campaign_pool": "INTEGER NOT NULL DEFAULT 1"},
        "event_drops": {
            "variant_id": "INTEGER REFERENCES event_drop_variants(id)",
            "variant_name": "TEXT NOT NULL DEFAULT 'Standard Drop'",
            "rarity": "TEXT NOT NULL DEFAULT ''",
            "variant_selection": "TEXT NOT NULL DEFAULT 'standard'",
        },
    }
    for table, columns in additions.items():
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_drops_variant ON event_drops(campaign_id,variant_id)"
    )
    fields = ",".join(SNAPSHOT_FIELDS)
    selected = ",".join("c." + field for field in SNAPSHOT_FIELDS[:-2])
    # V1 never stored historical appearance. Preserve its best-known campaign
    # appearance and original drop asset/points; do not invent old variant data.
    db.execute(
        f"INSERT INTO event_drop_snapshots(drop_id,{fields}) SELECT d.id,{selected},0,0 FROM event_drops d JOIN event_drop_campaigns c ON c.id=d.campaign_id"
    )
    db.execute("INSERT INTO event_drop_schema VALUES(2,unixepoch())")
