"""Shared helpers for Discord history and full CSV imports."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional


CHANNEL_ID_PATTERN = re.compile(r"\[(\d+)\]\s*$")


def infer_export_channel(path: Path) -> tuple[Optional[str], str]:
    """Infer a Discord channel ID and readable name from an export filename."""
    name = path.stem.strip()
    match = CHANNEL_ID_PATTERN.search(name)
    channel_id = match.group(1) if match else None
    if match:
        name = name[: match.start()].rstrip()
    if " - " in name:
        name = name.rsplit(" - ", 1)[-1]
    return channel_id, name or path.stem


def _table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def get_json_activity_imported_channel_ids(
    db_path: Path,
    *,
    guild_id: Optional[int] = None,
) -> set[str]:
    """Return channel IDs with successful, non-empty JSON activity imports."""
    if not db_path.exists():
        return set()

    connection = sqlite3.connect(
        f"file:{db_path.resolve()}?mode=ro",
        uri=True,
    )
    try:
        table_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'stats_activity_imports'
            """
        ).fetchone()
        if not table_exists:
            return set()

        columns = _table_columns(connection, "stats_activity_imports")
        selected = [
            column
            for column in (
                "guild_id",
                "filename",
                "source_file",
                "source_format",
                "channel_id",
                "messages_imported",
                "status",
                "imported_for_activity",
            )
            if column in columns
        ]
        if not selected:
            return set()

        where = ""
        parameters: tuple[object, ...] = ()
        if guild_id is not None and "guild_id" in columns:
            where = " WHERE guild_id = ?"
            parameters = (guild_id,)
        rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM stats_activity_imports{where}",
            parameters,
        ).fetchall()

        channel_ids: set[str] = set()
        for raw_row in rows:
            row = dict(zip(selected, raw_row))
            status = str(row.get("status") or "").casefold()
            imported = int(row.get("messages_imported") or 0)
            if status not in {"completed", "partially_completed"} or imported <= 0:
                continue
            if (
                "imported_for_activity" in row
                and row["imported_for_activity"] is not None
                and not int(row["imported_for_activity"])
            ):
                continue

            source_file = str(
                row.get("source_file") or row.get("filename") or ""
            )
            source_format = str(row.get("source_format") or "").casefold()
            if source_format != "json" and Path(source_file).suffix.casefold() != ".json":
                continue

            channel_id = str(row.get("channel_id") or "").strip()
            if not channel_id or channel_id == "0":
                inferred_id, _ = infer_export_channel(Path(source_file))
                channel_id = inferred_id or ""
            if channel_id.isdigit() and int(channel_id) > 0:
                channel_ids.add(channel_id)
        return channel_ids
    finally:
        connection.close()
