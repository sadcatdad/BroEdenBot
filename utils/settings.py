"""Database-backed, non-secret runtime settings for BroEdenBot."""

from __future__ import annotations

import os
import re
import sqlite3
import logging
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.sqlite import configure_sync_connection

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_KEY_PARTS = ("TOKEN", "API_KEY", "PASSWORD", "SECRET")
SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")
_SETTING_CACHE: dict[tuple[str, str], str] = {}
_SETTING_READ_WARNINGS: set[tuple[str, str, str]] = set()


def _warn_setting_read_failure(
    cache_key: tuple[str, str],
    key: str,
    fallback: str,
    exc: sqlite3.Error,
) -> None:
    warning_key = (cache_key[0], key, fallback)
    if warning_key in _SETTING_READ_WARNINGS:
        return
    _SETTING_READ_WARNINGS.add(warning_key)
    logger.warning(
        "Settings database read failed for %s; falling back to %s: %s",
        key,
        fallback,
        type(exc).__name__,
    )


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    section: str
    value_type: str
    description: str
    default: str = ""
    minimum: Optional[int] = None
    editable: bool = True
    title: str = ""
    picker: str = ""
    single: bool = False
    visible: bool = True
    maximum: Optional[int] = None
    placeholders: tuple[str, ...] = ()


SETTING_DEFINITIONS = (
    SettingDefinition(
        "ASK_ALLOWED_CHANNEL_IDS",
        "ask",
        "csv_ids",
        "Comma-separated Discord channel IDs. Spaces will be removed.",
        picker="channel",
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
        picker="role",
    ),
    SettingDefinition(
        "STAFF_AI_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to use private staff AI.",
        picker="role",
    ),
    SettingDefinition(
        "STAFF_NOTES_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to use staff notes.",
        picker="role",
    ),
    SettingDefinition(
        "REMINDER_ALLOWED_ROLE_IDS",
        "reminders",
        "csv_ids",
        "Legacy fallback roles for staff reminder actions. Prefer the command-specific reminder role settings below.",
        picker="role",
    ),
    SettingDefinition(
        "REMINDER_PERSONAL_ALLOWED_ROLE_IDS",
        "reminders",
        "csv_ids",
        "Roles allowed to use /remind personal. Leave blank to allow every server member.",
        picker="role",
    ),
    SettingDefinition(
        "REMINDER_EVENT_ALLOWED_ROLE_IDS",
        "reminders",
        "csv_ids",
        "Roles allowed to use /remind event. Leave blank to use the legacy configured staff roles.",
        picker="role",
    ),
    SettingDefinition(
        "REMINDER_MANAGE_ALLOWED_ROLE_IDS",
        "reminders",
        "csv_ids",
        "Roles allowed to use /remind manage for their own reminders. Leave blank to allow every server member.",
        picker="role",
    ),
    SettingDefinition(
        "REMINDER_MANAGE_ALL_ROLE_IDS",
        "reminders",
        "csv_ids",
        "Roles allowed to manage every reminder in the server. Leave blank to use the legacy configured staff roles.",
        picker="role",
    ),
    SettingDefinition(
        "REMINDER_SUBSCRIPTIONS_ALLOWED_ROLE_IDS",
        "reminders",
        "csv_ids",
        "Roles allowed to subscribe to events and use /remind subscriptions. Leave blank to allow every server member.",
        picker="role",
    ),
    SettingDefinition(
        "REMINDER_TIMEZONE",
        "reminders",
        "string",
        "IANA timezone used for reminder date/time input.",
        default="America/Chicago",
    ),
    SettingDefinition(
        "ENABLE_LEGACY_REMINDER_COMMANDS",
        "reminders",
        "bool",
        "Keep the temporary /reminder and /remind subscribe transition routes.",
        default="true",
    ),
    SettingDefinition(
        "REMINDER_DELIVERY_GRACE_MINUTES",
        "reminders",
        "int",
        "Maximum age in minutes for a missed reminder to be delivered after downtime.",
        default="120",
        minimum=1,
        maximum=1440,
    ),
    SettingDefinition(
        "REMINDER_EVENT_AUTO_SUBSCRIBE_CREATOR",
        "reminders",
        "bool",
        "Automatically subscribe an event creator using the event default timings.",
        default="true",
    ),
    SettingDefinition(
        "EVENTS_HEADER_ASSET_ID",
        "reminders",
        "asset_id",
        "Saved Embed/Message Editor asset shown as the header for /events. Leave blank to use the "
        "built-in Upcoming Events card. Supports {count} (upcoming event count) and {next_event} placeholders.",
        picker="asset",
    ),
    SettingDefinition(
        "MESSAGE_CONTEXT_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to use private message context.",
        picker="role",
    ),
    SettingDefinition(
        "STATS_ALLOWED_ROLE_IDS",
        "stats_features",
        "csv_ids",
        "Roles allowed to create and refresh stats.",
        picker="role",
    ),
    SettingDefinition(
        "STREAK_TIMEZONE",
        "streaks",
        "string",
        "Timezone used for daily message streak boundaries.",
        default="America/Chicago",
    ),
    SettingDefinition(
        "STREAK_MIN_WORDS",
        "streaks",
        "int",
        "Minimum words required for a message to count toward a streak.",
        default="4",
        minimum=4,
    ),
    SettingDefinition(
        "STREAK_DUPLICATE_LOOKBACK_DAYS",
        "streaks",
        "int",
        "Days of qualifying message hashes checked for exact duplicates.",
        default="30",
        minimum=1,
    ),
    SettingDefinition(
        "STREAK_EXCLUDED_CHANNEL_IDS",
        "streaks",
        "csv_ids",
        "Additional channels excluded from activity streaks.",
        picker="channel",
    ),
    SettingDefinition(
        "STREAK_EXCLUDED_CATEGORY_IDS",
        "streaks",
        "csv_ids",
        "Channel categories excluded from activity streaks. Every channel in a selected category is excluded.",
        picker="category",
    ),
    SettingDefinition(
        "STREAK_MILESTONE_CHANNEL_ID",
        "streaks",
        "csv_ids",
        "Optional channel for automatic streak-milestone announcements. Leave blank to keep milestone notices private to each member's streak view.",
        picker="channel",
        single=True,
    ),
    SettingDefinition(
        "STREAK_MILESTONE_MESSAGE",
        "streaks",
        "string",
        "Message used for streak milestones in the configured announcement channel and a member's streak view. Use {member} for the member mention and {days} for the milestone length.",
        default="🎉 Congratulations {member}! You reached a **{days}-day** activity streak!",
        title="Streak Milestone Message",
        picker="emoji_text",
        maximum=2000,
        placeholders=("{member}", "{days}"),
        visible=False,
    ),
    SettingDefinition(
        "STREAK_MILESTONE_ASSET_ID",
        "streaks",
        "asset_id",
        "Saved Embed/Message Editor asset used for streak milestone announcements. Supports {user.feature}, {role.feature}, {member}, and {days} placeholders.",
        picker="asset",
        single=True,
        title="Streak Milestone Message / Embed",
    ),
    SettingDefinition(
        "STREAK_LEADERBOARD_CHANNEL_ID",
        "streaks",
        "csv_ids",
        "Channel used for the persistent weekly streak leaderboard.",
        picker="channel",
        single=True,
    ),
    SettingDefinition(
        "STREAK_RESTORE_ENABLED",
        "streaks",
        "bool",
        "Automatically queue Discord history recovery when a heartbeat gap is detected.",
        default="true",
    ),
    SettingDefinition(
        "STREAK_RESTORE_GAP_MINUTES",
        "streaks",
        "int",
        "Minimum missing-heartbeat gap before automatic streak recovery is queued.",
        default="10",
        minimum=2,
    ),
    SettingDefinition(
        "STREAK_RESTORE_MAX_DAYS",
        "streaks",
        "int",
        "Maximum number of calendar days accepted by one restore request.",
        default="14",
        minimum=1,
    ),
    SettingDefinition(
        "STREAK_RESTORE_MAX_MESSAGES",
        "streaks",
        "int",
        "Maximum Discord messages scanned by one restore request.",
        default="50000",
        minimum=100,
    ),
    SettingDefinition(
        "DISBOARD_BOT_USER_ID",
        "bumps",
        "csv_ids",
        "Official DISBOARD bot user ID trusted for successful bump responses.",
        single=True,
    ),
    SettingDefinition(
        "BUMP_REWARD_ROLE_ID",
        "bumps",
        "csv_ids",
        "Temporary external-automation role granted after a verified bump.",
        picker="role",
        single=True,
    ),
    SettingDefinition(
        "BUMP_SUCCESS_MESSAGE",
        "bumps",
        "string",
        "Authoritative message sent after a verified successful /bump. It replaces the regular message stored in the selected success template. Use {member} for the member who bumped, {points} for the awarded points, and {reward_status} for the reward-role result.",
        default=(
            "Thanks for bumping our server, {member}! You gained:\n"
            "- 💥 + {points} Bump Points\n"
            "{reward_status}\n"
            "A bump reminder will be posted in 2 hours."
        ),
        title="Successful Bump Response Message",
        picker="emoji_text",
        maximum=2000,
        placeholders=("{member}", "{points}", "{reward_status}"),
        visible=False,
    ),
    SettingDefinition(
        "BUMP_SUCCESS_EMBED_ID",
        "bumps",
        "embed_id",
        "Saved embed card and buttons used after a verified successful /bump. Its regular message is ignored; the success message setting supplies the text. The first four template buttons are used and the bump feature adds Bump Leaderboard.",
        picker="embed",
        single=True,
        title="Successful Bump Response Embed",
        visible=False,
    ),
    SettingDefinition(
        "BUMP_SUCCESS_ASSET_ID",
        "bumps",
        "asset_id",
        "Saved Embed/Message Editor asset sent after a verified /bump. Supports {user.feature}, {role.feature}, {member}, {points}, and {reward_status} placeholders.",
        picker="asset",
        single=True,
        title="Successful Bump Response",
    ),
    SettingDefinition(
        "BUMP_PING_ROLE_ID",
        "bumps",
        "csv_ids",
        "Role pinged with opted-in two-hour bump reminders.",
        picker="role",
        single=True,
    ),
    SettingDefinition(
        "BUMP_REMINDER_MESSAGE",
        "bumps",
        "string",
        "Authoritative bump reminder message. It replaces the regular message stored in the selected Embed Editor template. Use {role} for the reminder role and {member} for the member whose bump scheduled the reminder.",
        default="{role}",
        title="Bump Reminder Message",
        picker="emoji_text",
        maximum=2000,
        placeholders=("{role}", "{member}"),
        visible=False,
    ),
    SettingDefinition(
        "BUMP_REMINDER_EMBED_ID",
        "bumps",
        "embed_id",
        "Saved embed card design used for the two-hour reminder. Its regular message and buttons are ignored; the bump feature supplies both.",
        picker="embed",
        single=True,
        title="Bump Reminder Embed",
        visible=False,
    ),
    SettingDefinition(
        "BUMP_REMINDER_ASSET_ID",
        "bumps",
        "asset_id",
        "Saved Embed/Message Editor asset sent for the two-hour bump reminder. Supports {user.feature}, {role.feature}, {member}, and {role} placeholders.",
        picker="asset",
        single=True,
        title="Bump Reminder Message / Embed",
    ),
    SettingDefinition(
        "BUMP_LEADERBOARD_CHANNEL_ID",
        "bumps",
        "csv_ids",
        "Channel that receives the Bump Legends leaderboard every seven days.",
        picker="channel",
        single=True,
    ),
    SettingDefinition(
        "BUMP_POINTS_PER_SUCCESS",
        "bumps",
        "int",
        "Points awarded for each verified successful DISBOARD bump.",
        default="1000",
        minimum=1,
    ),
    SettingDefinition(
        "ACTIVITY_EXCLUDED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles excluded from message activity stats.",
        picker="role",
    ),
    SettingDefinition(
        "ACTIVITY_EXCLUDED_USER_IDS",
        "permissions",
        "csv_ids",
        "Users excluded from message activity stats.",
    ),
    SettingDefinition(
        "VCSTATS_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to use VC stats and reward previews.",
        picker="role",
    ),
    SettingDefinition(
        "VC_EXCLUDED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles excluded from VC stats and rewards.",
        picker="role",
    ),
    SettingDefinition(
        "VC_EXCLUDED_USER_IDS",
        "permissions",
        "csv_ids",
        "Users excluded from VC stats and rewards.",
    ),
    SettingDefinition(
        "EXCLUDED_VOICE_CHANNEL_IDS",
        "permissions",
        "csv_ids",
        "Voice channel IDs excluded from dashboard voice analytics.",
        picker="channel",
    ),
    SettingDefinition(
        "BANK_ALLOWED_ROLE_IDS",
        "permissions",
        "csv_ids",
        "Roles allowed to use bank commands.",
        picker="role",
    ),
    SettingDefinition(
        "BANK_LOG_CHANNEL_ID",
        "permissions",
        "csv_ids",
        "Deprecated placeholder for bank logs. The current bank implementation does not read this value.",
        picker="channel",
        single=True,
        visible=False,
    ),
    SettingDefinition(
        "BOT_OWNER_USER_IDS",
        "permissions",
        "csv_ids",
        "Users allowed to use owner-only bot controls.",
    ),
    SettingDefinition(
        "LEADERBOARD_RESET_ROLE_IDS",
        "stats_features",
        "csv_ids",
        "Additional roles allowed to reset leaderboard points.",
        picker="role",
    ),
    SettingDefinition(
        "AUDIT_LOG_THREAD_ID",
        "permissions",
        "csv_ids",
        "Existing Discord thread used for selected audit events.",
        picker="channel",
        single=True,
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
        picker="role",
        single=True,
    ),
    SettingDefinition(
        "VCXP_EXCLUDED_ROLE_IDS",
        "vcxp",
        "csv_ids",
        "Roles excluded from VC XP pulses while remaining visible in VC stats.",
        picker="role",
    ),
    SettingDefinition(
        "VCXP_EXCLUDED_VOICE_CHANNEL_IDS",
        "vcxp",
        "csv_ids",
        "Voice channels that do not count toward VC XP pulses.",
        picker="channel",
    ),
    SettingDefinition(
        "VCXP_REWARD_START_AT",
        "vcxp",
        "datetime",
        "Earliest completed VC session timestamp that can earn VC XP.",
    ),
    SettingDefinition(
        "VC_XP_PULSE_MINUTES",
        "vcxp",
        "int",
        "Eligible VC minutes required for one pulse.",
        default="30",
        minimum=1,
    ),
    SettingDefinition(
        "VCXP_MINUTES_PER_PULSE",
        "vcxp",
        "int",
        "Legacy VC XP pulse interval setting. Use VC_XP_PULSE_MINUTES.",
        default="30",
        minimum=1,
        visible=False,
    ),
    SettingDefinition(
        "VCXP_ROLE_REMOVE_DELAY_SECONDS",
        "vcxp",
        "int",
        "Legacy setting. BroEdenBot no longer removes the VC XP pulse role.",
        default="30",
        minimum=0,
        visible=False,
    ),
    SettingDefinition(
        "VCXP_DAILY_PULSE_CAP",
        "vcxp",
        "int",
        "Legacy daily pulse cap. Eligible-time cooldown now controls pulse cadence.",
        default="4",
        minimum=0,
        visible=False,
    ),
    SettingDefinition(
        "VCXP_WEEKLY_PULSE_CAP",
        "vcxp",
        "int",
        "Legacy weekly pulse cap. Eligible-time cooldown now controls pulse cadence.",
        default="20",
        minimum=0,
        visible=False,
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
    SettingDefinition(
        "admin_role_ids",
        "dashboard_json",
        "json_ids",
        "Dashboard-managed Discord role IDs treated as administrators.",
        title="Admin Roles",
        picker="role",
        visible=False,
    ),
    SettingDefinition(
        "staff_role_ids",
        "dashboard_json",
        "json_ids",
        "Dashboard-managed Discord role IDs treated as staff.",
        title="Staff Roles",
        picker="role",
        visible=False,
    ),
    SettingDefinition(
        "bot_role_ids_excluded_from_stats",
        "dashboard_json",
        "json_ids",
        "Bot role IDs excluded from analytics and stats calculations.",
        title="Excluded Bot Roles",
        picker="role",
        visible=False,
    ),
    SettingDefinition(
        "analytics_excluded_channel_ids",
        "dashboard_json",
        "json_ids",
        "Channel IDs excluded from analytics.",
        title="Analytics Excluded Channels",
        picker="channel",
    ),
    SettingDefinition(
        "analytics_excluded_category_ids",
        "dashboard_json",
        "json_ids",
        "Category IDs whose child channels are excluded from analytics.",
        title="Analytics Excluded Categories",
        picker="category",
    ),
    SettingDefinition(
        "bank_allowed_role_ids",
        "dashboard_json",
        "json_ids",
        "Dashboard-managed roles allowed to use bank tools.",
        title="Bank Allowed Roles",
        picker="role",
        visible=False,
    ),
    SettingDefinition(
        "bank_log_channel_id",
        "dashboard_json",
        "json_ids",
        "Dashboard-managed bank log channel ID.",
        title="Bank Log Channel",
        picker="channel",
        single=True,
        visible=False,
    ),
    SettingDefinition(
        "message_context_excluded_channel_ids",
        "dashboard_json",
        "json_ids",
        "Channels excluded from /context results (e.g. staff-only channels "
        "that lower-level staff should not see surfaced).",
        title="Context Excluded Channels",
        picker="channel",
    ),
    SettingDefinition(
        "knowledge_allowed_channel_ids",
        "dashboard_json",
        "json_ids",
        "Channels allowed to use knowledge features.",
        title="Knowledge Allowed Channels",
        picker="channel",
    ),
    SettingDefinition(
        "knowledge_allowed_category_ids",
        "dashboard_json",
        "json_ids",
        "Categories whose child channels are allowed to use knowledge features.",
        title="Knowledge Allowed Categories",
        picker="category",
    ),
    SettingDefinition(
        "ask_command_allowed_channel_ids",
        "dashboard_json",
        "json_ids",
        "Channels allowed to use /ask.",
        title="/ask Allowed Channels",
        picker="channel",
        visible=False,
    ),
    SettingDefinition(
        "ask_command_allowed_category_ids",
        "dashboard_json",
        "json_ids",
        "Categories whose child channels are allowed to use /ask.",
        title="/ask Allowed Categories",
        picker="category",
        visible=False,
    ),
    SettingDefinition(
        "import_archive_path",
        "advanced",
        "string",
        "Default local archive path for completed imports.",
        title="Import Archive Path",
    ),
    SettingDefinition(
        "import_context_only_default",
        "advanced",
        "bool",
        "Default import mode for context-only imports.",
        default="false",
        title="Context-only Import Default",
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
    return configure_sync_connection(connection, readonly=readonly)


def initialize_settings_from_env() -> None:
    with closing(_connect()) as connection:
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
        cache_key = (str(settings_database_path()), key)
        try:
            with closing(_connect(readonly=True)) as connection:
                row = connection.execute(
                    "SELECT value FROM bot_settings WHERE key = ?",
                    (key,),
                ).fetchone()
            if row is not None:
                value = str(row["value"])
                _SETTING_CACHE[cache_key] = value
                return value
            _SETTING_CACHE.pop(cache_key, None)
        except sqlite3.Error as exc:
            cached = _SETTING_CACHE.get(cache_key)
            if cached is not None:
                _warn_setting_read_failure(cache_key, key, "cached value", exc)
                return cached
            _warn_setting_read_failure(cache_key, key, "environment", exc)
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


def get_json_ids_setting(key: str) -> list[int]:
    """Read a ``json_ids`` (or legacy CSV) setting as a list of ints."""
    text = str(get_setting(key, "") or "").strip()
    if not text:
        return []
    if text.startswith("["):
        import json

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return [int(item) for item in parsed if str(item).strip().isdigit()]
    return [int(item) for item in text.split(",") if item.strip().isdigit()]


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
    if definition.value_type in {"embed_id", "asset_id"}:
        if not text:
            return ""
        if not text.isdigit():
            raise ValueError("Choose a saved Embed/Message Editor asset.")
        return text
    if definition.value_type == "datetime":
        if not text:
            return ""
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Use an ISO 8601 date/time.") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
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
    if definition.value_type == "json_ids":
        if not text:
            return "[]"
        raw_items = text.split(",")
        if text.lstrip().startswith("["):
            import json

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError("Value must be a JSON array of Discord IDs.") from exc
            if not isinstance(parsed, list):
                raise ValueError("Value must be a JSON array of Discord IDs.")
            raw_items = [str(item) for item in parsed]
        items = [item.strip() for item in raw_items]
        if any(not item or not SNOWFLAKE_RE.fullmatch(item) for item in items):
            raise ValueError("Use Discord IDs containing 17 to 20 digits.")
        import json

        return json.dumps(items)
    if definition.maximum is not None and len(text) > definition.maximum:
        raise ValueError(f"Value cannot exceed {definition.maximum:,} characters.")
    return text


def set_setting(key: str, value: str, *, changed_by: str = "system") -> str:
    normalized = normalize_setting_value(key, value)
    definition = DEFINITIONS_BY_KEY[key]
    now = datetime.now(timezone.utc).isoformat()
    with closing(_connect()) as connection:
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
    _SETTING_CACHE[(str(settings_database_path()), key)] = normalized
    return normalized


def set_settings(values: dict[str, str], *, changed_by: str = "system") -> dict[str, str]:
    """Validate and save a group of settings in one SQLite transaction."""
    normalized_values = {
        str(key): normalize_setting_value(str(key), value)
        for key, value in values.items()
    }
    now = datetime.now(timezone.utc).isoformat()
    changed: dict[str, str] = {}
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for key, normalized in normalized_values.items():
            definition = DEFINITIONS_BY_KEY[key]
            row = connection.execute(
                "SELECT value FROM bot_settings WHERE key = ?", (key,)
            ).fetchone()
            old_value = str(row["value"]) if row is not None else None
            if old_value == normalized:
                continue
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
                (key, normalized, definition.value_type, definition.description, now),
            )
            connection.execute(
                """
                INSERT INTO bot_settings_audit (
                    key, old_value, new_value, changed_by, changed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (key, old_value, normalized, changed_by, now),
            )
            changed[key] = normalized
        connection.commit()
    database_key = str(settings_database_path())
    for key, value in normalized_values.items():
        _SETTING_CACHE[(database_key, key)] = value
    return changed


def setting_source(key: str) -> str:
    """Return the effective configuration source without exposing its value."""
    definition = DEFINITIONS_BY_KEY.get(key)
    if definition and definition.editable:
        try:
            with closing(_connect(readonly=True)) as connection:
                row = connection.execute(
                    "SELECT 1 FROM bot_settings WHERE key = ?", (key,)
                ).fetchone()
            if row is not None:
                return "Database"
        except sqlite3.Error:
            pass
    if key in os.environ:
        return "Environment"
    if definition and definition.default != "":
        return "Default"
    return "Not configured"


def _setting_title(definition: SettingDefinition) -> str:
    if definition.title:
        return definition.title
    title = definition.key.replace("_", " ").title()
    replacements = {
        "Ai": "AI", "Api": "API", "Csv": "CSV", "Db": "DB",
        "Discord Id": "Discord ID", "Embed Id": "Embed ID", "Guild Id": "Guild ID",
        "Ids": "IDs", "Json": "JSON", "Modai": "ModAI", "Url": "URL",
        "Vc ": "VC ", "Vcxp": "VC XP", "Xp": "XP",
    }
    for old, new in replacements.items():
        title = title.replace(old, new)
    return title


def settings_for_dashboard() -> dict[str, list[dict[str, object]]]:
    sections = {
        "ask": [],
        "permissions": [],
        "vcxp": [],
        "models": [],
        "dashboard_json": [],
        "advanced": [],
        "bumps": [],
        "reminders": [],
        "streaks": [],
        "stats_features": [],
    }
    for definition in SETTING_DEFINITIONS:
        if not definition.visible:
            continue
        value = get_setting(definition.key, "") or ""
        if not value and definition.key == "BUMP_SUCCESS_ASSET_ID":
            value = get_setting("BUMP_SUCCESS_EMBED_ID", "") or ""
        elif not value and definition.key == "BUMP_REMINDER_ASSET_ID":
            value = get_setting("BUMP_REMINDER_EMBED_ID", "") or ""
        value_format = "json" if definition.value_type == "json_ids" else "csv"
        from dashboard.features import feature_key_for_setting

        source = setting_source(definition.key)
        sections[definition.section].append(
            {
                "key": definition.key,
                "value": value,
                "value_type": definition.value_type,
                "description": definition.description,
                "editable": definition.editable,
                "title": _setting_title(definition),
                "picker": definition.picker,
                "single": definition.single,
                "value_format": value_format,
                "maximum": definition.maximum,
                "placeholders": definition.placeholders,
                "source": source,
                "source_note": (
                    "Stored in SQLite and used at runtime."
                    if source == "Database"
                    else "Currently supplied by the process environment. Saving here creates a database override."
                    if source == "Environment"
                    else "Using the built-in safe default."
                    if source == "Default"
                    else "No value is currently configured."
                ),
                "feature_key": feature_key_for_setting(definition.key),
            }
        )
    return sections


def settings_for_feature(feature_key: str) -> list[dict[str, object]]:
    return [
        setting
        for section in settings_for_dashboard().values()
        for setting in section
        if setting.get("feature_key") == feature_key
    ]


def recent_setting_changes(limit: int = 10) -> list[dict[str, object]]:
    try:
        with closing(_connect(readonly=True)) as connection:
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
