#!/usr/bin/env python3
"""Stream DiscordChatExporter CSV files into message_context.db."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.message_context import (
    MESSAGE_CONTEXT_FTS_SQL,
    MESSAGE_CONTEXT_FTS_TRIGGER_SQL,
    MESSAGE_CONTEXT_INDEX_SQL,
    MESSAGE_CONTEXT_TABLE_SQL,
    content_digest,
    deterministic_import_id,
    infer_channel,
    parse_timestamp,
    safe_excerpt,
    utcnow_iso,
)


DEFAULT_FOLDER = Path("imports/message_context")
DEFAULT_DATABASE = Path(
    os.getenv("MESSAGE_CONTEXT_DB_PATH", "message_context.db")
)
DEFAULT_ARCHIVE = DEFAULT_FOLDER / "archive"
SKIPPED_FOLDERS = {
    "archive",
    "local_archive",
    "broken",
    "broken_exports",
    "repaired",
}
BATCH_SIZE = 1_000

COLUMN_ALIASES = {
    "author_id": ("AuthorID", "Author Id", "AuthorId", "author_id", "UserID"),
    "author": ("Author", "AuthorName", "Username", "author_name"),
    "date": ("Date", "Timestamp", "CreatedAt", "created_at"),
    "content": ("Content", "Message", "Text", "content"),
    "attachments": ("Attachments", "Attachment", "Files", "attachments"),
    "message_id": ("MessageID", "Message Id", "MessageId", "message_id", "ID"),
    "channel_id": ("ChannelID", "Channel Id", "ChannelId", "channel_id"),
    "channel_name": ("Channel", "ChannelName", "channel_name"),
}


@dataclass
class ImportResult:
    file: Path
    channel_id: str = "unknown"
    channel_name: str = ""
    seen: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0
    earliest: Optional[dt.datetime] = None
    latest: Optional[dt.datetime] = None
    failed: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import Discord CSV exports into the private message archive."
    )
    parser.add_argument("--file", type=Path)
    parser.add_argument("--folder", type=Path, default=DEFAULT_FOLDER)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--guild-id", required=True)
    parser.add_argument("--channel-id")
    parser.add_argument("--channel-name")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--archive-completed", action="store_true")
    parser.add_argument("--archive-duplicates", action="store_true")
    parser.add_argument("--archive-folder", type=Path, default=DEFAULT_ARCHIVE)
    return parser.parse_args()


def ensure_schema(connection: sqlite3.Connection) -> bool:
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute(MESSAGE_CONTEXT_TABLE_SQL)
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(message_context_messages)"
        ).fetchall()
    }
    for name, definition in (
        ("source_file", "TEXT"),
        ("row_number", "INTEGER"),
        ("imported_at", "TEXT"),
    ):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE message_context_messages "
                f"ADD COLUMN {name} {definition}"
            )
    for statement in MESSAGE_CONTEXT_INDEX_SQL:
        connection.execute(statement)
    fts = True
    try:
        fts_existed = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'message_context_fts'
            """
        ).fetchone() is not None
        connection.execute(MESSAGE_CONTEXT_FTS_SQL)
        for statement in MESSAGE_CONTEXT_FTS_TRIGGER_SQL:
            connection.execute(statement)
        if not fts_existed:
            connection.execute(
                "INSERT INTO message_context_fts(message_context_fts) "
                "VALUES ('rebuild')"
            )
    except sqlite3.OperationalError as exc:
        if "fts5" not in str(exc).casefold():
            raise
        fts = False
    connection.commit()
    return fts


def input_files(args: argparse.Namespace) -> list[Path]:
    if args.file:
        return [args.file]
    if not args.folder.exists():
        return []
    archive = args.archive_folder.resolve()
    files: list[Path] = []
    for root, directories, filenames in os.walk(args.folder):
        root_path = Path(root)
        if root_path.name.casefold() in SKIPPED_FOLDERS or root_path.resolve() == archive:
            directories[:] = []
            continue
        directories[:] = [
            name
            for name in directories
            if name.casefold() not in SKIPPED_FOLDERS
            and (root_path / name).resolve() != archive
        ]
        files.extend(
            root_path / filename
            for filename in filenames
            if Path(filename).suffix.casefold() == ".csv"
        )
    return sorted(files)


def _find_header(headers: list[str], field: str) -> Optional[str]:
    folded = {header.casefold(): header for header in headers}
    for alias in COLUMN_ALIASES[field]:
        if alias.casefold() in folded:
            return folded[alias.casefold()]
    return None


def stream_rows(path: Path) -> Iterator[tuple[int, dict[str, str], dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        mapping = {
            field: _find_header(headers, field)
            for field in COLUMN_ALIASES
        }
        required = ("author_id", "author", "date", "content")
        missing = [field for field in required if not mapping[field]]
        if missing:
            raise ValueError(
                "Missing required columns: " + ", ".join(missing)
            )
        for row_number, row in enumerate(reader, start=2):
            yield row_number, row, mapping


def _value(row: dict[str, str], mapping: dict[str, str], field: str) -> str:
    header = mapping.get(field)
    return str(row.get(header, "") if header else "").strip()


def _attachment_names(value: str) -> list[str]:
    if not value:
        return []
    return [
        safe_excerpt(item.strip().rsplit("/", 1)[-1], 180)
        for item in value.replace("\r", "\n").split("\n")
        if item.strip()
    ][:20]


def process_file(
    connection: Optional[sqlite3.Connection],
    path: Path,
    args: argparse.Namespace,
) -> ImportResult:
    inferred_id, inferred_name = infer_channel(path)
    fallback_channel_id = str(args.channel_id or inferred_id or "unknown")
    fallback_channel_name = str(args.channel_name or inferred_name)
    result = ImportResult(
        file=path,
        channel_id=fallback_channel_id,
        channel_name=fallback_channel_name,
    )
    pending: list[tuple] = []
    dry_run_seen: set[tuple[str, str, str, str, str]] = set()

    def flush() -> None:
        if not pending:
            return
        if args.dry_run:
            for values in pending:
                (
                    guild_id,
                    channel_id,
                    _channel_name,
                    message_id,
                    author_id,
                    _author,
                    _display,
                    timestamp,
                    _content,
                    digest,
                    *_rest,
                ) = values
                identity = (
                    guild_id,
                    channel_id,
                    author_id,
                    timestamp,
                    digest,
                )
                duplicate = identity in dry_run_seen
                if not duplicate and connection is not None:
                    duplicate = connection.execute(
                        """
                        SELECT 1 FROM message_context_messages
                        WHERE message_id = ?
                           OR (
                               source = 'imported_csv'
                               AND guild_id = ?
                               AND channel_id = ?
                               AND author_id = ?
                               AND timestamp = ?
                               AND content_hash = ?
                           )
                        LIMIT 1
                        """,
                        (
                            message_id,
                            guild_id,
                            channel_id,
                            author_id,
                            timestamp,
                            digest,
                        ),
                    ).fetchone() is not None
                dry_run_seen.add(identity)
                if duplicate:
                    result.duplicates += 1
                else:
                    result.imported += 1
            pending.clear()
            return
        if connection is None:
            raise RuntimeError("A database connection is required for imports.")
        inserted = 0
        for values in pending:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO message_context_messages (
                    guild_id, channel_id, channel_name, message_id, author_id,
                    author_name, author_display_name, timestamp, is_deleted,
                    is_bot, is_webhook, content, content_hash, attachment_count,
                    attachment_names, embed_count, sticker_count, source,
                    source_file, row_number, imported_at, stored_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, 0, 0,
                    'imported_csv', ?, ?, ?, ?
                )
                """,
                values,
            )
            inserted += max(0, cursor.rowcount)
        connection.commit()
        result.imported += inserted
        result.duplicates += len(pending) - inserted
        pending.clear()

    try:
        for row_number, row, mapping in stream_rows(path):
            result.seen += 1
            author_id = _value(row, mapping, "author_id")
            author = _value(row, mapping, "author")
            timestamp = parse_timestamp(_value(row, mapping, "date"))
            content = _value(row, mapping, "content")
            attachment_names = _attachment_names(
                _value(row, mapping, "attachments")
            )
            if not author_id or timestamp is None or not (content or attachment_names):
                result.skipped += 1
                continue
            digest = content_digest(content)
            channel_id = _value(row, mapping, "channel_id") or fallback_channel_id
            channel_name = (
                _value(row, mapping, "channel_name") or fallback_channel_name
            )
            result.channel_id = channel_id
            result.channel_name = channel_name
            message_id = _value(row, mapping, "message_id") or deterministic_import_id(
                path.name,
                row_number,
                author_id,
                digest,
                channel_id,
            )
            if result.earliest is None or timestamp < result.earliest:
                result.earliest = timestamp
            if result.latest is None or timestamp > result.latest:
                result.latest = timestamp
            imported_at = utcnow_iso()
            pending.append(
                (
                    str(args.guild_id),
                    channel_id,
                    channel_name,
                    message_id,
                    author_id,
                    author,
                    author,
                    timestamp.isoformat(),
                    content,
                    digest,
                    len(attachment_names),
                    json.dumps(attachment_names) if attachment_names else None,
                    path.name,
                    row_number,
                    imported_at,
                    imported_at,
                )
            )
            if len(pending) >= BATCH_SIZE:
                flush()
        flush()
    except Exception:
        result.failed = True
        raise
    return result


def archive_file(path: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    counter = 1
    while target.exists():
        target = folder / f"{path.stem}-{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(target))
    return target


def main() -> int:
    args = parse_args()
    files = input_files(args)
    if not files:
        print("No CSV files found.")
        return 0
    connection = None
    if not args.dry_run:
        args.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(args.database)
        fts = ensure_schema(connection)
        print(f"Database ready. FTS5: {'yes' if fts else 'no (LIKE fallback)'}")
    else:
        print("Dry run: no database writes or file moves will occur.")

    failures = 0
    try:
        for path in files:
            try:
                result = process_file(connection, path, args)
            except Exception as exc:
                failures += 1
                print(f"{path}: failed ({type(exc).__name__}: {exc})")
                continue
            print(
                f"{path}: seen={result.seen} imported={result.imported} "
                f"duplicates={result.duplicates} skipped={result.skipped}"
            )
            duplicate_only = result.imported == 0 and result.duplicates > 0
            should_archive = (
                not args.dry_run
                and args.archive_completed
                and (result.imported > 0 or (
                    duplicate_only and args.archive_duplicates
                ))
            )
            if should_archive:
                print(f"Archived to {archive_file(path, args.archive_folder)}")
    finally:
        if connection is not None:
            connection.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
