#!/usr/bin/env python3
"""Import DiscordChatExporter metadata into BroEdenBot activity statistics."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional


UTC = dt.timezone.utc
DEFAULT_DATABASE = Path("data.db")
SUPPORTED_SUFFIXES = {".json", ".csv"}


@dataclass
class FileResult:
    filename: str
    channel_id: int = 0
    channel_name: str = ""
    messages_seen: int = 0
    messages_imported: int = 0
    duplicates_skipped: int = 0
    messages_skipped: int = 0
    earliest: Optional[dt.datetime] = None
    latest: Optional[dt.datetime] = None
    status: str = "completed"
    notes: Optional[str] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import DiscordChatExporter message metadata into BroEdenBot stats."
        )
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--file", type=Path, help="One JSON or CSV export file")
    inputs.add_argument(
        "--folder",
        type=Path,
        help="Folder recursively containing JSON or CSV exports",
    )
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--channel-id", type=int)
    parser.add_argument("--channel-name")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source", default="imported")
    return parser.parse_args()


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS stats_message_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            channel_name TEXT,
            user_id INTEGER NOT NULL,
            display_name TEXT,
            username TEXT,
            activity_date TEXT NOT NULL,
            activity_hour TEXT NOT NULL,
            message_count INTEGER DEFAULT 0,
            source TEXT DEFAULT 'live',
            imported_at TEXT,
            import_batch_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(stats_message_activity)"
        ).fetchall()
    }
    for name, definition in (
        ("source", "TEXT DEFAULT 'live'"),
        ("imported_at", "TEXT"),
        ("import_batch_id", "TEXT"),
    ):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE stats_message_activity ADD COLUMN {name} {definition}"
            )

    connection.execute(
        """
        UPDATE stats_message_activity
        SET source = COALESCE(NULLIF(source, ''), 'live'),
            import_batch_id = CASE
                WHEN COALESCE(NULLIF(source, ''), 'live') = 'live'
                THEN COALESCE(import_batch_id, 'live')
                ELSE import_batch_id
            END
        """
    )
    connection.execute("DROP INDEX IF EXISTS idx_stats_message_activity_hour")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stats_message_activity_bucket
        ON stats_message_activity (
            guild_id, channel_id, user_id, activity_hour, source, import_batch_id
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stats_message_activity_guild_date
        ON stats_message_activity (guild_id, activity_date)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stats_message_activity_channel_date
        ON stats_message_activity (guild_id, channel_id, activity_date)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stats_message_activity_user_date
        ON stats_message_activity (guild_id, user_id, activity_date)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stats_message_activity_source_date
        ON stats_message_activity (guild_id, source, activity_date)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS stats_activity_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            import_batch_id TEXT NOT NULL,
            filename TEXT,
            channel_id INTEGER,
            channel_name TEXT,
            imported_by INTEGER,
            imported_at TEXT NOT NULL,
            messages_seen INTEGER DEFAULT 0,
            messages_imported INTEGER DEFAULT 0,
            messages_skipped INTEGER DEFAULT 0,
            duplicates_skipped INTEGER DEFAULT 0,
            earliest_message_at TEXT,
            latest_message_at TEXT,
            status TEXT DEFAULT 'completed',
            notes TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stats_activity_imports_guild_date
        ON stats_activity_imports (guild_id, imported_at)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS stats_activity_imported_messages (
            guild_id INTEGER NOT NULL,
            message_id TEXT NOT NULL,
            import_batch_id TEXT,
            imported_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, message_id)
        )
        """
    )
    connection.commit()


def input_files(args: argparse.Namespace) -> list[Path]:
    if args.file:
        return [args.file]
    return sorted(
        path
        for path in args.folder.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def infer_channel_name(path: Path) -> str:
    name = path.stem
    if name.endswith("]") and " [" in name:
        name = name.rsplit(" [", 1)[0]
    if " - " in name:
        name = name.rsplit(" - ", 1)[-1]
    return name.strip()


def nested(record: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = record
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value not in (None, ""):
            return value
    return None


def parse_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        try:
            raw = float(text)
            if raw > 10_000_000_000:
                raw /= 1000
            return dt.datetime.fromtimestamp(raw, tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def is_system_message(message: dict[str, Any]) -> bool:
    if truthy(nested(message, "isSystem", "system")):
        return True
    message_type = str(nested(message, "type", "messageType") or "").lower()
    system_types = (
        "system",
        "recipientadd",
        "recipientremove",
        "channelpinnedmessage",
        "guildmemberjoin",
    )
    normalized_type = message_type.replace("_", "").replace("-", "")
    return any(marker in normalized_type for marker in system_types)


def json_messages(path: Path) -> tuple[Iterator[dict[str, Any]], dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    metadata: dict[str, Any] = data if isinstance(data, dict) else {}
    raw_messages = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(raw_messages, list):
        raise ValueError("JSON export does not contain a messages array")
    return (
        (item for item in raw_messages if isinstance(item, dict)),
        metadata,
    )


def csv_messages(path: Path) -> tuple[Iterator[dict[str, Any]], dict[str, Any]]:
    handle = path.open("r", encoding="utf-8-sig", newline="")
    reader = csv.DictReader(handle)

    def rows() -> Iterator[dict[str, Any]]:
        try:
            yield from reader
        finally:
            handle.close()

    return rows(), {}


def file_messages(
    path: Path,
) -> tuple[Iterator[dict[str, Any]], dict[str, Any]]:
    if path.suffix.lower() == ".json":
        return json_messages(path)
    if path.suffix.lower() == ".csv":
        return csv_messages(path)
    raise ValueError(f"Unsupported file type: {path.suffix}")


def channel_metadata(
    metadata: dict[str, Any],
    path: Path,
    fallback_id: Optional[int],
    fallback_name: Optional[str],
) -> tuple[int, str]:
    channel_id = parse_int(
        nested(metadata, "channel.id", "channelId", "channel_id")
    )
    channel_name = nested(
        metadata, "channel.name", "channelName", "channel_name"
    )
    return (
        channel_id if channel_id is not None else (fallback_id or 0),
        str(channel_name or fallback_name or infer_channel_name(path)),
    )


def message_fields(
    message: dict[str, Any],
    default_channel_id: int,
    default_channel_name: str,
) -> Optional[tuple[str, dt.datetime, int, str, str, int, str]]:
    message_id = nested(message, "id", "messageId", "message_id")
    timestamp = parse_timestamp(
        nested(message, "timestamp", "date", "createdAt", "sent_at")
    )
    user_id = parse_int(nested(message, "author.id", "user.id", "userId", "user_id"))
    if message_id in (None, "") or timestamp is None or user_id is None:
        return None
    if truthy(nested(message, "author.isBot", "author.bot", "user.isBot", "user.bot")):
        return None
    if is_system_message(message):
        return None
    channel_id = parse_int(
        nested(message, "channel.id", "channelId", "channel_id")
    )
    channel_name = nested(
        message, "channel.name", "channelName", "channel_name"
    )
    username = nested(
        message,
        "author.username",
        "author.name",
        "user.username",
        "user.name",
    )
    display_name = nested(
        message,
        "author.displayName",
        "author.nickname",
        "author.name",
        "user.displayName",
        "user.name",
    )
    return (
        str(message_id),
        timestamp,
        user_id,
        str(username or user_id),
        str(display_name or username or user_id),
        channel_id if channel_id is not None else default_channel_id,
        str(channel_name or default_channel_name),
    )


def update_range(result: FileResult, timestamp: dt.datetime) -> None:
    if result.earliest is None or timestamp < result.earliest:
        result.earliest = timestamp
    if result.latest is None or timestamp > result.latest:
        result.latest = timestamp


def record_import(
    connection: sqlite3.Connection,
    guild_id: int,
    batch_id: str,
    imported_at: str,
    result: FileResult,
) -> None:
    connection.execute(
        """
        INSERT INTO stats_activity_imports (
            guild_id, import_batch_id, filename, channel_id, channel_name,
            imported_at, messages_seen, messages_imported, messages_skipped,
            duplicates_skipped, earliest_message_at, latest_message_at,
            status, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            batch_id,
            result.filename,
            result.channel_id,
            result.channel_name,
            imported_at,
            result.messages_seen,
            result.messages_imported,
            result.messages_skipped,
            result.duplicates_skipped,
            result.earliest.isoformat() if result.earliest else None,
            result.latest.isoformat() if result.latest else None,
            result.status,
            result.notes,
        ),
    )


def process_file(
    connection: sqlite3.Connection,
    path: Path,
    args: argparse.Namespace,
    batch_id: str,
) -> FileResult:
    result = FileResult(filename=str(path))
    messages, metadata = file_messages(path)
    result.channel_id, result.channel_name = channel_metadata(
        metadata,
        path,
        args.channel_id,
        args.channel_name,
    )
    imported_at = utcnow().isoformat()
    buckets: dict[tuple[int, int, str], dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "channel_name": "", "username": "", "display_name": ""}
    )
    dry_run_ids: set[str] = set()
    dedupe_table_exists = bool(
        connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'stats_activity_imported_messages'
            """
        ).fetchone()
    )

    connection.execute("BEGIN")
    try:
        for message in messages:
            result.messages_seen += 1
            parsed = message_fields(
                message, result.channel_id, result.channel_name
            )
            if parsed is None:
                result.messages_skipped += 1
                continue
            (
                message_id,
                timestamp,
                user_id,
                username,
                display_name,
                channel_id,
                channel_name,
            ) = parsed
            update_range(result, timestamp)

            if args.dry_run:
                duplicate = message_id in dry_run_ids
                if not duplicate and dedupe_table_exists:
                    duplicate = bool(
                        connection.execute(
                            """
                            SELECT 1 FROM stats_activity_imported_messages
                            WHERE guild_id = ? AND message_id = ?
                            """,
                            (args.guild_id, message_id),
                        ).fetchone()
                    )
                dry_run_ids.add(message_id)
                if duplicate:
                    result.duplicates_skipped += 1
                    continue
            else:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO stats_activity_imported_messages (
                        guild_id, message_id, import_batch_id, imported_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (args.guild_id, message_id, batch_id, imported_at),
                )
                if cursor.rowcount == 0:
                    result.duplicates_skipped += 1
                    continue

            hour = timestamp.replace(
                minute=0, second=0, microsecond=0
            ).isoformat()
            bucket = buckets[(channel_id, user_id, hour)]
            bucket["count"] += 1
            bucket["channel_name"] = channel_name
            bucket["username"] = username
            bucket["display_name"] = display_name
            result.messages_imported += 1

        if args.dry_run:
            connection.rollback()
            return result

        now = utcnow().isoformat()
        connection.executemany(
            """
            INSERT INTO stats_message_activity (
                guild_id, channel_id, channel_name, user_id, display_name,
                username, activity_date, activity_hour, message_count, source,
                imported_at, import_batch_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                guild_id, channel_id, user_id, activity_hour, source,
                import_batch_id
            )
            DO UPDATE SET
                channel_name = excluded.channel_name,
                display_name = excluded.display_name,
                username = excluded.username,
                message_count = message_count + excluded.message_count,
                updated_at = excluded.updated_at
            """,
            [
                (
                    args.guild_id,
                    channel_id,
                    values["channel_name"],
                    user_id,
                    values["display_name"],
                    values["username"],
                    hour[:10],
                    hour,
                    values["count"],
                    args.source,
                    imported_at,
                    batch_id,
                    now,
                    now,
                )
                for (channel_id, user_id, hour), values in buckets.items()
            ],
        )
        record_import(connection, args.guild_id, batch_id, imported_at, result)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def date_text(value: Optional[dt.datetime]) -> str:
    return value.isoformat() if value else "n/a"


def print_file_result(result: FileResult) -> None:
    print(f"\nFile: {result.filename}")
    print(f"  Status: {result.status}")
    print(f"  Channel: {result.channel_name or 'Unknown'} ({result.channel_id})")
    print(f"  Messages seen: {result.messages_seen:,}")
    print(f"  Messages imported: {result.messages_imported:,}")
    print(f"  Duplicates skipped: {result.duplicates_skipped:,}")
    print(f"  Messages skipped: {result.messages_skipped:,}")
    print(f"  Date range: {date_text(result.earliest)} to {date_text(result.latest)}")
    if result.notes:
        print(f"  Error: {result.notes}")


def main() -> int:
    args = parse_args()
    if args.guild_id <= 0:
        print("--guild-id must be a positive integer.", file=sys.stderr)
        return 2
    if not args.source.strip():
        print("--source cannot be empty.", file=sys.stderr)
        return 2

    files = input_files(args)
    if not files:
        print("No JSON or CSV export files were found.", file=sys.stderr)
        return 1

    if args.dry_run and args.database.exists():
        database_uri = f"file:{args.database.resolve()}?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True)
    elif args.dry_run:
        connection = sqlite3.connect(":memory:")
    else:
        connection = sqlite3.connect(args.database)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    if not args.dry_run:
        ensure_schema(connection)
    batch_id = str(uuid.uuid4())
    results: list[FileResult] = []
    channel_totals: Counter[tuple[int, str]] = Counter()

    print(f"Import batch: {batch_id}")
    print(f"Database: {args.database}")
    print(f"Mode: {'dry run' if args.dry_run else 'write'}")
    for path in files:
        try:
            result = process_file(connection, path, args, batch_id)
        except Exception as exc:
            result = FileResult(
                filename=str(path),
                channel_id=args.channel_id or 0,
                channel_name=args.channel_name or infer_channel_name(path),
                status="failed",
                notes=f"{type(exc).__name__}: {exc}",
            )
            if not args.dry_run:
                record_import(
                    connection,
                    args.guild_id,
                    batch_id,
                    utcnow().isoformat(),
                    result,
                )
                connection.commit()
        results.append(result)
        if result.messages_imported:
            channel_totals[(result.channel_id, result.channel_name)] += (
                result.messages_imported
            )
        print_file_result(result)
    connection.close()

    successful = [result for result in results if result.status == "completed"]
    dated = [result for result in successful if result.earliest and result.latest]
    print("\nFinal summary")
    print(f"  Files processed: {len(successful):,}")
    print(f"  Files failed: {len(results) - len(successful):,}")
    print(f"  Total messages seen: {sum(r.messages_seen for r in results):,}")
    print(
        f"  Total messages imported: "
        f"{sum(r.messages_imported for r in results):,}"
    )
    print(
        f"  Total duplicates skipped: "
        f"{sum(r.duplicates_skipped for r in results):,}"
    )
    print(
        f"  Total messages skipped: "
        f"{sum(r.messages_skipped for r in results):,}"
    )
    if dated:
        print(
            "  Overall date range: "
            f"{min(r.earliest for r in dated).isoformat()} to "
            f"{max(r.latest for r in dated).isoformat()}"
        )
    else:
        print("  Overall date range: n/a")
    print("  Top 10 channels by imported messages:")
    for (channel_id, channel_name), count in channel_totals.most_common(10):
        print(f"    {channel_name or 'Unknown'} ({channel_id}): {count:,}")
    if not channel_totals:
        print("    None")
    return 1 if any(result.status == "failed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
