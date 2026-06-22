#!/usr/bin/env python3
"""Import full-server CSV exports into context and missing activity history."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import import_discord_history as activity_importer
from scripts import import_message_context as context_importer
from utils.import_helpers import (
    get_json_activity_imported_channel_ids,
    infer_export_channel,
)


DEFAULT_FOLDER = Path("imports/message_context")
DEFAULT_CONTEXT_DATABASE = Path("message_context.db")
DEFAULT_ACTIVITY_DATABASE = Path("data.db")
DEFAULT_ARCHIVE_FOLDER = DEFAULT_FOLDER / "archive"


@dataclass
class CombinedResult:
    file: Path
    channel_id: str
    channel_name: str
    context: Optional[context_importer.ImportResult] = None
    activity: Optional[activity_importer.FileResult] = None
    activity_status: str = "not_run"
    activity_note: str = ""
    archived: bool = False
    failed: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import every CSV into private message context and backfill activity "
            "only for channels without completed JSON activity imports."
        )
    )
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--file", type=Path)
    inputs.add_argument("--folder", type=Path, default=DEFAULT_FOLDER)
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument(
        "--context-database",
        type=Path,
        default=DEFAULT_CONTEXT_DATABASE,
    )
    parser.add_argument(
        "--activity-database",
        type=Path,
        default=DEFAULT_ACTIVITY_DATABASE,
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--archive-completed", action="store_true")
    parser.add_argument("--archive-duplicates", action="store_true")
    parser.add_argument(
        "--archive-folder",
        type=Path,
        default=DEFAULT_ARCHIVE_FOLDER,
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--context-only", action="store_true")
    modes.add_argument("--activity-backfill-only", action="store_true")
    parser.add_argument(
        "--skip-activity-for-json-imported",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--force-activity",
        action="store_true",
        help=(
            "Dangerous: activity-import CSVs even when JSON activity already "
            "exists for the channel."
        ),
    )
    parser.add_argument("--channel-id")
    parser.add_argument("--channel-name")
    return parser.parse_args()


def _context_args(
    args: argparse.Namespace,
    channel_id: str,
    channel_name: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=str(args.guild_id),
        channel_id=channel_id,
        channel_name=channel_name,
        dry_run=args.dry_run,
    )


def _activity_args(
    args: argparse.Namespace,
    channel_id: int,
    channel_name: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=args.guild_id,
        database=args.activity_database,
        channel_id=channel_id,
        channel_name=channel_name,
        dry_run=args.dry_run,
        source="csv_backfill",
        archive_completed=False,
        archive_duplicates=False,
        archive_folder=args.archive_folder,
    )


def _open_context_database(
    args: argparse.Namespace,
) -> Optional[sqlite3.Connection]:
    if args.activity_backfill_only:
        return None
    if args.dry_run:
        if not args.context_database.exists():
            return None
        connection = sqlite3.connect(
            f"file:{args.context_database.resolve()}?mode=ro",
            uri=True,
        )
        table_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'message_context_messages'
            """
        ).fetchone()
        if table_exists:
            return connection
        connection.close()
        return None

    args.context_database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.context_database)
    context_importer.ensure_schema(connection)
    return connection


def _open_activity_database(
    args: argparse.Namespace,
) -> Optional[sqlite3.Connection]:
    if args.context_only:
        return None
    activity_args = SimpleNamespace(
        database=args.activity_database,
        dry_run=args.dry_run,
    )
    connection = activity_importer.open_database(activity_args)
    if not args.dry_run:
        activity_importer.ensure_schema(connection)
    return connection


def _files(args: argparse.Namespace) -> list[Path]:
    context_args = SimpleNamespace(
        file=args.file,
        folder=args.folder,
        archive_folder=args.archive_folder,
    )
    return context_importer.input_files(context_args)


def _resolve_channel(
    path: Path,
    args: argparse.Namespace,
) -> tuple[str, str]:
    inferred_id, inferred_name = infer_export_channel(path)
    channel_id = str(args.channel_id or inferred_id or "")
    channel_name = str(args.channel_name or inferred_name)
    if channel_id:
        return channel_id, channel_name

    try:
        for _row_number, row, mapping in context_importer.stream_rows(path):
            row_id = context_importer._value(row, mapping, "channel_id")
            row_name = context_importer._value(row, mapping, "channel_name")
            return row_id, row_name or channel_name
    except (OSError, ValueError):
        pass
    return "", channel_name


def _activity_should_run(
    args: argparse.Namespace,
    channel_id: str,
    json_channel_ids: set[str],
) -> tuple[bool, str, str]:
    if args.context_only:
        return False, "skipped_context_only", ""
    if not channel_id or not channel_id.isdigit() or int(channel_id) <= 0:
        return (
            False,
            "skipped_no_channel_id",
            "Supply --channel-id for a single file to enable activity backfill.",
        )
    if (
        args.skip_activity_for_json_imported
        and not args.force_activity
        and channel_id in json_channel_ids
    ):
        return (
            False,
            "skipped_json_already_imported",
            "JSON activity already exists for this channel ID.",
        )
    return True, "dry_run" if args.dry_run else "imported", ""


def _should_archive(result: CombinedResult, args: argparse.Namespace) -> bool:
    if args.dry_run or not args.archive_completed or result.failed:
        return False
    imported = 0
    duplicates = 0
    if result.context is not None:
        imported += result.context.imported
        duplicates += result.context.duplicates
    if result.activity is not None:
        imported += result.activity.messages_imported
        duplicates += result.activity.duplicates_skipped
    return imported > 0 or (args.archive_duplicates and duplicates > 0)


def _date_text(value) -> str:
    return value.isoformat() if value else "n/a"


def print_result(result: CombinedResult) -> None:
    print(f"\nFile: {result.file}")
    print(f"  Channel: {result.channel_name or 'Unknown'} [{result.channel_id or 'unknown'}]")
    if result.context is None:
        print("  Context: not selected")
    else:
        print(
            "  Context: "
            f"seen={result.context.seen:,} "
            f"imported={result.context.imported:,} "
            f"duplicates={result.context.duplicates:,} "
            f"skipped={result.context.skipped:,}"
        )
    if result.activity is None:
        print(f"  Activity status: {result.activity_status}")
        print("  Activity: counted=0 duplicates=0 skipped=0")
    else:
        print(f"  Activity status: {result.activity_status}")
        print(
            "  Activity: "
            f"counted={result.activity.messages_imported:,} "
            f"duplicates={result.activity.duplicates_skipped:,} "
            f"skipped={result.activity.messages_skipped:,}"
        )
    if result.activity_note:
        print(f"  Activity note: {result.activity_note}")
    earliest_values = [
        value
        for value in (
            result.context.earliest if result.context else None,
            result.activity.earliest if result.activity else None,
        )
        if value is not None
    ]
    latest_values = [
        value
        for value in (
            result.context.latest if result.context else None,
            result.activity.latest if result.activity else None,
        )
        if value is not None
    ]
    print(
        "  Date range: "
        f"{_date_text(min(earliest_values) if earliest_values else None)} to "
        f"{_date_text(max(latest_values) if latest_values else None)}"
    )
    print(f"  Archived: {'yes' if result.archived else 'no'}")


def main() -> int:
    args = parse_args()
    if args.guild_id <= 0:
        print("--guild-id must be a positive integer.", file=sys.stderr)
        return 2
    if args.channel_id and (not args.file or not str(args.channel_id).isdigit()):
        print("--channel-id requires --file and a numeric ID.", file=sys.stderr)
        return 2
    if args.channel_name and not args.file:
        print("--channel-name requires --file.", file=sys.stderr)
        return 2
    if args.force_activity or not args.skip_activity_for_json_imported:
        print(
            "WARNING: JSON activity coverage protection is disabled; this can "
            "duplicate activity already imported from JSON."
        )

    files = _files(args)
    if not files:
        print("No CSV export files were found.", file=sys.stderr)
        return 1

    json_channel_ids = get_json_activity_imported_channel_ids(
        args.activity_database,
        guild_id=args.guild_id,
    )
    context_connection = _open_context_database(args)
    activity_connection = _open_activity_database(args)
    results: list[CombinedResult] = []

    print(f"Mode: {'dry run' if args.dry_run else 'write'}")
    print(f"CSV files: {len(files):,}")
    print(f"JSON-covered activity channels: {len(json_channel_ids):,}")
    if args.dry_run:
        print("No database writes or file moves will occur.")

    try:
        for path in files:
            channel_id, channel_name = _resolve_channel(path, args)
            result = CombinedResult(path, channel_id, channel_name)
            try:
                if not args.activity_backfill_only:
                    result.context = context_importer.process_file(
                        context_connection,
                        path,
                        _context_args(
                            args,
                            channel_id or "unknown",
                            channel_name,
                        ),
                    )

                run_activity, status, note = _activity_should_run(
                    args,
                    channel_id,
                    json_channel_ids,
                )
                result.activity_status = status
                result.activity_note = note
                if status == "skipped_json_already_imported":
                    print(
                        f"Skipping activity backfill for {channel_name} "
                        f"[{channel_id}] because JSON activity already exists."
                    )
                if run_activity:
                    if activity_connection is None:
                        raise RuntimeError("Activity database is unavailable.")
                    result.activity = activity_importer.process_file(
                        activity_connection,
                        path,
                        _activity_args(args, int(channel_id), channel_name),
                        str(uuid.uuid4()),
                    )
                    if not args.dry_run and result.context is not None:
                        activity_connection.execute(
                            """
                            UPDATE stats_activity_imports
                            SET imported_for_context = 1
                            WHERE id = (
                                SELECT id FROM stats_activity_imports
                                WHERE guild_id = ? AND filename = ?
                                ORDER BY id DESC LIMIT 1
                            )
                            """,
                            (args.guild_id, str(path)),
                        )
                        activity_importer.commit_with_retry(
                            activity_connection,
                            "recording combined context import",
                        )

                if _should_archive(result, args):
                    destination = activity_importer.archive_destination(
                        path,
                        args.archive_folder,
                    )
                    args.archive_folder.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(destination))
                    result.archived = True
            except Exception as exc:
                result.failed = True
                print(
                    f"{path}: failed ({type(exc).__name__}: {exc})",
                    file=sys.stderr,
                )
            results.append(result)
            print_result(result)
    finally:
        if context_connection is not None:
            context_connection.close()
        if activity_connection is not None:
            activity_connection.close()

    print("\nFinal summary")
    print(f"  Files processed: {len(results):,}")
    print(
        "  Context imported: "
        f"{sum(r.context.imported for r in results if r.context):,}"
    )
    print(
        "  Context duplicates: "
        f"{sum(r.context.duplicates for r in results if r.context):,}"
    )
    print(
        "  Activity imported: "
        f"{sum(r.activity.messages_imported for r in results if r.activity):,}"
    )
    print(
        "  Activity skipped for JSON coverage: "
        f"{sum(r.activity_status == 'skipped_json_already_imported' for r in results):,}"
    )
    print(f"  Failed files: {sum(r.failed for r in results):,}")
    return 1 if any(result.failed for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
