"""Runtime bridge between published Studio configuration and Pillow renderers."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Mapping, Optional, Tuple

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .registry import REGISTRY
from .repository import resolve_published_configuration
from .storage import asset_bytes


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeCustomization:
    template_key: str
    settings: Dict[str, Any]
    background_bytes: Optional[bytes]
    header_bytes: Optional[bytes]
    logo_bytes: Optional[bytes]
    watermark_bytes: Optional[bytes]
    warnings: Tuple[str, ...]
    customized: bool
    canvas_width: int
    canvas_height: int


def _hex_int(value: Any, fallback: int) -> int:
    text = str(value or "").strip().lstrip("#")
    try:
        return int(text, 16) if len(text) == 6 else fallback
    except ValueError:
        return fallback


def load_runtime_customization_sync(
    template_key: str,
    *,
    legacy_background: Optional[bytes] = None,
    legacy_header: Optional[bytes] = None,
) -> RuntimeCustomization:
    warnings = []
    try:
        resolved = resolve_published_configuration(template_key)
        settings = dict(resolved["settings"])
        warnings.extend(resolved.get("warnings") or [])
        assets = resolved.get("assets") or {}
        background = legacy_background
        header = legacy_header
        logo = None
        watermark = None
        if "background" in assets:
            try:
                background = asset_bytes(assets["background"]["storage_key"])
            except (OSError, ValueError) as exc:
                warnings.append("Custom background was unavailable; the legacy background was used.")
                logger.warning("Visual asset fallback template=%s slot=background error=%s", template_key, type(exc).__name__)
        if "header_graphic" in assets:
            try:
                header = asset_bytes(assets["header_graphic"]["storage_key"])
            except (OSError, ValueError) as exc:
                warnings.append("Custom header was unavailable; the legacy header was used.")
                logger.warning("Visual asset fallback template=%s slot=header_graphic error=%s", template_key, type(exc).__name__)
        for slot in ("logo", "watermark"):
            if slot not in assets:
                continue
            try:
                value = asset_bytes(assets[slot]["storage_key"])
            except (OSError, ValueError) as exc:
                warnings.append("Custom {} was unavailable and was skipped.".format(slot))
                logger.warning("Visual asset fallback template=%s slot=%s error=%s", template_key, slot, type(exc).__name__)
                continue
            if slot == "logo":
                logo = value
            else:
                watermark = value
        return RuntimeCustomization(
            template_key=template_key,
            settings=settings,
            background_bytes=background,
            header_bytes=header,
            logo_bytes=logo,
            watermark_bytes=watermark,
            warnings=tuple(warnings),
            customized=bool(resolved.get("customized")),
            canvas_width=int((resolved.get("canvas") or {}).get("width") or REGISTRY.get(template_key).width),
            canvas_height=int((resolved.get("canvas") or {}).get("height") or REGISTRY.get(template_key).height),
        )
    except Exception as exc:
        logger.exception("Visual customization resolution failed template=%s; using legacy renderer", template_key)
        return RuntimeCustomization(
            template_key=template_key,
            settings=dict(REGISTRY.get(template_key).defaults),
            background_bytes=legacy_background,
            header_bytes=legacy_header,
            logo_bytes=None,
            watermark_bytes=None,
            warnings=("Renderer fell back to built-in defaults.",),
            customized=False,
            canvas_width=REGISTRY.get(template_key).width,
            canvas_height=REGISTRY.get(template_key).height,
        )


async def load_runtime_customization(
    template_key: str,
    *,
    legacy_background: Optional[bytes] = None,
    legacy_header: Optional[bytes] = None,
) -> RuntimeCustomization:
    return await asyncio.to_thread(
        load_runtime_customization_sync,
        template_key,
        legacy_background=legacy_background,
        legacy_header=legacy_header,
    )


@lru_cache(maxsize=16)
def _prepare_background_cached(
    data: Optional[bytes],
    size: Tuple[int, int],
    settings_json: str,
) -> Optional[bytes]:
    if not data:
        return None
    settings = json.loads(settings_json)
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            fit = str(settings.get("background_fit", "cover"))
            focal = (
                max(0.0, min(float(settings.get("focal_x", 0.5)), 1.0)),
                max(0.0, min(float(settings.get("focal_y", 0.5)), 1.0)),
            )
            if fit == "contain":
                contained = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
                prepared = Image.new("RGB", size, (12, 13, 18))
                prepared.paste(contained, ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2))
            elif fit == "stretch":
                prepared = image.resize(size, Image.Resampling.LANCZOS)
            elif fit == "tile":
                prepared = Image.new("RGB", size, (12, 13, 18))
                tile = image.copy()
                if tile.width > size[0] or tile.height > size[1]:
                    tile.thumbnail(size, Image.Resampling.LANCZOS)
                for x in range(0, size[0], max(1, tile.width)):
                    for y in range(0, size[1], max(1, tile.height)):
                        prepared.paste(tile, (x, y))
            else:
                prepared = ImageOps.fit(image, size, Image.Resampling.LANCZOS, centering=focal)
            blur = float(settings.get("background_blur", 0.0))
            if blur:
                prepared = prepared.filter(ImageFilter.GaussianBlur(min(30.0, max(0.0, blur))))
            prepared = ImageEnhance.Brightness(prepared).enhance(float(settings.get("background_brightness", 1.0)))
            prepared = ImageEnhance.Color(prepared).enhance(float(settings.get("background_saturation", 1.0)))
            prepared = ImageEnhance.Contrast(prepared).enhance(float(settings.get("background_contrast", 1.0)))
            output = io.BytesIO()
            prepared.save(output, "PNG", optimize=True, compress_level=7)
            return output.getvalue()
    except (OSError, ValueError, TypeError):
        logger.warning("Custom background processing failed; renderer will use its built-in canvas")
        return None


def prepare_background(
    data: Optional[bytes],
    size: Tuple[int, int],
    settings: Mapping[str, Any],
) -> Optional[bytes]:
    """Return a bounded-cache renderer-ready background derivative."""
    relevant = {
        key: settings.get(key)
        for key in (
            "background_fit",
            "focal_x",
            "focal_y",
            "background_blur",
            "background_brightness",
            "background_saturation",
            "background_contrast",
        )
    }
    return _prepare_background_cached(
        data,
        size,
        json.dumps(relevant, sort_keys=True, separators=(",", ":")),
    )


def runtime_accent(settings: Mapping[str, Any], fallback: int) -> int:
    return _hex_int(settings.get("accent_color"), fallback)


def runtime_text(settings: Mapping[str, Any], key: str, fallback: str) -> str:
    value = str(settings.get(key, "") or "").strip()
    return value or fallback
