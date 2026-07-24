"""Safe persistent storage for Visual Content Studio image assets."""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageOps, UnidentifiedImageError

from .registry import (
    MAX_UPLOAD_BYTES,
    REGISTRY,
    AssetSlot,
    asset_type_guidance,
)
from .repository import (
    _audit,
    _connect,
    initialize_visual_studio_schema,
    invalidate_visual_cache,
    utcnow,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_TYPES = (
    "background",
    "logo",
    "watermark",
    "overlay",
    "header_graphic",
    "texture",
    "icon",
    "avatar_frame",
    "badge",
    "event_banner",
    "other",
)
MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}


def _brofile_badge_usage_count(
    connection: Any,
    asset_id: int,
) -> int:
    """Count active role-badge references without coupling schema startup."""
    from utils.brofiles import badge_asset_usage_count

    return badge_asset_usage_count(connection, asset_id)


def visual_asset_directory() -> Path:
    configured = os.getenv("VISUAL_ASSET_DIR", "").strip()
    path = Path(configured).expanduser() if configured else PROJECT_ROOT / "data" / "visual-assets"
    return path.resolve()


def ensure_asset_directories() -> Path:
    root = visual_asset_directory()
    for child in ("normalized", "thumbnails", "previews", "cache", "tmp"):
        (root / child).mkdir(parents=True, exist_ok=True)
    return root


def _safe_child(storage_key: str) -> Path:
    if not storage_key or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/." for character in storage_key):
        raise ValueError("Invalid asset storage key.")
    root = ensure_asset_directories()
    candidate = (root / storage_key).resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError("Asset path is outside the configured storage directory.")
    return candidate


def asset_path(storage_key: str) -> Path:
    path = _safe_child(storage_key)
    if not path.is_file():
        raise FileNotFoundError("Visual asset file is unavailable.")
    return path


def _discord_asset_bytes(source_url: str) -> bytes:
    parsed = urllib.parse.urlparse(str(source_url or ""))
    if parsed.scheme != "https" or parsed.hostname not in {"cdn.discordapp.com", "media.discordapp.net"}:
        raise ValueError("Visual asset Discord source URL is invalid.")
    request = urllib.request.Request(
        source_url,
        headers={"User-Agent": "BroEdenBot/VisualContentStudio"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        data = response.read(MAX_UPLOAD_BYTES + 1)
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Visual asset Discord source is empty or too large.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("Visual asset Discord source is not a valid image.") from exc
    return data


def asset_bytes(storage_key: str, source_url: Optional[str] = None) -> bytes:
    try:
        return asset_path(storage_key).read_bytes()
    except FileNotFoundError:
        if not source_url:
            raise
    data = _discord_asset_bytes(source_url)
    target = _safe_child(storage_key)
    temporary = target.with_name("{}.{}.tmp".format(target.name, secrets.token_hex(4)))
    try:
        temporary.write_bytes(data)
        os.replace(str(temporary), str(target))
    finally:
        temporary.unlink(missing_ok=True)
    return data


def _slot(template_key: Optional[str], slot_key: Optional[str]) -> Optional[AssetSlot]:
    if not template_key or not slot_key:
        return None
    return REGISTRY.get(template_key).slot(slot_key)


def _aspect_delta(width: int, height: int, target_width: int, target_height: int) -> float:
    return abs((width / height) - (target_width / target_height)) / (target_width / target_height)


def inspect_upload(
    data: bytes,
    *,
    filename: str,
    asset_type: str,
    template_key: Optional[str] = None,
    slot_key: Optional[str] = None,
) -> Dict[str, Any]:
    if not data:
        raise ValueError("Choose an image file to upload.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("The file exceeds the 10 MB upload limit.")
    if asset_type not in ASSET_TYPES:
        raise ValueError("Unsupported visual asset type.")
    extension = Path(filename or "").suffix.casefold()
    if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("Supported files are PNG, JPG, and WEBP.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.seek(0)
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            detected_format = str(image.format or "").upper()
            if detected_format not in MIME_BY_FORMAT:
                raise ValueError("The detected image format is not supported.")
            expected_extensions = {
                "PNG": {".png"},
                "JPEG": {".jpg", ".jpeg"},
                "WEBP": {".webp"},
            }[detected_format]
            if extension not in expected_extensions:
                raise ValueError("The file extension does not match the detected image type.")
            if getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) > 1:
                raise ValueError("Animated images are not supported. Upload a still PNG, JPG, or WEBP.")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > 40_000_000:
                raise ValueError("Image dimensions are invalid or excessively large.")
            has_alpha = "A" in image.mode or (
                image.mode == "P" and "transparency" in image.info
            )
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("Asset file could not be decoded.") from exc

    slot = _slot(template_key, slot_key)
    guidance = slot.as_dict() if slot else asset_type_guidance(asset_type)
    target_width = int(guidance["recommended_width"])
    target_height = int(guidance["recommended_height"])
    too_small = width < int(guidance["minimum_width"]) or height < int(guidance["minimum_height"])
    too_large = width > int(guidance["maximum_width"]) or height > int(guidance["maximum_height"])
    wrong_aspect = (
        guidance.get("aspect_ratio") != "varies"
        and _aspect_delta(width, height, target_width, target_height) > 0.015
    )
    transparency_requirement = guidance.get("transparency", "supported")
    missing_alpha = transparency_requirement == "required" and not has_alpha
    warnings = []
    if wrong_aspect:
        warnings.append(
            "This image is {} x {} px ({}). The slot recommends {} x {} px ({}); continuing will crop part of the image.".format(
                width,
                height,
                "{:.3f}:1".format(width / height),
                target_width,
                target_height,
                guidance.get("aspect_ratio", "target ratio"),
            )
        )
    if too_small:
        warnings.append(
            "This image is below the minimum accepted dimensions and may look soft after resizing."
        )
    if too_large:
        warnings.append("This image exceeds the maximum dimensions and will be normalized before storage.")
    if missing_alpha:
        warnings.append("This asset slot requires transparency, but the selected image has no alpha channel.")
    return {
        "filename": Path(filename or "upload").name[:255],
        "format": detected_format,
        "mime_type": MIME_BY_FORMAT[detected_format],
        "width": width,
        "height": height,
        "aspect_ratio": "{:.4f}:1".format(width / height),
        "file_size": len(data),
        "has_alpha": has_alpha,
        "too_small": too_small,
        "too_large": too_large,
        "wrong_aspect": wrong_aspect,
        "missing_alpha": missing_alpha,
        "warnings": warnings,
        "compatible": not (too_small or missing_alpha),
        "guidance": guidance,
        "checksum": hashlib.sha256(data).hexdigest(),
    }


def _normalized_image(
    data: bytes,
    *,
    inspection: Dict[str, Any],
    slot: Optional[AssetSlot],
    focal_x: float,
    focal_y: float,
) -> Image.Image:
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source)
        preserve_alpha = bool(inspection["has_alpha"]) or (
            slot is not None and slot.transparency in {"required", "supported"}
        )
        image = image.convert("RGBA" if preserve_alpha else "RGB")
        if slot is not None:
            size = (slot.recommended_width, slot.recommended_height)
            if slot.fit == "contain":
                contained = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA" if preserve_alpha else "RGB", size, (0, 0, 0, 0) if preserve_alpha else (12, 13, 18))
                canvas.paste(contained, ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2), contained if preserve_alpha else None)
                image = canvas
            else:
                image = ImageOps.fit(
                    image,
                    size,
                    Image.Resampling.LANCZOS,
                    centering=(max(0.0, min(focal_x, 1.0)), max(0.0, min(focal_y, 1.0))),
                )
        elif inspection["guidance"].get("aspect_ratio") != "varies":
            guidance = inspection["guidance"]
            size = (
                int(guidance["recommended_width"]),
                int(guidance["recommended_height"]),
            )
            contained = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
            canvas = Image.new(
                "RGBA" if preserve_alpha else "RGB",
                size,
                (0, 0, 0, 0) if preserve_alpha else (12, 13, 18),
            )
            canvas.paste(
                contained,
                ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2),
                contained if preserve_alpha else None,
            )
            image = canvas
        else:
            maximum = asset_type_guidance("other")
            image.thumbnail((maximum["maximum_width"], maximum["maximum_height"]), Image.Resampling.LANCZOS)
        return image.copy()


def _save_normalized(image: Image.Image, path: Path) -> None:
    output = image
    if output.mode not in {"RGB", "RGBA"}:
        output = output.convert("RGBA" if "A" in output.mode else "RGB")
    output.save(path, "PNG", optimize=True, compress_level=9)


def _save_thumbnail(image: Image.Image, path: Path) -> None:
    thumbnail = image.copy()
    thumbnail.thumbnail((480, 300), Image.Resampling.LANCZOS)
    if thumbnail.mode not in {"RGB", "RGBA"}:
        thumbnail = thumbnail.convert("RGBA" if "A" in thumbnail.mode else "RGB")
    thumbnail.save(path, "PNG", optimize=True, compress_level=9)


def save_asset(
    data: bytes,
    *,
    filename: str,
    name: str,
    asset_type: str,
    actor: str,
    template_key: Optional[str] = None,
    slot_key: Optional[str] = None,
    focal_x: float = 0.5,
    focal_y: float = 0.5,
    acknowledge_quality: bool = False,
    allow_crop: bool = False,
    replace_asset_id: Optional[int] = None,
) -> Tuple[int, Dict[str, Any]]:
    initialize_visual_studio_schema()
    inspection = inspect_upload(
        data,
        filename=filename,
        asset_type=asset_type,
        template_key=template_key,
        slot_key=slot_key,
    )
    if inspection["wrong_aspect"] and not allow_crop:
        raise ValueError("Asset aspect ratio does not match the destination. Confirm the aspect-ratio adjustment to continue.")
    if inspection["too_small"] and not acknowledge_quality:
        raise ValueError("Asset dimensions are below the minimum. Confirm the quality warning to continue.")
    if inspection["missing_alpha"]:
        raise ValueError("This asset slot requires a transparent PNG or WEBP.")
    slot = _slot(template_key, slot_key)
    normalized = _normalized_image(
        data,
        inspection=inspection,
        slot=slot,
        focal_x=focal_x,
        focal_y=focal_y,
    )
    inspection["normalized_width"] = normalized.width
    inspection["normalized_height"] = normalized.height
    storage_token = "{}-{}".format(inspection["checksum"][:16], secrets.token_hex(5))
    storage_key = "normalized/{}.png".format(storage_token)
    thumbnail_key = "thumbnails/{}.png".format(storage_token)
    normalized_path = _safe_child(storage_key)
    thumbnail_path = _safe_child(thumbnail_key)
    _save_normalized(normalized, normalized_path)
    _save_thumbnail(normalized, thumbnail_path)
    clean_name = " ".join(str(name or Path(filename).stem).split())[:100]
    now = utcnow()
    metadata = {
        "source": inspection,
        "template_key": template_key,
        "slot_key": slot_key,
        "focal_x": max(0.0, min(float(focal_x), 1.0)),
        "focal_y": max(0.0, min(float(focal_y), 1.0)),
        "thumbnail_key": thumbnail_key,
        "normalized": True,
    }
    try:
        with _connect() as connection:
            duplicate = connection.execute(
                "SELECT id, name FROM visual_assets WHERE checksum=? AND archived_at IS NULL ORDER BY id LIMIT 1",
                (inspection["checksum"],),
            ).fetchone()
            if duplicate and replace_asset_id is None:
                raise ValueError("An identical asset already exists as '{}' (asset #{}).".format(duplicate["name"], duplicate["id"]))
            if replace_asset_id is None:
                cursor = connection.execute(
                    """
                    INSERT INTO visual_assets(name,asset_type,storage_key,original_filename,mime_type,width,height,aspect_ratio,file_size,checksum,uploaded_by,created_at,updated_at,metadata_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (clean_name, asset_type, storage_key, inspection["filename"], "image/png", normalized.width, normalized.height, "{:.4f}:1".format(normalized.width / normalized.height), normalized_path.stat().st_size, inspection["checksum"], actor, now, now, json.dumps(metadata, sort_keys=True)),
                )
                asset_id = int(cursor.lastrowid)
                action = "asset_uploaded"
            else:
                existing = connection.execute("SELECT * FROM visual_assets WHERE id=?", (replace_asset_id,)).fetchone()
                if existing is None:
                    raise ValueError("Asset to replace was not found.")
                connection.execute(
                    """
                    UPDATE visual_assets SET name=?,asset_type=?,storage_key=?,original_filename=?,mime_type='image/png',width=?,height=?,aspect_ratio=?,file_size=?,checksum=?,updated_at=?,archived_at=NULL,metadata_json=? WHERE id=?
                    """,
                    (clean_name, asset_type, storage_key, inspection["filename"], normalized.width, normalized.height, "{:.4f}:1".format(normalized.width / normalized.height), normalized_path.stat().st_size, inspection["checksum"], now, json.dumps(metadata, sort_keys=True), replace_asset_id),
                )
                asset_id = replace_asset_id
                action = "asset_replaced"
                for key in (existing["storage_key"], json.loads(existing["metadata_json"] or "{}").get("thumbnail_key")):
                    if key:
                        try:
                            _safe_child(key).unlink(missing_ok=True)
                        except (OSError, ValueError):
                            pass
            _audit(connection, action, "asset", asset_id, "Visual asset {}.".format("uploaded" if action.endswith("uploaded") else "replaced"), actor, {"asset_type": asset_type, "template_key": template_key, "slot_key": slot_key})
            connection.commit()
    except Exception:
        normalized_path.unlink(missing_ok=True)
        thumbnail_path.unlink(missing_ok=True)
        raise
    invalidate_visual_cache()
    return asset_id, inspection


def list_assets(
    *,
    asset_type: Optional[str] = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    initialize_visual_studio_schema()
    clauses = []
    parameters: List[Any] = []
    if asset_type:
        clauses.append("a.asset_type=?")
        parameters.append(asset_type)
    if not include_archived:
        clauses.append("a.archived_at IS NULL")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    parameters.extend((max(1, min(limit, 200)), max(0, offset)))
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT a.*, d.storage_thread_id AS discord_storage_thread_id,
                   d.message_id AS discord_message_id,
                   d.attachment_url AS discord_attachment_url,
                   d.sync_status AS discord_storage_status,
                   d.last_error AS discord_storage_error,
                   (
                       SELECT j.status
                       FROM visual_asset_storage_jobs j
                       WHERE j.asset_id = a.id
                       ORDER BY j.id DESC LIMIT 1
                   ) AS discord_storage_job_status,
                   COUNT(u.id) AS usage_count
            FROM visual_assets a
            LEFT JOIN visual_asset_discord_storage d ON d.asset_id=a.id
            LEFT JOIN visual_asset_usage u ON u.asset_id=a.id
            {} GROUP BY a.id ORDER BY a.updated_at DESC LIMIT ? OFFSET ?
            """.format(where),
            tuple(parameters),
        ).fetchall()
    result = []
    with _connect() as usage_connection:
        for row in rows:
            value = dict(row)
            value["metadata"] = json.loads(value.get("metadata_json") or "{}")
            value["usage_count"] = (
                int(value.get("usage_count") or 0)
                + _brofile_badge_usage_count(usage_connection, int(value["id"]))
            )
            result.append(value)
    return result


def get_asset(asset_id: int) -> Optional[Dict[str, Any]]:
    initialize_visual_studio_schema()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT a.*, d.storage_thread_id AS discord_storage_thread_id,
                   d.message_id AS discord_message_id,
                   d.attachment_url AS discord_attachment_url,
                   d.sync_status AS discord_storage_status,
                   d.last_error AS discord_storage_error,
                   (
                       SELECT j.status
                       FROM visual_asset_storage_jobs j
                       WHERE j.asset_id = a.id
                       ORDER BY j.id DESC LIMIT 1
                   ) AS discord_storage_job_status
            FROM visual_assets a
            LEFT JOIN visual_asset_discord_storage d ON d.asset_id=a.id
            WHERE a.id=?
            """,
            (asset_id,),
        ).fetchone()
        if row is None:
            return None
        usages = [
            dict(item)
            for item in connection.execute(
                """
                SELECT u.*, vt.template_key, vt.display_name AS template_name, th.name AS theme_name, v.name AS variant_name
                FROM visual_asset_usage u
                LEFT JOIN visual_templates vt ON vt.id=u.template_id
                LEFT JOIN visual_themes th ON th.id=u.theme_id
                LEFT JOIN visual_template_variants v ON v.id=u.variant_id
                WHERE u.asset_id=? ORDER BY u.id
                """,
                (asset_id,),
            ).fetchall()
        ]
    value = dict(row)
    value["metadata"] = json.loads(value.get("metadata_json") or "{}")
    brofile_usage_count = 0
    with _connect() as connection:
        brofile_usage_count = _brofile_badge_usage_count(connection, asset_id)
    if brofile_usage_count:
        usages.append(
            {
                "usage_slot": "BROfile role badge",
                "external_type": "brofile_badge",
                "reference_count": brofile_usage_count,
            }
        )
    value["usages"] = usages
    value["usage_count"] = (
        len(usages) - (1 if brofile_usage_count else 0) + brofile_usage_count
    )
    return value


def rename_asset(asset_id: int, name: str, actor: str) -> None:
    clean = " ".join(str(name).split())[:100]
    if not clean:
        raise ValueError("Asset name is required.")
    with _connect() as connection:
        connection.execute("UPDATE visual_assets SET name=?,updated_at=? WHERE id=?", (clean, utcnow(), asset_id))
        if connection.total_changes == 0:
            raise ValueError("Asset was not found.")
        _audit(connection, "asset_renamed", "asset", asset_id, "Visual asset renamed.", actor)
        connection.commit()
    invalidate_visual_cache()


def archive_asset(asset_id: int, actor: str, *, restore: bool = False) -> None:
    with _connect() as connection:
        if not restore and (
            connection.execute(
                "SELECT 1 FROM visual_asset_usage WHERE asset_id=? LIMIT 1",
                (asset_id,),
            ).fetchone()
            or _brofile_badge_usage_count(connection, asset_id)
        ):
            raise ValueError("This asset is actively referenced. Remove or replace it before archiving.")
        archived_at = None if restore else utcnow()
        connection.execute("UPDATE visual_assets SET archived_at=?,updated_at=? WHERE id=?", (archived_at, utcnow(), asset_id))
        if connection.total_changes == 0:
            raise ValueError("Asset was not found.")
        _audit(connection, "asset_restored" if restore else "asset_archived", "asset", asset_id, "Visual asset {}.".format("restored" if restore else "archived"), actor)
        connection.commit()
    invalidate_visual_cache()


def delete_asset(asset_id: int, actor: str) -> bool:
    with _connect() as connection:
        row = connection.execute("SELECT * FROM visual_assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise ValueError("Asset was not found.")
        if not row["archived_at"]:
            raise ValueError("Archive the asset before permanently deleting it.")
        if (
            connection.execute(
                "SELECT 1 FROM visual_asset_usage WHERE asset_id=? LIMIT 1",
                (asset_id,),
            ).fetchone()
            or _brofile_badge_usage_count(connection, asset_id)
        ):
            raise ValueError("This asset is still referenced and cannot be deleted.")
        from .discord_storage import prepare_asset_deletion

        discord_delete_queued = prepare_asset_deletion(connection, asset_id, actor)
        metadata = json.loads(row["metadata_json"] or "{}")
        connection.execute("DELETE FROM visual_assets WHERE id=?", (asset_id,))
        _audit(connection, "asset_deleted", "asset", asset_id, "Archived visual asset permanently deleted.", actor)
        connection.commit()
    for key in (row["storage_key"], metadata.get("thumbnail_key")):
        if key:
            try:
                _safe_child(key).unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
    invalidate_visual_cache()
    return discord_delete_queued


def clean_preview_cache(maximum_age_seconds: int = 86_400) -> int:
    root = ensure_asset_directories() / "previews"
    removed = 0
    import time
    threshold = time.time() - max(60, maximum_age_seconds)
    for path in root.glob("*.png"):
        try:
            if path.stat().st_mtime < threshold:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
