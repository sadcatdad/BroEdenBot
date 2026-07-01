"""Shared AI knowledge-base storage and search helpers."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

import aiosqlite

from utils.settings import settings_database_path
from utils.sqlite import configure_sync_connection


SOURCE_TYPES = {"rule", "guide", "channel_guide", "faq", "role_guide", "staff_note"}
VISIBILITIES = {"public", "staff"}
MAX_SOURCE_CHARS = 2 * 1024 * 1024
CHUNK_TARGET_CHARS = 5_500
CHUNK_MAX_CHARS = 7_000


@dataclass(frozen=True)
class KBChunk:
    section_title: str
    content: str


def normalize_kb_text(value: object) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"<#(\d+)>", r" channel \1 ", text)
    text = re.sub(r"<@!?(\d+)>", r" user \1 ", text)
    text = re.sub(r"[^\w\s'-]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _connect() -> sqlite3.Connection:
    path = settings_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return configure_sync_connection(sqlite3.connect(path, timeout=30))


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def validate_source_type(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in SOURCE_TYPES:
        raise ValueError("Invalid source type.")
    return normalized


def validate_visibility(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in VISIBILITIES:
        raise ValueError("Invalid visibility.")
    return normalized


async def initialize_ai_kb_schema_async(connection: aiosqlite.Connection) -> None:
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_kb_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source_name TEXT NOT NULL UNIQUE,
            source_type TEXT NOT NULL,
            source_visibility TEXT NOT NULL DEFAULT 'public',
            raw_content TEXT NOT NULL,
            metadata_json TEXT
        )
        """
    )
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_kb_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_visibility TEXT NOT NULL DEFAULT 'public',
            section_title TEXT,
            chunk_index INTEGER DEFAULT 0,
            content TEXT NOT NULL,
            normalized_content TEXT,
            metadata_json TEXT,
            FOREIGN KEY(source_id) REFERENCES ai_kb_sources(id) ON DELETE CASCADE
        )
        """
    )
    cursor = await connection.execute("PRAGMA table_info(ai_kb_chunks)")
    columns = {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    if "source_id" not in columns:
        await connection.execute("ALTER TABLE ai_kb_chunks ADD COLUMN source_id INTEGER")
    for column in ("source_type", "source_visibility", "source_name", "updated_at"):
        await connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_ai_kb_chunks_{column} "
            f"ON ai_kb_chunks ({column})"
        )
    await connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_kb_sources_updated_at "
        "ON ai_kb_sources (updated_at)"
    )
    await connection.commit()


def initialize_ai_kb_schema() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_kb_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source_name TEXT NOT NULL UNIQUE,
                source_type TEXT NOT NULL,
                source_visibility TEXT NOT NULL DEFAULT 'public',
                raw_content TEXT NOT NULL,
                metadata_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_kb_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_visibility TEXT NOT NULL DEFAULT 'public',
                section_title TEXT,
                chunk_index INTEGER DEFAULT 0,
                content TEXT NOT NULL,
                normalized_content TEXT,
                metadata_json TEXT,
                FOREIGN KEY(source_id) REFERENCES ai_kb_sources(id) ON DELETE CASCADE
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ai_kb_chunks)").fetchall()
        }
        if "source_id" not in columns:
            connection.execute("ALTER TABLE ai_kb_chunks ADD COLUMN source_id INTEGER")
        for column in ("source_type", "source_visibility", "source_name", "updated_at"):
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_ai_kb_chunks_{column} "
                f"ON ai_kb_chunks ({column})"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_kb_sources_updated_at "
            "ON ai_kb_sources (updated_at)"
        )
        connection.commit()


def chunk_text(raw_text: str) -> list[KBChunk]:
    text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    chunks: list[KBChunk] = []
    current: list[str] = []
    current_title = "General"
    current_size = 0
    for block in re.split(r"\n{2,}", text):
        block = block.strip()
        if not block:
            continue
        heading = re.match(r"^(#{1,6}\s+|[A-Z][A-Z0-9 '\-/]{3,}:)\s*(.+)?", block)
        if heading:
            title = heading.group(2) or block.lstrip("#").strip()
            current_title = title[:180].strip() or current_title
        if current and current_size + len(block) > CHUNK_TARGET_CHARS:
            chunks.append(KBChunk(current_title, "\n\n".join(current).strip()))
            current = []
            current_size = 0
        if len(block) > CHUNK_MAX_CHARS:
            for start in range(0, len(block), CHUNK_TARGET_CHARS):
                part = block[start : start + CHUNK_TARGET_CHARS].strip()
                if part:
                    chunks.append(KBChunk(current_title, part))
            continue
        current.append(block)
        current_size += len(block)
    if current:
        chunks.append(KBChunk(current_title, "\n\n".join(current).strip()))
    return chunks


def upsert_kb_source(
    *,
    source_name: str,
    source_type: str,
    visibility: str,
    raw_text: str,
    metadata: Optional[dict[str, object]] = None,
) -> dict[str, Any]:
    initialize_ai_kb_schema()
    name = str(source_name or "").strip()
    if not name:
        raise ValueError("Source name is required.")
    normalized_type = validate_source_type(source_type)
    normalized_visibility = validate_visibility(visibility)
    content = str(raw_text or "").strip()
    if not content:
        raise ValueError("Source content is required.")
    if len(content.encode("utf-8")) > MAX_SOURCE_CHARS:
        raise ValueError("Source content is too large. Limit is 2 MB.")
    chunks = chunk_text(content)
    if not chunks:
        raise ValueError("Source content did not produce any searchable chunks.")
    now = _now()
    metadata_json = json.dumps(metadata or {}, sort_keys=True)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id, created_at FROM ai_kb_sources WHERE source_name = ?",
            (name,),
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO ai_kb_sources (
                    created_at, updated_at, source_name, source_type,
                    source_visibility, raw_content, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (now, now, name, normalized_type, normalized_visibility, content, metadata_json),
            )
            source_id = int(cursor.lastrowid)
        else:
            source_id = int(row["id"])
            connection.execute(
                """
                UPDATE ai_kb_sources
                SET updated_at = ?, source_type = ?, source_visibility = ?,
                    raw_content = ?, metadata_json = ?
                WHERE id = ?
                """,
                (now, normalized_type, normalized_visibility, content, metadata_json, source_id),
            )
        connection.execute("DELETE FROM ai_kb_chunks WHERE source_name = ?", (name,))
        for index, chunk in enumerate(chunks):
            connection.execute(
                """
                INSERT INTO ai_kb_chunks (
                    source_id, created_at, updated_at, source_name, source_type,
                    source_visibility, section_title, chunk_index, content,
                    normalized_content, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    now,
                    now,
                    name,
                    normalized_type,
                    normalized_visibility,
                    chunk.section_title,
                    index,
                    chunk.content,
                    normalize_kb_text(chunk.content),
                    metadata_json,
                ),
            )
        connection.commit()
    return {
        "source_name": name,
        "source_type": normalized_type,
        "visibility": normalized_visibility,
        "chunk_count": len(chunks),
        "updated_at": now,
    }


def delete_kb_source(source_name: str) -> int:
    initialize_ai_kb_schema()
    name = str(source_name or "").strip()
    if not name:
        raise ValueError("Source name is required.")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM ai_kb_chunks WHERE source_name = ?",
            (name,),
        ).fetchone()
        count = int(row["count"] or 0)
        connection.execute("DELETE FROM ai_kb_chunks WHERE source_name = ?", (name,))
        connection.execute("DELETE FROM ai_kb_sources WHERE source_name = ?", (name,))
        connection.commit()
    return count


def get_kb_source(source_name: str) -> Optional[dict[str, Any]]:
    initialize_ai_kb_schema()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT source_name, source_type, source_visibility, raw_content,
                   metadata_json, created_at, updated_at
            FROM ai_kb_sources
            WHERE source_name = ?
            """,
            (str(source_name or "").strip(),),
        ).fetchone()
    return dict(row) if row else None


def list_kb_sources() -> list[dict[str, Any]]:
    initialize_ai_kb_schema()
    with _connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT
                    s.source_name, s.source_type, s.source_visibility,
                    s.updated_at, COUNT(c.id) AS chunk_count
                FROM ai_kb_sources AS s
                LEFT JOIN ai_kb_chunks AS c ON c.source_name = s.source_name
                GROUP BY s.source_name
                ORDER BY s.updated_at DESC, s.source_name COLLATE NOCASE
                """
            ).fetchall()
        ]


def get_kb_status() -> dict[str, Any]:
    initialize_ai_kb_schema()
    with _connect() as connection:
        totals = connection.execute(
            """
            SELECT
                COUNT(DISTINCT source_name) AS total_sources,
                COUNT(*) AS total_chunks,
                SUM(source_visibility = 'public') AS public_chunks,
                SUM(source_visibility = 'staff') AS staff_chunks
            FROM ai_kb_chunks
            """
        ).fetchone()
        by_type = [
            dict(row)
            for row in connection.execute(
                """
                SELECT source_type, COUNT(*) AS chunk_count
                FROM ai_kb_chunks
                GROUP BY source_type
                ORDER BY source_type
                """
            ).fetchall()
        ]
        latest = connection.execute(
            """
            SELECT source_name, source_type, source_visibility, updated_at
            FROM ai_kb_sources
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    return {
        "total_sources": int(totals["total_sources"] or 0),
        "total_chunks": int(totals["total_chunks"] or 0),
        "public_chunks": int(totals["public_chunks"] or 0),
        "staff_chunks": int(totals["staff_chunks"] or 0),
        "by_type": by_type,
        "latest_source": dict(latest) if latest else None,
    }


def _visibility_values(visibility: str) -> set[str]:
    value = str(visibility or "public").strip().casefold()
    if value == "all":
        return {"public", "staff"}
    if value == "staff":
        return {"public", "staff"}
    return {"public"}


def _score_chunk(normalized_query: str, tokens: list[str], row: sqlite3.Row) -> int:
    haystack = " ".join(
        normalize_kb_text(row[key])
        for key in ("source_name", "section_title", "content")
        if key in row.keys()
    )
    score = 0
    if normalized_query and normalized_query in haystack:
        score += 20
    for token in tokens:
        if len(token) < 2:
            continue
        score += haystack.count(token)
    return score


def search_kb(
    *,
    query: str,
    visibility: str = "public",
    limit: int = 5,
    source_types: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    initialize_ai_kb_schema()
    normalized_query = normalize_kb_text(query)
    tokens = [token for token in normalized_query.split() if len(token) >= 2][:12]
    if not tokens:
        return []
    visibilities = _visibility_values(visibility)
    clauses = [
        "source_visibility IN ({})".format(",".join("?" for _ in visibilities))
    ]
    params: list[Any] = sorted(visibilities)
    normalized_types = []
    for source_type in source_types or []:
        try:
            normalized_types.append(validate_source_type(source_type))
        except ValueError:
            continue
    if normalized_types:
        clauses.append("source_type IN ({})".format(",".join("?" for _ in normalized_types)))
        params.extend(normalized_types)
    like_clauses = []
    for token in tokens:
        like_clauses.append(
            "(normalized_content LIKE ? OR source_name LIKE ? OR section_title LIKE ?)"
        )
        params.extend([f"%{token}%"] * 3)
    clauses.append("(" + " OR ".join(like_clauses) + ")")
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT id, source_name, source_type, source_visibility,
                   section_title, chunk_index, content, updated_at
            FROM ai_kb_chunks
            WHERE {" AND ".join(clauses)}
            ORDER BY updated_at DESC, source_name COLLATE NOCASE, chunk_index
            LIMIT 100
            """,
            tuple(params),
        ).fetchall()
    ranked = []
    for row in rows:
        score = _score_chunk(normalized_query, tokens, row)
        if score <= 0:
            continue
        item = dict(row)
        item["score"] = score
        item["excerpt"] = excerpt_for_query(item["content"], tokens)
        ranked.append(item)
    ranked.sort(key=lambda item: (-int(item["score"]), str(item["source_name"]).casefold()))
    return ranked[: max(1, min(int(limit or 5), 25))]


def excerpt_for_query(content: str, tokens: Iterable[str], limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if len(text) <= limit:
        return text
    lowered = text.casefold()
    positions = [lowered.find(token) for token in tokens if lowered.find(token) >= 0]
    start = max(0, (min(positions) if positions else 0) - 80)
    end = min(len(text), start + limit)
    prefix = "..." if start else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


def format_kb_context(chunks: list[dict[str, Any]], *, max_chars: int = 18_000) -> str:
    parts = []
    size = 0
    for index, chunk in enumerate(chunks, start=1):
        section = chunk.get("section_title") or "General"
        text = (
            f"Source {index}: {chunk.get('source_name')} / {section} "
            f"({chunk.get('source_type')}, {chunk.get('source_visibility')})\n"
            f"{chunk.get('content')}"
        )
        if parts and size + len(text) > max_chars:
            break
        parts.append(text)
        size += len(text)
    return "\n\n---\n\n".join(parts)
