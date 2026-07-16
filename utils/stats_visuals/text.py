import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from PIL import ImageDraw, ImageFont

from .models import RenderState
from .theme import FALLBACK_FONTS, PRIMARY_FONT, TYPOGRAPHY


FONT_FAMILIES = {
    "Open Sans Emoji": PRIMARY_FONT,
    "Calibri Regular": FALLBACK_FONTS[0],
    "Calibri": FALLBACK_FONTS[1],
}


def load_font(
    role: str,
    scale: float = 1.0,
    family: Optional[str] = None,
    *,
    size_override: Optional[float] = None,
) -> ImageFont.FreeTypeFont:
    base_size = float(size_override) if size_override is not None else TYPOGRAPHY[role]
    size = max(10, int(round(base_size * scale)))
    preferred = FONT_FAMILIES.get(str(family or ""))
    paths = ((preferred,) if preferred else ()) + (PRIMARY_FONT,) + FALLBACK_FONTS
    for path in paths:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def plain_text(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?!\*)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!_)_(?!_)(.*?)_(?!_)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`{1,3}(.*?)`{1,3}", r"\1", text, flags=re.DOTALL)
    return " ".join(text.replace("\x00", "").split())


def text_with_glyph_fallbacks(
    text: str,
    font: ImageFont.FreeTypeFont,
) -> str:
    """Replace unsupported glyphs without exposing font tofu boxes."""
    text = text.replace("🏳️‍🌈", "🌈")
    unknown = bytes(font.getmask("\U0010ffff"))
    rendered = []
    index = 0
    while index < len(text):
        char = text[index]
        codepoint = ord(char)
        if (
            0x1F1E6 <= codepoint <= 0x1F1FF
            and index + 1 < len(text)
            and 0x1F1E6 <= ord(text[index + 1]) <= 0x1F1FF
        ):
            rendered.append(chr(ord("A") + codepoint - 0x1F1E6))
            rendered.append(chr(ord("A") + ord(text[index + 1]) - 0x1F1E6))
            index += 2
            continue
        if char in {"\u200d", "\ufe0e", "\ufe0f"}:
            index += 1
            continue
        if char.isspace() or unicodedata.combining(char):
            rendered.append(char)
        else:
            try:
                rendered.append(char if bytes(font.getmask(char)) != unknown else "?")
            except (OSError, ValueError):
                rendered.append("?")
        index += 1
    return "".join(rendered)


def truncate_text(
    draw: ImageDraw.ImageDraw,
    text: object,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    state: Optional[RenderState] = None,
) -> str:
    value = text_with_glyph_fallbacks(plain_text(text), font)
    if max_width <= 0:
        if state is not None and value:
            state.truncated_text_count += 1
        return ""
    if draw.textlength(value, font=font) <= max_width:
        return value
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = value[:middle].rstrip() + "…"
        if draw.textlength(candidate, font=font) <= max_width:
            low = middle
        else:
            high = middle - 1
    if state is not None:
        state.truncated_text_count += 1
    return value[:low].rstrip() + "…"


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: object,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    max_lines: int,
    state: Optional[RenderState] = None,
) -> Iterable[str]:
    value = text_with_glyph_fallbacks(plain_text(text), font)
    if not value:
        return []
    lines = []
    current = ""
    for word in value.split():
        candidate = "{} {}".format(current, word).strip()
        if not current or draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = truncate_text(
            draw, lines[-1] + "…", font, max_width, state=state
        )
        if state is not None:
            state.truncated_text_count += 1
    return lines


def format_number(value: float, compact: bool = False) -> str:
    value = float(value or 0)
    if compact:
        absolute = abs(value)
        for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
            if absolute >= divisor:
                number = value / divisor
                rendered = "{:.1f}".format(number).rstrip("0").rstrip(".")
                return "{}{}".format(rendered, suffix)
    if value.is_integer():
        return "{:,}".format(int(value))
    return "{:,.2f}".format(value).rstrip("0").rstrip(".")


def format_percent(value: float, include_sign: bool = False) -> str:
    prefix = "+" if include_sign and value > 0 else ""
    return "{}{:.1f}%".format(prefix, value)


def pluralize(count: int, singular: str, plural: Optional[str] = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_date_range(start: Optional[date], end: Optional[date]) -> str:
    if start is None and end is None:
        return "All time"
    if start is None:
        return "Through {}".format(end.strftime("%b %-d, %Y"))
    if end is None:
        return "Since {}".format(start.strftime("%b %-d, %Y"))
    if start == end:
        return start.strftime("%b %-d, %Y")
    return "{} – {}".format(start.strftime("%b %-d, %Y"), end.strftime("%b %-d, %Y"))
