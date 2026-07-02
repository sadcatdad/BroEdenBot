"""Local feedback storage for member-facing /ask answers."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import aiosqlite


VALID_FEEDBACK = {"helped", "confused"}


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True)
    except (TypeError, ValueError):
        return "[]"


async def initialize_ask_feedback_schema(
    connection: aiosqlite.Connection,
) -> None:
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ask_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            guild_id TEXT,
            channel_id TEXT,
            user_id TEXT,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            feedback TEXT,
            feedback_at TEXT,
            kb_sources_json TEXT NOT NULL DEFAULT '[]',
            model_used TEXT,
            tier_used TEXT
        )
        """
    )
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ask_feedback_created_at
        ON ask_feedback (created_at)
        """
    )
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ask_feedback_feedback_at
        ON ask_feedback (feedback, feedback_at)
        """
    )
    await connection.commit()


async def create_ask_feedback(
    connection: Optional[aiosqlite.Connection],
    *,
    guild_id: Optional[object],
    channel_id: Optional[object],
    user_id: Optional[object],
    question: str,
    answer: str,
    kb_sources: list[dict[str, object]],
    model_used: Optional[str],
    tier_used: Optional[str],
) -> Optional[int]:
    if connection is None:
        return None
    await initialize_ask_feedback_schema(connection)
    now = _now()
    cursor = await connection.execute(
        """
        INSERT INTO ask_feedback (
            created_at, updated_at, guild_id, channel_id, user_id,
            question, answer, kb_sources_json, model_used, tier_used
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            now,
            str(guild_id) if guild_id is not None else None,
            str(channel_id) if channel_id is not None else None,
            str(user_id) if user_id is not None else None,
            question[:1000],
            answer[:4000],
            _safe_json(kb_sources),
            model_used,
            tier_used,
        ),
    )
    await connection.commit()
    return int(cursor.lastrowid)


async def record_ask_feedback(
    connection: Optional[aiosqlite.Connection],
    feedback_id: Optional[int],
    feedback: str,
) -> bool:
    if connection is None or feedback_id is None or feedback not in VALID_FEEDBACK:
        return False
    await initialize_ask_feedback_schema(connection)
    now = _now()
    cursor = await connection.execute(
        """
        UPDATE ask_feedback
        SET feedback = ?, feedback_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (feedback, now, now, feedback_id),
    )
    await connection.commit()
    return bool(cursor.rowcount)
