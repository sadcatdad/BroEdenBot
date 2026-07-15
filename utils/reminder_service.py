"""Canonical persistence and scheduling service for Bro Eden reminders.

Discord callbacks, the background task, the migration CLI, and the dashboard
all use this module.  The legacy reminder tables remain intact so a database
backup can be restored without reverse-migrating rows.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


logger = logging.getLogger(__name__)

DEFAULT_EVENT_OFFSETS = (15, 0)
MAX_TIMING_OFFSETS = 5
MAX_OFFSET_MINUTES = 60 * 24 * 30
MAX_GENERATED_OCCURRENCES = 60
DEFAULT_DELIVERY_GRACE_MINUTES = 120
DEFAULT_DELIVERY_LEASE_MINUTES = 10
MAX_DELIVERY_ATTEMPTS = 4

REMINDER_TYPES = {"personal", "event"}
REMINDER_STATUSES = {"upcoming", "completed", "cancelled", "deleted", "failed"}
SUBSCRIPTION_STATUSES = {
    "active",
    "cancelled",
    "completed",
    "delivery_unavailable",
}
RECURRENCE_TYPES = {"none", "daily", "weekly", "monthly", "interval"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reminder_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_source TEXT,
    legacy_id TEXT,
    reminder_type TEXT NOT NULL CHECK (reminder_type IN ('personal', 'event')),
    guild_id TEXT NOT NULL,
    creator_user_id TEXT NOT NULL,
    host_user_id TEXT,
    target_user_id TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    destination_channel_id TEXT,
    destination_channel_name TEXT,
    public_channel_id TEXT,
    public_message_id TEXT,
    scheduled_at_utc TEXT NOT NULL,
    interpretation_timezone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'upcoming'
        CHECK (status IN ('upcoming', 'completed', 'cancelled', 'deleted', 'failed')),
    default_offsets_json TEXT NOT NULL DEFAULT '[0]',
    allow_custom_timing INTEGER NOT NULL DEFAULT 1,
    close_subscriptions_at_start INTEGER NOT NULL DEFAULT 1,
    keep_public_card INTEGER NOT NULL DEFAULT 1,
    auto_subscribe_creator INTEGER NOT NULL DEFAULT 0,
    recurrence_type TEXT NOT NULL DEFAULT 'none'
        CHECK (recurrence_type IN ('none', 'daily', 'weekly', 'monthly', 'interval')),
    recurrence_interval INTEGER NOT NULL DEFAULT 1,
    recurrence_end_count INTEGER,
    recurrence_end_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    cancelled_at_utc TEXT,
    cancelled_by_user_id TEXT,
    cancellation_reason TEXT,
    failure_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (legacy_source, legacy_id)
);
CREATE INDEX IF NOT EXISTS idx_reminder_items_guild_status_time
    ON reminder_items (guild_id, status, scheduled_at_utc);
CREATE INDEX IF NOT EXISTS idx_reminder_items_creator
    ON reminder_items (creator_user_id, status);

CREATE TABLE IF NOT EXISTS reminder_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL,
    occurrence_index INTEGER NOT NULL,
    scheduled_at_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'upcoming'
        CHECK (status IN ('upcoming', 'completed', 'cancelled', 'failed')),
    created_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    cancelled_at_utc TEXT,
    failure_reason TEXT,
    FOREIGN KEY (reminder_id) REFERENCES reminder_items(id),
    UNIQUE (reminder_id, occurrence_index),
    UNIQUE (reminder_id, scheduled_at_utc)
);
CREATE INDEX IF NOT EXISTS idx_reminder_occurrences_due
    ON reminder_occurrences (status, scheduled_at_utc);

CREATE TABLE IF NOT EXISTS reminder_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    delivery_mode TEXT NOT NULL DEFAULT 'dm' CHECK (delivery_mode IN ('dm')),
    custom_offsets_json TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'cancelled', 'completed', 'delivery_unavailable')),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    cancelled_at_utc TEXT,
    failure_reason TEXT,
    legacy_subscriber_id TEXT,
    FOREIGN KEY (reminder_id) REFERENCES reminder_items(id),
    UNIQUE (reminder_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_reminder_subscriptions_user_status
    ON reminder_subscriptions (user_id, status);
CREATE INDEX IF NOT EXISTS idx_reminder_subscriptions_reminder_status
    ON reminder_subscriptions (reminder_id, status);

CREATE TABLE IF NOT EXISTS reminder_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurrence_id INTEGER NOT NULL,
    subscription_id INTEGER,
    recipient_user_id TEXT NOT NULL,
    delivery_mode TEXT NOT NULL CHECK (delivery_mode IN ('dm', 'channel')),
    destination_channel_id TEXT,
    trigger_key TEXT NOT NULL,
    offset_minutes INTEGER NOT NULL,
    due_at_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'claimed', 'retry', 'sent', 'failed',
            'permanent_failure', 'stale', 'cancelled'
        )),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_utc TEXT,
    claimed_at_utc TEXT,
    lease_expires_at_utc TEXT,
    sent_at_utc TEXT,
    failed_at_utc TEXT,
    error_category TEXT,
    error_detail TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (occurrence_id) REFERENCES reminder_occurrences(id),
    FOREIGN KEY (subscription_id) REFERENCES reminder_subscriptions(id),
    UNIQUE (occurrence_id, recipient_user_id, trigger_key)
);
CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_claim
    ON reminder_deliveries (status, due_at_utc, next_attempt_at_utc);
CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_occurrence
    ON reminder_deliveries (occurrence_id, status);

CREATE TABLE IF NOT EXISTS reminder_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER,
    occurrence_id INTEGER,
    subscription_id INTEGER,
    delivery_id INTEGER,
    guild_id TEXT,
    actor_user_id TEXT,
    action TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminder_audit_reminder
    ON reminder_audit (reminder_id, created_at_utc DESC);

CREATE TABLE IF NOT EXISTS reminder_migrations (
    migration_key TEXT PRIMARY KEY,
    applied_at_utc TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_dashboard_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL,
    guild_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('edit', 'duplicate', 'cancel', 'retry', 'archive')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    requested_at_utc TEXT NOT NULL,
    processed_at_utc TEXT,
    failure_reason TEXT,
    FOREIGN KEY (reminder_id) REFERENCES reminder_items(id)
);
CREATE INDEX IF NOT EXISTS idx_reminder_dashboard_actions_status
    ON reminder_dashboard_actions (status, requested_at_utc);
"""


@dataclass(frozen=True)
class MigrationReport:
    personal_migrated: int = 0
    events_migrated: int = 0
    subscriptions_migrated: int = 0
    malformed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "personal_migrated": self.personal_migrated,
            "events_migrated": self.events_migrated,
            "subscriptions_migrated": self.subscriptions_migrated,
            "malformed": self.malformed,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: Optional[datetime] = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


def parse_utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def env_bool(name: str, default: bool) -> bool:
    try:
        from utils.settings import get_setting

        raw = get_setting(name) or os.getenv(name)
    except (ImportError, sqlite3.Error, OSError):
        raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def delivery_grace_minutes() -> int:
    try:
        from utils.settings import get_setting

        raw = get_setting("REMINDER_DELIVERY_GRACE_MINUTES") or os.getenv(
            "REMINDER_DELIVERY_GRACE_MINUTES",
            str(DEFAULT_DELIVERY_GRACE_MINUTES),
        )
        return max(1, min(24 * 60, int(raw)))
    except ValueError:
        return DEFAULT_DELIVERY_GRACE_MINUTES
    except (ImportError, sqlite3.Error, OSError):
        return DEFAULT_DELIVERY_GRACE_MINUTES


def normalize_title(value: str) -> str:
    title = re.sub(r"\s+", " ", str(value or "").strip())
    title = re.sub(r"^#{1,6}\s*", "", title)
    if not title:
        raise ValueError("Reminder title cannot be blank.")
    if len(title) > 100:
        raise ValueError("Reminder title must be 100 characters or fewer.")
    return title


def sanitize_text(value: str, *, limit: int = 4000) -> str:
    text = str(value or "").strip()
    text = re.sub(
        r"@(everyone|here)\b",
        lambda match: "@\u200b" + match.group(1),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"<@&\d+>", "[role mention]", text)
    return text[:limit]


def timing_label(offset_minutes: int) -> str:
    if offset_minutes == 0:
        return "When the event begins"
    if offset_minutes % 1440 == 0:
        days = offset_minutes // 1440
        return f"{days} day{'s' if days != 1 else ''} before"
    if offset_minutes % 60 == 0:
        hours = offset_minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''} before"
    return f"{offset_minutes} minute{'s' if offset_minutes != 1 else ''} before"


def timing_summary(offsets: Sequence[int]) -> str:
    return "\n".join(f"• {timing_label(value)}" for value in normalize_offsets(offsets))


def normalize_offsets(offsets: Iterable[int]) -> tuple[int, ...]:
    normalized: set[int] = set()
    for value in offsets:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError("Reminder timing offsets must be whole minutes.")
        if parsed < 0 or parsed > MAX_OFFSET_MINUTES:
            raise ValueError("Reminder timing must be between event start and 30 days before.")
        normalized.add(parsed)
    if not normalized:
        raise ValueError("Choose at least one reminder timing.")
    if len(normalized) > MAX_TIMING_OFFSETS:
        raise ValueError(f"Choose no more than {MAX_TIMING_OFFSETS} reminder timings.")
    return tuple(sorted(normalized, reverse=True))


def parse_offsets(value: Any, default: Sequence[int] = DEFAULT_EVENT_OFFSETS) -> tuple[int, ...]:
    if value is None or value == "":
        return normalize_offsets(default)
    if isinstance(value, (list, tuple, set)):
        return normalize_offsets(value)
    text = str(value).strip()
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Reminder timing data is invalid.") from exc
        return normalize_offsets(decoded)
    aliases = {
        "start": 0,
        "now": 0,
        "15m": 15,
        "1h": 60,
        "1d": 1440,
    }
    parsed_values: list[int] = []
    for token in re.split(r"[,\s]+", text.casefold()):
        if not token:
            continue
        if token in aliases:
            parsed_values.append(aliases[token])
            continue
        match = re.fullmatch(r"(\d+)\s*(m|min|h|hr|d|day)?", token)
        if match is None:
            raise ValueError(
                "Use timings such as `start`, `15m`, `1h`, or `1d`, separated by commas."
            )
        amount = int(match.group(1))
        unit = match.group(2) or "m"
        multiplier = 1440 if unit in {"d", "day"} else 60 if unit in {"h", "hr"} else 1
        parsed_values.append(amount * multiplier)
    return normalize_offsets(parsed_values)


def offsets_json(offsets: Iterable[int]) -> str:
    return json.dumps(list(normalize_offsets(offsets)), separators=(",", ":"))


def split_legacy_message(message: Any) -> tuple[str, str]:
    text = sanitize_text(str(message or ""))
    lines = text.splitlines()
    first = next((line.strip() for line in lines if line.strip()), "Untitled reminder")
    title = re.sub(r"^#{1,6}\s*", "", first)[:100] or "Untitled reminder"
    remaining = list(lines)
    try:
        remaining.remove(next(line for line in lines if line.strip()))
    except StopIteration:
        pass
    return title, "\n".join(remaining).strip()


def recurrence_dates(
    first: datetime,
    recurrence_type: str,
    *,
    interval: int = 1,
    count: Optional[int] = None,
    end_at: Optional[datetime] = None,
) -> list[datetime]:
    recurrence_type = str(recurrence_type or "none").casefold()
    if recurrence_type not in RECURRENCE_TYPES:
        raise ValueError("Recurrence must be none, daily, weekly, monthly, or interval.")
    interval = max(1, min(365, int(interval or 1)))
    if recurrence_type == "none":
        return [first.astimezone(timezone.utc)]
    limit = max(1, min(MAX_GENERATED_OCCURRENCES, int(count or MAX_GENERATED_OCCURRENCES)))
    source_timezone = first.tzinfo or timezone.utc
    end_value = end_at.astimezone(source_timezone) if end_at else None
    results: list[datetime] = []
    current = first.astimezone(source_timezone)
    base_day = current.day
    while len(results) < limit and (end_value is None or current <= end_value):
        results.append(current.astimezone(timezone.utc))
        if recurrence_type == "daily":
            current += timedelta(days=interval)
        elif recurrence_type == "weekly":
            current += timedelta(weeks=interval)
        elif recurrence_type == "interval":
            current += timedelta(days=interval)
        else:
            month_index = current.year * 12 + current.month - 1 + interval
            year, zero_month = divmod(month_index, 12)
            month = zero_month + 1
            day = min(base_day, calendar.monthrange(year, month)[1])
            current = current.replace(year=year, month=month, day=day)
    if not results:
        raise ValueError("The recurrence rule has no future occurrence.")
    return results


def _dict_row(cursor: Any, row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(zip([item[0] for item in cursor.description or ()], row))


def _table_exists_sync(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def initialize_schema_sync(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(reminder_items)").fetchall()
    }
    if "host_user_id" not in columns:
        connection.execute("ALTER TABLE reminder_items ADD COLUMN host_user_id TEXT")
    connection.commit()


class ReminderService:
    """Async canonical reminder service backed by the bot's aiosqlite handle."""

    def __init__(self, database: Any) -> None:
        self.db = database

    async def fetch_one(self, sql: str, parameters: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        cursor = await self.db.execute(sql, tuple(parameters))
        row = await cursor.fetchone()
        result = _dict_row(cursor, row)
        await cursor.close()
        return result

    async def fetch_all(self, sql: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        cursor = await self.db.execute(sql, tuple(parameters))
        rows = await cursor.fetchall()
        result = [_dict_row(cursor, row) or {} for row in rows]
        await cursor.close()
        return result

    async def initialize(self) -> MigrationReport:
        await self.db.executescript(SCHEMA_SQL)
        cursor = await self.db.execute("PRAGMA table_info(reminder_items)")
        columns = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        if "host_user_id" not in columns:
            await self.db.execute("ALTER TABLE reminder_items ADD COLUMN host_user_id TEXT")
        await self.db.commit()
        report = await self.migrate_legacy()
        await self.reconcile_deliveries()
        return report

    async def audit(
        self,
        action: str,
        *,
        reminder_id: Optional[int] = None,
        occurrence_id: Optional[int] = None,
        subscription_id: Optional[int] = None,
        delivery_id: Optional[int] = None,
        guild_id: Any = None,
        actor_user_id: Any = None,
        metadata: Optional[dict[str, Any]] = None,
        commit: bool = True,
    ) -> None:
        safe_metadata = json.dumps(metadata or {}, separators=(",", ":"), default=str)[:4000]
        await self.db.execute(
            """
            INSERT INTO reminder_audit (
                reminder_id, occurrence_id, subscription_id, delivery_id,
                guild_id, actor_user_id, action, metadata_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reminder_id,
                occurrence_id,
                subscription_id,
                delivery_id,
                str(guild_id) if guild_id is not None else None,
                str(actor_user_id) if actor_user_id is not None else None,
                action,
                safe_metadata,
                utc_text(),
            ),
        )
        if commit:
            await self.db.commit()

    async def create_reminder(
        self,
        *,
        reminder_type: str,
        guild_id: int,
        creator_user_id: int,
        host_user_id: Optional[int] = None,
        title: str,
        description: str,
        scheduled_at_utc: datetime,
        interpretation_timezone: str,
        destination_channel_id: Optional[int] = None,
        destination_channel_name: str = "",
        target_user_id: Optional[int] = None,
        public_channel_id: Optional[int] = None,
        default_offsets: Sequence[int] = (0,),
        allow_custom_timing: bool = True,
        close_subscriptions_at_start: bool = True,
        keep_public_card: bool = True,
        auto_subscribe_creator: bool = False,
        recurrence_type: str = "none",
        recurrence_interval: int = 1,
        recurrence_end_count: Optional[int] = None,
        recurrence_end_at_utc: Optional[datetime] = None,
    ) -> dict[str, Any]:
        if reminder_type not in REMINDER_TYPES:
            raise ValueError("Reminder type must be personal or event.")
        scheduled_at_utc = scheduled_at_utc.astimezone(timezone.utc)
        if scheduled_at_utc <= utc_now():
            raise ValueError("Reminder date/time must be in the future.")
        clean_title = normalize_title(title)
        clean_description = sanitize_text(description)
        offsets = normalize_offsets(default_offsets)
        try:
            recurrence_base = scheduled_at_utc.astimezone(ZoneInfo(interpretation_timezone))
        except (ValueError, ZoneInfoNotFoundError):
            recurrence_base = scheduled_at_utc
        dates = recurrence_dates(
            recurrence_base,
            recurrence_type,
            interval=recurrence_interval,
            count=recurrence_end_count,
            end_at=recurrence_end_at_utc,
        )
        now = utc_text()
        cursor = await self.db.execute(
            """
            INSERT INTO reminder_items (
                reminder_type, guild_id, creator_user_id, host_user_id, target_user_id,
                title, description, destination_channel_id, destination_channel_name,
                public_channel_id, scheduled_at_utc, interpretation_timezone,
                default_offsets_json, allow_custom_timing,
                close_subscriptions_at_start, keep_public_card,
                auto_subscribe_creator, recurrence_type, recurrence_interval,
                recurrence_end_count, recurrence_end_at_utc,
                created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reminder_type,
                str(guild_id),
                str(creator_user_id),
                str(host_user_id or creator_user_id) if reminder_type == "event" else None,
                str(target_user_id or creator_user_id) if reminder_type == "personal" else None,
                clean_title,
                clean_description,
                str(destination_channel_id) if destination_channel_id else None,
                sanitize_text(destination_channel_name, limit=100),
                str(public_channel_id) if public_channel_id else None,
                scheduled_at_utc.isoformat(),
                interpretation_timezone,
                offsets_json(offsets),
                int(allow_custom_timing),
                int(close_subscriptions_at_start),
                int(keep_public_card),
                int(auto_subscribe_creator),
                recurrence_type,
                max(1, int(recurrence_interval)),
                recurrence_end_count,
                recurrence_end_at_utc.astimezone(timezone.utc).isoformat()
                if recurrence_end_at_utc else None,
                now,
                now,
            ),
        )
        reminder_id = int(cursor.lastrowid)
        await cursor.close()
        for index, occurrence_time in enumerate(dates):
            await self.db.execute(
                """
                INSERT INTO reminder_occurrences (
                    reminder_id, occurrence_index, scheduled_at_utc, status, created_at_utc
                ) VALUES (?, ?, ?, 'upcoming', ?)
                """,
                (reminder_id, index, occurrence_time.isoformat(), now),
            )
        await self.audit(
            "created",
            reminder_id=reminder_id,
            guild_id=guild_id,
            actor_user_id=creator_user_id,
            metadata={"type": reminder_type, "occurrences": len(dates)},
            commit=False,
        )
        await self.db.commit()
        if reminder_type == "personal":
            await self.ensure_personal_deliveries(reminder_id)
        elif auto_subscribe_creator:
            await self.subscribe(reminder_id, creator_user_id)
        row = await self.get_reminder(reminder_id)
        if row is None:
            raise RuntimeError("Reminder creation did not return a row.")
        return row

    async def get_reminder(self, reminder_id: int) -> Optional[dict[str, Any]]:
        return await self.fetch_one(
            """
            SELECT r.*,
                   (SELECT COUNT(*) FROM reminder_subscriptions s
                    WHERE s.reminder_id = r.id AND s.status = 'active') AS subscriber_count,
                   (SELECT COUNT(*) FROM reminder_occurrences o
                    WHERE o.reminder_id = r.id) AS occurrence_count
            FROM reminder_items r
            WHERE r.id = ?
            """,
            (reminder_id,),
        )

    async def list_reminders(
        self,
        guild_id: int,
        user_id: int,
        *,
        staff: bool = False,
        status: str = "upcoming",
        reminder_type: str = "all",
        recurrence: str = "all",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        filters = ["r.guild_id = ?"]
        parameters: list[Any] = [str(guild_id)]
        if status and status != "all":
            filters.append("r.status = ?")
            parameters.append(status)
        if reminder_type in REMINDER_TYPES:
            filters.append("r.reminder_type = ?")
            parameters.append(reminder_type)
        if recurrence == "recurring":
            filters.append("r.recurrence_type != 'none'")
        elif recurrence == "one_time":
            filters.append("r.recurrence_type = 'none'")
        if not staff:
            filters.append("r.creator_user_id = ?")
            parameters.append(str(user_id))
        parameters.append(max(1, min(100, limit)))
        return await self.fetch_all(
            f"""
            SELECT r.*,
                   (SELECT COUNT(*) FROM reminder_subscriptions s
                    WHERE s.reminder_id = r.id AND s.status = 'active') AS subscriber_count,
                   (SELECT COUNT(*) FROM reminder_deliveries d
                    JOIN reminder_occurrences o ON o.id = d.occurrence_id
                    WHERE o.reminder_id = r.id AND d.status = 'failed') AS failed_deliveries
            FROM reminder_items r
            WHERE {' AND '.join(filters)}
            ORDER BY CASE r.status WHEN 'upcoming' THEN 0 ELSE 1 END,
                     r.scheduled_at_utc ASC
            LIMIT ?
            """,
            parameters,
        )

    async def set_public_message(
        self,
        reminder_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        await self.db.execute(
            """
            UPDATE reminder_items
            SET public_channel_id = ?, public_message_id = ?, updated_at_utc = ?, version = version + 1
            WHERE id = ? AND reminder_type = 'event'
            """,
            (str(channel_id), str(message_id), utc_text(), reminder_id),
        )
        await self.db.commit()

    async def ensure_personal_deliveries(self, reminder_id: int) -> int:
        row = await self.get_reminder(reminder_id)
        if row is None or row["reminder_type"] != "personal":
            return 0
        occurrences = await self.fetch_all(
            "SELECT * FROM reminder_occurrences WHERE reminder_id = ? AND status = 'upcoming'",
            (reminder_id,),
        )
        inserted = 0
        for occurrence in occurrences:
            delivery_mode = "channel" if row.get("destination_channel_id") else "dm"
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO reminder_deliveries (
                    occurrence_id, recipient_user_id, delivery_mode,
                    destination_channel_id, trigger_key, offset_minutes,
                    due_at_utc, status, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, 'start', 0, ?, 'pending', ?, ?)
                """,
                (
                    occurrence["id"],
                    row["target_user_id"] or row["creator_user_id"],
                    delivery_mode,
                    row.get("destination_channel_id"),
                    occurrence["scheduled_at_utc"],
                    utc_text(),
                    utc_text(),
                ),
            )
            inserted += max(0, int(cursor.rowcount or 0))
            await cursor.close()
        await self.db.commit()
        return inserted

    async def subscribe(
        self,
        reminder_id: int,
        user_id: int,
        *,
        offsets: Optional[Sequence[int]] = None,
    ) -> tuple[dict[str, Any], bool]:
        reminder = await self.get_reminder(reminder_id)
        if reminder is None or reminder["reminder_type"] != "event":
            raise ValueError("This event no longer exists.")
        if reminder["status"] != "upcoming":
            raise ValueError("This event is no longer accepting subscriptions.")
        if (
            reminder["close_subscriptions_at_start"]
            and parse_utc(reminder["scheduled_at_utc"]) <= utc_now()
        ):
            raise ValueError("Subscriptions for this event are closed.")
        chosen = normalize_offsets(offsets) if offsets is not None else None
        if chosen is not None and not reminder["allow_custom_timing"]:
            raise ValueError("This event uses the organizer's reminder timings.")
        now = utc_text()
        cursor = await self.db.execute(
            """
            INSERT OR IGNORE INTO reminder_subscriptions (
                reminder_id, user_id, custom_offsets_json, status,
                created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (reminder_id, str(user_id), offsets_json(chosen) if chosen else None, now, now),
        )
        created = bool(cursor.rowcount)
        await cursor.close()
        if not created:
            existing = await self.fetch_one(
                "SELECT * FROM reminder_subscriptions WHERE reminder_id = ? AND user_id = ?",
                (reminder_id, str(user_id)),
            )
            if existing is not None and existing["status"] != "active":
                cursor = await self.db.execute(
                    """
                    UPDATE reminder_subscriptions
                    SET custom_offsets_json = CASE WHEN ? IS NULL
                            THEN custom_offsets_json ELSE ? END,
                        status = 'active', updated_at_utc = ?, cancelled_at_utc = NULL,
                        failure_reason = NULL
                    WHERE id = ? AND status != 'active'
                    """,
                    (
                        offsets_json(chosen) if chosen else None,
                        offsets_json(chosen) if chosen else None,
                        now,
                        existing["id"],
                    ),
                )
                created = bool(cursor.rowcount)
                await cursor.close()
        await self.db.commit()
        subscription = await self.fetch_one(
            "SELECT * FROM reminder_subscriptions WHERE reminder_id = ? AND user_id = ?",
            (reminder_id, str(user_id)),
        )
        if subscription is None:
            raise RuntimeError("Subscription creation did not return a row.")
        await self.rebuild_subscription_deliveries(int(subscription["id"]))
        await self.audit(
            "subscribed" if created else "subscription_reused",
            reminder_id=reminder_id,
            subscription_id=int(subscription["id"]),
            guild_id=reminder["guild_id"],
            actor_user_id=user_id,
        )
        return subscription, created

    async def subscription_offsets(self, subscription: dict[str, Any], reminder: dict[str, Any]) -> tuple[int, ...]:
        raw = subscription.get("custom_offsets_json") or reminder.get("default_offsets_json")
        return parse_offsets(raw, DEFAULT_EVENT_OFFSETS)

    async def rebuild_subscription_deliveries(self, subscription_id: int) -> int:
        row = await self.fetch_one(
            """
            SELECT s.*, r.default_offsets_json, r.status AS reminder_status
            FROM reminder_subscriptions s
            JOIN reminder_items r ON r.id = s.reminder_id
            WHERE s.id = ?
            """,
            (subscription_id,),
        )
        if row is None:
            return 0
        await self.db.execute(
            """
            UPDATE reminder_deliveries
            SET status = 'cancelled', updated_at_utc = ?
            WHERE subscription_id = ? AND status IN ('pending', 'retry')
            """,
            (utc_text(), subscription_id),
        )
        if row["status"] != "active" or row["reminder_status"] != "upcoming":
            await self.db.commit()
            return 0
        offsets = parse_offsets(row.get("custom_offsets_json") or row["default_offsets_json"])
        occurrences = await self.fetch_all(
            """
            SELECT * FROM reminder_occurrences
            WHERE reminder_id = ? AND status = 'upcoming'
            ORDER BY scheduled_at_utc
            """,
            (row["reminder_id"],),
        )
        inserted = 0
        now = utc_now()
        for occurrence in occurrences:
            occurrence_time = parse_utc(occurrence["scheduled_at_utc"])
            for offset in offsets:
                due = occurrence_time - timedelta(minutes=offset)
                if due <= now:
                    continue
                trigger_key = "start" if offset == 0 else f"before:{offset}"
                cursor = await self.db.execute(
                    """
                    INSERT INTO reminder_deliveries (
                        occurrence_id, subscription_id, recipient_user_id,
                        delivery_mode, trigger_key, offset_minutes, due_at_utc,
                        status, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, 'dm', ?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT (occurrence_id, recipient_user_id, trigger_key) DO UPDATE SET
                        subscription_id = excluded.subscription_id,
                        due_at_utc = excluded.due_at_utc,
                        status = CASE
                            WHEN reminder_deliveries.status IN ('sent', 'claimed')
                            THEN reminder_deliveries.status ELSE 'pending' END,
                        updated_at_utc = excluded.updated_at_utc
                    """,
                    (
                        occurrence["id"],
                        subscription_id,
                        row["user_id"],
                        trigger_key,
                        offset,
                        due.isoformat(),
                        utc_text(),
                        utc_text(),
                    ),
                )
                inserted += max(0, int(cursor.rowcount or 0))
                await cursor.close()
        await self.db.commit()
        return inserted

    async def update_subscription_offsets(
        self,
        subscription_id: int,
        user_id: int,
        offsets: Optional[Sequence[int]],
    ) -> dict[str, Any]:
        subscription = await self.fetch_one(
            """
            SELECT s.*, r.allow_custom_timing, r.guild_id, r.default_offsets_json
            FROM reminder_subscriptions s
            JOIN reminder_items r ON r.id = s.reminder_id
            WHERE s.id = ? AND s.user_id = ? AND s.status = 'active'
            """,
            (subscription_id, str(user_id)),
        )
        if subscription is None:
            raise ValueError("That active subscription was not found.")
        if offsets is not None and not subscription["allow_custom_timing"]:
            raise ValueError("This event does not allow custom reminder timing.")
        await self.db.execute(
            """
            UPDATE reminder_subscriptions
            SET custom_offsets_json = ?, updated_at_utc = ?
            WHERE id = ? AND user_id = ?
            """,
            (offsets_json(offsets) if offsets is not None else None, utc_text(), subscription_id, str(user_id)),
        )
        await self.db.commit()
        await self.rebuild_subscription_deliveries(subscription_id)
        await self.audit(
            "subscription_timing_changed" if offsets is not None else "subscription_defaults_restored",
            reminder_id=int(subscription["reminder_id"]),
            subscription_id=subscription_id,
            guild_id=subscription["guild_id"],
            actor_user_id=user_id,
            metadata={"offsets": list(offsets) if offsets is not None else None},
        )
        updated = await self.fetch_one("SELECT * FROM reminder_subscriptions WHERE id = ?", (subscription_id,))
        if updated is None:
            raise RuntimeError("Subscription update did not return a row.")
        return updated

    async def unsubscribe(self, subscription_id: int, user_id: int) -> bool:
        row = await self.fetch_one(
            """
            SELECT s.*, r.guild_id FROM reminder_subscriptions s
            JOIN reminder_items r ON r.id = s.reminder_id
            WHERE s.id = ? AND s.user_id = ?
            """,
            (subscription_id, str(user_id)),
        )
        if row is None:
            return False
        cursor = await self.db.execute(
            """
            UPDATE reminder_subscriptions
            SET status = 'cancelled', cancelled_at_utc = ?, updated_at_utc = ?
            WHERE id = ? AND user_id = ? AND status = 'active'
            """,
            (utc_text(), utc_text(), subscription_id, str(user_id)),
        )
        changed = bool(cursor.rowcount)
        await cursor.close()
        if changed:
            await self.db.execute(
                """
                UPDATE reminder_deliveries SET status = 'cancelled', updated_at_utc = ?
                WHERE subscription_id = ? AND status IN ('pending', 'retry', 'claimed')
                """,
                (utc_text(), subscription_id),
            )
            await self.audit(
                "unsubscribed",
                reminder_id=int(row["reminder_id"]),
                subscription_id=subscription_id,
                guild_id=row["guild_id"],
                actor_user_id=user_id,
                commit=False,
            )
        await self.db.commit()
        return changed

    async def list_subscriptions(self, guild_id: int, user_id: int, *, active_only: bool = True) -> list[dict[str, Any]]:
        status_sql = "AND s.status = 'active' AND r.status = 'upcoming'" if active_only else ""
        return await self.fetch_all(
            f"""
            SELECT s.*, r.title, r.description, r.scheduled_at_utc,
                   r.destination_channel_id, r.destination_channel_name,
                   r.default_offsets_json, r.allow_custom_timing,
                   r.status AS event_status, r.guild_id, r.creator_user_id,
                   r.host_user_id
            FROM reminder_subscriptions s
            JOIN reminder_items r ON r.id = s.reminder_id
            WHERE r.guild_id = ? AND s.user_id = ? {status_sql}
            ORDER BY r.scheduled_at_utc ASC LIMIT 25
            """,
            (str(guild_id), str(user_id)),
        )

    async def cancel_reminder(
        self,
        reminder_id: int,
        actor_user_id: int,
        *,
        reason: str = "",
        staff: bool = False,
    ) -> Optional[dict[str, Any]]:
        row = await self.get_reminder(reminder_id)
        if row is None or row["status"] != "upcoming":
            return None
        if not staff and str(row["creator_user_id"]) != str(actor_user_id):
            raise PermissionError("You may only cancel reminders you created.")
        now = utc_text()
        await self.db.execute(
            """
            UPDATE reminder_items
            SET status = 'cancelled', cancelled_at_utc = ?, cancelled_by_user_id = ?,
                cancellation_reason = ?, updated_at_utc = ?, version = version + 1
            WHERE id = ? AND status = 'upcoming'
            """,
            (now, str(actor_user_id), sanitize_text(reason, limit=500), now, reminder_id),
        )
        await self.db.execute(
            """
            UPDATE reminder_occurrences SET status = 'cancelled', cancelled_at_utc = ?
            WHERE reminder_id = ? AND status = 'upcoming'
            """,
            (now, reminder_id),
        )
        await self.db.execute(
            """
            UPDATE reminder_deliveries SET status = 'cancelled', updated_at_utc = ?
            WHERE occurrence_id IN (
                SELECT id FROM reminder_occurrences WHERE reminder_id = ?
            ) AND status IN ('pending', 'retry', 'claimed')
            """,
            (now, reminder_id),
        )
        await self.db.execute(
            """
            UPDATE reminder_subscriptions
            SET status = 'cancelled', cancelled_at_utc = ?, updated_at_utc = ?
            WHERE reminder_id = ? AND status IN ('active', 'delivery_unavailable')
            """,
            (now, now, reminder_id),
        )
        await self.audit(
            "cancelled",
            reminder_id=reminder_id,
            guild_id=row["guild_id"],
            actor_user_id=actor_user_id,
            metadata={"reason": sanitize_text(reason, limit=500)},
            commit=False,
        )
        await self.db.commit()
        return await self.get_reminder(reminder_id)

    async def update_reminder(
        self,
        reminder_id: int,
        actor_user_id: int,
        *,
        staff: bool = False,
        title: Optional[str] = None,
        description: Optional[str] = None,
        scheduled_at_utc: Optional[datetime] = None,
        destination_channel_id: Optional[int] = None,
        destination_channel_name: Optional[str] = None,
        clear_destination: bool = False,
        default_offsets: Optional[Sequence[int]] = None,
    ) -> tuple[dict[str, Any], dict[str, tuple[Any, Any]]]:
        row = await self.get_reminder(reminder_id)
        if row is None or row["status"] != "upcoming":
            raise ValueError("That upcoming reminder was not found.")
        if not staff and str(row["creator_user_id"]) != str(actor_user_id):
            raise PermissionError("You may only edit reminders you created.")
        values: dict[str, Any] = {}
        changes: dict[str, tuple[Any, Any]] = {}
        if title is not None:
            value = normalize_title(title)
            if value != row["title"]:
                values["title"] = value
                changes["title"] = (row["title"], value)
        if description is not None:
            value = sanitize_text(description)
            if value != row["description"]:
                values["description"] = value
                changes["description"] = (row["description"], value)
        if clear_destination:
            if row.get("destination_channel_id"):
                values["destination_channel_id"] = None
                values["destination_channel_name"] = ""
                changes["destination"] = (row.get("destination_channel_id"), None)
        elif destination_channel_id is not None:
            value = str(destination_channel_id)
            if value != str(row.get("destination_channel_id") or ""):
                values["destination_channel_id"] = value
                values["destination_channel_name"] = sanitize_text(destination_channel_name or "", limit=100)
                changes["destination"] = (row.get("destination_channel_id"), value)
        if default_offsets is not None:
            value = offsets_json(default_offsets)
            if value != row["default_offsets_json"]:
                values["default_offsets_json"] = value
                changes["timings"] = (row["default_offsets_json"], value)
        if scheduled_at_utc is not None:
            value_dt = scheduled_at_utc.astimezone(timezone.utc)
            if value_dt <= utc_now():
                raise ValueError("Reminder date/time must be in the future.")
            value = value_dt.isoformat()
            if value != row["scheduled_at_utc"]:
                if row["recurrence_type"] != "none":
                    raise ValueError("Use occurrence controls to reschedule a recurring series.")
                values["scheduled_at_utc"] = value
                changes["scheduled_at_utc"] = (row["scheduled_at_utc"], value)
                await self.db.execute(
                    """
                    UPDATE reminder_occurrences SET scheduled_at_utc = ?
                    WHERE reminder_id = ? AND occurrence_index = 0 AND status = 'upcoming'
                    """,
                    (value, reminder_id),
                )
        if not values:
            return row, changes
        values["updated_at_utc"] = utc_text()
        assignments = ", ".join(f"{key} = ?" for key in values)
        await self.db.execute(
            f"UPDATE reminder_items SET {assignments}, version = version + 1 WHERE id = ?",
            (*values.values(), reminder_id),
        )
        await self.audit(
            "edited",
            reminder_id=reminder_id,
            guild_id=row["guild_id"],
            actor_user_id=actor_user_id,
            metadata={"fields": sorted(changes)},
            commit=False,
        )
        await self.db.commit()
        if "scheduled_at_utc" in changes or "timings" in changes:
            subscriptions = await self.fetch_all(
                "SELECT id FROM reminder_subscriptions WHERE reminder_id = ? AND status = 'active'",
                (reminder_id,),
            )
            for subscription in subscriptions:
                await self.rebuild_subscription_deliveries(int(subscription["id"]))
            if row["reminder_type"] == "personal":
                await self.db.execute(
                    """
                    UPDATE reminder_deliveries SET due_at_utc = (
                        SELECT scheduled_at_utc FROM reminder_occurrences
                        WHERE reminder_occurrences.id = reminder_deliveries.occurrence_id
                    ), updated_at_utc = ?
                    WHERE occurrence_id IN (SELECT id FROM reminder_occurrences WHERE reminder_id = ?)
                      AND status IN ('pending', 'retry')
                    """,
                    (utc_text(), reminder_id),
                )
                await self.db.commit()
        updated = await self.get_reminder(reminder_id)
        if updated is None:
            raise RuntimeError("Reminder update did not return a row.")
        return updated, changes

    async def duplicate_reminder(self, reminder_id: int, actor_user_id: int, *, staff: bool = False) -> dict[str, Any]:
        row = await self.get_reminder(reminder_id)
        if row is None:
            raise ValueError("That reminder was not found.")
        if not staff and str(row["creator_user_id"]) != str(actor_user_id):
            raise PermissionError("You may only duplicate reminders you created.")
        first_time = max(utc_now() + timedelta(minutes=5), parse_utc(row["scheduled_at_utc"]))
        return await self.create_reminder(
            reminder_type=row["reminder_type"],
            guild_id=int(row["guild_id"]),
            creator_user_id=actor_user_id,
            host_user_id=int(row["host_user_id"]) if row.get("host_user_id") else None,
            target_user_id=int(row["target_user_id"]) if row.get("target_user_id") else None,
            title=f"{row['title']} (copy)"[:100],
            description=row["description"],
            scheduled_at_utc=first_time,
            interpretation_timezone=row["interpretation_timezone"],
            destination_channel_id=int(row["destination_channel_id"]) if row.get("destination_channel_id") else None,
            destination_channel_name=row.get("destination_channel_name") or "",
            public_channel_id=int(row["public_channel_id"]) if row.get("public_channel_id") else None,
            default_offsets=parse_offsets(row["default_offsets_json"]),
            allow_custom_timing=bool(row["allow_custom_timing"]),
            close_subscriptions_at_start=bool(row["close_subscriptions_at_start"]),
            keep_public_card=bool(row["keep_public_card"]),
            recurrence_type=row["recurrence_type"],
            recurrence_interval=int(row["recurrence_interval"] or 1),
            recurrence_end_count=int(row["recurrence_end_count"])
            if row.get("recurrence_end_count") else None,
            recurrence_end_at_utc=parse_utc(row["recurrence_end_at_utc"])
            if row.get("recurrence_end_at_utc") else None,
        )

    async def reschedule_occurrence(
        self,
        occurrence_id: int,
        actor_user_id: int,
        scheduled_at_utc: datetime,
        *,
        scope: str = "one",
        staff: bool = False,
    ) -> dict[str, Any]:
        occurrence = await self.fetch_one(
            """
            SELECT o.*, r.creator_user_id, r.guild_id, r.reminder_type
            FROM reminder_occurrences o
            JOIN reminder_items r ON r.id = o.reminder_id
            WHERE o.id = ? AND o.status = 'upcoming' AND r.status = 'upcoming'
            """,
            (occurrence_id,),
        )
        if occurrence is None:
            raise ValueError("That upcoming occurrence was not found.")
        if not staff and str(occurrence["creator_user_id"]) != str(actor_user_id):
            raise PermissionError("You may only edit occurrences you created.")
        if scope not in {"one", "future", "all"}:
            raise ValueError("Occurrence edit scope must be one, future, or all.")
        target = scheduled_at_utc.astimezone(timezone.utc)
        if target <= utc_now():
            raise ValueError("Occurrence date/time must be in the future.")
        current = parse_utc(occurrence["scheduled_at_utc"])
        delta = target - current
        if scope == "one":
            selected = [occurrence]
        else:
            comparison = ">=" if scope == "future" else ">="
            threshold = occurrence["occurrence_index"] if scope == "future" else 0
            selected = await self.fetch_all(
                f"""
                SELECT * FROM reminder_occurrences
                WHERE reminder_id = ? AND status = 'upcoming'
                  AND occurrence_index {comparison} ?
                ORDER BY occurrence_index
                """,
                (occurrence["reminder_id"], threshold),
            )
        for selected_occurrence in selected:
            before = parse_utc(selected_occurrence["scheduled_at_utc"])
            after = target if scope == "one" else before + delta
            await self.db.execute(
                "UPDATE reminder_occurrences SET scheduled_at_utc = ? WHERE id = ? AND status = 'upcoming'",
                (after.isoformat(), selected_occurrence["id"]),
            )
            await self.db.execute(
                """
                UPDATE reminder_deliveries
                SET due_at_utc = ?, status = CASE
                    WHEN status IN ('sent', 'claimed') THEN status ELSE 'pending' END,
                    next_attempt_at_utc = NULL, updated_at_utc = ?
                WHERE occurrence_id = ?
                """,
                (after.isoformat(), utc_text(), selected_occurrence["id"]),
            )
        earliest = await self.fetch_one(
            "SELECT MIN(scheduled_at_utc) AS next_time FROM reminder_occurrences WHERE reminder_id = ? AND status = 'upcoming'",
            (occurrence["reminder_id"],),
        )
        await self.db.execute(
            "UPDATE reminder_items SET scheduled_at_utc = ?, updated_at_utc = ?, version = version + 1 WHERE id = ?",
            (earliest["next_time"], utc_text(), occurrence["reminder_id"]),
        )
        await self.audit(
            "occurrence_rescheduled",
            reminder_id=int(occurrence["reminder_id"]),
            occurrence_id=occurrence_id,
            guild_id=occurrence["guild_id"],
            actor_user_id=actor_user_id,
            metadata={"scope": scope, "before": occurrence["scheduled_at_utc"], "after": target.isoformat()},
            commit=False,
        )
        await self.db.commit()
        if occurrence["reminder_type"] == "event":
            subscriptions = await self.fetch_all(
                "SELECT id FROM reminder_subscriptions WHERE reminder_id = ? AND status = 'active'",
                (occurrence["reminder_id"],),
            )
            for subscription in subscriptions:
                await self.rebuild_subscription_deliveries(int(subscription["id"]))
        return await self.fetch_one("SELECT * FROM reminder_occurrences WHERE id = ?", (occurrence_id,)) or occurrence

    async def cancel_occurrence(
        self,
        occurrence_id: int,
        actor_user_id: int,
        *,
        staff: bool = False,
    ) -> bool:
        occurrence = await self.fetch_one(
            """
            SELECT o.*, r.creator_user_id, r.guild_id
            FROM reminder_occurrences o JOIN reminder_items r ON r.id = o.reminder_id
            WHERE o.id = ? AND o.status = 'upcoming' AND r.status = 'upcoming'
            """,
            (occurrence_id,),
        )
        if occurrence is None:
            return False
        if not staff and str(occurrence["creator_user_id"]) != str(actor_user_id):
            raise PermissionError("You may only cancel occurrences you created.")
        now = utc_text()
        await self.db.execute(
            "UPDATE reminder_occurrences SET status = 'cancelled', cancelled_at_utc = ? WHERE id = ?",
            (now, occurrence_id),
        )
        await self.db.execute(
            "UPDATE reminder_deliveries SET status = 'cancelled', updated_at_utc = ? WHERE occurrence_id = ? AND status IN ('pending', 'retry', 'claimed')",
            (now, occurrence_id),
        )
        await self.audit(
            "occurrence_cancelled",
            reminder_id=int(occurrence["reminder_id"]),
            occurrence_id=occurrence_id,
            guild_id=occurrence["guild_id"],
            actor_user_id=actor_user_id,
            commit=False,
        )
        await self.db.commit()
        await self.complete_finished_occurrences()
        return True

    async def archive_reminder(
        self,
        reminder_id: int,
        actor_user_id: int,
        *,
        staff: bool = False,
    ) -> bool:
        row = await self.get_reminder(reminder_id)
        if row is None or row["status"] == "deleted":
            return False
        if not staff and str(row["creator_user_id"]) != str(actor_user_id):
            raise PermissionError("You may only delete reminders you created.")
        if row["status"] == "upcoming":
            raise ValueError("Cancel an upcoming reminder before deleting it from normal views.")
        await self.db.execute(
            "UPDATE reminder_items SET status = 'deleted', updated_at_utc = ?, version = version + 1 WHERE id = ?",
            (utc_text(), reminder_id),
        )
        await self.audit(
            "deleted",
            reminder_id=reminder_id,
            guild_id=row["guild_id"],
            actor_user_id=actor_user_id,
            metadata={"retained_history": True},
            commit=False,
        )
        await self.db.commit()
        return True

    async def reconcile_deliveries(self) -> None:
        now = utc_now()
        lease_cutoff = now.isoformat()
        await self.db.execute(
            """
            UPDATE reminder_deliveries
            SET status = 'retry', claimed_at_utc = NULL, lease_expires_at_utc = NULL,
                next_attempt_at_utc = ?, updated_at_utc = ?
            WHERE status = 'claimed' AND lease_expires_at_utc <= ?
            """,
            (now.isoformat(), now.isoformat(), lease_cutoff),
        )
        stale_before = (now - timedelta(minutes=delivery_grace_minutes())).isoformat()
        await self.db.execute(
            """
            UPDATE reminder_deliveries
            SET status = 'stale', failed_at_utc = ?, error_category = 'missed_grace_window',
                updated_at_utc = ?
            WHERE status IN ('pending', 'retry') AND due_at_utc < ?
            """,
            (now.isoformat(), now.isoformat(), stale_before),
        )
        await self.db.commit()

    async def claim_due_deliveries(self, *, limit: int = 25, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        now = (now or utc_now()).astimezone(timezone.utc)
        await self.reconcile_deliveries()
        candidates = await self.fetch_all(
            """
            SELECT d.id
            FROM reminder_deliveries d
            JOIN reminder_occurrences o ON o.id = d.occurrence_id
            JOIN reminder_items r ON r.id = o.reminder_id
            LEFT JOIN reminder_subscriptions s ON s.id = d.subscription_id
            WHERE d.status IN ('pending', 'retry')
              AND d.due_at_utc <= ?
              AND (d.next_attempt_at_utc IS NULL OR d.next_attempt_at_utc <= ?)
              AND r.status = 'upcoming' AND o.status = 'upcoming'
              AND (d.subscription_id IS NULL OR s.status = 'active')
            ORDER BY d.due_at_utc ASC, d.id ASC LIMIT ?
            """,
            (now.isoformat(), now.isoformat(), max(1, min(100, limit))),
        )
        claimed: list[int] = []
        lease = now + timedelta(minutes=DEFAULT_DELIVERY_LEASE_MINUTES)
        for candidate in candidates:
            cursor = await self.db.execute(
                """
                UPDATE reminder_deliveries
                SET status = 'claimed', claimed_at_utc = ?, lease_expires_at_utc = ?,
                    attempt_count = attempt_count + 1, updated_at_utc = ?
                WHERE id = ? AND status IN ('pending', 'retry')
                """,
                (now.isoformat(), lease.isoformat(), now.isoformat(), candidate["id"]),
            )
            if cursor.rowcount:
                claimed.append(int(candidate["id"]))
            await cursor.close()
        for delivery_id in claimed:
            await self.db.execute(
                """
                INSERT INTO reminder_audit (
                    reminder_id, occurrence_id, subscription_id, delivery_id,
                    guild_id, action, metadata_json, created_at_utc
                )
                SELECT o.reminder_id, d.occurrence_id, d.subscription_id, d.id,
                       r.guild_id, 'delivery_claimed',
                       '{"attempt":' || d.attempt_count || '}', ?
                FROM reminder_deliveries d
                JOIN reminder_occurrences o ON o.id = d.occurrence_id
                JOIN reminder_items r ON r.id = o.reminder_id
                WHERE d.id = ?
                """,
                (now.isoformat(), delivery_id),
            )
        await self.db.commit()
        if not claimed:
            return []
        placeholders = ",".join("?" for _ in claimed)
        return await self.fetch_all(
            f"""
            SELECT d.*, o.reminder_id, o.scheduled_at_utc AS occurrence_at_utc,
                   r.reminder_type, r.guild_id, r.creator_user_id, r.host_user_id, r.title,
                   r.description, r.destination_channel_id AS reminder_channel_id,
                   r.destination_channel_name, r.public_channel_id, r.public_message_id,
                   r.status AS reminder_status, r.recurrence_type,
                   s.status AS subscription_status
            FROM reminder_deliveries d
            JOIN reminder_occurrences o ON o.id = d.occurrence_id
            JOIN reminder_items r ON r.id = o.reminder_id
            LEFT JOIN reminder_subscriptions s ON s.id = d.subscription_id
            WHERE d.id IN ({placeholders})
            ORDER BY d.due_at_utc ASC, d.id ASC
            """,
            claimed,
        )

    async def mark_delivery_sent(self, delivery_id: int) -> None:
        now = utc_text()
        await self.db.execute(
            """
            UPDATE reminder_deliveries
            SET status = 'sent', sent_at_utc = ?, claimed_at_utc = NULL,
                lease_expires_at_utc = NULL, error_category = NULL,
                error_detail = NULL, updated_at_utc = ?
            WHERE id = ? AND status = 'claimed'
            """,
            (now, now, delivery_id),
        )
        row = await self.fetch_one(
            """
            SELECT o.reminder_id, o.id AS occurrence_id, r.guild_id
            FROM reminder_deliveries d
            JOIN reminder_occurrences o ON o.id = d.occurrence_id
            JOIN reminder_items r ON r.id = o.reminder_id
            WHERE d.id = ?
            """,
            (delivery_id,),
        )
        if row:
            await self.audit(
                "delivery_sent",
                reminder_id=int(row["reminder_id"]),
                occurrence_id=int(row["occurrence_id"]),
                delivery_id=delivery_id,
                guild_id=row["guild_id"],
                commit=False,
            )
        await self.db.commit()
        await self.complete_finished_occurrences()

    async def mark_delivery_failed(
        self,
        delivery_id: int,
        category: str,
        detail: str,
        *,
        permanent: bool,
    ) -> str:
        row = await self.fetch_one(
            """
            SELECT d.*, o.reminder_id, r.guild_id FROM reminder_deliveries d
            JOIN reminder_occurrences o ON o.id = d.occurrence_id
            JOIN reminder_items r ON r.id = o.reminder_id WHERE d.id = ?
            """,
            (delivery_id,),
        )
        if row is None:
            return "missing"
        attempts = int(row["attempt_count"] or 0)
        now = utc_now()
        if permanent or attempts >= MAX_DELIVERY_ATTEMPTS:
            status = "permanent_failure" if permanent else "failed"
            next_attempt = None
        else:
            status = "retry"
            next_attempt = now + timedelta(seconds=min(900, 30 * (2 ** max(0, attempts - 1))))
        await self.db.execute(
            """
            UPDATE reminder_deliveries
            SET status = ?, next_attempt_at_utc = ?, claimed_at_utc = NULL,
                lease_expires_at_utc = NULL, failed_at_utc = ?, error_category = ?,
                error_detail = ?, updated_at_utc = ?
            WHERE id = ? AND status = 'claimed'
            """,
            (
                status,
                next_attempt.isoformat() if next_attempt else None,
                now.isoformat() if status != "retry" else None,
                sanitize_text(category, limit=100),
                sanitize_text(detail, limit=500),
                now.isoformat(),
                delivery_id,
            ),
        )
        if permanent and row.get("subscription_id"):
            await self.db.execute(
                """
                UPDATE reminder_subscriptions
                SET status = 'delivery_unavailable', failure_reason = ?, updated_at_utc = ?
                WHERE id = ? AND status = 'active'
                """,
                (category, now.isoformat(), row["subscription_id"]),
            )
            await self.db.execute(
                """
                UPDATE reminder_deliveries SET status = 'cancelled', updated_at_utc = ?
                WHERE subscription_id = ? AND status IN ('pending', 'retry')
                """,
                (now.isoformat(), row["subscription_id"]),
            )
        await self.audit(
            "delivery_failed" if status != "retry" else "delivery_retry_scheduled",
            reminder_id=int(row["reminder_id"]),
            occurrence_id=int(row["occurrence_id"]),
            subscription_id=int(row["subscription_id"]) if row.get("subscription_id") else None,
            delivery_id=delivery_id,
            guild_id=row["guild_id"],
            metadata={"category": category, "status": status, "attempt": attempts},
            commit=False,
        )
        await self.db.commit()
        await self.complete_finished_occurrences()
        return status

    async def retry_failed_delivery(
        self,
        reminder_id: int,
        delivery_id: int,
        actor_user_id: Any,
    ) -> bool:
        """Return an exhausted temporary failure to the durable delivery queue."""
        now = utc_text()
        cursor = await self.db.execute(
            """
            UPDATE reminder_deliveries
            SET status = 'pending', attempt_count = 0, next_attempt_at_utc = ?,
                claimed_at_utc = NULL, lease_expires_at_utc = NULL,
                failed_at_utc = NULL, error_category = NULL, error_detail = NULL,
                updated_at_utc = ?
            WHERE id = ? AND status = 'failed' AND occurrence_id IN (
                SELECT id FROM reminder_occurrences WHERE reminder_id = ?
            )
            """,
            (now, now, delivery_id, reminder_id),
        )
        changed = bool(cursor.rowcount)
        await cursor.close()
        if changed:
            reminder = await self.get_reminder(reminder_id)
            await self.audit(
                "delivery_retry_requested",
                reminder_id=reminder_id,
                delivery_id=delivery_id,
                guild_id=reminder.get("guild_id") if reminder else None,
                actor_user_id=actor_user_id,
                commit=False,
            )
        await self.db.commit()
        return changed

    async def complete_finished_occurrences(self) -> int:
        now = utc_text()
        cursor = await self.db.execute(
            """
            UPDATE reminder_occurrences
            SET status = 'completed', completed_at_utc = ?
            WHERE status = 'upcoming' AND scheduled_at_utc <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM reminder_deliveries d
                  WHERE d.occurrence_id = reminder_occurrences.id
                    AND d.status IN ('pending', 'claimed', 'retry')
              )
            """,
            (now, now),
        )
        changed = max(0, int(cursor.rowcount or 0))
        await cursor.close()
        await self.db.execute(
            """
            UPDATE reminder_items
            SET scheduled_at_utc = (
                    SELECT MIN(o.scheduled_at_utc) FROM reminder_occurrences o
                    WHERE o.reminder_id = reminder_items.id AND o.status = 'upcoming'
                ),
                updated_at_utc = ?, version = version + 1
            WHERE status = 'upcoming' AND EXISTS (
                SELECT 1 FROM reminder_occurrences o
                WHERE o.reminder_id = reminder_items.id AND o.status = 'upcoming'
            ) AND scheduled_at_utc != (
                SELECT MIN(o.scheduled_at_utc) FROM reminder_occurrences o
                WHERE o.reminder_id = reminder_items.id AND o.status = 'upcoming'
            )
            """,
            (now,),
        )
        await self.db.execute(
            """
            UPDATE reminder_items
            SET status = 'completed', completed_at_utc = ?, updated_at_utc = ?
            WHERE status = 'upcoming'
              AND NOT EXISTS (
                  SELECT 1 FROM reminder_occurrences o
                  WHERE o.reminder_id = reminder_items.id AND o.status = 'upcoming'
              )
            """,
            (now, now),
        )
        await self.db.execute(
            """
            UPDATE reminder_subscriptions SET status = 'completed', updated_at_utc = ?
            WHERE status = 'active' AND reminder_id IN (
                SELECT id FROM reminder_items WHERE status = 'completed'
            )
            """,
            (now,),
        )
        await self.db.commit()
        return changed

    async def migrate_legacy(self) -> MigrationReport:
        personal = events = subscriptions = malformed = 0
        personal_rows: list[dict[str, Any]] = []
        event_rows: list[dict[str, Any]] = []
        try:
            personal_rows = await self.fetch_all("SELECT * FROM reminders")
        except Exception as exc:
            if "no such table" not in str(exc).casefold():
                raise
        for legacy in personal_rows:
            try:
                scheduled = parse_utc(legacy["scheduled_at_utc"])
                title, description = split_legacy_message(legacy.get("message"))
            except (KeyError, TypeError, ValueError) as exc:
                malformed += 1
                logger.warning("Reminder migration skipped malformed personal id=%s error=%s", legacy.get("id"), type(exc).__name__)
                continue
            status_map = {"pending": "upcoming", "sent": "completed", "deleted": "deleted", "failed": "failed"}
            status = status_map.get(str(legacy.get("status")), "failed")
            now = utc_text()
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO reminder_items (
                    legacy_source, legacy_id, reminder_type, guild_id,
                    creator_user_id, target_user_id, title, description,
                    destination_channel_id, scheduled_at_utc, interpretation_timezone,
                    status, default_offsets_json, created_at_utc, updated_at_utc,
                    completed_at_utc, failure_reason
                ) VALUES ('reminders', ?, 'personal', ?, ?, ?, ?, ?, ?, ?, ?, ?, '[0]', ?, ?, ?, ?)
                """,
                (
                    str(legacy["id"]), legacy["guild_id"], legacy["creator_user_id"],
                    legacy.get("target_user_id") or legacy["creator_user_id"], title, description,
                    legacy.get("channel_id"), scheduled.isoformat(),
                    os.getenv("REMINDER_TIMEZONE", "America/Chicago"), status,
                    legacy.get("created_at_utc") or now, legacy.get("updated_at_utc") or now,
                    legacy.get("sent_at_utc") if status == "completed" else None,
                    legacy.get("failure_reason"),
                ),
            )
            if cursor.rowcount:
                personal += 1
            await cursor.close()
        try:
            event_rows = await self.fetch_all("SELECT * FROM reminder_subscription_posts")
        except Exception as exc:
            if "no such table" not in str(exc).casefold():
                raise
        for legacy in event_rows:
            try:
                scheduled = parse_utc(legacy["scheduled_at_utc"])
                title, description = split_legacy_message(legacy.get("message"))
            except (KeyError, TypeError, ValueError) as exc:
                malformed += 1
                logger.warning("Reminder migration skipped malformed event id=%s error=%s", legacy.get("id"), type(exc).__name__)
                continue
            status_map = {"open": "upcoming", "completed": "completed", "failed": "failed"}
            status = status_map.get(str(legacy.get("status")), "failed")
            now = utc_text()
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO reminder_items (
                    legacy_source, legacy_id, reminder_type, guild_id,
                    creator_user_id, host_user_id, title, description, destination_channel_id,
                    destination_channel_name, public_channel_id, public_message_id,
                    scheduled_at_utc, interpretation_timezone, status,
                    default_offsets_json, allow_custom_timing, created_at_utc,
                    updated_at_utc, completed_at_utc, failure_reason
                ) VALUES ('reminder_subscription_posts', ?, 'event', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[0]', 1, ?, ?, ?, ?)
                """,
                (
                    str(legacy["id"]), legacy["guild_id"], legacy["creator_user_id"],
                    legacy["creator_user_id"], title, description, legacy.get("destination_channel_id"),
                    legacy.get("destination_channel_name"), legacy.get("channel_id"),
                    legacy.get("message_id"), scheduled.isoformat(),
                    os.getenv("REMINDER_TIMEZONE", "America/Chicago"), status,
                    legacy.get("created_at_utc") or now, legacy.get("created_at_utc") or now,
                    legacy.get("completed_at_utc") if status == "completed" else None,
                    legacy.get("failure_reason"),
                ),
            )
            if cursor.rowcount:
                events += 1
            await cursor.close()
        await self.db.commit()
        canonical = await self.fetch_all("SELECT * FROM reminder_items WHERE legacy_source IS NOT NULL")
        for row in canonical:
            occurrence_status = "upcoming" if row["status"] == "upcoming" else (
                "completed" if row["status"] == "completed" else "cancelled" if row["status"] in {"cancelled", "deleted"} else "failed"
            )
            await self.db.execute(
                """
                INSERT OR IGNORE INTO reminder_occurrences (
                    reminder_id, occurrence_index, scheduled_at_utc, status,
                    created_at_utc, completed_at_utc, failure_reason
                ) VALUES (?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"], row["scheduled_at_utc"], occurrence_status,
                    row["created_at_utc"], row.get("completed_at_utc"), row.get("failure_reason"),
                ),
            )
            if row["reminder_type"] == "personal" and row["status"] == "upcoming":
                await self.ensure_personal_deliveries(int(row["id"]))
        try:
            legacy_subscribers = await self.fetch_all("SELECT * FROM reminder_subscribers")
        except Exception as exc:
            if "no such table" not in str(exc).casefold():
                raise
            legacy_subscribers = []
        for legacy in legacy_subscribers:
            reminder = await self.fetch_one(
                "SELECT * FROM reminder_items WHERE legacy_source = 'reminder_subscription_posts' AND legacy_id = ?",
                (str(legacy.get("post_id")),),
            )
            if reminder is None:
                malformed += 1
                continue
            status_map = {
                "subscribed": "active", "processing": "active", "sent": "completed",
                "cancelled": "cancelled", "failed": "delivery_unavailable",
            }
            status = status_map.get(str(legacy.get("status")), "delivery_unavailable")
            now = utc_text()
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO reminder_subscriptions (
                    reminder_id, user_id, status, created_at_utc, updated_at_utc,
                    cancelled_at_utc, failure_reason, legacy_subscriber_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder["id"], legacy["user_id"], status,
                    legacy.get("subscribed_at_utc") or now,
                    legacy.get("sent_at_utc") or legacy.get("cancelled_at_utc") or now,
                    legacy.get("cancelled_at_utc"), legacy.get("failure_reason"), str(legacy["id"]),
                ),
            )
            if cursor.rowcount:
                subscriptions += 1
            await cursor.close()
        await self.db.commit()
        active_subscriptions = await self.fetch_all(
            "SELECT id FROM reminder_subscriptions WHERE status = 'active'"
        )
        for row in active_subscriptions:
            await self.rebuild_subscription_deliveries(int(row["id"]))
        report = MigrationReport(personal, events, subscriptions, malformed)
        await self.db.execute(
            """
            INSERT OR REPLACE INTO reminder_migrations (migration_key, applied_at_utc, report_json)
            VALUES ('canonical-v1', ?, ?)
            """,
            (utc_text(), json.dumps(report.as_dict(), separators=(",", ":"))),
        )
        await self.db.commit()
        logger.info("Reminder canonical migration report=%s", report.as_dict())
        return report


def dashboard_overview(connection: sqlite3.Connection, *, guild_id: str = "") -> dict[str, Any]:
    """Return reminder dashboard metrics and rows from the canonical schema."""
    initialize_schema_sync(connection)
    where = "WHERE guild_id = ?" if guild_id else ""
    params: tuple[Any, ...] = (guild_id,) if guild_id else ()
    counts = {
        str(row["status"]): int(row["total"])
        for row in connection.execute(
            f"SELECT status, COUNT(*) AS total FROM reminder_items {where} GROUP BY status",
            params,
        ).fetchall()
    }
    subscriber_where = "WHERE r.guild_id = ? AND s.status = 'active'" if guild_id else "WHERE s.status = 'active'"
    failed_where = "WHERE r.guild_id = ? AND d.status IN ('failed', 'permanent_failure')" if guild_id else "WHERE d.status IN ('failed', 'permanent_failure')"
    return {
        "counts": counts,
        "active_subscriptions": int(connection.execute(
            f"SELECT COUNT(*) FROM reminder_subscriptions s JOIN reminder_items r ON r.id = s.reminder_id {subscriber_where}",
            params,
        ).fetchone()[0]),
        "failed_deliveries": int(connection.execute(
            f"SELECT COUNT(*) FROM reminder_deliveries d JOIN reminder_occurrences o ON o.id = d.occurrence_id JOIN reminder_items r ON r.id = o.reminder_id {failed_where}",
            params,
        ).fetchone()[0]),
    }
