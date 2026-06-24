from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from dashboard.db import find_database_path, readonly_connection, table_columns, table_names
from utils.settings import get_setting


ROLE_SETTING_KEYS = (
    "MODAI_ALLOWED_ROLE_IDS",
    "STAFF_AI_ALLOWED_ROLE_IDS",
    "STAFF_NOTES_ALLOWED_ROLE_IDS",
    "MESSAGE_CONTEXT_ALLOWED_ROLE_IDS",
    "STATS_ALLOWED_ROLE_IDS",
    "VCSTATS_ALLOWED_ROLE_IDS",
    "BANK_ALLOWED_ROLE_IDS",
    "BOT_OWNER_USER_IDS",
    "admin_role_ids",
    "staff_role_ids",
    "bank_allowed_role_ids",
    "bot_role_ids_excluded_from_stats",
)


def _snowflake(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.isdigit() and 17 <= len(text) <= 20 else None


def _add_object(
    objects: dict[str, dict[str, Any]],
    object_id: Any,
    *,
    name: str | None = None,
    object_type: str = "unknown",
    parent_id: Any = None,
    position: Any = None,
) -> None:
    snowflake = _snowflake(object_id)
    if not snowflake:
        return
    existing = objects.setdefault(
        snowflake,
        {
            "id": snowflake,
            "name": name or None,
            "type": object_type,
            "parent_id": _snowflake(parent_id),
            "position": _int_or_none(position),
        },
    )
    if name and not existing.get("name"):
        existing["name"] = str(name)
    if object_type != "unknown" and existing.get("type") == "unknown":
        existing["type"] = object_type
    if parent_id and not existing.get("parent_id"):
        existing["parent_id"] = _snowflake(parent_id)
    if position is not None and existing.get("position") is None:
        existing["position"] = _int_or_none(position)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_or_csv_ids(value: str | None) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = []
        return [item for item in (_snowflake(item) for item in parsed) if item]
    return [item for item in (_snowflake(part) for part in text.split(",")) if item]


def _read_snapshot_table(
    path: Path,
    table: str,
    *,
    object_type: str,
) -> dict[str, dict[str, Any]]:
    objects: dict[str, dict[str, Any]] = {}
    try:
        with readonly_connection(path) as connection:
            if table not in table_names(connection):
                return objects
            columns = table_columns(connection, table)
            id_column = _first_column(columns, ("id", "role_id", "channel_id", "category_id"))
            if not id_column:
                return objects
            selected = [
                column
                for column in (
                    id_column,
                    "name",
                    "type",
                    "parent_id",
                    "parent_category_id",
                    "category_id",
                    "position",
                    "sort_position",
                )
                if column in columns
            ]
            for row in connection.execute(
                f'SELECT {", ".join(_quote(column) for column in selected)} FROM "{table}"'
            ):
                parent = (
                    row["parent_id"]
                    if "parent_id" in row.keys()
                    else row["parent_category_id"]
                    if "parent_category_id" in row.keys()
                    else row["category_id"]
                    if "category_id" in row.keys()
                    else None
                )
                position = (
                    row["position"]
                    if "position" in row.keys()
                    else row["sort_position"]
                    if "sort_position" in row.keys()
                    else None
                )
                _add_object(
                    objects,
                    row[id_column],
                    name=row["name"] if "name" in row.keys() else None,
                    object_type=(
                        row["type"] if "type" in row.keys() and row["type"] else object_type
                    ),
                    parent_id=parent,
                    position=position,
                )
    except (OSError, sqlite3.Error):
        return {}
    return objects


def _first_column(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    return next((column for column in candidates if column in columns), None)


def _quote(column: str) -> str:
    return f'"{column}"'


def roles_metadata() -> list[dict[str, Any]]:
    path = find_database_path()
    roles = _read_snapshot_table(path, "dashboard_discord_roles", object_type="role")
    try:
        with readonly_connection(path) as connection:
            tables = table_names(connection)
            if "role_stat_embeds" in tables:
                columns = table_columns(connection, "role_stat_embeds")
                for column in (
                    "role_id",
                    "role_1_id",
                    "role_2_id",
                    "has_role_id",
                    "missing_role_id",
                ):
                    if column in columns:
                        for row in connection.execute(
                            f'SELECT DISTINCT "{column}" AS id FROM role_stat_embeds'
                        ):
                            _add_object(roles, row["id"], object_type="role")
    except (OSError, sqlite3.Error):
        pass

    for key in ROLE_SETTING_KEYS:
        for role_id in _json_or_csv_ids(get_setting(key, "")):
            _add_object(roles, role_id, object_type="role")
    return _sorted_objects(roles)


def channels_metadata() -> list[dict[str, Any]]:
    path = find_database_path()
    channels = _read_snapshot_table(path, "dashboard_discord_channels", object_type="text")
    try:
        with readonly_connection(path) as connection:
            tables = table_names(connection)
            if "stats_message_activity" in tables:
                columns = table_columns(connection, "stats_message_activity")
                if {"channel_id", "channel_name"}.issubset(columns):
                    for row in connection.execute(
                        """
                        SELECT channel_id AS id, MAX(channel_name) AS name
                        FROM stats_message_activity
                        WHERE channel_id IS NOT NULL
                        GROUP BY channel_id
                        """
                    ):
                        _add_object(
                            channels,
                            row["id"],
                            name=row["name"],
                            object_type="text",
                        )
            for table, id_column, name_column in (
                ("vc_sessions", "channel_id", "channel_name"),
                ("vc_imported_sessions", "voice_channel_id", "voice_channel_name"),
            ):
                if table in tables:
                    columns = table_columns(connection, table)
                    if {id_column, name_column}.issubset(columns):
                        for row in connection.execute(
                            f"""
                            SELECT "{id_column}" AS id, MAX("{name_column}") AS name
                            FROM "{table}"
                            WHERE "{id_column}" IS NOT NULL
                            GROUP BY "{id_column}"
                            """
                        ):
                            _add_object(
                                channels,
                                row["id"],
                                name=row["name"],
                                object_type="voice",
                            )
    except (OSError, sqlite3.Error):
        pass

    for key in (
        "ASK_ALLOWED_CHANNEL_IDS",
        "EXCLUDED_VOICE_CHANNEL_IDS",
        "analytics_excluded_channel_ids",
        "knowledge_allowed_channel_ids",
        "ask_command_allowed_channel_ids",
        "bank_log_channel_id",
    ):
        for channel_id in _json_or_csv_ids(get_setting(key, "")):
            _add_object(channels, channel_id, object_type="unknown")
    return _sorted_objects(channels)


def categories_metadata() -> list[dict[str, Any]]:
    path = find_database_path()
    categories = _read_snapshot_table(
        path,
        "dashboard_discord_categories",
        object_type="category",
    )
    for category_id in _json_or_csv_ids(get_setting("analytics_excluded_category_ids", "")):
        _add_object(categories, category_id, object_type="category")
    return _sorted_objects(categories)


def guild_structure() -> dict[str, list[dict[str, Any]]]:
    return {
        "roles": roles_metadata(),
        "categories": categories_metadata(),
        "channels": channels_metadata(),
    }


def object_exists(object_id: str, objects: list[dict[str, Any]]) -> bool:
    return str(object_id) in {str(item["id"]) for item in objects}


def channel_matches_selection(
    channel_id: str | int,
    parent_category_id: str | int | None,
    *,
    channel_ids: list[str],
    category_ids: list[str],
) -> bool:
    channel = _snowflake(channel_id)
    parent = _snowflake(parent_category_id)
    return bool(
        (channel and channel in {str(item) for item in channel_ids})
        or (parent and parent in {str(item) for item in category_ids})
    )


def _sorted_objects(objects: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    for item in objects.values():
        if not item.get("name"):
            item["name"] = f"Missing: {item['id']}"
            item["missing"] = True
        else:
            item["missing"] = False
    return sorted(
        objects.values(),
        key=lambda item: (
            item.get("position") if item.get("position") is not None else 999999,
            str(item.get("name") or "").casefold(),
            str(item["id"]),
        ),
    )
