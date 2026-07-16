"""Deterministic, private-safe server previews for Studio templates."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .registry import REGISTRY, TemplateDefinition
from .repository import (
    _connect,
    _deep_merge,
    _load_asset_references,
    _load_json,
    get_visual_template,
    initialize_visual_studio_schema,
    resolve_published_configuration,
)
from .runtime import prepare_background
from .storage import asset_bytes


PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
FONT_PATH = PROJECT_ROOT / "assets" / "OpenSansEmoji.ttf"


def _rgb(value: Any, fallback: Tuple[int, int, int]) -> Tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    try:
        return tuple(bytes.fromhex(text)) if len(text) == 6 else fallback
    except ValueError:
        return fallback


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), max(12, size))


def preview_configuration(template_key: str, draft: bool) -> Dict[str, Any]:
    if not draft:
        return resolve_published_configuration(template_key, use_cache=False)
    initialize_visual_studio_schema()
    definition = REGISTRY.get(template_key)
    template = get_visual_template(template_key)
    draft_settings = template["draft_settings"]
    if draft_settings is None:
        return resolve_published_configuration(template_key, use_cache=False)
    with _connect() as connection:
        global_row = connection.execute("SELECT settings_json FROM visual_global_settings WHERE id=1").fetchone()
        theme_id = template.get("draft_theme_id")
        theme = connection.execute("SELECT * FROM visual_themes WHERE id=? AND archived_at IS NULL", (theme_id,)).fetchone() if theme_id else None
        settings = _deep_merge(
            definition.defaults,
            _load_json(global_row[0] if global_row else "{}"),
            _load_json(
                theme["settings_json"]
                if theme and not bool(theme["is_builtin"])
                else "{}"
            ),
            draft_settings,
        )
        assets, warnings = _load_asset_references(connection, settings)
    return {
        "template_key": template_key,
        "definition": definition.as_dict(),
        "settings": settings,
        "assets": assets,
        "theme_id": theme_id,
        "theme_name": theme["name"] if theme else "Built-in defaults",
        "warnings": warnings,
        "published_version": template["published_version"],
        "draft": True,
    }


def _asset_data(configuration: Mapping[str, Any], slot: str) -> Optional[bytes]:
    asset = (configuration.get("assets") or {}).get(slot)
    if not asset:
        return None
    try:
        return asset_bytes(asset["storage_key"])
    except (OSError, ValueError):
        return None


def render_preview(
    template_key: str,
    *,
    draft: bool = False,
    edge_case: str = "maximum",
    safe_area: bool = False,
) -> bytes:
    definition = REGISTRY.get(template_key)
    configuration = preview_configuration(template_key, draft)
    settings = configuration["settings"]
    background = prepare_background(
        _asset_data(configuration, "background"),
        (definition.width, definition.height),
        settings,
    )
    if background:
        image = Image.open(io.BytesIO(background)).convert("RGB")
    else:
        image = Image.new("RGB", (definition.width, definition.height), (12, 13, 18))
    draw = ImageDraw.Draw(image, "RGBA")
    scale = min(definition.width / 1200, definition.height / 900)
    accent = _rgb(settings.get("accent_color"), (240, 49, 155))
    text = _rgb(settings.get("text_color"), (244, 244, 247))
    muted = _rgb(settings.get("muted_text_color"), (167, 168, 179))
    panel = _rgb(settings.get("panel_color"), (23, 24, 32))
    panel_alpha = int(max(0.0, min(float(settings.get("panel_opacity", 1.0)), 1.0)) * 255)
    margin = max(24, int(48 * scale))
    radius = max(10, int(22 * scale))

    if definition.key == "queue_next":
        draw.rounded_rectangle((70, 95, 625, 225), radius=24, fill=(*panel, min(panel_alpha, 220)))
        draw.ellipse((667, 45, 834, 212), fill=(*accent, 210), outline=(*text, 255), width=4)
        draw.text((120, 145), "@LongUnicodeName_🌈", font=_font(40), fill=(*text, 255))
    else:
        header_height = int(208 * min(definition.width / 1200, 1.33))
        draw.rounded_rectangle(
            (margin, margin, definition.width - margin, margin + header_height),
            radius=radius,
            fill=(*panel, min(panel_alpha, 235)),
            outline=(*accent, 255),
            width=max(2, int(3 * scale)),
        )
        title = str(settings.get("title") or definition.display_name)
        subtitle = str(settings.get("subtitle") or definition.description)
        draw.text((margin + 34, margin + 28), title[:70], font=_font(int(42 * scale)), fill=(*text, 255))
        draw.text((margin + 34, margin + int(94 * scale)), subtitle[:105], font=_font(int(20 * scale)), fill=(*muted, 255))
        content_top = margin + header_height + int(30 * scale)
        content_bottom = definition.height - margin - int(42 * scale)
        draw.rounded_rectangle(
            (margin, content_top, definition.width - margin, content_bottom),
            radius=radius,
            fill=(*panel, min(panel_alpha, 230)),
            outline=(*muted, 100),
            width=max(1, int(2 * scale)),
        )
        if definition.maximum_items:
            count = definition.maximum_items if edge_case == "maximum" else (0 if edge_case == "empty" else 3)
            if count == 0:
                draw.text((margin + 40, content_top + 70), "Nothing to show yet", font=_font(int(28 * scale)), fill=(*text, 255))
                draw.text((margin + 40, content_top + 120), "Empty-state preview", font=_font(int(18 * scale)), fill=(*muted, 255))
            else:
                available = content_bottom - content_top - int(80 * scale)
                row_height = max(int(54 * scale), available // count - int(8 * scale))
                for index in range(count):
                    y = content_top + int(55 * scale) + index * (row_height + int(6 * scale))
                    draw.rounded_rectangle((margin + 24, y, definition.width - margin - 24, y + row_height), radius=max(8, radius // 2), fill=(*(panel if index % 2 else (32, 33, 43)), 235))
                    avatar_size = min(row_height - 14, int(48 * scale))
                    draw.ellipse((margin + 40, y + 7, margin + 40 + avatar_size, y + 7 + avatar_size), fill=(*accent, 190))
                    label = "DecorativeUnicode_🌈_VeryLongName" if index == 0 else "Sample Member {}".format(index + 1)
                    draw.text((margin + 55 + avatar_size, y + max(7, row_height // 4)), label[:34], font=_font(max(14, int(21 * scale))), fill=(*text, 255))
                    value = "9,999,999" if index == 0 else "{:,}".format((count - index) * 1250)
                    value_width = draw.textlength(value, font=_font(max(14, int(20 * scale))))
                    draw.text((definition.width - margin - 50 - value_width, y + max(7, row_height // 4)), value, font=_font(max(14, int(20 * scale))), fill=(*text, 255))
        else:
            metrics = 3 if definition.key != "stats_error" else 1
            gap = int(18 * scale)
            inner_width = definition.width - margin * 2 - int(48 * scale)
            card_width = (inner_width - gap * (metrics - 1)) // metrics
            for index in range(metrics):
                x = margin + int(24 * scale) + index * (card_width + gap)
                draw.rounded_rectangle((x, content_top + int(44 * scale), x + card_width, content_top + int(205 * scale)), radius=radius // 2, fill=(32, 33, 43, 240), outline=(*accent, 190), width=2)
                draw.text((x + 22, content_top + int(72 * scale)), "Metric {}".format(index + 1), font=_font(int(18 * scale)), fill=(*muted, 255))
                draw.text((x + 22, content_top + int(116 * scale)), "{:,}".format((index + 1) * 1247), font=_font(int(38 * scale)), fill=(*text, 255))
        footer = str(settings.get("footer_text") or "BRO EDEN • VISUAL CONTENT STUDIO PREVIEW")
        draw.text((margin, definition.height - margin), footer[:90], font=_font(max(12, int(15 * scale))), fill=(*muted, 255))

    if safe_area:
        slot = definition.slot("background")
        safe = slot.safe_area
        draw.rectangle(
            (safe.left, safe.top, definition.width - safe.right, definition.height - safe.bottom),
            outline=(94, 211, 154, 255),
            width=max(2, int(3 * scale)),
        )
        for x, y, width, height, label in safe.obscured_regions:
            draw.rectangle((x, y, x + width, y + height), fill=(240, 49, 155, 45), outline=(240, 49, 155, 210), width=2)
            draw.text((x + 8, y + 6), label, font=_font(max(12, int(14 * scale))), fill=(255, 255, 255, 255))

    output = io.BytesIO()
    image.save(output, "PNG", optimize=True, compress_level=9)
    value = output.getvalue()
    if len(value) > definition.max_output_bytes:
        raise ValueError("Preview exceeded the configured Discord file-size target.")
    return value


def validate_preview(template_key: str, draft: bool = True) -> None:
    data = render_preview(template_key, draft=draft, edge_case="maximum")
    with Image.open(io.BytesIO(data)) as image:
        definition = REGISTRY.get(template_key)
        if image.size != (definition.width, definition.height):
            raise ValueError("Preview dimensions do not match the registered canvas.")
