import io
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Tuple

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = PROJECT_ROOT / "assets" / "OpenSansEmoji.ttf"

WIDTH = 1500
HEIGHT = 780
PADDING = 52
BACKGROUND = (18, 20, 24)
PANEL = (29, 32, 38)
PANEL_ALT = (35, 38, 45)
TEXT = (241, 243, 247)
MUTED = (170, 177, 190)


def render_rolecompare_report(
    *,
    title: str,
    body: str,
    role_1_name: str,
    role_2_name: str,
    counts: Dict[str, int],
    updated_at: datetime,
    accent_color: int,
) -> bytes:
    image, draw, fonts, accent = _base_card(title, body, updated_at, accent_color)
    cards = [
        (role_1_name, counts["role_1_total"]),
        (role_2_name, counts["role_2_total"]),
        ("In both", counts["both"]),
        (f"Only {role_1_name}", counts["role_1_only"]),
        (f"Only {role_2_name}", counts["role_2_only"]),
    ]

    card_y = 260
    gap = 18
    card_width = (WIDTH - PADDING * 2 - gap * 4) // 5
    maximum = max((value for _, value in cards), default=1) or 1
    colors = [
        accent,
        _lighten(accent, 0.22),
        (88, 101, 242),
        _lighten(accent, 0.1),
        _lighten(accent, 0.34),
    ]

    for index, ((label, value), color) in enumerate(zip(cards, colors)):
        x = PADDING + index * (card_width + gap)
        _metric_card(
            draw,
            x,
            card_y,
            card_width,
            185,
            label,
            value,
            maximum,
            color,
            fonts,
        )

    diagram_y = 575
    center_x = WIDTH // 2
    radius = 112
    left_center = center_x - 75
    right_center = center_x + 75
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.ellipse(
        (
            left_center - radius,
            diagram_y - radius,
            left_center + radius,
            diagram_y + radius,
        ),
        fill=(*accent, 105),
        outline=(*accent, 235),
        width=4,
    )
    second = _lighten(accent, 0.28)
    overlay_draw.ellipse(
        (
            right_center - radius,
            diagram_y - radius,
            right_center + radius,
            diagram_y + radius,
        ),
        fill=(*second, 105),
        outline=(*second, 235),
        width=4,
    )
    image.alpha_composite(overlay)
    draw = ImageDraw.Draw(image)
    _center_text(draw, left_center - 72, diagram_y - 10, str(counts["role_1_only"]), fonts["metric"], TEXT)
    _center_text(draw, center_x, diagram_y - 10, str(counts["both"]), fonts["metric"], TEXT)
    _center_text(draw, right_center + 72, diagram_y - 10, str(counts["role_2_only"]), fonts["metric"], TEXT)
    _center_text(draw, left_center - 72, diagram_y + 43, "only", fonts["small"], MUTED)
    _center_text(draw, center_x, diagram_y + 43, "both", fonts["small"], MUTED)
    _center_text(draw, right_center + 72, diagram_y + 43, "only", fonts["small"], MUTED)

    return _save_png(image)


def render_missingrole_report(
    *,
    title: str,
    body: str,
    has_role_name: str,
    missing_role_name: str,
    has_role_total: int,
    missing_role_total: int,
    missing_count: int,
    missing_percent: float,
    updated_at: datetime,
    accent_color: int,
) -> bytes:
    image, draw, fonts, accent = _base_card(title, body, updated_at, accent_color)
    cards = [
        (f"With {has_role_name}", has_role_total),
        (f"With {missing_role_name}", missing_role_total),
        ("Missing required role", missing_count),
    ]
    gap = 24
    card_width = (WIDTH - PADDING * 2 - gap * 2) // 3
    maximum = max(has_role_total, missing_role_total, missing_count, 1)
    for index, (label, value) in enumerate(cards):
        x = PADDING + index * (card_width + gap)
        _metric_card(
            draw,
            x,
            270,
            card_width,
            190,
            label,
            value,
            maximum,
            accent if index != 2 else (237, 88, 101),
            fonts,
        )

    gauge_x = PADDING
    gauge_y = 535
    gauge_width = WIDTH - PADDING * 2
    draw.rounded_rectangle(
        (gauge_x, gauge_y, gauge_x + gauge_width, gauge_y + 92),
        radius=18,
        fill=PANEL,
    )
    draw.text(
        (gauge_x + 28, gauge_y + 20),
        f"{missing_percent:.1f}% missing {missing_role_name}",
        font=fonts["section"],
        fill=TEXT,
    )
    bar_x = gauge_x + 28
    bar_y = gauge_y + 61
    bar_width = gauge_width - 56
    draw.rounded_rectangle(
        (bar_x, bar_y, bar_x + bar_width, bar_y + 14),
        radius=7,
        fill=PANEL_ALT,
    )
    filled = int(bar_width * min(max(missing_percent, 0), 100) / 100)
    if filled:
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + filled, bar_y + 14),
            radius=7,
            fill=(237, 88, 101),
        )

    return _save_png(image)


def render_report_error(
    *,
    title: str,
    message: str,
    updated_at: datetime,
    accent_color: int,
) -> bytes:
    image, draw, fonts, accent = _base_card(title, "", updated_at, accent_color)
    draw.rounded_rectangle(
        (PADDING, 285, WIDTH - PADDING, 560),
        radius=24,
        fill=PANEL,
        outline=(237, 88, 101),
        width=4,
    )
    draw.text(
        (PADDING + 36, 330),
        "Report unavailable",
        font=fonts["section"],
        fill=(237, 88, 101),
    )
    lines = _wrap_text(draw, _plain_text(message), fonts["body"], WIDTH - PADDING * 2 - 72)
    y = 395
    for line in lines[:5]:
        draw.text((PADDING + 36, y), line, font=fonts["body"], fill=TEXT)
        y += 34
    return _save_png(image)


def _base_card(
    title: str,
    body: str,
    updated_at: datetime,
    accent_color: int,
):
    image = Image.new("RGBA", (WIDTH, HEIGHT), (*BACKGROUND, 255))
    draw = ImageDraw.Draw(image)
    accent = _rgb_from_int(accent_color)
    fonts = {
        "title": ImageFont.truetype(str(FONT_PATH), 42),
        "body": ImageFont.truetype(str(FONT_PATH), 22),
        "section": ImageFont.truetype(str(FONT_PATH), 27),
        "metric": ImageFont.truetype(str(FONT_PATH), 38),
        "label": ImageFont.truetype(str(FONT_PATH), 18),
        "small": ImageFont.truetype(str(FONT_PATH), 16),
    }
    draw.rounded_rectangle(
        (PADDING, PADDING, WIDTH - PADDING, 220),
        radius=24,
        fill=PANEL,
    )
    draw.rounded_rectangle(
        (PADDING, PADDING, PADDING + 8, 220),
        radius=4,
        fill=accent,
    )
    draw.text(
        (PADDING + 30, PADDING + 23),
        _fit_text(draw, _plain_text(title), fonts["title"], WIDTH - PADDING * 2 - 60),
        font=fonts["title"],
        fill=TEXT,
    )
    body_lines = _wrap_text(
        draw,
        _plain_text(body),
        fonts["body"],
        WIDTH - PADDING * 2 - 60,
    )
    y = PADDING + 88
    for line in body_lines[:3]:
        draw.text((PADDING + 30, y), line, font=fonts["body"], fill=MUTED)
        y += 31
    timestamp = updated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    footer = f"Last updated {timestamp}"
    footer_width = draw.textlength(footer, font=fonts["small"])
    draw.text(
        (WIDTH - PADDING - footer_width, HEIGHT - 34),
        footer,
        font=fonts["small"],
        fill=MUTED,
    )
    return image, draw, fonts, accent


def _metric_card(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    height: int,
    label: str,
    value: int,
    maximum: int,
    color: Tuple[int, int, int],
    fonts: Dict[str, ImageFont.FreeTypeFont],
) -> None:
    draw.rounded_rectangle((x, y, x + width, y + height), radius=20, fill=PANEL)
    draw.text(
        (x + 22, y + 22),
        _fit_text(draw, label, fonts["label"], width - 44),
        font=fonts["label"],
        fill=MUTED,
    )
    draw.text((x + 22, y + 60), f"{value:,}", font=fonts["metric"], fill=TEXT)
    bar_x = x + 22
    bar_y = y + height - 37
    bar_width = width - 44
    draw.rounded_rectangle(
        (bar_x, bar_y, bar_x + bar_width, bar_y + 13),
        radius=6,
        fill=PANEL_ALT,
    )
    filled = max(4, int(bar_width * value / maximum)) if value else 0
    if filled:
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + filled, bar_y + 13),
            radius=6,
            fill=color,
        )


def _plain_text(value: str) -> str:
    value = re.sub(r"\*\*(.*?)\*\*", r"\1", value or "", flags=re.DOTALL)
    value = re.sub(r"__(.*?)__", r"\1", value, flags=re.DOTALL)
    value = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?!\*)", r"\1", value, flags=re.DOTALL)
    value = re.sub(r"(?<!_)_(?!_)(.*?)_(?!_)", r"\1", value, flags=re.DOTALL)
    value = re.sub(r"`{1,3}(.*?)`{1,3}", r"\1", value, flags=re.DOTALL)
    return value.strip()


def _wrap_text(draw, text, font, max_width) -> Iterable[str]:
    lines = []
    for paragraph in text.splitlines():
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _fit_text(draw, text, font, max_width) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle].rstrip() + "…"
        if draw.textlength(candidate, font=font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def _center_text(draw, x, y, text, font, fill):
    width = draw.textlength(text, font=font)
    draw.text((x - width / 2, y), text, font=font, fill=fill)


def _lighten(color: Tuple[int, int, int], amount: float):
    return tuple(int(component + (255 - component) * amount) for component in color)


def _rgb_from_int(color: int):
    return ((color >> 16) & 255, (color >> 8) & 255, color & 255)


def _save_png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.convert("RGB").save(output, "PNG", optimize=True, compress_level=9)
    return output.getvalue()
