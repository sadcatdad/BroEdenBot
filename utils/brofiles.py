"""Persistent member BROfile profiles, media, and role badge mappings."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageOps, UnidentifiedImageError

from utils.settings import settings_database_path
from utils.sqlite import AutoClosingSQLiteConnection, configure_sync_connection
from utils.visual_studio.storage import visual_asset_directory


TAGLINE_MAX_LENGTH = 90
ABOUT_MAX_LENGTH = 500
DETAIL_MAX_LENGTH = 220
BADGE_LABEL_MAX_LENGTH = 60
MAX_MEDIA_BYTES = 8 * 1024 * 1024
DEFAULT_ACCENT_COLOR = "#7DD3A7"
DEFAULT_BACKGROUND_COLOR_START = "#101A18"
DEFAULT_BACKGROUND_COLOR_END = "#17231F"
MEDIA_SIZES = {
    "banner": (1600, 500),
    "spotlight": (900, 900),
}

_HEX_COLOR = re.compile(r"#[0-9A-F]{6}")
_INITIALIZED_DATABASES: set[str] = set()


def _connect() -> sqlite3.Connection:
    path = settings_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path,
        timeout=30,
        factory=AutoClosingSQLiteConnection,
    )
    configured = configure_sync_connection(connection)
    configured.execute("PRAGMA foreign_keys = ON")
    return configured


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_brofile_schema() -> None:
    """Create the additive BROfile schema once for the active database."""
    database_key = str(settings_database_path().expanduser().resolve())
    if database_key in _INITIALIZED_DATABASES:
        return
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS brofiles (
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                avatar_url TEXT,
                tagline TEXT NOT NULL DEFAULT '',
                about TEXT NOT NULL DEFAULT '',
                interests TEXT NOT NULL DEFAULT '',
                skills TEXT NOT NULL DEFAULT '',
                favorite_things TEXT NOT NULL DEFAULT '',
                proudest_moment TEXT NOT NULL DEFAULT '',
                directory_visible INTEGER NOT NULL DEFAULT 0
                    CHECK(directory_visible IN (0, 1)),
                accent_color TEXT NOT NULL DEFAULT '#7DD3A7',
                background_color_start TEXT NOT NULL DEFAULT '#101A18',
                background_color_end TEXT NOT NULL DEFAULT '#17231F',
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS brofile_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                media_type TEXT NOT NULL CHECK(media_type IN ('banner', 'spotlight')),
                storage_key TEXT NOT NULL UNIQUE,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                file_size INTEGER NOT NULL,
                checksum TEXT NOT NULL,
                uploaded_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (guild_id, user_id, media_type),
                FOREIGN KEY (guild_id, user_id)
                    REFERENCES brofiles(guild_id, user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS brofile_badges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                label TEXT NOT NULL,
                asset_id INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (guild_id, role_id)
            );

            CREATE INDEX IF NOT EXISTS idx_brofiles_directory
            ON brofiles(guild_id, directory_visible, display_name);
            CREATE INDEX IF NOT EXISTS idx_brofile_badges_priority
            ON brofile_badges(guild_id, active, priority DESC, id);
            """
        )
        connection.commit()
    _INITIALIZED_DATABASES.add(database_key)


def normalize_color(value: Any, label: str) -> str:
    color = str(value or "").strip().upper()
    if not _HEX_COLOR.fullmatch(color):
        raise ValueError("{} must be a six-digit color such as #7DD3A7.".format(label))
    return color


def discord_avatar_url(user: Dict[str, Any]) -> Optional[str]:
    user_id = str(user.get("discord_user_id") or "").strip()
    avatar_hash = str(user.get("discord_avatar") or "").strip()
    if not user_id.isdigit() or not avatar_hash:
        return None
    extension = "gif" if avatar_hash.startswith("a_") else "png"
    return "https://cdn.discordapp.com/avatars/{}/{}.{}?size=256".format(
        user_id,
        avatar_hash,
        extension,
    )


def identity_from_dashboard_user(user: Dict[str, Any]) -> Dict[str, str]:
    user_id = str(user.get("discord_user_id") or "").strip()
    username = str(user.get("discord_username") or user.get("username") or "").strip()
    display_name = str(
        user.get("discord_global_name")
        or user.get("discord_username")
        or user.get("username")
        or "BRO"
    ).strip()
    return {
        "user_id": user_id,
        "username": username or ("member-{}".format(user_id) if user_id else "member"),
        "display_name": display_name,
        "avatar_url": discord_avatar_url(user) or "",
    }


def _ensure_profile(
    connection: sqlite3.Connection,
    guild_id: str,
    user_id: str,
    identity: Optional[Dict[str, str]] = None,
) -> None:
    now = _now()
    identity = identity or {}
    connection.execute(
        """
        INSERT INTO brofiles (
            guild_id, user_id, username, display_name, avatar_url,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            username = CASE
                WHEN excluded.username != '' THEN excluded.username
                ELSE brofiles.username
            END,
            display_name = CASE
                WHEN excluded.display_name != '' THEN excluded.display_name
                ELSE brofiles.display_name
            END,
            avatar_url = CASE
                WHEN excluded.avatar_url != '' THEN excluded.avatar_url
                ELSE brofiles.avatar_url
            END
        """,
        (
            str(guild_id),
            str(user_id),
            str(identity.get("username") or "")[:100],
            str(identity.get("display_name") or "")[:100],
            str(identity.get("avatar_url") or "")[:500] or None,
            now,
            now,
        ),
    )


def get_brofile(
    guild_id: str,
    user_id: str,
    *,
    create: bool = False,
    identity: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    initialize_brofile_schema()
    with _connect() as connection:
        if create:
            _ensure_profile(connection, guild_id, user_id, identity)
            connection.commit()
        row = connection.execute(
            """
            SELECT * FROM brofiles
            WHERE guild_id = ? AND user_id = ?
            """,
            (str(guild_id), str(user_id)),
        ).fetchone()
        if row is None:
            return None
        profile = dict(row)
        media_rows = connection.execute(
            """
            SELECT * FROM brofile_media
            WHERE guild_id = ? AND user_id = ?
            """,
            (str(guild_id), str(user_id)),
        ).fetchall()
    profile["media"] = {str(row["media_type"]): dict(row) for row in media_rows}
    return profile


def update_brofile(
    guild_id: str,
    user_id: str,
    *,
    identity: Dict[str, str],
    tagline: Any,
    about: Any,
    interests: Any,
    skills: Any,
    favorite_things: Any,
    proudest_moment: Any,
    directory_visible: bool,
    accent_color: Any,
    background_color_start: Any,
    background_color_end: Any,
) -> Dict[str, Any]:
    initialize_brofile_schema()
    colors = (
        normalize_color(accent_color, "Accent color"),
        normalize_color(background_color_start, "Background start color"),
        normalize_color(background_color_end, "Background end color"),
    )
    with _connect() as connection:
        _ensure_profile(connection, guild_id, user_id, identity)
        connection.execute(
            """
            UPDATE brofiles
            SET username = ?, display_name = ?, avatar_url = ?,
                tagline = ?, about = ?, interests = ?, skills = ?,
                favorite_things = ?, proudest_moment = ?,
                directory_visible = ?, accent_color = ?,
                background_color_start = ?, background_color_end = ?,
                revision = revision + 1, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                str(identity.get("username") or "")[:100],
                str(identity.get("display_name") or "")[:100],
                str(identity.get("avatar_url") or "")[:500] or None,
                str(tagline or "").strip()[:TAGLINE_MAX_LENGTH],
                str(about or "").strip()[:ABOUT_MAX_LENGTH],
                str(interests or "").strip()[:DETAIL_MAX_LENGTH],
                str(skills or "").strip()[:DETAIL_MAX_LENGTH],
                str(favorite_things or "").strip()[:DETAIL_MAX_LENGTH],
                str(proudest_moment or "").strip()[:DETAIL_MAX_LENGTH],
                int(bool(directory_visible)),
                colors[0],
                colors[1],
                colors[2],
                _now(),
                str(guild_id),
                str(user_id),
            ),
        )
        connection.commit()
    return get_brofile(guild_id, user_id) or {}


def list_directory_brofiles(guild_id: str, limit: int = 500) -> List[Dict[str, Any]]:
    initialize_brofile_schema()
    bounded_limit = max(1, min(int(limit), 1000))
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM brofiles
            WHERE guild_id = ? AND directory_visible = 1
            ORDER BY display_name COLLATE NOCASE, username COLLATE NOCASE
            LIMIT ?
            """,
            (str(guild_id), bounded_limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _member_role_ids(connection: sqlite3.Connection, user_id: str) -> set[str]:
    row = connection.execute(
        """
        SELECT discord_role_ids_json FROM dashboard_users
        WHERE discord_user_id = ? AND status = 'active'
        """,
        (str(user_id),),
    ).fetchone()
    if row is None:
        return set()
    try:
        values = json.loads(str(row["discord_role_ids_json"] or "[]"))
    except (json.JSONDecodeError, TypeError):
        return set()
    return {str(value) for value in values if str(value).isdigit()}


def badge_for_member(guild_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Return the highest-priority active badge backed by a current cached role."""
    initialize_brofile_schema()
    with _connect() as connection:
        role_ids = _member_role_ids(connection, user_id)
        if not role_ids:
            return None
        placeholders = ",".join("?" for _ in role_ids)
        row = connection.execute(
            """
            SELECT badges.*, assets.name AS asset_name,
                   assets.storage_key AS asset_storage_key,
                   assets.archived_at AS asset_archived_at
            FROM brofile_badges badges
            JOIN visual_assets assets ON assets.id = badges.asset_id
            WHERE badges.guild_id = ? AND badges.active = 1
              AND badges.role_id IN ({})
              AND assets.archived_at IS NULL
            ORDER BY badges.priority DESC, badges.id ASC
            LIMIT 1
            """.format(placeholders),
            (str(guild_id), *sorted(role_ids)),
        ).fetchone()
    return dict(row) if row is not None else None


def list_badge_mappings(guild_id: str) -> List[Dict[str, Any]]:
    initialize_brofile_schema()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT badges.*, assets.name AS asset_name,
                   assets.archived_at AS asset_archived_at
            FROM brofile_badges badges
            LEFT JOIN visual_assets assets ON assets.id = badges.asset_id
            WHERE badges.guild_id = ?
            ORDER BY badges.priority DESC, badges.label COLLATE NOCASE
            """,
            (str(guild_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def save_badge_mapping(
    guild_id: str,
    *,
    role_id: Any,
    label: Any,
    asset_id: Any,
    priority: Any,
) -> Dict[str, Any]:
    initialize_brofile_schema()
    clean_role_id = str(role_id or "").strip()
    clean_label = " ".join(str(label or "").split())[:BADGE_LABEL_MAX_LENGTH]
    if not clean_role_id.isdigit():
        raise ValueError("Choose a valid Discord role.")
    if not clean_label:
        raise ValueError("Badge label is required.")
    try:
        clean_asset_id = int(asset_id)
        clean_priority = int(priority)
    except (TypeError, ValueError) as exc:
        raise ValueError("Choose a badge asset and a numeric priority.") from exc
    clean_priority = max(-1000, min(clean_priority, 1000))
    with _connect() as connection:
        asset = connection.execute(
            """
            SELECT id FROM visual_assets
            WHERE id = ? AND asset_type = 'badge' AND archived_at IS NULL
            """,
            (clean_asset_id,),
        ).fetchone()
        if asset is None:
            raise ValueError("Choose an active Badge asset from the Asset Library.")
        now = _now()
        connection.execute(
            """
            INSERT INTO brofile_badges (
                guild_id, role_id, label, asset_id, priority, active,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(guild_id, role_id) DO UPDATE SET
                label = excluded.label,
                asset_id = excluded.asset_id,
                priority = excluded.priority,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (
                str(guild_id),
                clean_role_id,
                clean_label,
                clean_asset_id,
                clean_priority,
                now,
                now,
            ),
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT * FROM brofile_badges
            WHERE guild_id = ? AND role_id = ?
            """,
            (str(guild_id), clean_role_id),
        ).fetchone()
    return dict(row)


def delete_badge_mapping(guild_id: str, mapping_id: int) -> bool:
    initialize_brofile_schema()
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM brofile_badges WHERE guild_id = ? AND id = ?",
            (str(guild_id), int(mapping_id)),
        )
        connection.commit()
        return cursor.rowcount > 0


def badge_asset_usage_count(connection: sqlite3.Connection, asset_id: int) -> int:
    """Return active BROfile badge references without requiring module init."""
    table = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'brofile_badges'
        """
    ).fetchone()
    if table is None:
        return 0
    return int(
        connection.execute(
            """
            SELECT COUNT(*) FROM brofile_badges
            WHERE asset_id = ? AND active = 1
            """,
            (int(asset_id),),
        ).fetchone()[0]
    )


def _media_root() -> Path:
    root = (visual_asset_directory() / "brofiles").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _media_path(storage_key: str) -> Path:
    if not storage_key or not re.fullmatch(r"[A-Za-z0-9_./-]+", storage_key):
        raise ValueError("Invalid BROfile media key.")
    root = _media_root()
    candidate = (root / storage_key).resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError("BROfile media path is outside its storage directory.")
    return candidate


def media_path(storage_key: str) -> Path:
    path = _media_path(storage_key)
    if not path.is_file():
        raise FileNotFoundError("BROfile media is unavailable.")
    return path


def _normalized_media(data: bytes, filename: str, media_type: str) -> Tuple[bytes, int, int]:
    if media_type not in MEDIA_SIZES:
        raise ValueError("Unknown BROfile image type.")
    if not data:
        raise ValueError("Choose an image file to upload.")
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("BROfile images must be 8 MB or smaller.")
    extension = Path(str(filename or "")).suffix.casefold()
    if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("Upload a PNG, JPG, or WEBP image.")
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.seek(0)
            if getattr(source, "is_animated", False) and getattr(source, "n_frames", 1) > 1:
                raise ValueError("Animated BROfile images are not supported.")
            image = ImageOps.exif_transpose(source).convert("RGBA")
            if image.width * image.height > 40_000_000:
                raise ValueError("BROfile image dimensions are too large.")
            width, height = MEDIA_SIZES[media_type]
            image = ImageOps.fit(
                image,
                (width, height),
                Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            output = io.BytesIO()
            image.save(output, "PNG", optimize=True, compress_level=9)
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("BROfile image could not be decoded.") from exc
    return output.getvalue(), width, height


def save_brofile_media(
    guild_id: str,
    user_id: str,
    media_type: str,
    *,
    data: bytes,
    filename: str,
    uploaded_by: str,
    identity: Dict[str, str],
) -> Dict[str, Any]:
    initialize_brofile_schema()
    normalized, width, height = _normalized_media(data, filename, media_type)
    checksum = hashlib.sha256(normalized).hexdigest()
    storage_key = "{}/{}/{}-{}-{}.png".format(
        str(guild_id),
        str(user_id),
        media_type,
        checksum[:16],
        secrets.token_hex(4),
    )
    destination = _media_path(storage_key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name("{}.tmp".format(destination.name))
    temporary.write_bytes(normalized)
    previous_key = None
    try:
        with _connect() as connection:
            _ensure_profile(connection, guild_id, user_id, identity)
            previous = connection.execute(
                """
                SELECT storage_key FROM brofile_media
                WHERE guild_id = ? AND user_id = ? AND media_type = ?
                """,
                (str(guild_id), str(user_id), media_type),
            ).fetchone()
            previous_key = str(previous["storage_key"]) if previous else None
            now = _now()
            connection.execute(
                """
                INSERT INTO brofile_media (
                    guild_id, user_id, media_type, storage_key,
                    width, height, file_size, checksum, uploaded_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, media_type) DO UPDATE SET
                    storage_key = excluded.storage_key,
                    width = excluded.width,
                    height = excluded.height,
                    file_size = excluded.file_size,
                    checksum = excluded.checksum,
                    uploaded_by = excluded.uploaded_by,
                    updated_at = excluded.updated_at
                """,
                (
                    str(guild_id),
                    str(user_id),
                    media_type,
                    storage_key,
                    width,
                    height,
                    len(normalized),
                    checksum,
                    str(uploaded_by or "")[:100],
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE brofiles
                SET directory_visible = 1, revision = revision + 1, updated_at = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (now, str(guild_id), str(user_id)),
            )
            os.replace(str(temporary), str(destination))
            connection.commit()
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    if previous_key and previous_key != storage_key:
        try:
            _media_path(previous_key).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    return get_brofile(guild_id, user_id) or {}


def remove_brofile_media(guild_id: str, user_id: str, media_type: str) -> bool:
    if media_type not in MEDIA_SIZES:
        raise ValueError("Unknown BROfile image type.")
    initialize_brofile_schema()
    storage_key = None
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT storage_key FROM brofile_media
            WHERE guild_id = ? AND user_id = ? AND media_type = ?
            """,
            (str(guild_id), str(user_id), media_type),
        ).fetchone()
        if row is None:
            return False
        storage_key = str(row["storage_key"])
        connection.execute(
            """
            DELETE FROM brofile_media
            WHERE guild_id = ? AND user_id = ? AND media_type = ?
            """,
            (str(guild_id), str(user_id), media_type),
        )
        connection.execute(
            """
            UPDATE brofiles
            SET revision = revision + 1, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (_now(), str(guild_id), str(user_id)),
        )
        connection.commit()
    try:
        _media_path(storage_key).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    return True
