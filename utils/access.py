"""Shared runtime checks for configured Bro Eden owners and staff."""

from __future__ import annotations

import json
from pathlib import Path

from utils.settings import get_csv_ids_setting, get_json_ids_setting

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "riffbot_config.json"


def _configured_ids(env_key: str, config_key: str) -> set[int]:
    ids = set(get_csv_ids_setting(env_key))
    ids.update(get_json_ids_setting(config_key))
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ids
    values = data.get(config_key, [])
    if not isinstance(values, list):
        values = [values]
    ids.update(
        int(value) for value in values if str(value).strip().isdigit()
    )
    return ids


def configured_owner_user_ids() -> set[int]:
    return _configured_ids("BOT_OWNER_USER_IDS", "owner_user_ids")


def configured_admin_role_ids() -> set[int]:
    return _configured_ids("ADMIN_ROLE_IDS", "admin_role_ids")


def configured_staff_role_ids() -> set[int]:
    role_ids: set[int] = set()
    for env_key, config_key in (
        ("OWNER_ROLE_IDS", "owner_role_ids"),
        ("ADMIN_ROLE_IDS", "admin_role_ids"),
        ("MODERATOR_ROLE_IDS", "moderator_role_ids"),
        ("STAFF_ROLE_IDS", "staff_role_ids"),
    ):
        role_ids.update(_configured_ids(env_key, config_key))
    return role_ids


def is_configured_owner(user: object) -> bool:
    return getattr(user, "id", None) in configured_owner_user_ids()


def is_configured_staff(user: object) -> bool:
    if is_configured_owner(user):
        return True
    permissions = getattr(user, "guild_permissions", None)
    if permissions and getattr(permissions, "administrator", False):
        return True
    allowed_roles = configured_staff_role_ids()
    return any(
        getattr(role, "id", None) in allowed_roles
        for role in getattr(user, "roles", ())
    )
