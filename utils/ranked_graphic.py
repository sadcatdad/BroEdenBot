import asyncio
import io
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = PROJECT_ROOT / "assets" / "OpenSansEmoji.ttf"

WIDTH = 1500
PADDING = 42
HEADER_HEIGHT = 190
FOOTER_HEIGHT = 48
SECTION_HEADER_HEIGHT = 50
ROW_HEIGHT = 68
ROW_GAP = 9
COLUMN_GAP = 18
AVATAR_SIZE = 42

BACKGROUND = (13, 15, 20)
PANEL = (25, 28, 35)
PANEL_ALT = (30, 34, 42)
TEXT = (244, 246, 250)
MUTED = (164, 172, 187)
TRACK = (48, 53, 64)
GOLD = (255, 203, 92)
SILVER = (202, 208, 220)
BRONZE = (213, 145, 91)

_AVATAR_CACHE: Dict[str, bytes] = {}


@dataclass(frozen=True)
class RankedGraphicItem:
    label: str
    value: str
    subtitle: str = ""
    avatar_url: Optional[str] = None
    score: float = 0


@dataclass(frozen=True)
class RankedGraphicSection:
    title: str
    items: Sequence[RankedGraphicItem]
    rank_start: int = 1


async def render_ranked_graphic(
    *,
    title: str,
    subtitle: str,
    sections: Iterable[RankedGraphicSection],
    updated_at: datetime,
    accent_color: int,
    total_entries: Optional[int] = None,
) -> bytes:
    sections = list(sections)
    avatar_bytes = await _fetch_avatar_bytes(
        item
        for section in sections
        for item in section.items
    )
    return await asyncio.to_thread(
        _render,
        title,
        subtitle,
        sections,
        updated_at,
        _rgb_from_int(accent_color),
        avatar_bytes,
        total_entries,
    )


async def _fetch_avatar_bytes(
    items: Iterable[RankedGraphicItem],
) -> Dict[str, bytes]:
    urls = {item.avatar_url for item in items if item.avatar_url}
    cached = {url: _AVATAR_CACHE[url] for url in urls if url in _AVATAR_CACHE}
    pending = urls.difference(cached)
    if not pending:
        return cached

    timeout = aiohttp.ClientTimeout(total=8)
    connector = aiohttp.TCPConnector(limit=10)
    semaphore = asyncio.Semaphore(10)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def fetch(url: str):
            try:
                async with semaphore:
                    async with session.get(url) as response:
                        if response.status != 200:
                            return url, None
                        if response.content_length and response.content_length > 2_000_000:
                            return url, None
                        return url, await response.read()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return url, None

        results = await asyncio.gather(*(fetch(url) for url in pending))

    for url, data in results:
        if data:
            _AVATAR_CACHE[url] = data
            cached[url] = data
    while len(_AVATAR_CACHE) > 500:
        _AVATAR_CACHE.pop(next(iter(_AVATAR_CACHE)))
    return cached


def _render(
    title: str,
    subtitle: str,
    sections: Sequence[RankedGraphicSection],
    updated_at: datetime,
    accent: Tuple[int, int, int],
    avatar_bytes: Dict[str, bytes],
    total_entries: Optional[int],
) -> bytes:
    layouts = _section_layouts(sections)
    row_count = max((layout[2] for layout in layouts), default=1)
    height = (
        PADDING
        + HEADER_HEIGHT
        + 24
        + SECTION_HEADER_HEIGHT
        + row_count * (ROW_HEIGHT + ROW_GAP)
        + FOOTER_HEIGHT
        + PADDING
    )
    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    fonts = {
        "title": ImageFont.truetype(str(FONT_PATH), 40),
        "subtitle": ImageFont.truetype(str(FONT_PATH), 20),
        "section": ImageFont.truetype(str(FONT_PATH), 22),
        "rank": ImageFont.truetype(str(FONT_PATH), 20),
        "name": ImageFont.truetype(str(FONT_PATH), 20),
        "small": ImageFont.truetype(str(FONT_PATH), 15),
        "value": ImageFont.truetype(str(FONT_PATH), 19),
    }

    _draw_glow(image, accent)
    header = (PADDING, PADDING, WIDTH - PADDING, PADDING + HEADER_HEIGHT)
    draw.rounded_rectangle(header, radius=24, fill=PANEL)
    draw.rounded_rectangle(
        (PADDING, PADDING, PADDING + 8, PADDING + HEADER_HEIGHT),
        radius=4,
        fill=accent,
    )
    title_x = PADDING + 30
    draw.text(
        (title_x, PADDING + 28),
        _fit_text(draw, title, fonts["title"], WIDTH - PADDING * 2 - 60),
        font=fonts["title"],
        fill=TEXT,
    )
    draw.text(
        (title_x, PADDING + 88),
        _fit_text(draw, subtitle, fonts["subtitle"], WIDTH - PADDING * 2 - 60),
        font=fonts["subtitle"],
        fill=MUTED,
    )
    if total_entries is None:
        total_entries = sum(len(section.items) for section in sections)
    draw.text(
        (title_x, PADDING + 132),
        f"{total_entries:,} ranked entr{'y' if total_entries == 1 else 'ies'}",
        font=fonts["small"],
        fill=_lighten(accent, 0.28),
    )

    rows_top = PADDING + HEADER_HEIGHT + 24
    for section, (section_x, section_width, section_rows, columns) in zip(
        sections,
        layouts,
    ):
        _draw_section(
            image=image,
            draw=draw,
            section=section,
            x=section_x,
            y=rows_top,
            width=section_width,
            rows=section_rows,
            columns=columns,
            accent=accent,
            fonts=fonts,
            avatar_bytes=avatar_bytes,
        )

    stamp = updated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    footer = f"Live stats • Last updated {stamp}"
    footer_width = draw.textlength(footer, font=fonts["small"])
    draw.text(
        (WIDTH - PADDING - footer_width, height - PADDING - 12),
        footer,
        font=fonts["small"],
        fill=MUTED,
    )
    output = io.BytesIO()
    image.save(output, "PNG", optimize=True, compress_level=9)
    return output.getvalue()


def _section_layouts(sections: Sequence[RankedGraphicSection]):
    if len(sections) > 1:
        width = (WIDTH - PADDING * 2 - COLUMN_GAP) // 2
        return [
            (
                PADDING + index * (width + COLUMN_GAP),
                width,
                max(1, len(section.items)),
                1,
            )
            for index, section in enumerate(sections[:2])
        ]

    section = sections[0] if sections else RankedGraphicSection("", [])
    columns = 2 if len(section.items) > 8 else 1
    rows = max(1, math.ceil(max(len(section.items), 1) / columns))
    return [(PADDING, WIDTH - PADDING * 2, rows, columns)]


def _draw_section(
    *,
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    section: RankedGraphicSection,
    x: int,
    y: int,
    width: int,
    rows: int,
    columns: int,
    accent: Tuple[int, int, int],
    fonts: dict,
    avatar_bytes: Dict[str, bytes],
) -> None:
    section_height = SECTION_HEADER_HEIGHT + rows * (ROW_HEIGHT + ROW_GAP)
    draw.rounded_rectangle(
        (x, y, x + width, y + section_height),
        radius=20,
        fill=(18, 21, 27),
    )
    draw.text(
        (x + 18, y + 14),
        _fit_text(draw, section.title, fonts["section"], width - 36),
        font=fonts["section"],
        fill=TEXT,
    )
    content_y = y + SECTION_HEADER_HEIGHT
    available_height = section_height - SECTION_HEADER_HEIGHT
    actual_rows = max(1, math.ceil(max(len(section.items), 1) / columns))
    item_height = max(52, min(ROW_HEIGHT, (available_height - ROW_GAP) // actual_rows))
    column_width = (width - 24 - COLUMN_GAP * (columns - 1)) // columns
    maximum = max((item.score for item in section.items), default=0) or 1

    if not section.items:
        draw.text(
            (x + 20, content_y + 16),
            "No matching activity yet.",
            font=fonts["name"],
            fill=MUTED,
        )
        return

    for index, item in enumerate(section.items):
        column = index // actual_rows
        row = index % actual_rows
        item_x = x + 12 + column * (column_width + COLUMN_GAP)
        item_y = content_y + row * (item_height + ROW_GAP)
        _draw_row(
            image=image,
            draw=draw,
            item=item,
            rank=section.rank_start + index,
            x=item_x,
            y=item_y,
            width=column_width,
            height=item_height,
            accent=accent,
            maximum=maximum,
            fonts=fonts,
            avatar_data=avatar_bytes.get(item.avatar_url or ""),
        )


def _draw_row(
    *,
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    item: RankedGraphicItem,
    rank: int,
    x: int,
    y: int,
    width: int,
    height: int,
    accent: Tuple[int, int, int],
    maximum: float,
    fonts: dict,
    avatar_data: Optional[bytes],
) -> None:
    rank_color = {1: GOLD, 2: SILVER, 3: BRONZE}.get(rank, MUTED)
    fill = PANEL_ALT if rank <= 3 else PANEL
    draw.rounded_rectangle((x, y, x + width, y + height), radius=14, fill=fill)
    progress = int((width - 8) * max(0, item.score) / maximum)
    if progress:
        draw.rounded_rectangle(
            (x + 4, y + height - 5, x + 4 + progress, y + height - 2),
            radius=2,
            fill=accent,
        )
    rank_text = f"#{rank}"
    draw.text((x + 14, y + 20), rank_text, font=fonts["rank"], fill=rank_color)
    text_x = x + 63
    avatar = _prepare_avatar(avatar_data)
    if avatar is not None:
        avatar_y = y + (height - AVATAR_SIZE) // 2
        image.paste(avatar, (text_x, avatar_y), avatar)
        text_x += AVATAR_SIZE + 11
    else:
        circle_y = y + (height - AVATAR_SIZE) // 2
        draw.ellipse(
            (text_x, circle_y, text_x + AVATAR_SIZE, circle_y + AVATAR_SIZE),
            fill=_darken(accent, 0.18),
        )
        initial = (item.label[:1] or "?").upper()
        initial_width = draw.textlength(initial, font=fonts["rank"])
        draw.text(
            (text_x + (AVATAR_SIZE - initial_width) / 2, circle_y + 8),
            initial,
            font=fonts["rank"],
            fill=TEXT,
        )
        text_x += AVATAR_SIZE + 11

    value_width = draw.textlength(item.value, font=fonts["value"])
    pill_width = min(max(value_width + 28, 100), width // 3)
    pill_x = x + width - pill_width - 14
    draw.rounded_rectangle(
        (pill_x, y + 14, x + width - 14, y + height - 14),
        radius=16,
        fill=(42, 47, 58),
    )
    draw.text(
        (pill_x + (pill_width - value_width) / 2, y + 21),
        item.value,
        font=fonts["value"],
        fill=TEXT,
    )
    max_text_width = pill_x - text_x - 12
    draw.text(
        (text_x, y + 12),
        _fit_text(draw, item.label, fonts["name"], max_text_width),
        font=fonts["name"],
        fill=TEXT,
    )
    if item.subtitle:
        draw.text(
            (text_x, y + 38),
            _fit_text(draw, item.subtitle, fonts["small"], max_text_width),
            font=fonts["small"],
            fill=MUTED,
        )


def _draw_glow(image: Image.Image, accent: Tuple[int, int, int]) -> None:
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-180, -220, 650, 430), fill=(*accent, 80))
    glow = glow.filter(ImageFilter.GaussianBlur(95))
    image.paste(glow.convert("RGB"), (0, 0), glow)


def _prepare_avatar(data: Optional[bytes]) -> Optional[Image.Image]:
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as source:
            avatar = ImageOps.fit(
                source.convert("RGBA"),
                (AVATAR_SIZE, AVATAR_SIZE),
                method=Image.Resampling.LANCZOS,
            )
        mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, AVATAR_SIZE - 1, AVATAR_SIZE - 1), fill=255)
        avatar.putalpha(mask)
        return avatar
    except (OSError, ValueError):
        return None


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
) -> str:
    text = str(text or "")
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


def _rgb_from_int(color: int) -> Tuple[int, int, int]:
    return ((color >> 16) & 255, (color >> 8) & 255, color & 255)


def _lighten(color: Tuple[int, int, int], amount: float):
    return tuple(int(value + (255 - value) * amount) for value in color)


def _darken(color: Tuple[int, int, int], amount: float):
    return tuple(int(value * (1 - amount)) for value in color)
