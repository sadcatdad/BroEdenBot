#!/usr/bin/env python3
"""Stream DiscordChatExporter CSV files into the private staff context DB."""

from __future__ import annotations

import argparse
import csv
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

from utils.staff_context import (
    STAFF_CONTEXT_FTS_SQL,
    STAFF_CONTEXT_FTS_TRIGGER_SQL,
    STAFF_CONTEXT_INDEX_SQL,
    STAFF_CONTEXT_TABLE_SQL,
    build_dedupe_key,
    content_digest,
    infer_channel,
    parse_timestamp,
    redact_sensitive_text,
    utcnow_iso,
)


DEFAULT_FOLDER = Path("imports/staff_context")
DEFAULT_DATABASE = Path(os.getenv("STAFF_CONTEXT_DB_PATH", "staff_context.db"))
DEFAULT_ARCHIVE = DEFAULT_FOLDER / "archive"
BATCH_SIZE = 1_000
PROGRESS_INTERVAL = 10_000
SKIPPED_FOLDER_NAMES = {
    "archive",
    "archived",
    "broken",
    "broken_exports",
}
REQUIRED_COLUMNS = {
    "AuthorID",
    "Author",
    "Date",
    "Content",
    "Attachments",
    "Reactions",
}


@dataclass
class ImportResult:
    source_file: str
    seen: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0
    earliest: Optional[str] = None
    latest: Optional[str] = None
    completed: bool = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import private staff-channel CSV exports for /staffai."
    )
    parser.add_argument("--folder", type=Path, default=DEFAULT_FOLDER)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--channel-id", type=int)
    parser.add_argument("--channel-name")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--archive-completed", action="store_true")
    parser.add_argument("--archive-folder", type=Path, default=DEFAULT_ARCHIVE)
    return parser.parse_args()


def ensure_schema(connection: sqlite3.Connection) -> bool:
    existing = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'staff_context_messages'
        """
    ).fetchone()
    if existing:
        columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(staff_context_messages)"
            ).fetchall()
        }
        legacy_constraints = (
            "source" not in columns
            or bool(columns.get("source_file", (None,) * 4)[3])
            or bool(columns.get("row_number", (None,) * 4)[3])
        )
        if legacy_constraints:
            connection.execute("DROP TABLE IF EXISTS staff_context_fts")
            for trigger in (
                "staff_context_messages_ai",
                "staff_context_messages_ad",
                "staff_context_messages_au",
            ):
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            connection.execute(
                "ALTER TABLE staff_context_messages RENAME TO staff_context_messages_legacy"
            )
            connection.execute(STAFF_CONTEXT_TABLE_SQL)
            connection.execute(
                """
                INSERT INTO staff_context_messages (
                    id, guild_id, channel_id, channel_name, message_id,
                    author_id, author_name, timestamp, content, content_hash,
                    source, source_file, row_number, dedupe_key,
                    attachment_count, attachment_names, edited_at, deleted,
                    deleted_at, imported_at, stored_at
                )
                SELECT
                    id, guild_id, channel_id, channel_name, NULL,
                    author_id, author_name, timestamp, content, content_hash,
                    'imported_csv', source_file, row_number, dedupe_key,
                    0, NULL, NULL, 0, NULL, imported_at,
                    COALESCE(imported_at, timestamp)
                FROM staff_context_messages_legacy
                """
            )
            connection.execute("DROP TABLE staff_context_messages_legacy")
    connection.execute(STAFF_CONTEXT_TABLE_SQL)
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(staff_context_messages)"
        ).fetchall()
    }
    for name, definition in (
        ("message_id", "INTEGER"),
        ("source", "TEXT NOT NULL DEFAULT 'imported_csv'"),
        ("attachment_count", "INTEGER NOT NULL DEFAULT 0"),
        ("attachment_names", "TEXT"),
        ("edited_at", "TEXT"),
        ("deleted", "INTEGER NOT NULL DEFAULT 0"),
        ("deleted_at", "TEXT"),
        ("stored_at", "TEXT"),
    ):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE staff_context_messages ADD COLUMN {name} {definition}"
            )
    connection.execute(
        """
        UPDATE staff_context_messages
        SET source = COALESCE(NULLIF(source, ''), 'imported_csv'),
            stored_at = COALESCE(stored_at, imported_at, timestamp)
        """
    )
    for statement in STAFF_CONTEXT_INDEX_SQL:
        connection.execute(statement)
    fts_available = True
    try:
        connection.execute(STAFF_CONTEXT_FTS_SQL)
        for statement in STAFF_CONTEXT_FTS_TRIGGER_SQL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO staff_context_fts(staff_context_fts) VALUES ('rebuild')"
        )
    except sqlite3.OperationalError as exc:
        if "fts5" not in str(exc).casefold():
            raise
        fts_available = False
    connection.commit()
    return fts_available


def input_files(args: argparse.Namespace) -> list[Path]:
    if args.file:
        return [args.file]
    archive = args.archive_folder.resolve()
    files: list[Path] = []
    if not args.folder.exists():
        return files
    for root, directory_names, filenames in os.walk(args.folder):
        root_path = Path(root)
        if (
            root_path.name.casefold() in SKIPPED_FOLDER_NAMES
            or root_path.resolve() == archive
        ):
            directory_names[:] = []
            continue
        directory_names[:] = [
            name
            for name in directory_names
            if name.casefold() not in SKIPPED_FOLDER_NAMES
            and (root_path / name).resolve() != archive
        ]
        files.extend(
            root_path / name
            for name in filenames
            if Path(name).suffix.casefold() == ".csv"
        )
    return sorted(files)


def _parse_author_id(value: object) -> Optional[int]:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def stream_rows(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - headers
        if missing:
            raise ValueError(
                "Missing required DiscordChatExporter columns: "
                + ", ".join(sorted(missing))
            )
        for row_number, row in enumerate(reader, start=2):
            yield row_number, row


def process_file(
    connection: sqlite3.Connection,
    path: Path,
    args: argparse.Namespace,
) -> ImportResult:
    result = ImportResult(source_file=path.name)
    inferred_channel_id, inferred_channel_name = infer_channel(path)
    channel_id = (
        args.channel_id
        if getattr(args, "channel_id", None) is not None
        else inferred_channel_id
    )
    channel_name = (
        str(args.channel_name).strip()
        if getattr(args, "channel_name", None)
        else inferred_channel_name
    )
    imported_at = utcnow_iso()
    pending = []

    def flush() -> None:
        if not pending or args.dry_run:
            pending.clear()
            return
        inserted = 0
        for values in pending:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO staff_context_messages (
                    guild_id, channel_id, channel_name, author_id, author_name,
                    timestamp, content, content_hash, source, source_file,
                    row_number, dedupe_key, attachment_count, attachment_names,
                    imported_at, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            inserted += max(0, cursor.rowcount)
        connection.commit()
        result.imported += inserted
        result.duplicates += len(pending) - inserted
        pending.clear()

    try:
        for row_number, row in stream_rows(path):
            result.seen += 1
            author_id = _parse_author_id(row.get("AuthorID"))
            timestamp = parse_timestamp(row.get("Date"))
            content = redact_sensitive_text(row.get("Content")).strip()
            attachment_names = redact_sensitive_text(
                row.get("Attachments")
            ).strip()
            if author_id is None or timestamp is None or not (
                content or attachment_names
            ):
                result.skipped += 1
                continue
            if not content:
                content = "[Attachment metadata only]"
            timestamp_text = timestamp.isoformat()
            digest = content_digest(content)
            dedupe_key = build_dedupe_key(
                path.name,
                row_number,
                timestamp_text,
                author_id,
                digest,
            )
            result.earliest = min(
                value for value in (result.earliest, timestamp_text) if value
            )
            result.latest = max(
                value for value in (result.latest, timestamp_text) if value
            )
            if args.dry_run:
                exists = connection.execute(
                    "SELECT 1 FROM staff_context_messages WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if exists:
                    result.duplicates += 1
                else:
                    result.imported += 1
            else:
                pending.append(
                    (
                        args.guild_id,
                        channel_id,
                        channel_name,
                        author_id,
                        str(row.get("Author") or "").strip(),
                        timestamp_text,
                        content,
                        digest,
                        "imported_csv",
                        path.name,
                        row_number,
                        dedupe_key,
                        1 if attachment_names else 0,
                        attachment_names or None,
                        imported_at,
                        imported_at,
                    )
                )
                if len(pending) >= BATCH_SIZE:
                    flush()
            if result.seen % PROGRESS_INTERVAL == 0:
                print(
                    f"{path.name}: scanned {result.seen:,} rows; "
                    f"accepted {result.imported:,}; skipped {result.skipped:,}."
                )
        flush()
    except (OSError, csv.Error, ValueError, sqlite3.Error) as exc:
        connection.rollback()
        result.completed = False
        print(
            f"{path.name}: import failed ({type(exc).__name__}).",
            file=sys.stderr,
        )
    return result


def archive_file(path: Path, args: argparse.Namespace) -> None:
    if args.dry_run:
        print(f"Would archive completed file: {path.name}")
        return
    args.archive_folder.mkdir(parents=True, exist_ok=True)
    destination = args.archive_folder / path.name
    counter = 2
    while destination.exists():
        destination = (
            args.archive_folder / f"{path.stem}_{counter}{path.suffix}"
        )
        counter += 1
    shutil.move(str(path), str(destination))
    print(f"Archived completed file: {path.name}")


def main() -> int:
    args = parse_args()
    files = input_files(args)
    if not files:
        print("No staff-context CSV files found.")
        return 0

    if args.dry_run:
        connection = sqlite3.connect(":memory:")
    else:
        args.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(args.database, timeout=60)
    try:
        fts_available = ensure_schema(connection)
        print(
            f"Staff context import: {len(files)} file(s), "
            f"FTS5={'available' if fts_available else 'unavailable; LIKE fallback'}."
        )
        totals = ImportResult(source_file="all files")
        failures = 0
        for path in files:
            result = process_file(connection, path, args)
            totals.seen += result.seen
            totals.imported += result.imported
            totals.duplicates += result.duplicates
            totals.skipped += result.skipped
            if not result.completed:
                failures += 1
            print(
                f"{path.name}: rows={result.seen:,}, "
                f"accepted={result.imported:,}, "
                f"duplicates={result.duplicates:,}, "
                f"skipped={result.skipped:,}, "
                f"status={'completed' if result.completed else 'failed'}."
            )
            if (
                result.completed
                and args.archive_completed
            ):
                archive_file(path, args)
        print(
            f"Finished: rows={totals.seen:,}, accepted={totals.imported:,}, "
            f"duplicates={totals.duplicates:,}, skipped={totals.skipped:,}, "
            f"failures={failures}."
        )
        return 1 if failures else 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
