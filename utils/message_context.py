"""Shared schema and helpers for the full-server staff message archive."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Iterable, Optional


UTC = dt.timezone.utc

MESSAGE_CONTEXT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS message_context_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_name TEXT,
    parent_channel_id TEXT,
    parent_channel_name TEXT,
    thread_id TEXT,
    thread_name TEXT,
    message_id TEXT UNIQUE NOT NULL,
    author_id TEXT NOT NULL,
    author_name TEXT,
    author_display_name TEXT,
    timestamp TEXT NOT NULL,
    edited_at TEXT,
    deleted_at TEXT,
    is_deleted INTEGER DEFAULT 0,
    is_bot INTEGER DEFAULT 0,
    is_webhook INTEGER DEFAULT 0,
    content TEXT,
    content_hash TEXT,
    attachment_count INTEGER DEFAULT 0,
    attachment_names TEXT,
    embed_count INTEGER DEFAULT 0,
    sticker_count INTEGER DEFAULT 0,
    jump_url TEXT,
    source TEXT DEFAULT 'live_discord',
    stored_at TEXT NOT NULL
)
"""

MESSAGE_CONTEXT_INDEX_SQL = tuple(
    f"CREATE INDEX IF NOT EXISTS idx_message_context_{column} "
    f"ON message_context_messages ({column})"
    for column in (
        "guild_id",
        "channel_id",
        "author_id",
        "timestamp",
        "message_id",
        "content_hash",
        "source",
        "is_deleted",
    )
) + (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_message_context_import_identity
    ON message_context_messages (
        guild_id, channel_id, author_id, timestamp, content_hash
    )
    WHERE source = 'imported_csv'
    """,
)

MESSAGE_CONTEXT_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS message_context_fts USING fts5(
    content,
    author_name,
    channel_name,
    content='message_context_messages',
    content_rowid='id'
)
"""

MESSAGE_CONTEXT_FTS_TRIGGER_SQL = (
    """
    CREATE TRIGGER IF NOT EXISTS message_context_messages_ai
    AFTER INSERT ON message_context_messages BEGIN
        INSERT INTO message_context_fts(
            rowid, content, author_name, channel_name
        ) VALUES (new.id, new.content, new.author_name, new.channel_name);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS message_context_messages_ad
    AFTER DELETE ON message_context_messages BEGIN
        INSERT INTO message_context_fts(
            message_context_fts, rowid, content, author_name, channel_name
        ) VALUES (
            'delete', old.id, old.content, old.author_name, old.channel_name
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS message_context_messages_au
    AFTER UPDATE ON message_context_messages BEGIN
        INSERT INTO message_context_fts(
            message_context_fts, rowid, content, author_name, channel_name
        ) VALUES (
            'delete', old.id, old.content, old.author_name, old.channel_name
        );
        INSERT INTO message_context_fts(
            rowid, content, author_name, channel_name
        ) VALUES (new.id, new.content, new.author_name, new.channel_name);
    END
    """,
)


def utcnow_iso() -> str:
    return dt.datetime.now(UTC).isoformat()


def parse_bool(value: object, *, default: bool = False) -> bool:
    text = str(value or "").strip().casefold()
    return default if not text else text in {"1", "true", "yes", "on"}


def parse_id_set(value: object) -> set[int]:
    return {
        int(item)
        for item in re.split(r"[\s,]+", str(value or "").strip())
        if item.isdigit() and int(item) > 0
    }


def parse_retention_days(value: object) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        days = int(text)
    except ValueError:
        return None
    return days if days > 0 else None


def has_message_context_access(
    user_id: int,
    role_ids: Iterable[int],
    allowed_role_ids: set[int],
    owner_user_ids: set[int],
) -> bool:
    return user_id in owner_user_ids or bool(
        set(role_ids).intersection(allowed_role_ids)
    )


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def deterministic_import_id(
    source_file: str,
    row_number: int,
    author_id: str,
    content_hash: str,
) -> str:
    return (
        f"imported_csv:{Path(source_file).name}:{row_number}::"
        f"{author_id}:{content_hash}"
    )


def parse_timestamp(value: object) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    now = dt.datetime.now(UTC)
    if lowered in {"now", "today"}:
        return now if lowered == "now" else now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if lowered == "yesterday":
        return (now - dt.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for pattern in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%m/%d/%Y %I:%M:%S %p",
            "%m/%d/%Y %I:%M %p",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y %H:%M",
        ):
            try:
                parsed = dt.datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_date_boundary(value: Optional[str], *, end: bool = False) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = parse_timestamp(text)
    if parsed is None:
        raise ValueError(
            "Use `yesterday`, `today`, or an ISO date/time such as "
            "`2026-06-21 20:00`."
        )
    if end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed += dt.timedelta(days=1)
    return parsed.isoformat()


def fts_query(value: str) -> str:
    tokens = re.findall(r"[\w'-]+", value.casefold(), flags=re.UNICODE)[:12]
    return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)


def safe_excerpt(value: object, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def infer_channel(path: Path) -> tuple[Optional[str], str]:
    name = path.stem.strip()
    match = re.search(r"\[(\d+)\]\s*$", name)
    channel_id = match.group(1) if match else None
    if match:
        name = name[: match.start()].rstrip()
    if " - " in name:
        name = name.rsplit(" - ", 1)[-1]
    return channel_id, name or path.stem
