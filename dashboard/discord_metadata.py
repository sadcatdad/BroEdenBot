from __future__ import annotations

import json
from typing import Any

from utils.discord_metadata import (
    categories_snapshot,
    channels_snapshot,
    guild_structure_snapshot,
    metadata_status,
    queue_discord_metadata_refresh,
    roles_snapshot,
)
from utils.settings import get_setting


def roles_metadata() -> list[dict[str, Any]]:
    return roles_snapshot()


def channels_metadata() -> list[dict[str, Any]]:
    return channels_snapshot()


def categories_metadata() -> list[dict[str, Any]]:
    return categories_snapshot()


def guild_structure() -> dict[str, Any]:
    return guild_structure_snapshot()


def discord_metadata_status() -> dict[str, Any]:
    return metadata_status()


def queue_metadata_refresh(requested_by: str = "dashboard") -> int:
    return queue_discord_metadata_refresh(requested_by)


def selected_ids_for_setting(key: str) -> list[str]:
    return _json_or_csv_ids(get_setting(key, "") or "")


def missing_saved_ids(key: str, objects: list[dict[str, Any]]) -> list[str]:
    known = {str(item["id"]) for item in objects}
    return [item for item in selected_ids_for_setting(key) if item not in known]


def picker_metadata() -> dict[str, Any]:
    roles = roles_metadata()
    channels = channels_metadata()
    categories = categories_metadata()
    role_keys = (
        "admin_role_ids",
        "staff_role_ids",
        "bot_role_ids_excluded_from_stats",
        "bank_allowed_role_ids",
        "VCXP_EXCLUDED_ROLE_IDS",
    )
    channel_keys = (
        "analytics_excluded_channel_ids",
        "knowledge_allowed_channel_ids",
        "ask_command_allowed_channel_ids",
        "bank_log_channel_id",
    )
    category_keys = (
        "analytics_excluded_category_ids",
        "knowledge_allowed_category_ids",
        "ask_command_allowed_category_ids",
    )
    return {
        "status": discord_metadata_status(),
        "missing": {
            **{key: missing_saved_ids(key, roles) for key in role_keys},
            **{key: missing_saved_ids(key, channels) for key in channel_keys},
            **{key: missing_saved_ids(key, categories) for key in category_keys},
        },
    }


def channel_matches_selection(
    channel_id: str | int,
    parent_category_id: str | int | None,
    *,
    channel_ids: list[str],
    category_ids: list[str],
) -> bool:
    channel = str(channel_id or "").strip()
    parent = str(parent_category_id or "").strip()
    return bool(
        (channel and channel in {str(item) for item in channel_ids})
        or (parent and parent in {str(item) for item in category_ids})
    )


def _json_or_csv_ids(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = []
        return [str(item).strip() for item in parsed if str(item).strip().isdigit()]
    return [item.strip() for item in text.split(",") if item.strip().isdigit()]
