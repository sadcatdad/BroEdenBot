"""Shared helpers for the private staff-context database and importer."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Iterable, Optional

from utils.privacy import redact_sensitive_text


UTC = dt.timezone.utc
STAFF_CONTEXT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS staff_context_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER,
    channel_name TEXT NOT NULL,
    message_id INTEGER,
    author_id INTEGER NOT NULL,
    author_name TEXT,
    timestamp TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'imported_csv',
    source_file TEXT,
    row_number INTEGER,
    dedupe_key TEXT NOT NULL,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    attachment_names TEXT,
    edited_at TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    imported_at TEXT,
    stored_at TEXT NOT NULL
)
"""
STAFF_CONTEXT_REQUIRED_COLUMNS = {
    "message_id": "INTEGER",
    "source": "TEXT NOT NULL DEFAULT 'imported_csv'",
    "attachment_count": "INTEGER NOT NULL DEFAULT 0",
    "attachment_names": "TEXT",
    "edited_at": "TEXT",
    "deleted": "INTEGER NOT NULL DEFAULT 0",
    "deleted_at": "TEXT",
    "stored_at": "TEXT",
}
STAFF_CONTEXT_INDEX_SQL = (
    """
    CREATE INDEX IF NOT EXISTS idx_staff_context_timestamp
    ON staff_context_messages (timestamp)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_staff_context_author_id
    ON staff_context_messages (author_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_staff_context_channel_name
    ON staff_context_messages (channel_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_staff_context_content_hash
    ON staff_context_messages (content_hash)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_context_dedupe_key
    ON staff_context_messages (dedupe_key)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_context_live_message
    ON staff_context_messages (guild_id, message_id)
    WHERE message_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_staff_context_source_timestamp
    ON staff_context_messages (source, timestamp)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_staff_context_channel_id_timestamp
    ON staff_context_messages (guild_id, channel_id, timestamp)
    """,
)
STAFF_CONTEXT_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS staff_context_fts USING fts5(
    content,
    author_name,
    channel_name,
    content='staff_context_messages',
    content_rowid='id'
)
"""
STAFF_CONTEXT_FTS_TRIGGER_SQL = (
    """
    CREATE TRIGGER IF NOT EXISTS staff_context_messages_ai
    AFTER INSERT ON staff_context_messages BEGIN
        INSERT INTO staff_context_fts(
            rowid, content, author_name, channel_name
        ) VALUES (new.id, new.content, new.author_name, new.channel_name);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS staff_context_messages_ad
    AFTER DELETE ON staff_context_messages BEGIN
        INSERT INTO staff_context_fts(
            staff_context_fts, rowid, content, author_name, channel_name
        ) VALUES (
            'delete', old.id, old.content, old.author_name, old.channel_name
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS staff_context_messages_au
    AFTER UPDATE ON staff_context_messages BEGIN
        INSERT INTO staff_context_fts(
            staff_context_fts, rowid, content, author_name, channel_name
        ) VALUES (
            'delete', old.id, old.content, old.author_name, old.channel_name
        );
        INSERT INTO staff_context_fts(
            rowid, content, author_name, channel_name
        ) VALUES (new.id, new.content, new.author_name, new.channel_name);
    END
    """,
)


def utcnow_iso() -> str:
    return dt.datetime.now(UTC).isoformat()


def parse_timestamp(value: object) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for pattern in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%m/%d/%Y %I:%M %p",
            "%m/%d/%Y %I:%M:%S %p",
            "%m/%d/%Y %H:%M",
            "%m/%d/%Y %H:%M:%S",
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
        raise ValueError("Use an ISO date or timestamp, such as 2026-06-01.")
    if end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed = parsed + dt.timedelta(days=1)
    return parsed.isoformat()


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_dedupe_key(
    source_file: str,
    row_number: int,
    timestamp: str,
    author_id: int,
    content_hash: str,
) -> str:
    raw = (
        f"{source_file}\0{row_number}\0{timestamp}\0"
        f"{author_id}\0{content_hash}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def infer_channel(path: Path) -> tuple[Optional[int], str]:
    name = path.stem.strip()
    channel_id = None
    match = re.search(r"\[(\d+)\]\s*$", name)
    if match:
        channel_id = int(match.group(1))
        name = name[: match.start()].rstrip()
    if "headquarters" in name.casefold():
        return channel_id, "staff"
    if " - " in name:
        name = name.rsplit(" - ", 1)[-1]
    return channel_id, name.strip() or path.stem


def parse_id_set(raw_value: str) -> set[int]:
    return {
        int(value)
        for value in re.split(r"[\s,]+", str(raw_value or "").strip())
        if value.isdigit()
    }


def parse_bool(raw_value: object, default: bool = False) -> bool:
    text = str(raw_value or "").strip().casefold()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def has_staff_ai_access(
    user_id: int,
    role_ids: Iterable[int],
    allowed_role_ids: set[int],
    owner_user_ids: set[int],
) -> bool:
    return user_id in owner_user_ids or bool(
        set(role_ids).intersection(allowed_role_ids)
    )


def fts_query(value: str) -> str:
    tokens = re.findall(r"[\w'-]+", value.casefold(), flags=re.UNICODE)
    ignored = {
        "about",
        "after",
        "before",
        "could",
        "did",
        "does",
        "from",
        "have",
        "how",
        "our",
        "that",
        "the",
        "this",
        "was",
        "what",
        "when",
        "where",
        "which",
        "with",
        "would",
    }
    selected = [
        token
        for token in tokens
        if len(token) >= 3 and token not in ignored
    ][:12]
    if not selected:
        selected = tokens[:12]
    return " OR ".join(
        f'"{token.replace(chr(34), "")}"' for token in selected
    )


def short_excerpt(content: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", redact_sensitive_text(content)).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def source_label(value: object) -> str:
    return "Live Discord" if value == "live_discord" else "Imported CSV"


def source_summary(rows: Iterable[object]) -> str:
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        channel = str(row["channel_name"] or "unknown-channel")
        source = source_label(row["source"])
        grouped.setdefault((channel, source), []).append(str(row["timestamp"]))
    parts = []
    for (channel, source), timestamps in sorted(grouped.items()):
        parts.append(
            f"#{channel} ({source}): "
            f"{min(timestamps)[:10]} to {max(timestamps)[:10]}"
        )
    return "; ".join(parts)
