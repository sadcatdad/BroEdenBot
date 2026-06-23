"""Database-backed, non-secret runtime settings for BroEdenBot."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_KEY_PARTS = ("TOKEN", "API_KEY", "PASSWORD", "SECRET")
SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    section: str
    value_type: str
    description: str
    default: str = ""
    minimum: Optional[int] = None
    editable: bool = True


SETTING_DEFINITIONS = (
    SettingDefinition(
        "ASK_ALLOWED_CHANNEL_IDS",
        "ask",
        "csv_ids",
        "Comma-separated Discord channel IDs. Spaces will be removed.",
    ),
    SettingDefinition(
        "ASK_COOLDOWN_SECONDS",
        "ask",
        "int",
        "Per-user cooldown for /ask in seconds.",
        default="30",
        minimum=0,
    ),
    SettingDefinition(
        "MODAI_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to use ModAI.",
    ),
    SettingDefinition(
        "STAFF_AI_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to use private staff AI.",
    ),
    SettingDefinition(
        "STAFF_NOTES_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to use staff notes.",
    ),
    SettingDefinition(
        "MESSAGE_CONTEXT_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to use private message context.",
    ),
    SettingDefinition(
        "STATS_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to create and refresh stats.",
    ),
    SettingDefinition(
        "VCSTATS_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to use VC stats and reward previews.",
    ),
    SettingDefinition(
        "BANK_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to use bank commands.",
    ),
    SettingDefinition(
        "BOT_OWNER_USER_IDS",
        "permissions",
        "csv_ids",
        "Users allowed to use owner-only bot controls.",
    ),
    SettingDefinition(
        "VCXP_ENABLED",
        "vcxp",
        "bool",
        "Enable automatic and manual VC XP role pulses.",
        default="false",
    ),
    SettingDefinition(
        "VCXP_TRIGGER_ROLE_ID",
        "vcxp",
        "csv_ids",
        "Discord role ID used for the temporary XP pulse.",
    ),
    SettingDefinition(
        "VCXP_MINUTES_PER_PULSE",
        "vcxp",
        "int",
        "Eligible VC minutes required for one pulse.",
        default="30",
        minimum=1,
    ),
    SettingDefinition(
        "VCXP_ROLE_REMOVE_DELAY_SECONDS",
        "vcxp",
        "int",
        "Seconds before the temporary role is removed.",
        default="30",
        minimum=0,
    ),
    SettingDefinition(
        "VCXP_DAILY_PULSE_CAP",
        "vcxp",
        "int",
        "Maximum pulses per member per UTC day. Zero disables the cap.",
        default="4",
        minimum=0,
    ),
    SettingDefinition(
        "VCXP_WEEKLY_PULSE_CAP",
        "vcxp",
        "int",
        "Maximum pulses per member in seven days. Zero disables the cap.",
        default="20",
        minimum=0,
    ),
    SettingDefinition("GUILD_ID", "models", "string", "Configured Discord guild.", editable=False),
    SettingDefinition("MODAI_MODEL", "models", "string", "Primary ModAI model.", editable=False),
    SettingDefinition(
        "MODAI_FALLBACK_MODEL",
        "models",
        "string",
        "Fallback ModAI model.",
        editable=False,
    ),
    SettingDefinition("ASK_MODEL", "models", "string", "Primary /ask model.", editable=False),
    SettingDefinition(
        "ASK_FALLBACK_MODEL",
        "models",
        "string",
        "Fallback /ask model.",
        editable=False,
    ),
)
DEFINITIONS_BY_KEY = {definition.key: definition for definition in SETTING_DEFINITIONS}
EDITABLE_SETTING_KEYS = {
    definition.key for definition in SETTING_DEFINITIONS if definition.editable
}


def settings_database_path() -> Path:
    configured = os.getenv("DATABASE_PATH", "").strip()
    path = Path(configured).expanduser() if configured else PROJECT_ROOT / "data.db"
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def is_forbidden_key(key: str) -> bool:
    normalized = str(key or "").strip().upper()
    return any(part in normalized for part in FORBIDDEN_KEY_PARTS)


def _connect(*, readonly: bool = False) -> sqlite3.Connection:
    path = settings_database_path()
    if readonly:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    if readonly:
        connection.execute("PRAGMA query_only = ON")
    return connection


def initialize_settings_from_env() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                value_type TEXT NOT NULL DEFAULT 'string',
                description TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_by TEXT,
                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for definition in SETTING_DEFINITIONS:
            if not definition.editable or is_forbidden_key(definition.key):
                continue
            if definition.key not in os.environ:
                continue
            try:
                value = normalize_setting_value(
                    definition.key,
                    os.environ[definition.key],
                )
            except ValueError:
                continue
            connection.execute(
                """
                INSERT OR IGNORE INTO bot_settings (
                    key, value, value_type, description
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    definition.key,
                    value,
                    definition.value_type,
                    definition.description,
                ),
            )
        connection.commit()


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    if is_forbidden_key(key):
        return default
    definition = DEFINITIONS_BY_KEY.get(key)
    if definition and definition.editable:
        try:
            with _connect(readonly=True) as connection:
                row = connection.execute(
                    "SELECT value FROM bot_settings WHERE key = ?",
                    (key,),
                ).fetchone()
            if row is not None:
                return str(row["value"])
        except sqlite3.Error:
            pass
    value = os.getenv(key)
    if value is not None:
        return value
    if definition and definition.default != "":
        return definition.default
    return default


def get_bool_setting(key: str, default: bool = False) -> bool:
    value = get_setting(key)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return default


def get_int_setting(key: str, default: int = 0) -> int:
    value = get_setting(key)
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def get_csv_ids_setting(key: str) -> list[int]:
    value = get_setting(key, "") or ""
    return [int(item) for item in value.split(",") if item.strip().isdigit()]


def normalize_setting_value(key: str, value: str) -> str:
    if is_forbidden_key(key) or key not in EDITABLE_SETTING_KEYS:
        raise ValueError("This setting is not editable.")
    definition = DEFINITIONS_BY_KEY[key]
    text = str(value or "").strip()
    if definition.value_type == "bool":
        normalized = text.casefold()
        if normalized not in {"true", "false"}:
            raise ValueError("Value must be true or false.")
        return normalized
    if definition.value_type == "int":
        try:
            parsed = int(text)
        except ValueError as exc:
            raise ValueError("Value must be an integer.") from exc
        if definition.minimum is not None and parsed < definition.minimum:
            raise ValueError(f"Value must be at least {definition.minimum}.")
        return str(parsed)
    if definition.value_type == "csv_ids":
        if not text:
            return ""
        items = [item.strip() for item in text.split(",")]
        if any(not item or not SNOWFLAKE_RE.fullmatch(item) for item in items):
            raise ValueError(
                "Use comma-separated Discord IDs containing 17 to 20 digits."
            )
        if key == "VCXP_TRIGGER_ROLE_ID" and len(items) != 1:
            raise ValueError("Use one Discord role ID.")
        return ",".join(items)
    return text


def set_setting(key: str, value: str, *, changed_by: str = "system") -> str:
    normalized = normalize_setting_value(key, value)
    definition = DEFINITIONS_BY_KEY[key]
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT value FROM bot_settings WHERE key = ?",
            (key,),
        ).fetchone()
        old_value = str(row["value"]) if row is not None else None
        if old_value == normalized:
            connection.commit()
            return normalized
        connection.execute(
            """
            INSERT INTO bot_settings (
                key, value, value_type, description, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                value_type = excluded.value_type,
                description = excluded.description,
                updated_at = excluded.updated_at
            """,
            (
                key,
                normalized,
                definition.value_type,
                definition.description,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO bot_settings_audit (
                key, old_value, new_value, changed_by, changed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (key, old_value, normalized, changed_by, now),
        )
        connection.commit()
    return normalized


def settings_for_dashboard() -> dict[str, list[dict[str, object]]]:
    sections = {"ask": [], "permissions": [], "vcxp": [], "models": []}
    for definition in SETTING_DEFINITIONS:
        value = get_setting(definition.key, "") or ""
        sections[definition.section].append(
            {
                "key": definition.key,
                "value": value,
                "value_type": definition.value_type,
                "description": definition.description,
                "editable": definition.editable,
            }
        )
    return sections


def recent_setting_changes(limit: int = 10) -> list[dict[str, object]]:
    try:
        with _connect(readonly=True) as connection:
            rows = connection.execute(
                """
                SELECT key, old_value, new_value, changed_by, changed_at
                FROM bot_settings_audit
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]
