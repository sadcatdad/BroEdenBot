"""Live Discord knowledge source storage, formatting, and search helpers."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import aiosqlite

from utils.ai_kb import chunk_text, normalize_kb_text
from utils.settings import settings_database_path
from utils.sqlite import AutoClosingSQLiteConnection, configure_sync_connection


KNOWLEDGE_SOURCE_TYPES = {
    "public",
    "rules",
    "survival_guide",
    "channel_index",
    "bot_commands",
    "vc_guide",
    "events",
    "staff",
}
KNOWLEDGE_VISIBILITIES = {"public", "staff_only"}
KNOWLEDGE_SYNC_MODES = {"live", "manual"}
IMAGE_EXTENSIONS = {
    ".apng",
    ".avif",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".webp",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_source_type(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in KNOWLEDGE_SOURCE_TYPES:
        raise ValueError("Invalid knowledge source type.")
    return normalized


def validate_visibility(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized == "staff":
        normalized = "staff_only"
    if normalized not in KNOWLEDGE_VISIBILITIES:
        raise ValueError("Invalid knowledge visibility.")
    return normalized


def validate_sync_mode(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in KNOWLEDGE_SYNC_MODES:
        raise ValueError("Invalid knowledge sync mode.")
    return normalized


def content_digest(content: str) -> str:
    normalized = normalize_kb_text(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def initialize_live_knowledge_schema(
    connection: aiosqlite.Connection,
) -> None:
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            channel_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            ai_enabled INTEGER NOT NULL DEFAULT 1,
            visibility TEXT NOT NULL DEFAULT 'public',
            sync_mode TEXT NOT NULL DEFAULT 'live',
            last_synced_message_id INTEGER,
            last_synced_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(guild_id, channel_id)
        )
        """
    )
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            source_channel_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            source_type TEXT NOT NULL,
            visibility TEXT NOT NULL,
            title TEXT,
            content TEXT NOT NULL,
            content_hash TEXT,
            author_id INTEGER,
            created_at TEXT,
            edited_at TEXT,
            indexed_at TEXT NOT NULL,
            UNIQUE(guild_id, source_message_id)
        )
        """
    )
    cursor = await connection.execute("PRAGMA table_info(knowledge_sources)")
    source_columns = {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    if "ai_enabled" not in source_columns:
        await connection.execute(
            "ALTER TABLE knowledge_sources ADD COLUMN ai_enabled INTEGER NOT NULL DEFAULT 1"
        )
    cursor = await connection.execute("PRAGMA table_info(knowledge_entries)")
    columns = {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    if "content_hash" not in columns:
        await connection.execute("ALTER TABLE knowledge_entries ADD COLUMN content_hash TEXT")
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_knowledge_sources_enabled
        ON knowledge_sources (guild_id, enabled, sync_mode)
        """
    )
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_knowledge_entries_visibility
        ON knowledge_entries (guild_id, visibility, source_type)
        """
    )
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_knowledge_entries_channel
        ON knowledge_entries (guild_id, source_channel_id, indexed_at)
        """
    )
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_knowledge_entries_hash
        ON knowledge_entries (guild_id, content_hash)
        """
    )
    await connection.commit()


async def upsert_knowledge_source(
    connection: aiosqlite.Connection,
    *,
    guild_id: int,
    channel_id: int,
    channel_name: str,
    source_type: str,
    visibility: str,
    sync_mode: str = "live",
    enabled: bool = True,
    ai_enabled: bool = True,
) -> None:
    source_type = validate_source_type(source_type)
    visibility = validate_visibility(visibility)
    sync_mode = validate_sync_mode(sync_mode)
    now = utcnow_iso()
    await connection.execute(
        """
        INSERT INTO knowledge_sources (
            guild_id, channel_id, channel_name, source_type, enabled, ai_enabled,
            visibility, sync_mode, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, channel_id) DO UPDATE SET
            channel_name = excluded.channel_name,
            source_type = excluded.source_type,
            enabled = excluded.enabled,
            ai_enabled = excluded.ai_enabled,
            visibility = excluded.visibility,
            sync_mode = excluded.sync_mode,
            updated_at = excluded.updated_at
        """,
        (
            guild_id,
            channel_id,
            channel_name,
            source_type,
            1 if enabled else 0,
            1 if ai_enabled else 0,
            visibility,
            sync_mode,
            now,
            now,
        ),
    )


async def delete_knowledge_source(
    connection: aiosqlite.Connection,
    *,
    guild_id: int,
    channel_id: int,
) -> int:
    await delete_ai_kb_sources_for_channel(
        connection,
        guild_id=guild_id,
        source_channel_id=channel_id,
    )
    cursor = await connection.execute(
        """
        DELETE FROM knowledge_entries
        WHERE guild_id = ? AND source_channel_id = ?
        """,
        (guild_id, channel_id),
    )
    deleted_entries = cursor.rowcount if cursor.rowcount is not None else 0
    await cursor.close()
    await connection.execute(
        """
        DELETE FROM knowledge_sources
        WHERE guild_id = ? AND channel_id = ?
        """,
        (guild_id, channel_id),
    )
    return int(deleted_entries)


async def list_knowledge_sources(
    connection: aiosqlite.Connection,
    *,
    guild_id: int,
) -> list[aiosqlite.Row]:
    cursor = await connection.execute(
        """
        SELECT
            s.*,
            COUNT(e.id) AS entry_count,
            MAX(e.indexed_at) AS latest_indexed_at
        FROM knowledge_sources AS s
        LEFT JOIN knowledge_entries AS e
          ON e.guild_id = s.guild_id
         AND e.source_channel_id = s.channel_id
        WHERE s.guild_id = ?
        GROUP BY s.id
        ORDER BY s.enabled DESC, s.channel_name COLLATE NOCASE
        """,
        (guild_id,),
    )
    try:
        return await cursor.fetchall()
    finally:
        await cursor.close()


async def get_matching_source(
    connection: aiosqlite.Connection,
    *,
    guild_id: int,
    channel_id: int,
    parent_channel_id: Optional[int],
    live_only: bool = False,
) -> Optional[aiosqlite.Row]:
    channel_ids = [channel_id]
    if parent_channel_id and parent_channel_id != channel_id:
        channel_ids.append(parent_channel_id)
    mode_clause = "AND sync_mode = 'live'" if live_only else ""
    cursor = await connection.execute(
        f"""
        SELECT *
        FROM knowledge_sources
        WHERE guild_id = ?
          AND enabled = 1
          AND channel_id IN ({",".join("?" for _ in channel_ids)})
          {mode_clause}
        ORDER BY CASE WHEN channel_id = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        tuple([guild_id, *channel_ids, channel_id]),
    )
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


def source_name_for_message(guild_id: int, message_id: int) -> str:
    return f"live-discord:{guild_id}:{message_id}"


def _clean_text(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _embed_to_markdown(embed: object) -> str:
    parts = []
    title = _clean_text(getattr(embed, "title", ""))
    description = _clean_text(getattr(embed, "description", ""))
    if title:
        parts.append(f"# {title}")
    if description:
        parts.append(description)
    for field in getattr(embed, "fields", []) or []:
        name = _clean_text(getattr(field, "name", ""))
        value = _clean_text(getattr(field, "value", ""))
        if not name and not value:
            continue
        parts.append(f"## {name or 'Field'}\n{value}".strip())
    return "\n\n".join(parts).strip()


def _attachment_markdown(attachments: Iterable[object], has_text: bool) -> str:
    lines = []
    for attachment in attachments:
        filename = _clean_text(getattr(attachment, "filename", ""))
        url = _clean_text(getattr(attachment, "url", ""))
        if not filename:
            continue
        extension = "." + filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
        if extension in IMAGE_EXTENSIONS and not has_text:
            continue
        if extension in IMAGE_EXTENSIONS and has_text:
            lines.append(f"- Image attachment: {filename}")
        elif url:
            lines.append(f"- [{filename}]({url})")
        else:
            lines.append(f"- {filename}")
    if not lines:
        return ""
    return "## Attachments\n" + "\n".join(lines)


def message_to_knowledge(message: object) -> tuple[str, str]:
    """Convert a Discord message-like object into a title and Markdown body."""
    content_parts = []
    raw_content = _clean_text(getattr(message, "content", ""))
    if raw_content:
        content_parts.append(raw_content)
    for embed in getattr(message, "embeds", []) or []:
        embed_markdown = _embed_to_markdown(embed)
        if embed_markdown:
            content_parts.append(embed_markdown)

    has_text = bool(content_parts)
    attachment_text = _attachment_markdown(
        getattr(message, "attachments", []) or [],
        has_text,
    )
    if attachment_text:
        content_parts.append(attachment_text)
    content = "\n\n".join(part for part in content_parts if part).strip()

    channel = getattr(message, "channel", None)
    title_parts = []
    thread_name = _clean_text(getattr(channel, "name", "")) if _is_thread(channel) else ""
    if thread_name:
        title_parts.append(thread_name)
        if not content:
            content = f"# {thread_name}"
    if not content:
        return "", ""
    for embed in getattr(message, "embeds", []) or []:
        title = _clean_text(getattr(embed, "title", ""))
        if title and title not in title_parts:
            title_parts.append(title)
            break
    if not title_parts:
        first_line = raw_content.splitlines()[0].strip() if raw_content else ""
        if first_line.startswith("#"):
            first_line = first_line.lstrip("#").strip()
        title_parts.append(first_line[:120] or "Discord Knowledge")
    return " / ".join(title_parts)[:220], content


def _is_thread(channel: object) -> bool:
    return getattr(channel, "parent", None) is not None and hasattr(channel, "owner_id")


async def upsert_knowledge_entry_from_message(
    connection: aiosqlite.Connection,
    *,
    message: object,
    source: aiosqlite.Row,
) -> bool:
    title, content = message_to_knowledge(message)
    guild = getattr(message, "guild", None)
    guild_id = int(getattr(guild, "id", source["guild_id"]))
    message_id = int(getattr(message, "id"))
    if not content:
        await delete_knowledge_entry(
            connection,
            guild_id=guild_id,
            source_message_id=message_id,
        )
        return False
    digest = content_digest(content)
    cursor = await connection.execute(
        """
        SELECT source_message_id
        FROM knowledge_entries
        WHERE guild_id = ? AND content_hash = ?
          AND visibility = ?
          AND source_message_id != ?
        LIMIT 1
        """,
        (guild_id, digest, source["visibility"], message_id),
    )
    duplicate = await cursor.fetchone()
    await cursor.close()
    if duplicate is not None:
        await delete_knowledge_entry(
            connection,
            guild_id=guild_id,
            source_message_id=message_id,
        )
        return False

    source_channel_id = int(source["channel_id"])
    created_at = getattr(getattr(message, "created_at", None), "isoformat", lambda: None)()
    edited_value = getattr(message, "edited_at", None)
    edited_at = edited_value.isoformat() if edited_value else None
    indexed_at = utcnow_iso()
    await connection.execute(
        """
        INSERT INTO knowledge_entries (
            guild_id, source_channel_id, source_message_id, source_type,
            visibility, title, content, content_hash, author_id, created_at,
            edited_at, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, source_message_id) DO UPDATE SET
            source_channel_id = excluded.source_channel_id,
            source_type = excluded.source_type,
            visibility = excluded.visibility,
            title = excluded.title,
            content = excluded.content,
            content_hash = excluded.content_hash,
            author_id = excluded.author_id,
            created_at = excluded.created_at,
            edited_at = excluded.edited_at,
            indexed_at = excluded.indexed_at
        """,
        (
            guild_id,
            source_channel_id,
            message_id,
            source["source_type"],
            source["visibility"],
            title,
            content,
            digest,
            getattr(getattr(message, "author", None), "id", None),
            created_at,
            edited_at,
            indexed_at,
        ),
    )
    await upsert_ai_kb_source_for_entry(
        connection,
        guild_id=guild_id,
        source_channel_id=source_channel_id,
        source_message_id=message_id,
        source_type=source["source_type"],
        visibility=source["visibility"],
        ai_enabled=bool(source["ai_enabled"]),
        title=title,
        content=content,
        indexed_at=indexed_at,
    )
    return True


async def delete_knowledge_entry(
    connection: aiosqlite.Connection,
    *,
    guild_id: int,
    source_message_id: int,
) -> None:
    await connection.execute(
        """
        DELETE FROM knowledge_entries
        WHERE guild_id = ? AND source_message_id = ?
        """,
        (guild_id, source_message_id),
    )
    await delete_ai_kb_source(
        connection,
        source_name_for_message(guild_id, source_message_id),
    )


async def mark_source_synced(
    connection: aiosqlite.Connection,
    *,
    guild_id: int,
    channel_id: int,
    last_message_id: Optional[int],
) -> None:
    await connection.execute(
        """
        UPDATE knowledge_sources
        SET last_synced_message_id = COALESCE(?, last_synced_message_id),
            last_synced_at = ?,
            updated_at = ?
        WHERE guild_id = ? AND channel_id = ?
        """,
        (last_message_id, utcnow_iso(), utcnow_iso(), guild_id, channel_id),
    )


async def upsert_ai_kb_source_for_entry(
    connection: aiosqlite.Connection,
    *,
    guild_id: int,
    source_channel_id: int,
    source_message_id: int,
    source_type: str,
    visibility: str,
    ai_enabled: bool,
    title: str,
    content: str,
    indexed_at: str,
) -> None:
    source_name = source_name_for_message(guild_id, source_message_id)
    now = utcnow_iso()
    raw_text = f"# {title}\n\n{content}".strip()
    metadata = {
        "source": "live_discord",
        "guild_id": str(guild_id),
        "channel_id": str(source_channel_id),
        "message_id": str(source_message_id),
        "indexed_at": indexed_at,
    }
    metadata_json = json.dumps(metadata, sort_keys=True)
    if not ai_enabled:
        await delete_ai_kb_source(connection, source_name)
        return

    cursor = await connection.execute(
        "SELECT id, created_at FROM ai_kb_sources WHERE source_name = ?",
        (source_name,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        cursor = await connection.execute(
            """
            INSERT INTO ai_kb_sources (
                created_at, updated_at, source_name, source_type,
                source_visibility, ai_enabled, raw_content, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, now, source_name, source_type, visibility, 1, raw_text, metadata_json),
        )
        source_id = cursor.lastrowid
        await cursor.close()
    else:
        source_id = row[0]
        await connection.execute(
            """
            UPDATE ai_kb_sources
            SET updated_at = ?, source_type = ?, source_visibility = ?,
                ai_enabled = ?,
                raw_content = ?, metadata_json = ?
            WHERE id = ?
            """,
            (now, source_type, visibility, 1, raw_text, metadata_json, source_id),
        )
    await connection.execute("DELETE FROM ai_kb_chunks WHERE source_name = ?", (source_name,))
    for index, chunk in enumerate(chunk_text(raw_text)):
        await connection.execute(
            """
            INSERT INTO ai_kb_chunks (
                source_id, created_at, updated_at, source_name, source_type,
                source_visibility, ai_enabled, section_title, chunk_index, content,
                normalized_content, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                now,
                now,
                source_name,
                source_type,
                visibility,
                1,
                chunk.section_title,
                index,
                chunk.content,
                normalize_kb_text(chunk.content),
                metadata_json,
            ),
        )


async def delete_ai_kb_source(
    connection: aiosqlite.Connection,
    source_name: str,
) -> None:
    await connection.execute("DELETE FROM ai_kb_chunks WHERE source_name = ?", (source_name,))
    await connection.execute("DELETE FROM ai_kb_sources WHERE source_name = ?", (source_name,))


async def delete_ai_kb_sources_for_channel(
    connection: aiosqlite.Connection,
    *,
    guild_id: int,
    source_channel_id: int,
) -> None:
    pattern = f'"guild_id": "{guild_id}"'
    channel_pattern = f'"channel_id": "{source_channel_id}"'
    cursor = await connection.execute(
        """
        SELECT source_name
        FROM ai_kb_sources
        WHERE source_name LIKE ?
          AND metadata_json LIKE ?
          AND metadata_json LIKE ?
        """,
        ("live-discord:%", f"%{pattern}%", f"%{channel_pattern}%"),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    for row in rows:
        await delete_ai_kb_source(connection, row[0])


def _query_terms(query: str) -> list[str]:
    normalized = normalize_kb_text(query)
    return [token for token in normalized.split() if len(token) >= 2][:12]


def score_entry(query: str, title: str, content: str) -> int:
    normalized_query = normalize_kb_text(query)
    terms = _query_terms(query)
    if not terms:
        return 0
    haystack = normalize_kb_text(f"{title} {content}")
    score = 20 if normalized_query and normalized_query in haystack else 0
    for term in terms:
        score += haystack.count(term)
    return score


def excerpt_for_terms(content: str, terms: Iterable[str], limit: int = 700) -> str:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if len(text) <= limit:
        return text
    lowered = text.casefold()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, (min(positions) if positions else 0) - 100)
    end = min(len(text), start + limit)
    prefix = "..." if start else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


def live_entries_as_sources(
    *,
    rows: Iterable[dict[str, Any]],
    max_excerpt_chars: int,
) -> list[tuple[str, str, str]]:
    results = []
    for row in rows:
        source = f"Discord #{row.get('source_channel_id')} ({row.get('source_type')})"
        heading = str(row.get("title") or "Discord Knowledge")
        content = str(row.get("content") or "")
        terms = _query_terms(heading + " " + content)
        results.append(
            (
                source,
                heading,
                excerpt_for_terms(content, terms, limit=max_excerpt_chars),
            )
        )
    return results


def _connect() -> sqlite3.Connection:
    path = settings_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return configure_sync_connection(
        sqlite3.connect(
            path,
            timeout=30,
            factory=AutoClosingSQLiteConnection,
        )
    )


def initialize_live_knowledge_schema_sync() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                ai_enabled INTEGER NOT NULL DEFAULT 1,
                visibility TEXT NOT NULL DEFAULT 'public',
                sync_mode TEXT NOT NULL DEFAULT 'live',
                last_synced_message_id INTEGER,
                last_synced_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, channel_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                source_channel_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                visibility TEXT NOT NULL,
                title TEXT,
                content TEXT NOT NULL,
                content_hash TEXT,
                author_id INTEGER,
                created_at TEXT,
                edited_at TEXT,
                indexed_at TEXT NOT NULL,
                UNIQUE(guild_id, source_message_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT,
                result_message TEXT
            )
            """
        )
        _ensure_column(connection, "knowledge_sources", "ai_enabled", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(connection, "knowledge_entries", "content_hash", "TEXT")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_sources_enabled
            ON knowledge_sources (guild_id, enabled, sync_mode)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_entries_visibility
            ON knowledge_entries (guild_id, visibility, source_type)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_entries_channel
            ON knowledge_entries (guild_id, source_channel_id, indexed_at)
            """
        )
        connection.commit()


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }
    if column not in columns:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} {definition}')


def list_live_knowledge_sources_sync() -> list[dict[str, Any]]:
    initialize_live_knowledge_schema_sync()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT
                s.*,
                COUNT(e.id) AS entry_count,
                MAX(e.indexed_at) AS latest_indexed_at
            FROM knowledge_sources AS s
            LEFT JOIN knowledge_entries AS e
              ON e.guild_id = s.guild_id
             AND e.source_channel_id = s.channel_id
            GROUP BY s.id
            ORDER BY s.enabled DESC, s.channel_name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_live_knowledge_source_sync(
    *,
    guild_id: int,
    channel_id: int,
    channel_name: str,
    source_type: str,
    visibility: str,
    sync_mode: str,
    enabled: bool,
    ai_enabled: bool,
) -> None:
    initialize_live_knowledge_schema_sync()
    source_type = validate_source_type(source_type)
    visibility = validate_visibility(visibility)
    sync_mode = validate_sync_mode(sync_mode)
    now = utcnow_iso()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO knowledge_sources (
                guild_id, channel_id, channel_name, source_type, enabled,
                ai_enabled, visibility, sync_mode, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                channel_name = excluded.channel_name,
                source_type = excluded.source_type,
                enabled = excluded.enabled,
                ai_enabled = excluded.ai_enabled,
                visibility = excluded.visibility,
                sync_mode = excluded.sync_mode,
                updated_at = excluded.updated_at
            """,
            (
                guild_id,
                channel_id,
                channel_name,
                source_type,
                1 if enabled else 0,
                1 if ai_enabled else 0,
                visibility,
                sync_mode,
                now,
                now,
            ),
        )
        if not ai_enabled:
            _delete_ai_kb_sources_for_channel_sync(
                connection,
                guild_id=guild_id,
                source_channel_id=channel_id,
            )
        connection.commit()


def delete_live_knowledge_source_sync(
    *,
    guild_id: int,
    channel_id: int,
) -> int:
    initialize_live_knowledge_schema_sync()
    with _connect() as connection:
        _delete_ai_kb_sources_for_channel_sync(
            connection,
            guild_id=guild_id,
            source_channel_id=channel_id,
        )
        cursor = connection.execute(
            """
            DELETE FROM knowledge_entries
            WHERE guild_id = ? AND source_channel_id = ?
            """,
            (guild_id, channel_id),
        )
        deleted = int(cursor.rowcount or 0)
        connection.execute(
            """
            DELETE FROM knowledge_sources
            WHERE guild_id = ? AND channel_id = ?
            """,
            (guild_id, channel_id),
        )
        connection.commit()
    return deleted


def queue_live_knowledge_sync(
    *,
    guild_id: int,
    channel_id: int,
    limit: int,
    requested_by: str,
) -> int:
    initialize_live_knowledge_schema_sync()
    payload_json = json.dumps(
        {
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "limit": max(1, min(int(limit or 200), 1000)),
        },
        sort_keys=True,
    )
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO dashboard_actions (
                action_type, payload_json, status, requested_by
            ) VALUES ('sync_knowledge_source', ?, 'pending', ?)
            """,
            (payload_json, requested_by),
        )
        connection.commit()
        return int(cursor.lastrowid)


def _delete_ai_kb_sources_for_channel_sync(
    connection: sqlite3.Connection,
    *,
    guild_id: int,
    source_channel_id: int,
) -> None:
    pattern = f'"guild_id": "{guild_id}"'
    channel_pattern = f'"channel_id": "{source_channel_id}"'
    rows = connection.execute(
        """
        SELECT source_name
        FROM ai_kb_sources
        WHERE source_name LIKE ?
          AND metadata_json LIKE ?
          AND metadata_json LIKE ?
        """,
        ("live-discord:%", f"%{pattern}%", f"%{channel_pattern}%"),
    ).fetchall()
    for row in rows:
        connection.execute("DELETE FROM ai_kb_chunks WHERE source_name = ?", (row["source_name"],))
        connection.execute("DELETE FROM ai_kb_sources WHERE source_name = ?", (row["source_name"],))
