"""SQLite repository, resolver, drafts, versions, variants, and schedules."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from utils.settings import settings_database_path
from utils.sqlite import AutoClosingSQLiteConnection, configure_sync_connection

from .registry import REGISTRY, TemplateDefinition


SCHEMA_VERSION = 2
VERSION_RETENTION = 20
_CACHE_TTL_SECONDS = 15.0
_cache_lock = threading.Lock()
_resolution_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


SCHEMA = """
CREATE TABLE IF NOT EXISTS visual_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    original_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    width INTEGER NOT NULL CHECK(width > 0),
    height INTEGER NOT NULL CHECK(height > 0),
    aspect_ratio TEXT NOT NULL,
    file_size INTEGER NOT NULL CHECK(file_size > 0),
    checksum TEXT NOT NULL,
    uploaded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_visual_assets_type_archived
ON visual_assets(asset_type, archived_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_visual_assets_checksum
ON visual_assets(checksum);

CREATE TABLE IF NOT EXISTS visual_asset_discord_storage (
    asset_id INTEGER PRIMARY KEY REFERENCES visual_assets(id) ON DELETE CASCADE,
    storage_thread_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    attachment_url TEXT NOT NULL,
    sync_status TEXT NOT NULL DEFAULT 'ready',
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visual_asset_storage_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('upload', 'delete')),
    idempotency_key TEXT NOT NULL UNIQUE,
    requested_by TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'processing', 'completed', 'failed', 'superseded')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    storage_thread_id TEXT,
    message_id TEXT,
    attachment_url TEXT,
    result_message TEXT,
    failure_reason TEXT,
    requested_at TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_visual_asset_storage_jobs_pending
ON visual_asset_storage_jobs(status, id);

CREATE TABLE IF NOT EXISTS visual_themes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0, 1)),
    is_builtin INTEGER NOT NULL DEFAULT 0 CHECK(is_builtin IN (0, 1)),
    created_by TEXT,
    updated_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_visual_themes_one_default
ON visual_themes(is_default) WHERE is_default = 1 AND archived_at IS NULL;

CREATE TABLE IF NOT EXISTS visual_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    theme_id INTEGER REFERENCES visual_themes(id) ON DELETE SET NULL,
    published_settings_json TEXT NOT NULL DEFAULT '{}',
    draft_settings_json TEXT,
    draft_theme_id INTEGER REFERENCES visual_themes(id) ON DELETE SET NULL,
    published_version INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    updated_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_render_error TEXT,
    FOREIGN KEY(theme_id) REFERENCES visual_themes(id)
);
CREATE INDEX IF NOT EXISTS idx_visual_templates_theme ON visual_templates(theme_id);

CREATE TABLE IF NOT EXISTS visual_template_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES visual_templates(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    settings_json TEXT NOT NULL,
    theme_id INTEGER REFERENCES visual_themes(id) ON DELETE SET NULL,
    change_summary TEXT NOT NULL DEFAULT '',
    preview_storage_key TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(template_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_visual_versions_template
ON visual_template_versions(template_id, version_number DESC);

CREATE TABLE IF NOT EXISTS visual_template_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES visual_templates(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    width INTEGER,
    height INTEGER,
    theme_id INTEGER REFERENCES visual_themes(id) ON DELETE SET NULL,
    settings_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0, 1)),
    created_by TEXT,
    updated_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(template_id, name COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS visual_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER REFERENCES visual_templates(id) ON DELETE CASCADE,
    theme_id INTEGER NOT NULL REFERENCES visual_themes(id) ON DELETE RESTRICT,
    variant_id INTEGER REFERENCES visual_template_variants(id) ON DELETE SET NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'America/Chicago',
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(ends_at > starts_at)
);
CREATE INDEX IF NOT EXISTS idx_visual_schedules_active
ON visual_schedules(enabled, starts_at, ends_at, priority DESC);

CREATE TABLE IF NOT EXISTS visual_asset_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES visual_assets(id) ON DELETE RESTRICT,
    template_id INTEGER REFERENCES visual_templates(id) ON DELETE CASCADE,
    theme_id INTEGER REFERENCES visual_themes(id) ON DELETE CASCADE,
    variant_id INTEGER REFERENCES visual_template_variants(id) ON DELETE CASCADE,
    global_settings_id INTEGER REFERENCES visual_global_settings(id) ON DELETE CASCADE,
    usage_slot TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(template_id IS NOT NULL OR theme_id IS NOT NULL OR variant_id IS NOT NULL OR global_settings_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_visual_asset_usage_asset
ON visual_asset_usage(asset_id);

CREATE TABLE IF NOT EXISTS visual_global_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    settings_json TEXT NOT NULL DEFAULT '{}',
    draft_settings_json TEXT,
    updated_by TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visual_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT,
    summary TEXT NOT NULL,
    actor TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_visual_audit_created
ON visual_audit_log(created_at DESC);

CREATE TABLE IF NOT EXISTS visual_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    database = settings_database_path() if path is None else path
    connection = sqlite3.connect(
        database,
        timeout=30,
        factory=AutoClosingSQLiteConnection,
    )
    configured = configure_sync_connection(connection)
    configured.execute("PRAGMA foreign_keys = ON")
    return configured


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load_json(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _deep_merge(*sources: Mapping[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for source in sources:
        for key, value in source.items():
            if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
                result[key] = _deep_merge(result[key], value)  # type: ignore[arg-type]
            else:
                result[key] = value
    return result


def invalidate_visual_cache(template_key: Optional[str] = None) -> None:
    with _cache_lock:
        if template_key is None:
            _resolution_cache.clear()
        else:
            prefix = "{}:".format(template_key)
            for cache_key in tuple(_resolution_cache):
                if cache_key.startswith(prefix):
                    _resolution_cache.pop(cache_key, None)


def initialize_visual_studio_schema(database_path: Optional[str] = None) -> None:
    now = utcnow()
    with _connect(database_path) as connection:
        connection.executescript(SCHEMA)
        global_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(visual_global_settings)"
            ).fetchall()
        }
        if "draft_settings_json" not in global_columns:
            connection.execute(
                "ALTER TABLE visual_global_settings ADD COLUMN draft_settings_json TEXT"
            )
        usage_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(visual_asset_usage)"
            ).fetchall()
        }
        if "global_settings_id" not in usage_columns:
            connection.executescript(
                """
                CREATE TABLE visual_asset_usage_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id INTEGER NOT NULL REFERENCES visual_assets(id) ON DELETE RESTRICT,
                    template_id INTEGER REFERENCES visual_templates(id) ON DELETE CASCADE,
                    theme_id INTEGER REFERENCES visual_themes(id) ON DELETE CASCADE,
                    variant_id INTEGER REFERENCES visual_template_variants(id) ON DELETE CASCADE,
                    global_settings_id INTEGER REFERENCES visual_global_settings(id) ON DELETE CASCADE,
                    usage_slot TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    CHECK(template_id IS NOT NULL OR theme_id IS NOT NULL OR variant_id IS NOT NULL OR global_settings_id IS NOT NULL)
                );
                INSERT INTO visual_asset_usage_v2(
                    id, asset_id, template_id, theme_id, variant_id,
                    usage_slot, created_at
                )
                SELECT id, asset_id, template_id, theme_id, variant_id,
                       usage_slot, created_at
                FROM visual_asset_usage;
                DROP TABLE visual_asset_usage;
                ALTER TABLE visual_asset_usage_v2 RENAME TO visual_asset_usage;
                CREATE INDEX idx_visual_asset_usage_asset
                ON visual_asset_usage(asset_id);
                """
            )
        connection.execute(
            "INSERT OR IGNORE INTO visual_schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, now),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO visual_global_settings(id, settings_json, updated_at)
            VALUES (1, '{}', ?)
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO visual_themes(
                name, description, settings_json, is_default, is_builtin,
                created_by, updated_by, created_at, updated_at
            ) VALUES (?, ?, ?, 1, 1, 'system', 'system', ?, ?)
            """,
            (
                "Bro Eden Default",
                "Built-in theme matching the production Pillow visual system.",
                _json(
                    {
                        "accent_color": "#f0319b",
                        "panel_color": "#171820",
                        "text_color": "#f4f4f7",
                        "muted_text_color": "#a7a8b3",
                        "border_color": "#30313d",
                        "title_font": "Open Sans Emoji",
                        "body_font": "Open Sans Emoji",
                    }
                ),
                now,
                now,
            ),
        )
        for definition in REGISTRY.all():
            connection.execute(
                """
                INSERT INTO visual_templates(
                    template_key, display_name, published_settings_json,
                    created_at, updated_at
                ) VALUES (?, ?, '{}', ?, ?)
                ON CONFLICT(template_key) DO UPDATE SET
                    display_name = excluded.display_name
                """,
                (definition.key, definition.display_name, now, now),
            )
        connection.commit()
    invalidate_visual_cache()


def _color(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if len(text) == 7 and text.startswith("#"):
        try:
            int(text[1:], 16)
            return text.lower()
        except ValueError:
            pass
    raise ValueError("{} must be a six-digit hex color.".format(field.replace("_", " ").title()))


def validate_settings(
    definition: TemplateDefinition,
    settings: Mapping[str, Any],
    *,
    partial: bool = True,
) -> Dict[str, Any]:
    if not isinstance(settings, Mapping):
        raise ValueError("Visual settings must be a JSON object.")
    allowed = set(definition.supported_settings) | {"assets", "theme_id", "variant_id"}
    unknown = sorted(set(settings) - allowed)
    if unknown:
        raise ValueError("Unsupported settings for {}: {}".format(definition.display_name, ", ".join(unknown)))
    normalized = dict(settings)
    for key in (
        "accent_color",
        "panel_color",
        "text_color",
        "muted_text_color",
        "border_color",
        "background_overlay",
        "secondary_color",
        "title_color",
        "body_text_color",
        "divider_color",
        "first_place_color",
        "second_place_color",
        "third_place_color",
    ):
        if key in normalized:
            normalized[key] = _color(normalized[key], key)
    for key, minimum, maximum in (
        ("panel_opacity", 0.0, 1.0),
        ("focal_x", 0.0, 1.0),
        ("focal_y", 0.0, 1.0),
        ("background_brightness", 0.25, 2.0),
        ("background_saturation", 0.0, 2.0),
        ("background_contrast", 0.25, 2.0),
        ("background_blur", 0.0, 30.0),
        ("background_overlay_opacity", 0.0, 1.0),
        ("shadow_strength", 0.0, 1.0),
        ("border_radius", 0.0, 64.0),
        ("title_size", 18.0, 96.0),
        ("subtitle_size", 12.0, 72.0),
        ("body_size", 12.0, 72.0),
        ("footer_size", 10.0, 48.0),
    ):
        if key in normalized:
            try:
                number = float(normalized[key])
            except (TypeError, ValueError):
                raise ValueError("{} must be a number.".format(key.replace("_", " ").title()))
            if not minimum <= number <= maximum:
                raise ValueError("{} must be between {} and {}.".format(key.replace("_", " ").title(), minimum, maximum))
            normalized[key] = number
    if "maximum_rows" in normalized:
        try:
            rows = int(normalized["maximum_rows"])
        except (TypeError, ValueError):
            raise ValueError("Maximum rows must be a whole number.")
        if rows < 1 or rows > max(1, definition.maximum_items):
            raise ValueError("Maximum rows must be between 1 and {}.".format(max(1, definition.maximum_items)))
        normalized["maximum_rows"] = rows
    if "density" in normalized and normalized["density"] not in {"compact", "balanced", "spacious"}:
        raise ValueError("Density must be compact, balanced, or spacious.")
    if "avatar_shape" in normalized and normalized["avatar_shape"] not in {"circle", "rounded", "square"}:
        raise ValueError("Avatar shape must be circle, rounded, or square.")
    for key in ("title_font", "body_font"):
        if key in normalized and normalized[key] not in {
            "Open Sans Emoji",
            "Calibri Regular",
            "Calibri",
        }:
            raise ValueError("{} must use a bundled tested font.".format(key.replace("_", " ").title()))
    if "background_fit" in normalized and normalized["background_fit"] not in {"cover", "contain", "stretch", "tile"}:
        raise ValueError("Background fit must be cover, contain, stretch, or tile.")
    for key in ("title", "subtitle", "footer_text", "empty_state_message"):
        if key in normalized:
            limit = 100 if key == "title" else 300
            value = str(normalized[key]).strip()
            if len(value) > limit:
                raise ValueError("{} must be {} characters or fewer.".format(key.replace("_", " ").title(), limit))
            normalized[key] = value
    assets = normalized.get("assets")
    if assets is not None:
        if not isinstance(assets, Mapping):
            raise ValueError("Assets must be a slot-to-asset mapping.")
        slot_keys = {slot.key for slot in definition.asset_slots}
        clean_assets = {}
        for slot, raw_id in assets.items():
            if slot not in slot_keys:
                raise ValueError("Unknown asset slot: {}".format(slot))
            if raw_id in (None, "", 0, "0"):
                continue
            try:
                asset_id = int(raw_id)
            except (TypeError, ValueError):
                raise ValueError("Asset references must use numeric IDs.")
            if asset_id <= 0:
                raise ValueError("Asset references must use positive IDs.")
            clean_assets[str(slot)] = asset_id
        normalized["assets"] = clean_assets
    return normalized


def _validate_shared_settings(
    settings: Mapping[str, Any],
    *,
    label: str,
) -> Dict[str, Any]:
    """Validate theme/global values through the same per-template contracts."""
    if not isinstance(settings, Mapping):
        raise ValueError("{} settings must be a JSON object.".format(label))
    definitions = REGISTRY.all()
    allowed = set().union(*(definition.supported_settings for definition in definitions)) | {"assets"}
    unknown = sorted(set(settings) - allowed)
    if unknown:
        raise ValueError("Unsupported {} settings: {}".format(label.casefold(), ", ".join(unknown)))
    normalized: Dict[str, Any] = {}
    for key, value in settings.items():
        if key == "assets":
            if not isinstance(value, Mapping):
                raise ValueError("Assets must be a slot-to-asset mapping.")
            valid_slots = {
                slot.key for definition in definitions for slot in definition.asset_slots
            }
            clean_assets: Dict[str, int] = {}
            for slot, raw_id in value.items():
                if slot not in valid_slots:
                    raise ValueError("Unknown asset slot: {}".format(slot))
                if raw_id in (None, "", 0, "0"):
                    continue
                try:
                    asset_id = int(raw_id)
                except (TypeError, ValueError):
                    raise ValueError("Asset references must use numeric IDs.")
                if asset_id <= 0:
                    raise ValueError("Asset references must use positive IDs.")
                clean_assets[str(slot)] = asset_id
            normalized["assets"] = clean_assets
            continue
        definition = next(
            item for item in definitions if key in item.supported_settings
        )
        normalized.update(validate_settings(definition, {key: value}))
    return normalized


def _active_schedule(
    connection: sqlite3.Connection,
    template_id: int,
    now: str,
) -> Optional[sqlite3.Row]:
    return connection.execute(
        """
        SELECT s.*, t.settings_json AS theme_settings_json,
               t.is_builtin AS theme_is_builtin
        FROM visual_schedules s
        JOIN visual_themes t ON t.id = s.theme_id
        WHERE s.enabled = 1 AND s.starts_at <= ? AND s.ends_at > ?
          AND t.archived_at IS NULL
          AND (s.template_id = ? OR s.template_id IS NULL)
        ORDER BY CASE WHEN s.template_id = ? THEN 0 ELSE 1 END,
                 s.priority DESC, s.id DESC
        LIMIT 1
        """,
        (now, now, template_id, template_id),
    ).fetchone()


def _load_asset_references(
    connection: sqlite3.Connection,
    settings: Mapping[str, Any],
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    assets: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    for slot, asset_id in (settings.get("assets") or {}).items():
        row = connection.execute(
            """
            SELECT a.*, d.attachment_url AS discord_attachment_url,
                   d.storage_thread_id AS discord_storage_thread_id,
                   d.message_id AS discord_message_id
            FROM visual_assets a
            LEFT JOIN visual_asset_discord_storage d ON d.asset_id = a.id
            WHERE a.id = ?
            """,
            (int(asset_id),),
        ).fetchone()
        if row is None:
            warnings.append("Asset {} for {} no longer exists.".format(asset_id, slot))
            continue
        if row["archived_at"]:
            warnings.append("Asset {} for {} is archived.".format(asset_id, slot))
            continue
        assets[str(slot)] = {
            "id": int(row["id"]),
            "name": row["name"],
            "asset_type": row["asset_type"],
            "storage_key": row["storage_key"],
            "width": int(row["width"]),
            "height": int(row["height"]),
            "mime_type": row["mime_type"],
            "discord_attachment_url": row["discord_attachment_url"],
        }
    return assets, warnings


def _validate_asset_records(
    connection: sqlite3.Connection,
    settings: Mapping[str, Any],
    *,
    definition: Optional[TemplateDefinition] = None,
) -> None:
    expected_types = {
        slot.key: slot.asset_type
        for slot in (definition.asset_slots if definition else ())
    }
    if definition is None:
        for registered in REGISTRY.all():
            for slot in registered.asset_slots:
                expected_types.setdefault(slot.key, slot.asset_type)
    for slot, asset_id in (settings.get("assets") or {}).items():
        row = connection.execute(
            "SELECT asset_type, archived_at FROM visual_assets WHERE id=?",
            (int(asset_id),),
        ).fetchone()
        if row is None:
            raise ValueError("Asset #{} for {} was not found.".format(asset_id, slot))
        if row["archived_at"]:
            raise ValueError("Asset #{} for {} is archived.".format(asset_id, slot))
        expected = expected_types.get(str(slot))
        if expected and row["asset_type"] not in {expected, "other"}:
            raise ValueError(
                "Asset #{} is type '{}'; the {} slot requires '{}' or 'other'.".format(
                    asset_id,
                    row["asset_type"],
                    slot,
                    expected,
                )
            )


def resolve_published_configuration(
    template_key: str,
    *,
    variant_id: Optional[int] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    cache_key = "{}:{}".format(template_key, variant_id or 0)
    if use_cache:
        with _cache_lock:
            cached = _resolution_cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return json.loads(_json(cached[1]))
    initialize_visual_studio_schema()
    definition = REGISTRY.get(template_key)
    now = utcnow()
    warnings: List[str] = []
    with _connect() as connection:
        template = connection.execute(
            "SELECT * FROM visual_templates WHERE template_key = ?",
            (template_key,),
        ).fetchone()
        if template is None:
            raise KeyError("Visual template is not initialized: {}".format(template_key))
        global_row = connection.execute(
            "SELECT settings_json FROM visual_global_settings WHERE id = 1"
        ).fetchone()
        globals_settings = _load_json(global_row[0] if global_row else "{}")
        default_theme = connection.execute(
            "SELECT * FROM visual_themes WHERE is_default = 1 AND archived_at IS NULL"
        ).fetchone()
        theme_id = template["theme_id"] or (default_theme["id"] if default_theme else None)
        theme = None
        if theme_id:
            theme = connection.execute(
                "SELECT * FROM visual_themes WHERE id = ? AND archived_at IS NULL",
                (theme_id,),
            ).fetchone()
            if theme is None:
                warnings.append("Selected theme is missing or archived; the built-in theme was used.")
        schedule = _active_schedule(connection, int(template["id"]), now)
        scheduled_theme = (
            _load_json(schedule["theme_settings_json"])
            if schedule and not bool(schedule["theme_is_builtin"])
            else {}
        )
        variant = None
        chosen_variant_id = variant_id or (schedule["variant_id"] if schedule else None)
        if chosen_variant_id:
            variant = connection.execute(
                "SELECT * FROM visual_template_variants WHERE id = ? AND template_id = ? AND is_active = 1",
                (chosen_variant_id, template["id"]),
            ).fetchone()
        variant_theme = None
        if variant and variant["theme_id"]:
            variant_theme = connection.execute(
                "SELECT * FROM visual_themes WHERE id=? AND archived_at IS NULL",
                (variant["theme_id"],),
            ).fetchone()
            if variant_theme is None:
                warnings.append("The variant theme is missing or archived and was skipped.")
        settings = _deep_merge(
            definition.defaults,
            globals_settings,
            _load_json(
                theme["settings_json"]
                if theme and not bool(theme["is_builtin"])
                else "{}"
            ),
            _load_json(template["published_settings_json"]),
            _load_json(
                variant_theme["settings_json"]
                if variant_theme and not bool(variant_theme["is_builtin"])
                else "{}"
            ),
            _load_json(variant["settings_json"] if variant else "{}"),
            scheduled_theme,
        )
        supported = set(definition.supported_settings) | {
            "assets",
            "theme_id",
            "variant_id",
        }
        settings = {key: value for key, value in settings.items() if key in supported}
        if isinstance(settings.get("assets"), Mapping):
            valid_slots = {slot.key for slot in definition.asset_slots}
            settings["assets"] = {
                key: value
                for key, value in settings["assets"].items()
                if key in valid_slots
            }
        try:
            settings = validate_settings(definition, settings, partial=False)
        except ValueError as exc:
            warnings.append("Published customization failed validation: {}".format(exc))
            settings = validate_settings(definition, definition.defaults, partial=False)
        assets, asset_warnings = _load_asset_references(connection, settings)
        warnings.extend(asset_warnings)
        result = {
            "template_key": template_key,
            "definition": definition.as_dict(),
            "canvas": {
                "width": int(variant["width"] or definition.width) if variant else definition.width,
                "height": int(variant["height"] or definition.height) if variant else definition.height,
            },
            "settings": settings,
            "assets": assets,
            "theme_id": int(theme["id"]) if theme else None,
            "theme_name": theme["name"] if theme else "Built-in defaults",
            "variant_id": int(variant["id"]) if variant else None,
            "variant_theme_id": int(variant_theme["id"]) if variant_theme else None,
            "schedule_id": int(schedule["id"]) if schedule else None,
            "published_version": int(template["published_version"]),
            "customized": bool(
                int(template["published_version"])
                or globals_settings
                or variant
                or (schedule and not bool(schedule["theme_is_builtin"]))
                or (theme and not bool(theme["is_builtin"]))
            ),
            "warnings": warnings,
        }
    with _cache_lock:
        _resolution_cache[cache_key] = (time.monotonic() + _CACHE_TTL_SECONDS, result)
    return json.loads(_json(result))


def _status(row: sqlite3.Row) -> str:
    if row["last_render_error"]:
        return "rendering_error"
    if row["draft_settings_json"] is not None:
        return "draft_changes"
    if row["published_version"]:
        return "customized"
    return "default"


def list_visual_templates() -> List[Dict[str, Any]]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT vt.*, t.name AS theme_name
            FROM visual_templates vt
            LEFT JOIN visual_themes t ON t.id = vt.theme_id
            ORDER BY vt.display_name COLLATE NOCASE
            """
        ).fetchall()
    result = []
    for row in rows:
        definition = REGISTRY.get(row["template_key"])
        value = dict(row)
        value.update(definition.as_dict())
        value["status"] = _status(row)
        value["has_overrides"] = bool(_load_json(row["published_settings_json"]))
        value["inherits_globals"] = True
        result.append(value)
    return result


def get_visual_template(template_key: str) -> Dict[str, Any]:
    initialize_visual_studio_schema()
    definition = REGISTRY.get(template_key)
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT vt.*, t.name AS theme_name, dt.name AS draft_theme_name
            FROM visual_templates vt
            LEFT JOIN visual_themes t ON t.id = vt.theme_id
            LEFT JOIN visual_themes dt ON dt.id = vt.draft_theme_id
            WHERE vt.template_key = ?
            """,
            (template_key,),
        ).fetchone()
        if row is None:
            raise KeyError(template_key)
        result = dict(row)
        result.update(definition.as_dict())
        result["published_settings"] = _load_json(row["published_settings_json"])
        result["draft_settings"] = (
            _load_json(row["draft_settings_json"])
            if row["draft_settings_json"] is not None
            else None
        )
        result["status"] = _status(row)
        result["versions"] = [
            dict(item)
            for item in connection.execute(
                """
                SELECT v.*, t.name AS theme_name
                FROM visual_template_versions v
                LEFT JOIN visual_themes t ON t.id = v.theme_id
                WHERE v.template_id = ? ORDER BY version_number DESC LIMIT ?
                """,
                (row["id"], VERSION_RETENTION),
            ).fetchall()
        ]
        result["variants"] = [
            dict(item)
            for item in connection.execute(
                "SELECT * FROM visual_template_variants WHERE template_id = ? ORDER BY is_default DESC, name",
                (row["id"],),
            ).fetchall()
        ]
        result["schedules"] = [
            dict(item)
            for item in connection.execute(
                """
                SELECT s.*, t.name AS theme_name, v.name AS variant_name
                FROM visual_schedules s
                JOIN visual_themes t ON t.id = s.theme_id
                LEFT JOIN visual_template_variants v ON v.id = s.variant_id
                WHERE s.template_id = ? ORDER BY starts_at DESC
                """,
                (row["id"],),
            ).fetchall()
        ]
    return result


def _audit(
    connection: sqlite3.Connection,
    action: str,
    subject_type: str,
    subject_id: Any,
    summary: str,
    actor: Optional[str],
    metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO visual_audit_log(
            action_type, subject_type, subject_id, summary, actor,
            metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (action, subject_type, str(subject_id), summary, actor, _json(metadata or {}), utcnow()),
    )


def _sync_usage(
    connection: sqlite3.Connection,
    *,
    template_id: Optional[int] = None,
    theme_id: Optional[int] = None,
    variant_id: Optional[int] = None,
    global_settings_id: Optional[int] = None,
    settings: Mapping[str, Any],
) -> None:
    clauses = []
    parameters: List[Any] = []
    for column, value in (
        ("template_id", template_id),
        ("theme_id", theme_id),
        ("variant_id", variant_id),
        ("global_settings_id", global_settings_id),
    ):
        if value is not None:
            clauses.append("{} = ?".format(column))
            parameters.append(value)
    if clauses:
        connection.execute(
            "DELETE FROM visual_asset_usage WHERE " + " AND ".join(clauses),
            tuple(parameters),
        )
    for slot, asset_id in (settings.get("assets") or {}).items():
        connection.execute(
            """
            INSERT INTO visual_asset_usage(
                asset_id, template_id, theme_id, variant_id,
                global_settings_id, usage_slot, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(asset_id),
                template_id,
                theme_id,
                variant_id,
                global_settings_id,
                str(slot),
                utcnow(),
            ),
        )


def save_template_draft(
    template_key: str,
    settings: Mapping[str, Any],
    *,
    theme_id: Optional[int],
    actor: str,
) -> None:
    initialize_visual_studio_schema()
    definition = REGISTRY.get(template_key)
    clean = validate_settings(definition, settings)
    with _connect() as connection:
        row = connection.execute(
            "SELECT id FROM visual_templates WHERE template_key = ?", (template_key,)
        ).fetchone()
        if row is None:
            raise KeyError(template_key)
        if theme_id is not None and connection.execute(
            "SELECT 1 FROM visual_themes WHERE id = ? AND archived_at IS NULL", (theme_id,)
        ).fetchone() is None:
            raise ValueError("Selected theme does not exist or is archived.")
        connection.execute(
            """
            UPDATE visual_templates
            SET draft_settings_json = ?, draft_theme_id = ?, updated_by = ?, updated_at = ?
            WHERE id = ?
            """,
            (_json(clean), theme_id, actor, utcnow(), row["id"]),
        )
        _audit(connection, "template_draft_saved", "template", template_key, "Template draft saved.", actor)
        connection.commit()
    invalidate_visual_cache(template_key)


def discard_template_draft(template_key: str, actor: str) -> None:
    initialize_visual_studio_schema()
    with _connect() as connection:
        connection.execute(
            "UPDATE visual_templates SET draft_settings_json = NULL, draft_theme_id = NULL, updated_by = ?, updated_at = ? WHERE template_key = ?",
            (actor, utcnow(), template_key),
        )
        _audit(connection, "template_draft_discarded", "template", template_key, "Template draft discarded.", actor)
        connection.commit()


def publish_template(
    template_key: str,
    *,
    actor: str,
    change_summary: str,
    preview_validator: Optional[Any] = None,
) -> int:
    initialize_visual_studio_schema()
    definition = REGISTRY.get(template_key)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM visual_templates WHERE template_key = ?", (template_key,)
        ).fetchone()
        if row is None:
            raise KeyError(template_key)
        if row["draft_settings_json"] is None:
            raise ValueError("Save a draft before publishing.")
        draft = validate_settings(definition, _load_json(row["draft_settings_json"]))
        _validate_asset_records(connection, draft, definition=definition)
        assets, warnings = _load_asset_references(connection, draft)
        if warnings:
            raise ValueError(warnings[0])
        if preview_validator is not None:
            preview_validator(template_key, draft, assets)
        previous_version = int(row["published_version"])
        if previous_version > 0:
            connection.execute(
                """
                INSERT OR IGNORE INTO visual_template_versions(
                    template_id, version_number, settings_json, theme_id,
                    change_summary, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    previous_version,
                    row["published_settings_json"],
                    row["theme_id"],
                    "Snapshot before version {}".format(previous_version + 1),
                    actor,
                    utcnow(),
                ),
            )
        version = previous_version + 1
        now = utcnow()
        connection.execute(
            """
            UPDATE visual_templates
            SET published_settings_json = ?, theme_id = draft_theme_id,
                draft_settings_json = NULL, draft_theme_id = NULL,
                published_version = ?, published_at = ?, updated_at = ?,
                updated_by = ?, last_render_error = NULL
            WHERE id = ?
            """,
            (_json(draft), version, now, now, actor, row["id"]),
        )
        connection.execute(
            """
            INSERT INTO visual_template_versions(
                template_id, version_number, settings_json, theme_id,
                change_summary, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(template_id, version_number) DO UPDATE SET
                settings_json=excluded.settings_json, theme_id=excluded.theme_id,
                change_summary=excluded.change_summary, created_by=excluded.created_by,
                created_at=excluded.created_at
            """,
            (row["id"], version, _json(draft), row["draft_theme_id"], change_summary[:300], actor, now),
        )
        _sync_usage(connection, template_id=int(row["id"]), settings=draft)
        connection.execute(
            """
            DELETE FROM visual_template_versions
            WHERE template_id = ? AND version_number NOT IN (
                SELECT version_number FROM visual_template_versions
                WHERE template_id = ? ORDER BY version_number DESC LIMIT ?
            )
            """,
            (row["id"], row["id"], VERSION_RETENTION),
        )
        _audit(connection, "template_published", "template", template_key, "Published version {}.".format(version), actor, {"version": version})
        connection.commit()
    invalidate_visual_cache(template_key)
    return version


def restore_template_version(template_key: str, version: int, actor: str) -> None:
    initialize_visual_studio_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT id FROM visual_templates WHERE template_key = ?", (template_key,)
        ).fetchone()
        if row is None:
            raise KeyError(template_key)
        version_row = connection.execute(
            "SELECT * FROM visual_template_versions WHERE template_id = ? AND version_number = ?",
            (row["id"], version),
        ).fetchone()
        if version_row is None:
            raise ValueError("Version was not found.")
        connection.execute(
            """
            UPDATE visual_templates SET draft_settings_json = ?, draft_theme_id = ?,
                updated_by = ?, updated_at = ? WHERE id = ?
            """,
            (version_row["settings_json"], version_row["theme_id"], actor, utcnow(), row["id"]),
        )
        _audit(connection, "template_restored_to_draft", "template", template_key, "Version {} restored as a draft.".format(version), actor)
        connection.commit()


def reset_template_to_defaults(template_key: str, actor: str) -> None:
    save_template_draft(template_key, {}, theme_id=None, actor=actor)


def list_themes(include_archived: bool = False) -> List[Dict[str, Any]]:
    initialize_visual_studio_schema()
    where = "" if include_archived else "WHERE t.archived_at IS NULL"
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT t.*,
                   (SELECT COUNT(*) FROM visual_templates vt WHERE vt.theme_id=t.id OR vt.draft_theme_id=t.id) AS template_count,
                   (SELECT COUNT(*) FROM visual_asset_usage u WHERE u.theme_id=t.id) AS asset_count
            FROM visual_themes t {} ORDER BY t.is_default DESC, t.name COLLATE NOCASE
            """.format(where)
        ).fetchall()
    return [{**dict(row), "settings": _load_json(row["settings_json"])} for row in rows]


def get_theme(theme_id: int) -> Optional[Dict[str, Any]]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM visual_themes WHERE id = ?", (theme_id,)).fetchone()
    return ({**dict(row), "settings": _load_json(row["settings_json"])} if row else None)


def save_theme(
    *,
    name: str,
    description: str,
    settings: Mapping[str, Any],
    actor: str,
    theme_id: Optional[int] = None,
) -> int:
    initialize_visual_studio_schema()
    clean_name = " ".join(str(name).split())
    if not clean_name or len(clean_name) > 80:
        raise ValueError("Theme name must be 1 to 80 characters.")
    clean_settings = _validate_shared_settings(settings, label="Theme")
    now = utcnow()
    with _connect() as connection:
        _validate_asset_records(connection, clean_settings)
        if theme_id is None:
            cursor = connection.execute(
                """
                INSERT INTO visual_themes(name, description, settings_json, created_by, updated_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (clean_name, description[:300], _json(clean_settings), actor, actor, now, now),
            )
            theme_id = int(cursor.lastrowid)
            action = "theme_created"
        else:
            existing = connection.execute("SELECT is_builtin FROM visual_themes WHERE id = ?", (theme_id,)).fetchone()
            if existing is None:
                raise ValueError("Theme was not found.")
            if existing["is_builtin"]:
                raise ValueError("The built-in theme is read-only. Duplicate it to customize a copy.")
            connection.execute(
                "UPDATE visual_themes SET name=?, description=?, settings_json=?, updated_by=?, updated_at=? WHERE id=?",
                (clean_name, description[:300], _json(clean_settings), actor, now, theme_id),
            )
            action = "theme_updated"
        _sync_usage(connection, theme_id=theme_id, settings=clean_settings)
        _audit(connection, action, "theme", theme_id, "Theme {}.".format("created" if action.endswith("created") else "updated"), actor)
        connection.commit()
    invalidate_visual_cache()
    return theme_id


def duplicate_theme(theme_id: int, actor: str) -> int:
    initialize_visual_studio_schema()
    with _connect() as connection:
        source = connection.execute(
            "SELECT * FROM visual_themes WHERE id=? AND archived_at IS NULL",
            (theme_id,),
        ).fetchone()
        if source is None:
            raise ValueError("Theme was not found.")
        stem = "{} Copy".format(str(source["name"])[:68]).strip()
        name = stem
        suffix = 2
        while connection.execute(
            "SELECT 1 FROM visual_themes WHERE name=? COLLATE NOCASE", (name,)
        ).fetchone():
            name = "{} {}".format(stem[:76], suffix)
            suffix += 1
        now = utcnow()
        cursor = connection.execute(
            """
            INSERT INTO visual_themes(
                name, description, settings_json, created_by, updated_by,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                source["description"],
                source["settings_json"],
                actor,
                actor,
                now,
                now,
            ),
        )
        duplicate_id = int(cursor.lastrowid)
        settings = _load_json(source["settings_json"])
        _validate_asset_records(connection, settings)
        _sync_usage(connection, theme_id=duplicate_id, settings=settings)
        _audit(
            connection,
            "theme_duplicated",
            "theme",
            duplicate_id,
            "Theme duplicated from #{}.".format(theme_id),
            actor,
        )
        connection.commit()
    invalidate_visual_cache()
    return duplicate_id


def set_default_theme(theme_id: int, actor: str) -> None:
    initialize_visual_studio_schema()
    with _connect() as connection:
        if connection.execute("SELECT 1 FROM visual_themes WHERE id=? AND archived_at IS NULL", (theme_id,)).fetchone() is None:
            raise ValueError("Theme was not found.")
        connection.execute("UPDATE visual_themes SET is_default=0")
        connection.execute("UPDATE visual_themes SET is_default=1, updated_by=?, updated_at=? WHERE id=?", (actor, utcnow(), theme_id))
        _audit(connection, "theme_set_default", "theme", theme_id, "Default theme changed.", actor)
        connection.commit()
    invalidate_visual_cache()


def archive_theme(theme_id: int, actor: str) -> None:
    initialize_visual_studio_schema()
    with _connect() as connection:
        row = connection.execute("SELECT is_builtin, is_default FROM visual_themes WHERE id=?", (theme_id,)).fetchone()
        if row is None:
            raise ValueError("Theme was not found.")
        if row["is_builtin"] or row["is_default"]:
            raise ValueError("The built-in or active default theme cannot be archived.")
        if connection.execute(
            "SELECT 1 FROM visual_templates WHERE theme_id=? OR draft_theme_id=? LIMIT 1",
            (theme_id, theme_id),
        ).fetchone():
            raise ValueError("This theme is used by a template or draft.")
        if connection.execute(
            "SELECT 1 FROM visual_template_variants WHERE theme_id=? LIMIT 1",
            (theme_id,),
        ).fetchone():
            raise ValueError("This theme is used by a template variant.")
        if connection.execute(
            "SELECT 1 FROM visual_schedules WHERE theme_id=? LIMIT 1",
            (theme_id,),
        ).fetchone():
            raise ValueError("This theme is used by a schedule.")
        connection.execute("UPDATE visual_themes SET archived_at=?, updated_by=?, updated_at=? WHERE id=?", (utcnow(), actor, utcnow(), theme_id))
        _audit(connection, "theme_archived", "theme", theme_id, "Theme archived.", actor)
        connection.commit()
    invalidate_visual_cache()


def restore_theme(theme_id: int, actor: str) -> None:
    initialize_visual_studio_schema()
    with _connect() as connection:
        connection.execute(
            "UPDATE visual_themes SET archived_at=NULL, updated_by=?, updated_at=? WHERE id=?",
            (actor, utcnow(), theme_id),
        )
        if connection.total_changes == 0:
            raise ValueError("Theme was not found.")
        _audit(connection, "theme_restored", "theme", theme_id, "Theme restored.", actor)
        connection.commit()
    invalidate_visual_cache()


def delete_theme(theme_id: int, actor: str) -> None:
    initialize_visual_studio_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT is_builtin, archived_at FROM visual_themes WHERE id=?",
            (theme_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Theme was not found.")
        if row["is_builtin"]:
            raise ValueError("The built-in theme cannot be deleted.")
        if not row["archived_at"]:
            raise ValueError("Archive the theme before permanently deleting it.")
        if connection.execute(
            "SELECT 1 FROM visual_templates WHERE theme_id=? OR draft_theme_id=? LIMIT 1",
            (theme_id, theme_id),
        ).fetchone():
            raise ValueError("This theme is still referenced and cannot be deleted.")
        for table, column in (("visual_template_variants", "theme_id"), ("visual_schedules", "theme_id")):
            if connection.execute(
                "SELECT 1 FROM {} WHERE {}=? LIMIT 1".format(table, column),
                (theme_id,),
            ).fetchone():
                raise ValueError("This theme is still referenced and cannot be deleted.")
        connection.execute("DELETE FROM visual_asset_usage WHERE theme_id=?", (theme_id,))
        connection.execute("DELETE FROM visual_themes WHERE id=?", (theme_id,))
        _audit(connection, "theme_deleted", "theme", theme_id, "Archived theme permanently deleted.", actor)
        connection.commit()
    invalidate_visual_cache()


def get_global_settings() -> Dict[str, Any]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM visual_global_settings WHERE id=1").fetchone()
    result = dict(row) if row else {"id": 1, "settings_json": "{}"}
    result["settings"] = _load_json(result.get("settings_json"))
    result["draft_settings"] = (
        _load_json(result.get("draft_settings_json"))
        if result.get("draft_settings_json") is not None
        else None
    )
    return result


def save_global_settings(settings: Mapping[str, Any], actor: str) -> None:
    clean_settings = _validate_shared_settings(settings, label="Global")
    initialize_visual_studio_schema()
    with _connect() as connection:
        _validate_asset_records(connection, clean_settings)
        connection.execute("UPDATE visual_global_settings SET settings_json=?, updated_by=?, updated_at=? WHERE id=1", (_json(clean_settings), actor, utcnow()))
        _sync_usage(connection, global_settings_id=1, settings=clean_settings)
        _audit(connection, "global_settings_changed", "global_settings", 1, "Global visual settings updated.", actor)
        connection.commit()
    invalidate_visual_cache()


def save_global_settings_draft(settings: Mapping[str, Any], actor: str) -> None:
    clean_settings = _validate_shared_settings(settings, label="Global")
    initialize_visual_studio_schema()
    with _connect() as connection:
        _validate_asset_records(connection, clean_settings)
        connection.execute(
            "UPDATE visual_global_settings SET draft_settings_json=?, updated_by=?, updated_at=? WHERE id=1",
            (_json(clean_settings), actor, utcnow()),
        )
        _audit(connection, "global_settings_draft_saved", "global_settings", 1, "Global visual settings draft saved.", actor)
        connection.commit()


def publish_global_settings(actor: str) -> None:
    initialize_visual_studio_schema()
    with _connect() as connection:
        row = connection.execute("SELECT draft_settings_json FROM visual_global_settings WHERE id=1").fetchone()
        if row is None or row["draft_settings_json"] is None:
            raise ValueError("Save global settings as a draft before publishing.")
        draft_settings = _load_json(row["draft_settings_json"])
        _validate_asset_records(connection, draft_settings)
        connection.execute(
            "UPDATE visual_global_settings SET settings_json=draft_settings_json, draft_settings_json=NULL, updated_by=?, updated_at=? WHERE id=1",
            (actor, utcnow()),
        )
        _sync_usage(
            connection,
            global_settings_id=1,
            settings=draft_settings,
        )
        _audit(connection, "global_settings_published", "global_settings", 1, "Global visual settings published.", actor)
        connection.commit()
    invalidate_visual_cache()


def list_recent_audit(limit: int = 50) -> List[Dict[str, Any]]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM visual_audit_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
    return [dict(row) for row in rows]


def save_variant(
    template_key: str,
    *,
    name: str,
    description: str,
    settings: Mapping[str, Any],
    width: Optional[int],
    height: Optional[int],
    theme_id: Optional[int],
    actor: str,
    variant_id: Optional[int] = None,
) -> int:
    initialize_visual_studio_schema()
    definition = REGISTRY.get(template_key)
    clean = validate_settings(definition, settings)
    clean_name = " ".join(str(name).split())
    if not clean_name or len(clean_name) > 80:
        raise ValueError("Variant name must be 1 to 80 characters.")
    if (width is None) != (height is None):
        raise ValueError("Variant width and height must be set together.")
    if width is not None and definition.key == "queue_next":
        raise ValueError("Queue banner variants must use the registered 1024 x 258 canvas.")
    if width is not None and (width < definition.width // 2 or width > definition.width * 3 or height < definition.height // 2 or height > definition.height * 3):
        raise ValueError("Variant canvas dimensions are outside the supported range.")
    if width is not None:
        ratio_delta = abs((width / height) - (definition.width / definition.height)) / (definition.width / definition.height)
        if ratio_delta > 0.015:
            raise ValueError("Variant canvas dimensions must keep the registered aspect ratio.")
    with _connect() as connection:
        template = connection.execute("SELECT id FROM visual_templates WHERE template_key=?", (template_key,)).fetchone()
        if template is None:
            raise ValueError("Template was not found.")
        if theme_id is not None and connection.execute(
            "SELECT 1 FROM visual_themes WHERE id=? AND archived_at IS NULL",
            (theme_id,),
        ).fetchone() is None:
            raise ValueError("Theme was not found.")
        _validate_asset_records(connection, clean, definition=definition)
        now = utcnow()
        if variant_id is None:
            cursor = connection.execute(
                """
                INSERT INTO visual_template_variants(template_id,name,description,width,height,theme_id,settings_json,created_by,updated_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (template["id"], clean_name, description[:300], width, height, theme_id, _json(clean), actor, actor, now, now),
            )
            variant_id = int(cursor.lastrowid)
        else:
            connection.execute(
                "UPDATE visual_template_variants SET name=?,description=?,width=?,height=?,theme_id=?,settings_json=?,updated_by=?,updated_at=? WHERE id=? AND template_id=?",
                (clean_name, description[:300], width, height, theme_id, _json(clean), actor, now, variant_id, template["id"]),
            )
        _sync_usage(connection, variant_id=variant_id, settings=clean)
        _audit(connection, "variant_saved", "variant", variant_id, "Template variant saved.", actor)
        connection.commit()
    invalidate_visual_cache(template_key)
    return variant_id


def save_schedule(
    *,
    template_key: Optional[str],
    theme_id: int,
    variant_id: Optional[int],
    starts_at: str,
    ends_at: str,
    timezone_name: str,
    priority: int,
    actor: str,
) -> int:
    initialize_visual_studio_schema()
    try:
        start = datetime.fromisoformat(starts_at)
        end = datetime.fromisoformat(ends_at)
    except ValueError:
        raise ValueError("Schedule dates must be valid ISO date-times.")
    try:
        selected_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Schedule timezone must be a valid IANA timezone.")
    if start.tzinfo is None:
        start = start.replace(tzinfo=selected_timezone)
    if end.tzinfo is None:
        end = end.replace(tzinfo=selected_timezone)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end <= start:
        raise ValueError("Schedule end must be after its start.")
    with _connect() as connection:
        template_id = None
        if template_key:
            row = connection.execute("SELECT id FROM visual_templates WHERE template_key=?", (template_key,)).fetchone()
            if row is None:
                raise ValueError("Template was not found.")
            template_id = int(row["id"])
        if connection.execute("SELECT 1 FROM visual_themes WHERE id=? AND archived_at IS NULL", (theme_id,)).fetchone() is None:
            raise ValueError("Theme was not found.")
        if variant_id is not None:
            if template_id is None:
                raise ValueError("A global schedule cannot select a template variant.")
            if connection.execute(
                "SELECT 1 FROM visual_template_variants WHERE id=? AND template_id=? AND is_active=1",
                (variant_id, template_id),
            ).fetchone() is None:
                raise ValueError("Variant was not found for this template.")
        now = utcnow()
        cursor = connection.execute(
            """
            INSERT INTO visual_schedules(template_id,theme_id,variant_id,starts_at,ends_at,timezone,priority,created_by,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (template_id, theme_id, variant_id, start.isoformat(), end.isoformat(), timezone_name[:64], max(-100, min(int(priority), 100)), actor, now, now),
        )
        schedule_id = int(cursor.lastrowid)
        _audit(connection, "schedule_created", "schedule", schedule_id, "Visual schedule created.", actor)
        connection.commit()
    invalidate_visual_cache(template_key)
    return schedule_id


def list_global_schedules() -> List[Dict[str, Any]]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT s.*, t.name AS theme_name
            FROM visual_schedules s
            JOIN visual_themes t ON t.id=s.theme_id
            WHERE s.template_id IS NULL
            ORDER BY s.starts_at DESC, s.priority DESC, s.id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def set_schedule_enabled(schedule_id: int, enabled: bool, actor: str) -> Optional[str]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT s.id, vt.template_key
            FROM visual_schedules s
            LEFT JOIN visual_templates vt ON vt.id=s.template_id
            WHERE s.id=?
            """,
            (schedule_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Schedule was not found.")
        connection.execute(
            "UPDATE visual_schedules SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, utcnow(), schedule_id),
        )
        _audit(
            connection,
            "schedule_enabled" if enabled else "schedule_disabled",
            "schedule",
            schedule_id,
            "Visual schedule {}.".format("enabled" if enabled else "disabled"),
            actor,
        )
        connection.commit()
        template_key = row["template_key"]
    invalidate_visual_cache(template_key)
    return template_key


def delete_schedule(schedule_id: int, actor: str) -> Optional[str]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT s.id, vt.template_key
            FROM visual_schedules s
            LEFT JOIN visual_templates vt ON vt.id=s.template_id
            WHERE s.id=?
            """,
            (schedule_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Schedule was not found.")
        template_key = row["template_key"]
        connection.execute("DELETE FROM visual_schedules WHERE id=?", (schedule_id,))
        _audit(connection, "schedule_deleted", "schedule", schedule_id, "Visual schedule deleted.", actor)
        connection.commit()
    invalidate_visual_cache(template_key)
    return template_key


def export_configuration(template_key: Optional[str] = None) -> Dict[str, Any]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        where = "WHERE template_key = ?" if template_key else ""
        parameters: Tuple[Any, ...] = (template_key,) if template_key else ()
        templates = [
            {
                "template_key": row["template_key"],
                "theme_id": row["theme_id"],
                "theme_name": row["theme_name"],
                "published_settings": _load_json(row["published_settings_json"]),
                "published_version": row["published_version"],
            }
            for row in connection.execute(
                """
                SELECT vt.*, th.name AS theme_name
                FROM visual_templates vt
                LEFT JOIN visual_themes th ON th.id=vt.theme_id
                {} ORDER BY template_key
                """.format(where), parameters
            ).fetchall()
        ]
        themes = [
            {"name": row["name"], "description": row["description"], "settings": _load_json(row["settings_json"]), "is_default": bool(row["is_default"])}
            for row in connection.execute("SELECT * FROM visual_themes WHERE archived_at IS NULL ORDER BY id").fetchall()
        ]
    return {
        "schema": "broeden.visual-content-studio",
        "schema_version": SCHEMA_VERSION,
        "exported_at": utcnow(),
        "registry_keys": [definition.key for definition in REGISTRY.all()],
        "global_settings": get_global_settings()["settings"],
        "templates": templates,
        "themes": themes,
    }


def import_configuration_as_drafts(document: Mapping[str, Any], actor: str) -> List[str]:
    if document.get("schema") != "broeden.visual-content-studio" or int(document.get("schema_version", 0)) != SCHEMA_VERSION:
        raise ValueError("Unsupported Visual Content Studio export schema.")
    imported = []
    global_settings = document.get("global_settings")
    if isinstance(global_settings, Mapping):
        save_global_settings_draft(global_settings, actor)
    theme_ids = {item["name"].casefold(): int(item["id"]) for item in list_themes(include_archived=False)}
    for theme in document.get("themes", []):
        if not isinstance(theme, Mapping):
            continue
        name = " ".join(str(theme.get("name", "")).split())
        if not name or name.casefold() in theme_ids:
            continue
        theme_id = save_theme(
            name=name,
            description=str(theme.get("description", "Imported theme")),
            settings=theme.get("settings") if isinstance(theme.get("settings"), Mapping) else {},
            actor=actor,
        )
        theme_ids[name.casefold()] = theme_id
    for item in document.get("templates", []):
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("template_key", ""))
        if key not in {definition.key for definition in REGISTRY.all()}:
            continue
        imported_theme_id = theme_ids.get(str(item.get("theme_name") or "").casefold())
        save_template_draft(key, item.get("published_settings") or {}, theme_id=imported_theme_id, actor=actor)
        imported.append(key)
    return imported
