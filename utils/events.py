"""Discord Scheduled Event snapshots, dashboard ownership, and queued actions.

The dashboard only writes SQLite state.  The live bot owns every Discord API
call and reconciles the results back into this module and the canonical
reminder service.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

import aiosqlite

from utils.reminder_service import ReminderService, initialize_schema_sync
from utils.settings import settings_database_path
from utils.sqlite import configure_connection, configure_sync_connection


EVENT_TYPES = {"stage", "voice", "external"}
EVENT_ACTIONS = {"create", "edit", "cancel", "confirm_subscription"}
EVENT_ACTION_STATUSES = {"pending", "processing", "completed", "failed"}
DEFAULT_EVENT_OFFSETS = (15, 0)
CUSTOM_EVENT_OFFSETS = (360, 60, 15, 0)


EVENTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dashboard_scheduled_events (
    scheduled_event_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    entity_type TEXT NOT NULL,
    channel_id TEXT,
    location TEXT NOT NULL DEFAULT '',
    scheduled_at_utc TEXT NOT NULL,
    end_at_utc TEXT,
    event_url TEXT NOT NULL,
    image_url TEXT,
    discord_creator_id TEXT,
    discord_creator_name TEXT,
    interested_count INTEGER NOT NULL DEFAULT 0,
    recurrence_json TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    reminder_id INTEGER,
    last_sync_status TEXT NOT NULL DEFAULT 'synchronized',
    last_sync_error TEXT,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (reminder_id) REFERENCES reminder_items(id)
);
CREATE INDEX IF NOT EXISTS idx_dashboard_scheduled_events_upcoming
    ON dashboard_scheduled_events (guild_id, status, scheduled_at_utc);

CREATE TABLE IF NOT EXISTS dashboard_event_ownership (
    scheduled_event_id TEXT PRIMARY KEY,
    dashboard_user_id INTEGER,
    discord_user_id TEXT,
    organizer_name TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (scheduled_event_id) REFERENCES dashboard_scheduled_events(scheduled_event_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS dashboard_event_artwork (
    scheduled_event_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    storage_channel_id TEXT NOT NULL,
    storage_thread_id TEXT,
    storage_message_id TEXT NOT NULL,
    attachment_url TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'image/webp',
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (scheduled_event_id) REFERENCES dashboard_scheduled_events(scheduled_event_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS event_dashboard_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL CHECK (action IN ('create', 'edit', 'cancel', 'confirm_subscription')),
    scheduled_event_id TEXT,
    guild_id TEXT NOT NULL,
    requested_by_dashboard_user_id INTEGER,
    requested_by_discord_user_id TEXT,
    requested_by_name TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL DEFAULT '{}',
    image_bytes BLOB,
    image_content_type TEXT,
    storage_channel_id TEXT,
    storage_thread_id TEXT,
    storage_message_id TEXT,
    storage_attachment_url TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    requested_at_utc TEXT NOT NULL,
    processed_at_utc TEXT,
    result_event_id TEXT,
    result_message TEXT,
    failure_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_dashboard_actions_pending
    ON event_dashboard_actions (status, id);

CREATE TABLE IF NOT EXISTS dashboard_event_sync_status (
    guild_id TEXT PRIMARY KEY,
    last_attempt_at_utc TEXT,
    last_success_at_utc TEXT,
    event_count INTEGER NOT NULL DEFAULT 0,
    can_create_events INTEGER NOT NULL DEFAULT 0,
    can_manage_events INTEGER NOT NULL DEFAULT 0,
    eligible_channel_count INTEGER NOT NULL DEFAULT 0,
    storage_channel_ready INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


def utc_text(value: Optional[datetime] = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    path = settings_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return configure_sync_connection(sqlite3.connect(path, timeout=30))


def initialize_events_schema() -> None:
    with _connect() as connection:
        initialize_schema_sync(connection)
        connection.executescript(EVENTS_SCHEMA_SQL)
        _ensure_sync_column(connection, "event_dashboard_actions", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_sync_column(connection, "event_dashboard_actions", "storage_channel_id", "TEXT")
        _ensure_sync_column(connection, "event_dashboard_actions", "storage_thread_id", "TEXT")
        _ensure_sync_column(connection, "event_dashboard_actions", "storage_message_id", "TEXT")
        _ensure_sync_column(connection, "event_dashboard_actions", "storage_attachment_url", "TEXT")
        _ensure_sync_column(connection, "dashboard_scheduled_events", "last_sync_status", "TEXT NOT NULL DEFAULT 'synchronized'")
        _ensure_sync_column(connection, "dashboard_scheduled_events", "last_sync_error", "TEXT")
        _ensure_sync_column(connection, "dashboard_event_sync_status", "can_create_events", "INTEGER NOT NULL DEFAULT 0")
        _ensure_sync_column(connection, "dashboard_event_sync_status", "can_manage_events", "INTEGER NOT NULL DEFAULT 0")
        _ensure_sync_column(connection, "dashboard_event_sync_status", "eligible_channel_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_sync_column(connection, "dashboard_event_sync_status", "storage_channel_ready", "INTEGER NOT NULL DEFAULT 0")
        connection.commit()


async def initialize_events_schema_async(database: Any) -> None:
    await database.executescript(EVENTS_SCHEMA_SQL)
    cursor = await database.execute("PRAGMA table_info(event_dashboard_actions)")
    columns = {str(row[1]) for row in await cursor.fetchall()}
    await cursor.close()
    if "attempt_count" not in columns:
        await database.execute(
            "ALTER TABLE event_dashboard_actions ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
        )
    for column in ("storage_channel_id", "storage_thread_id", "storage_message_id", "storage_attachment_url"):
        if column not in columns:
            await database.execute(
                f'ALTER TABLE event_dashboard_actions ADD COLUMN "{column}" TEXT'
            )
    cursor = await database.execute("PRAGMA table_info(dashboard_event_sync_status)")
    sync_columns = {str(row[1]) for row in await cursor.fetchall()}
    await cursor.close()
    for column in ("can_create_events", "can_manage_events", "eligible_channel_count", "storage_channel_ready"):
        if column not in sync_columns:
            await database.execute(
                f'ALTER TABLE dashboard_event_sync_status ADD COLUMN "{column}" INTEGER NOT NULL DEFAULT 0'
            )
    cursor = await database.execute("PRAGMA table_info(dashboard_scheduled_events)")
    event_columns = {str(row[1]) for row in await cursor.fetchall()}
    await cursor.close()
    if "last_sync_status" not in event_columns:
        await database.execute(
            "ALTER TABLE dashboard_scheduled_events ADD COLUMN last_sync_status TEXT NOT NULL DEFAULT 'synchronized'"
        )
    if "last_sync_error" not in event_columns:
        await database.execute("ALTER TABLE dashboard_scheduled_events ADD COLUMN last_sync_error TEXT")
    await database.commit()


def _ensure_sync_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if column not in columns:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')


def _decode_json(value: Any, fallback: Any) -> Any:
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return decoded


def list_events(
    guild_id: int | str,
    *,
    user_id: int | str | None = None,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    initialize_events_schema()
    status_clause = "" if include_inactive else "AND e.status IN ('scheduled', 'active')"
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT e.*, o.dashboard_user_id, o.discord_user_id AS organizer_discord_user_id,
                   o.organizer_name,
                   a.storage_channel_id AS artwork_storage_channel_id,
                   a.storage_thread_id AS artwork_storage_thread_id,
                   a.storage_message_id AS artwork_storage_message_id,
                   a.attachment_url AS stored_image_url,
                   s.id AS subscription_id,
                   CASE WHEN s.status = 'active' THEN 1 ELSE 0 END AS subscribed,
                   s.custom_offsets_json,
                   r.default_offsets_json, r.status AS reminder_status
            FROM dashboard_scheduled_events e
            LEFT JOIN dashboard_event_ownership o
              ON o.scheduled_event_id = e.scheduled_event_id
            LEFT JOIN dashboard_event_artwork a
              ON a.scheduled_event_id = e.scheduled_event_id
            LEFT JOIN reminder_items r ON r.id = e.reminder_id
            LEFT JOIN reminder_subscriptions s
              ON s.reminder_id = e.reminder_id AND s.user_id = ?
            WHERE e.guild_id = ? {status_clause}
            ORDER BY e.scheduled_at_utc, lower(e.name)
            """,
            (str(user_id or ""), str(guild_id)),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["discord_cover_url"] = item.get("image_url")
        item["image_url"] = item.get("stored_image_url") or item.get("image_url")
        item["subscribed"] = bool(item.get("subscribed"))
        item["recurrence"] = _decode_json(item.get("recurrence_json"), None)
        item["is_recurring"] = item.get("recurrence_json") is not None
        item["custom_offsets"] = _decode_json(item.get("custom_offsets_json"), None)
        item["default_offsets"] = _decode_json(item.get("default_offsets_json"), list(DEFAULT_EVENT_OFFSETS))
        result.append(item)
    return result


def get_event(
    guild_id: int | str,
    scheduled_event_id: int | str,
    *,
    user_id: int | str | None = None,
) -> Optional[dict[str, Any]]:
    return next(
        (
            item
            for item in list_events(guild_id, user_id=user_id, include_inactive=True)
            if str(item["scheduled_event_id"]) == str(scheduled_event_id)
        ),
        None,
    )


def event_is_owned_by(event: dict[str, Any], dashboard_user_id: Any, discord_user_id: Any) -> bool:
    return bool(
        (dashboard_user_id and str(event.get("dashboard_user_id") or "") == str(dashboard_user_id))
        or (
            discord_user_id
            and str(event.get("organizer_discord_user_id") or "") == str(discord_user_id)
        )
        or (
            discord_user_id
            and str(event.get("discord_creator_id") or "") == str(discord_user_id)
        )
    )


def queue_event_action(
    *,
    action: str,
    guild_id: int | str,
    scheduled_event_id: int | str | None,
    requested_by_dashboard_user_id: int | None,
    requested_by_discord_user_id: int | str | None,
    requested_by_name: str,
    payload: dict[str, Any],
    image_bytes: bytes | None = None,
    image_content_type: str | None = None,
    idempotency_key: str | None = None,
) -> int:
    initialize_events_schema()
    normalized = str(action).strip().casefold()
    if normalized not in EVENT_ACTIONS:
        raise ValueError("Unsupported event action.")
    key = str(idempotency_key or uuid.uuid4().hex)
    now = utc_text()
    with _connect() as connection:
        existing = connection.execute(
            "SELECT id FROM event_dashboard_actions WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        cursor = connection.execute(
            """
            INSERT INTO event_dashboard_actions (
                action, scheduled_event_id, guild_id,
                requested_by_dashboard_user_id, requested_by_discord_user_id,
                requested_by_name, idempotency_key, payload_json,
                image_bytes, image_content_type, status, requested_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                normalized,
                str(scheduled_event_id) if scheduled_event_id is not None else None,
                str(guild_id),
                requested_by_dashboard_user_id,
                str(requested_by_discord_user_id) if requested_by_discord_user_id else None,
                str(requested_by_name or "")[:120],
                key,
                json.dumps(payload, separators=(",", ":"), default=str),
                image_bytes,
                image_content_type,
                now,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def get_event_action(action_id: int) -> Optional[dict[str, Any]]:
    initialize_events_schema()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT id, action, scheduled_event_id, guild_id,
                   requested_by_dashboard_user_id, requested_by_discord_user_id,
                   status, attempt_count, requested_at_utc,
                   processed_at_utc, result_event_id, result_message, failure_reason
            FROM event_dashboard_actions WHERE id = ?
            """,
            (int(action_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def get_event_artwork(scheduled_event_id: int | str) -> Optional[dict[str, Any]]:
    initialize_events_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM dashboard_event_artwork WHERE scheduled_event_id = ?",
            (str(scheduled_event_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def save_event_artwork(
    *,
    scheduled_event_id: int | str,
    guild_id: int | str,
    storage_channel_id: int | str,
    storage_thread_id: int | str | None,
    storage_message_id: int | str,
    attachment_url: str,
    content_type: str = "image/webp",
) -> None:
    initialize_events_schema()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO dashboard_event_artwork (
                scheduled_event_id, guild_id, storage_channel_id,
                storage_thread_id, storage_message_id, attachment_url,
                content_type, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (scheduled_event_id) DO UPDATE SET
                guild_id = excluded.guild_id,
                storage_channel_id = excluded.storage_channel_id,
                storage_thread_id = excluded.storage_thread_id,
                storage_message_id = excluded.storage_message_id,
                attachment_url = excluded.attachment_url,
                content_type = excluded.content_type,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                str(scheduled_event_id), str(guild_id), str(storage_channel_id),
                str(storage_thread_id) if storage_thread_id else None,
                str(storage_message_id), str(attachment_url), str(content_type), utc_text(),
            ),
        )
        connection.commit()


def record_event_action_storage(
    action_id: int,
    *,
    storage_channel_id: int | str,
    storage_thread_id: int | str | None,
    storage_message_id: int | str,
    attachment_url: str,
) -> None:
    """Persist a Discord upload receipt before the event API write is attempted."""
    initialize_events_schema()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE event_dashboard_actions
            SET storage_channel_id = ?, storage_thread_id = ?,
                storage_message_id = ?, storage_attachment_url = ?
            WHERE id = ?
            """,
            (
                str(storage_channel_id),
                str(storage_thread_id) if storage_thread_id else None,
                str(storage_message_id), str(attachment_url), int(action_id),
            ),
        )
        connection.commit()


def list_recent_actions(
    *, guild_id: int | str, dashboard_user_id: int | None = None, all_users: bool = False,
    limit: int = 30,
) -> list[dict[str, Any]]:
    initialize_events_schema()
    filters = ["guild_id = ?"]
    parameters: list[Any] = [str(guild_id)]
    if not all_users:
        filters.append("requested_by_dashboard_user_id = ?")
        parameters.append(int(dashboard_user_id or 0))
    parameters.append(max(1, min(100, int(limit))))
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT id, action, scheduled_event_id, requested_by_name, status,
                   requested_at_utc, processed_at_utc, result_event_id,
                   result_message, failure_reason
            FROM event_dashboard_actions
            WHERE {' AND '.join(filters)}
            ORDER BY id DESC LIMIT ?
            """,
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


def event_sync_status(guild_id: int | str) -> dict[str, Any]:
    initialize_events_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM dashboard_event_sync_status WHERE guild_id = ?",
            (str(guild_id),),
        ).fetchone()
        pending = connection.execute(
            "SELECT COUNT(*) FROM event_dashboard_actions WHERE guild_id = ? AND status IN ('pending', 'processing')",
            (str(guild_id),),
        ).fetchone()[0]
        failed = connection.execute(
            "SELECT COUNT(*) FROM event_dashboard_actions WHERE guild_id = ? AND status = 'failed'",
            (str(guild_id),),
        ).fetchone()[0]
    result = dict(row) if row is not None else {"guild_id": str(guild_id)}
    result.update({"pending_actions": int(pending), "failed_actions": int(failed)})
    return result


async def _with_reminder_service(callback):
    database = await aiosqlite.connect(settings_database_path())
    database.row_factory = aiosqlite.Row
    try:
        await configure_connection(database, foreign_keys=True)
        service = ReminderService(database)
        await service.initialize()
        return await callback(service)
    finally:
        await database.close()


async def subscribe_to_event(
    *, reminder_id: int, user_id: int, offsets: Sequence[int] = DEFAULT_EVENT_OFFSETS,
) -> tuple[dict[str, Any], bool]:
    return await _with_reminder_service(
        lambda service: service.subscribe(int(reminder_id), int(user_id), offsets=offsets)
    )


async def update_event_subscription(
    *, subscription_id: int, user_id: int, offsets: Sequence[int],
) -> dict[str, Any]:
    return await _with_reminder_service(
        lambda service: service.update_subscription_offsets(
            int(subscription_id), int(user_id), tuple(offsets)
        )
    )


async def unsubscribe_from_event(*, subscription_id: int, user_id: int) -> bool:
    return await _with_reminder_service(
        lambda service: service.unsubscribe(int(subscription_id), int(user_id))
    )


def parse_offsets(values: Iterable[Any]) -> tuple[int, ...]:
    parsed = tuple(sorted({int(value) for value in values}, reverse=True))
    if not parsed or any(value not in CUSTOM_EVENT_OFFSETS for value in parsed):
        raise ValueError("Choose one or more supported reminder timings.")
    return parsed
