#!/usr/bin/env python3
"""Import DiscordChatExporter metadata into BroEdenBot activity statistics."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional

import ijson

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.exclusions import env_csv_ids, load_excluded_user_cache
from utils.import_helpers import infer_export_channel


UTC = dt.timezone.utc
DEFAULT_DATABASE = Path("data.db")
SUPPORTED_SUFFIXES = {".json", ".csv"}
DATABASE_BATCH_SIZE = 5_000
PROGRESS_INTERVAL = 10_000
SQLITE_TIMEOUT_SECONDS = 60
SQLITE_BUSY_TIMEOUT_MS = 60_000
SQLITE_LOCK_RETRIES = 5
SQLITE_LOCK_BACKOFF_SECONDS = 0.5
UTF8_BOM = b"\xef\xbb\xbf"
DEFAULT_ARCHIVE_FOLDER = Path("imports/discord_history/archive")
SKIPPED_IMPORT_FOLDER_NAMES = {
    "archive",
    "archived",
    "broken_exports",
    "repaired_from_pi",
}
CSV_COLUMN_ALIASES = {
    "message_id": ("messageid", "id"),
    "timestamp": ("date", "timestamp", "sentat", "createdat"),
    "user_id": ("authorid", "userid"),
    "author_name": (
        "author",
        "authorname",
        "authorusername",
        "authordisplayname",
        "username",
        "displayname",
    ),
    "channel_id": ("channelid",),
    "channel_name": ("channel", "channelname"),
    "bot": (
        "isbot",
        "bot",
        "authorbot",
        "authorisbot",
        "userbot",
        "userisbot",
    ),
    "content": ("content", "message", "text"),
}


@dataclass
class FileResult:
    filename: str
    channel_id: int = 0
    channel_name: str = ""
    messages_seen: int = 0
    messages_imported: int = 0
    duplicates_skipped: int = 0
    messages_skipped: int = 0
    activity_excluded_role_rows_skipped: int = 0
    activity_excluded_role_messages_skipped: int = 0
    earliest: Optional[dt.datetime] = None
    latest: Optional[dt.datetime] = None
    status: str = "completed"
    notes: Optional[str] = None
    channel_totals: Counter[tuple[int, str]] = field(
        default_factory=Counter,
        repr=False,
    )


class FileProcessingError(RuntimeError):
    def __init__(self, result: FileResult):
        super().__init__(result.notes or "Import failed")
        self.result = result


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
    parser.add_argument(
        "--excluded-user-cache",
        type=Path,
        help=(
            "JSON cache of user IDs resolved from ACTIVITY_EXCLUDED_ROLE_IDS. "
            "Use scripts/export_excluded_role_members.py to generate it."
        ),
    )
    parser.add_argument(
        "--archive-completed",
        action="store_true",
        help="Move clean completed files into the archive folder",
    )
    parser.add_argument(
        "--archive-folder",
        type=Path,
        default=DEFAULT_ARCHIVE_FOLDER,
        help=f"Archive destination (default: {DEFAULT_ARCHIVE_FOLDER})",
    )
    parser.add_argument(
        "--archive-duplicates",
        action="store_true",
        help="Also archive clean files containing only duplicate messages",
    )
    return parser.parse_args()


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def activity_excluded_user_ids(args: argparse.Namespace) -> set[int]:
    excluded = env_csv_ids("ACTIVITY_EXCLUDED_USER_IDS")
    if getattr(args, "excluded_user_cache", None):
        excluded.update(load_excluded_user_cache(args.excluded_user_cache))
    return excluded


def is_database_locked(exc: BaseException) -> bool:
    return (
        isinstance(exc, sqlite3.OperationalError)
        and "locked" in str(exc).lower()
    )


def with_sqlite_lock_retry(operation, description: str):
    for attempt in range(1, SQLITE_LOCK_RETRIES + 1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_database_locked(exc):
                raise
            if attempt >= SQLITE_LOCK_RETRIES:
                print(
                    f"SQLite database remained locked while {description} "
                    f"after {SQLITE_LOCK_RETRIES} attempts. Stop broedenbot "
                    "during large imports, then try again.",
                    file=sys.stderr,
                )
                raise
            delay = SQLITE_LOCK_BACKOFF_SECONDS * attempt
            print(
                f"SQLite database is locked while {description}; retrying "
                f"in {delay:.1f}s ({attempt}/{SQLITE_LOCK_RETRIES}). "
                "For large imports, stop broedenbot to avoid write contention.",
                file=sys.stderr,
            )
            time.sleep(delay)


def execute_write(
    connection: sqlite3.Connection,
    sql: str,
    parameters=(),
    *,
    description: str,
):
    return with_sqlite_lock_retry(
        lambda: connection.execute(sql, parameters),
        description,
    )


def executemany_write(
    connection: sqlite3.Connection,
    sql: str,
    parameters,
    *,
    description: str,
):
    return with_sqlite_lock_retry(
        lambda: connection.executemany(sql, parameters),
        description,
    )


def commit_with_retry(
    connection: sqlite3.Connection,
    description: str = "committing imported activity",
) -> None:
    with_sqlite_lock_retry(connection.commit, description)


def ensure_schema(connection: sqlite3.Connection) -> None:
    execute_write(
        connection,
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
        """,
        description="creating the activity table",
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
            execute_write(
                connection,
                f"ALTER TABLE stats_message_activity ADD COLUMN {name} {definition}",
                description=f"adding the {name} activity column",
            )

    execute_write(
        connection,
        """
        UPDATE stats_message_activity
        SET source = COALESCE(NULLIF(source, ''), 'live'),
            import_batch_id = CASE
                WHEN COALESCE(NULLIF(source, ''), 'live') = 'live'
                THEN COALESCE(import_batch_id, 'live')
                ELSE import_batch_id
            END
        """,
        description="normalizing existing activity rows",
    )
    execute_write(
        connection,
        "DROP INDEX IF EXISTS idx_stats_message_activity_hour",
        description="updating the activity indexes",
    )
    execute_write(
        connection,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stats_message_activity_bucket
        ON stats_message_activity (
            guild_id, channel_id, user_id, activity_hour, source, import_batch_id
        )
        """,
        description="creating the activity dedupe index",
    )
    execute_write(
        connection,
        """
        CREATE INDEX IF NOT EXISTS idx_stats_message_activity_guild_date
        ON stats_message_activity (guild_id, activity_date)
        """,
        description="creating the guild activity index",
    )
    execute_write(
        connection,
        """
        CREATE INDEX IF NOT EXISTS idx_stats_message_activity_channel_date
        ON stats_message_activity (guild_id, channel_id, activity_date)
        """,
        description="creating the channel activity index",
    )
    execute_write(
        connection,
        """
        CREATE INDEX IF NOT EXISTS idx_stats_message_activity_user_date
        ON stats_message_activity (guild_id, user_id, activity_date)
        """,
        description="creating the member activity index",
    )
    execute_write(
        connection,
        """
        CREATE INDEX IF NOT EXISTS idx_stats_message_activity_source_date
        ON stats_message_activity (guild_id, source, activity_date)
        """,
        description="creating the source activity index",
    )
    execute_write(
        connection,
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
            notes TEXT,
            source_file TEXT,
            source_format TEXT,
            imported_for_activity INTEGER DEFAULT 1,
            imported_for_context INTEGER DEFAULT 0
        )
        """,
        description="creating the activity import log",
    )
    import_columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(stats_activity_imports)"
        ).fetchall()
    }
    for name, definition in (
        ("source_file", "TEXT"),
        ("source_format", "TEXT"),
        ("imported_for_activity", "INTEGER DEFAULT 1"),
        ("imported_for_context", "INTEGER DEFAULT 0"),
    ):
        if name not in import_columns:
            execute_write(
                connection,
                f"ALTER TABLE stats_activity_imports ADD COLUMN {name} {definition}",
                description=f"adding the {name} import-log column",
            )
    execute_write(
        connection,
        """
        CREATE INDEX IF NOT EXISTS idx_stats_activity_imports_guild_date
        ON stats_activity_imports (guild_id, imported_at)
        """,
        description="creating the import log index",
    )
    execute_write(
        connection,
        """
        CREATE TABLE IF NOT EXISTS stats_activity_imported_messages (
            guild_id INTEGER NOT NULL,
            message_id TEXT NOT NULL,
            import_batch_id TEXT,
            imported_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, message_id)
        )
        """,
        description="creating the imported-message dedupe table",
    )
    commit_with_retry(connection, "committing schema updates")


def input_files(args: argparse.Namespace) -> list[Path]:
    if args.file:
        return [args.file]

    archive_folder = args.archive_folder.resolve()
    files: list[Path] = []
    for root, directory_names, filenames in os.walk(args.folder):
        root_path = Path(root)
        if (
            root_path.name.casefold() in SKIPPED_IMPORT_FOLDER_NAMES
            or root_path.resolve() == archive_folder
        ):
            directory_names[:] = []
            continue
        directory_names[:] = [
            name
            for name in directory_names
            if (
                name.casefold() not in SKIPPED_IMPORT_FOLDER_NAMES
                and (root_path / name).resolve() != archive_folder
            )
        ]
        files.extend(
            root_path / filename
            for filename in filenames
            if Path(filename).suffix.lower() in SUPPORTED_SUFFIXES
        )
    return sorted(files)


def should_archive(result: FileResult, args: argparse.Namespace) -> bool:
    if not args.archive_completed or result.status != "completed":
        return False
    if result.messages_imported > 0:
        return True
    return bool(
        args.archive_duplicates
        and result.duplicates_skipped > 0
    )


def archive_destination(path: Path, archive_folder: Path) -> Path:
    destination = archive_folder / path.name
    if not destination.exists():
        return destination

    timestamp = utcnow().strftime("%Y%m%d_%H%M%S")
    candidate = archive_folder / f"{path.stem}_{timestamp}{path.suffix}"
    sequence = 2
    while candidate.exists():
        candidate = archive_folder / (
            f"{path.stem}_{timestamp}_{sequence}{path.suffix}"
        )
        sequence += 1
    return candidate


def archive_completed_file(
    path: Path,
    result: FileResult,
    args: argparse.Namespace,
) -> bool:
    if not should_archive(result, args):
        return True

    destination = archive_destination(path, args.archive_folder)
    if args.dry_run:
        print(f"Would archive completed file: {path} -> {destination}")
        return True

    try:
        args.archive_folder.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
    except OSError as exc:
        print(
            f"Could not archive completed file {path}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return False
    print(f"Archived completed file: {path} -> {destination}")
    return True


def infer_channel_name(path: Path) -> str:
    return infer_export_channel(path)[1]


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


@contextmanager
def open_json_stream(path: Path) -> Iterator[BinaryIO]:
    handle = path.open("rb")
    try:
        if handle.read(len(UTF8_BOM)) != UTF8_BOM:
            handle.seek(0)
        yield handle
    finally:
        handle.close()


def json_root_type(path: Path) -> str:
    with open_json_stream(path) as handle:
        try:
            _, event, _ = next(ijson.parse(handle))
        except StopIteration as exc:
            raise ValueError("JSON export is empty") from exc
    if event == "start_map":
        return "object"
    if event == "start_array":
        return "array"
    raise ValueError("JSON export must contain an object or array")


def json_metadata(
    path: Path,
    root_type: str,
) -> tuple[dict[str, Any], bool]:
    if root_type != "object":
        return {}, False

    metadata: dict[str, Any] = {}
    channel: dict[str, Any] = {}
    has_messages_array = False
    scalar_events = {"string", "number", "integer", "double"}
    with open_json_stream(path) as handle:
        for prefix, event, value in ijson.parse(handle):
            if prefix == "messages" and event == "start_array":
                has_messages_array = True
                break
            if event not in scalar_events:
                continue
            if prefix in {"channel.id", "channel.name"}:
                channel[prefix.rsplit(".", 1)[-1]] = value
            elif prefix in {
                "channelId",
                "channelName",
                "channel_id",
                "channel_name",
            }:
                metadata[prefix] = value
    if channel:
        metadata["channel"] = channel
    return metadata, has_messages_array


def json_messages(path: Path) -> tuple[Iterator[dict[str, Any]], dict[str, Any]]:
    root_type = json_root_type(path)
    metadata, has_messages_array = json_metadata(path, root_type)
    if root_type == "object" and not has_messages_array:
        raise ValueError("JSON export does not contain a messages array")
    item_prefix = "messages.item" if root_type == "object" else "item"

    def items() -> Iterator[dict[str, Any]]:
        with open_json_stream(path) as handle:
            for item in ijson.items(handle, item_prefix):
                if isinstance(item, dict):
                    yield item

    return items(), metadata


def normalize_csv_column(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().casefold())


def csv_column_indexes(header: list[str]) -> dict[str, Optional[int]]:
    normalized_header: dict[str, int] = {}
    for index, column_name in enumerate(header):
        normalized_header.setdefault(normalize_csv_column(column_name), index)
    return {
        field: next(
            (
                normalized_header[alias]
                for alias in aliases
                if alias in normalized_header
            ),
            None,
        )
        for field, aliases in CSV_COLUMN_ALIASES.items()
    }


def csv_row_value(
    row: list[str],
    indexes: dict[str, Optional[int]],
    field: str,
) -> Optional[str]:
    index = indexes.get(field)
    if index is None or index >= len(row):
        return None
    value = row[index].strip()
    return value or None


def canonical_csv_message(
    path: Path,
    row: list[str],
    indexes: dict[str, Optional[int]],
    row_number: int,
) -> dict[str, Any]:
    content = csv_row_value(row, indexes, "content") or ""
    author: dict[str, Any] = {
        "id": csv_row_value(row, indexes, "user_id"),
        "name": csv_row_value(row, indexes, "author_name"),
        "isBot": csv_row_value(row, indexes, "bot"),
    }
    channel: dict[str, Any] = {
        "id": csv_row_value(row, indexes, "channel_id"),
        "name": csv_row_value(row, indexes, "channel_name"),
    }
    return {
        "id": csv_row_value(row, indexes, "message_id"),
        "timestamp": csv_row_value(row, indexes, "timestamp"),
        "author": author,
        "channel": channel,
        "__csv_filename": path.name,
        "__csv_row_number": row_number,
        "__csv_content_hash": hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest(),
    }


def csv_messages(path: Path) -> tuple[Iterator[dict[str, Any]], dict[str, Any]]:
    def rows() -> Iterator[dict[str, Any]]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, strict=True)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise ValueError("CSV export is empty") from exc
            except csv.Error as exc:
                raise ValueError("CSV header could not be read") from exc

            indexes = csv_column_indexes(header)
            previous_error_line = -1
            while True:
                try:
                    row = next(reader)
                except StopIteration:
                    break
                except csv.Error:
                    error_line = reader.line_num
                    yield {
                        "__csv_error__": True,
                        "__csv_row_number": error_line,
                    }
                    if error_line <= previous_error_line:
                        raise ValueError(
                            "CSV parser could not continue after a malformed row"
                        )
                    previous_error_line = error_line
                    continue

                if len(row) != len(header):
                    yield {
                        "__csv_error__": True,
                        "__csv_row_number": reader.line_num,
                    }
                    continue
                yield canonical_csv_message(
                    path,
                    row,
                    indexes,
                    reader.line_num,
                )

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
    inferred_id, inferred_name = infer_export_channel(path)
    return (
        channel_id
        if channel_id is not None
        else (fallback_id or parse_int(inferred_id) or 0),
        str(channel_name or fallback_name or inferred_name),
    )


def message_fields(
    message: dict[str, Any],
    default_channel_id: int,
    default_channel_name: str,
) -> Optional[tuple[str, dt.datetime, int, str, str, int, str]]:
    if message.get("__csv_error__"):
        return None
    timestamp = parse_timestamp(
        nested(message, "timestamp", "date", "createdAt", "sent_at")
    )
    user_id = parse_int(nested(message, "author.id", "user.id", "userId", "user_id"))
    if timestamp is None or user_id is None:
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
    resolved_channel_id = (
        channel_id if channel_id is not None else default_channel_id
    )
    message_id = nested(message, "id", "messageId", "message_id")
    if message_id in (None, ""):
        csv_filename = message.get("__csv_filename")
        csv_row_number = message.get("__csv_row_number")
        if not csv_filename or csv_row_number is None:
            return None
        message["__legacy_activity_message_id"] = (
            f"csv:{csv_filename}:{csv_row_number}:{timestamp.isoformat()}:"
            f"{user_id}:{resolved_channel_id}"
        )
        message_id = (
            f"activity_csv:{resolved_channel_id}:{csv_filename}:"
            f"{csv_row_number}::{user_id}:"
            f"{message.get('__csv_content_hash') or hashlib.sha256(b'').hexdigest()}"
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
        resolved_channel_id,
        str(channel_name or default_channel_name),
    )


def update_range(result: FileResult, timestamp: dt.datetime) -> None:
    if result.earliest is None or timestamp < result.earliest:
        result.earliest = timestamp
    if result.latest is None or timestamp > result.latest:
        result.latest = timestamp


def update_result_channel(result: FileResult) -> None:
    if not result.channel_totals:
        return
    (channel_id, channel_name), _ = result.channel_totals.most_common(1)[0]
    result.channel_id = channel_id
    result.channel_name = channel_name


def pending_channel_totals(
    buckets: dict[tuple[int, int, str], dict[str, Any]],
) -> Counter[tuple[int, str]]:
    totals: Counter[tuple[int, str]] = Counter()
    for (channel_id, _, _), values in buckets.items():
        totals[(channel_id, values["channel_name"])] += values["count"]
    return totals


def safe_error_note(exc: Exception) -> str:
    if isinstance(exc, ijson.common.JSONError):
        return "Invalid or incomplete JSON export."
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: {exc}"
    if isinstance(exc, sqlite3.Error):
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__} while processing the export."


def record_import(
    connection: sqlite3.Connection,
    guild_id: int,
    batch_id: str,
    imported_at: str,
    result: FileResult,
) -> None:
    execute_write(
        connection,
        """
        INSERT INTO stats_activity_imports (
            guild_id, import_batch_id, filename, channel_id, channel_name,
            imported_at, messages_seen, messages_imported, messages_skipped,
            duplicates_skipped, earliest_message_at, latest_message_at,
            status, notes, source_file, source_format,
            imported_for_activity, imported_for_context
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
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
            result.filename,
            Path(result.filename).suffix.casefold().lstrip("."),
        ),
        description="recording the import result",
    )


def flush_activity_buckets(
    connection: sqlite3.Connection,
    buckets: dict[tuple[int, int, str], dict[str, Any]],
    args: argparse.Namespace,
    batch_id: str,
    imported_at: str,
) -> None:
    if buckets:
        now = utcnow().isoformat()
        rows = [
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
        ]
        executemany_write(
            connection,
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
            rows,
            description="writing imported activity buckets",
        )
        buckets.clear()
    commit_with_retry(connection)


def prepare_dry_run_dedupe(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA temp_store = FILE")
    connection.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS dry_run_imported_messages (
            guild_id INTEGER NOT NULL,
            message_id TEXT NOT NULL,
            PRIMARY KEY (guild_id, message_id)
        )
        """
    )


def is_dry_run_duplicate(
    connection: sqlite3.Connection,
    guild_id: int,
    message_id: str,
    dedupe_table_exists: bool,
) -> bool:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO dry_run_imported_messages (guild_id, message_id)
        VALUES (?, ?)
        """,
        (guild_id, message_id),
    )
    if cursor.rowcount == 0:
        return True
    if not dedupe_table_exists:
        return False
    return bool(
        connection.execute(
            """
            SELECT 1 FROM stats_activity_imported_messages
            WHERE guild_id = ? AND message_id = ?
            """,
            (guild_id, message_id),
        ).fetchone()
    )


def process_file(
    connection: sqlite3.Connection,
    path: Path,
    args: argparse.Namespace,
    batch_id: str,
) -> FileResult:
    result = FileResult(filename=str(path))
    item_label = "rows" if path.suffix.lower() == ".csv" else "messages"
    imported_at = utcnow().isoformat()
    excluded_user_ids = activity_excluded_user_ids(args)
    buckets: dict[tuple[int, int, str], dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "channel_name": "", "username": "", "display_name": ""}
    )

    try:
        messages, metadata = file_messages(path)
        result.channel_id, result.channel_name = channel_metadata(
            metadata,
            path,
            args.channel_id,
            args.channel_name,
        )
        dedupe_table_exists = bool(
            connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'stats_activity_imported_messages'
                """
            ).fetchone()
        )
        if args.dry_run:
            prepare_dry_run_dedupe(connection)

        for message in messages:
            result.messages_seen += 1
            parsed = message_fields(
                message, result.channel_id, result.channel_name
            )
            if parsed is None:
                result.messages_skipped += 1
            else:
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

                if user_id in excluded_user_ids:
                    result.activity_excluded_role_rows_skipped += 1
                    result.activity_excluded_role_messages_skipped += 1
                    result.messages_skipped += 1
                    continue

                if args.dry_run:
                    duplicate = is_dry_run_duplicate(
                        connection,
                        args.guild_id,
                        message_id,
                        dedupe_table_exists,
                    )
                    legacy_id = message.get("__legacy_activity_message_id")
                    if (
                        not duplicate
                        and legacy_id
                        and dedupe_table_exists
                    ):
                        duplicate = bool(
                            connection.execute(
                                """
                                SELECT 1 FROM stats_activity_imported_messages
                                WHERE guild_id = ? AND message_id = ?
                                """,
                                (args.guild_id, legacy_id),
                            ).fetchone()
                        )
                else:
                    legacy_id = message.get("__legacy_activity_message_id")
                    duplicate = bool(
                        legacy_id
                        and connection.execute(
                            """
                            SELECT 1 FROM stats_activity_imported_messages
                            WHERE guild_id = ? AND message_id = ?
                            """,
                            (args.guild_id, legacy_id),
                        ).fetchone()
                    )
                    if not duplicate:
                        cursor = execute_write(
                            connection,
                            """
                            INSERT OR IGNORE INTO stats_activity_imported_messages (
                                guild_id, message_id, import_batch_id, imported_at
                            )
                            VALUES (?, ?, ?, ?)
                            """,
                            (args.guild_id, message_id, batch_id, imported_at),
                            description="writing imported-message dedupe markers",
                        )
                        duplicate = cursor.rowcount == 0

                if duplicate:
                    result.duplicates_skipped += 1
                else:
                    hour = timestamp.replace(
                        minute=0, second=0, microsecond=0
                    ).isoformat()
                    bucket = buckets[(channel_id, user_id, hour)]
                    bucket["count"] += 1
                    bucket["channel_name"] = channel_name
                    bucket["username"] = username
                    bucket["display_name"] = display_name
                    result.messages_imported += 1
                    result.channel_totals[(channel_id, channel_name)] += 1

            if result.messages_seen % PROGRESS_INTERVAL == 0:
                print(
                    f"  Progress: {result.messages_seen:,} {item_label} seen; "
                    f"{result.messages_imported:,} ready to import; "
                    f"{result.duplicates_skipped:,} duplicates; "
                    f"{result.messages_skipped:,} skipped",
                    flush=True,
                )

            if (
                not args.dry_run
                and result.messages_seen % DATABASE_BATCH_SIZE == 0
            ):
                flush_activity_buckets(
                    connection,
                    buckets,
                    args,
                    batch_id,
                    imported_at,
                )

        if args.dry_run:
            buckets.clear()
            update_result_channel(result)
            return result

        flush_activity_buckets(
            connection,
            buckets,
            args,
            batch_id,
            imported_at,
        )
        update_result_channel(result)
        record_import(connection, args.guild_id, batch_id, imported_at, result)
        commit_with_retry(connection, "committing the import result")
        return result
    except Exception as exc:
        pending_totals = pending_channel_totals(buckets)
        connection.rollback()
        if not args.dry_run:
            pending_count = sum(pending_totals.values())
            result.messages_imported = max(
                0,
                result.messages_imported - pending_count,
            )
            result.channel_totals.subtract(pending_totals)
            result.channel_totals = +result.channel_totals
        update_result_channel(result)
        result.status = "failed"
        result.notes = safe_error_note(exc)
        raise FileProcessingError(result) from exc


def date_text(value: Optional[dt.datetime]) -> str:
    return value.isoformat() if value else "n/a"


def result_item_label(result: FileResult) -> str:
    return "Rows" if Path(result.filename).suffix.lower() == ".csv" else "Messages"


def print_file_result(result: FileResult) -> None:
    item_label = result_item_label(result)
    print(f"\nFile: {result.filename}")
    print(f"  Status: {result.status}")
    print(f"  Channel: {result.channel_name or 'Unknown'} ({result.channel_id})")
    print(f"  {item_label} seen: {result.messages_seen:,}")
    print(f"  Messages imported: {result.messages_imported:,}")
    print(f"  Duplicates skipped: {result.duplicates_skipped:,}")
    print(f"  {item_label} skipped: {result.messages_skipped:,}")
    print(
        "  Activity excluded-role rows skipped: "
        f"{result.activity_excluded_role_rows_skipped:,}"
    )
    print(
        "  Activity excluded-role messages skipped: "
        f"{result.activity_excluded_role_messages_skipped:,}"
    )
    print(f"  Date range: {date_text(result.earliest)} to {date_text(result.latest)}")
    if result.notes:
        print(f"  Notes: {result.notes}")


def open_database(args: argparse.Namespace) -> sqlite3.Connection:
    if args.dry_run and args.database.exists():
        database_uri = f"file:{args.database.resolve()}?mode=ro"
        connection = sqlite3.connect(
            database_uri,
            uri=True,
            timeout=SQLITE_TIMEOUT_SECONDS,
        )
    elif args.dry_run:
        connection = sqlite3.connect(
            ":memory:",
            timeout=SQLITE_TIMEOUT_SECONDS,
        )
    else:
        connection = sqlite3.connect(
            args.database,
            timeout=SQLITE_TIMEOUT_SECONDS,
        )

    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    if not args.dry_run:
        journal_mode = with_sqlite_lock_retry(
            lambda: connection.execute("PRAGMA journal_mode=WAL").fetchone(),
            "enabling SQLite WAL mode",
        )
        if journal_mode and str(journal_mode[0]).lower() != "wal":
            print(
                f"Warning: SQLite journal mode is {journal_mode[0]!r}, not WAL.",
                file=sys.stderr,
            )
    return connection


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

    connection = open_database(args)
    if not args.dry_run:
        ensure_schema(connection)
    batch_id = str(uuid.uuid4())
    results: list[FileResult] = []
    channel_totals: Counter[tuple[int, str]] = Counter()
    archive_failed = False

    print(f"Import batch: {batch_id}")
    print(f"Database: {args.database}")
    print(f"Mode: {'dry run' if args.dry_run else 'write'}")
    for path in files:
        try:
            result = process_file(connection, path, args, batch_id)
        except FileProcessingError as exc:
            result = exc.result
            if not args.dry_run:
                record_import(
                    connection,
                    args.guild_id,
                    batch_id,
                    utcnow().isoformat(),
                    result,
                )
                commit_with_retry(connection, "committing the failed import log")
        except Exception as exc:
            result = FileResult(
                filename=str(path),
                channel_id=args.channel_id or 0,
                channel_name=args.channel_name or infer_channel_name(path),
                status="failed",
                notes=safe_error_note(exc),
            )
            if not args.dry_run:
                record_import(
                    connection,
                    args.guild_id,
                    batch_id,
                    utcnow().isoformat(),
                    result,
                )
                commit_with_retry(connection, "committing the failed import log")
        results.append(result)
        channel_totals.update(result.channel_totals)
        print_file_result(result)
        if not archive_completed_file(path, result, args):
            archive_failed = True
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
    print(
        "  Activity excluded-role rows skipped: "
        f"{sum(r.activity_excluded_role_rows_skipped for r in results):,}"
    )
    print(
        "  Activity excluded-role messages skipped: "
        f"{sum(r.activity_excluded_role_messages_skipped for r in results):,}"
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
    return (
        1
        if archive_failed
        or any(result.status == "failed" for result in results)
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
