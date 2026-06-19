import asyncio
import io
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageOps


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = PROJECT_ROOT / "assets" / "OpenSansEmoji.ttf"

BACKGROUND = (20, 22, 26)
HEADER = (27, 29, 35)
ROW_DARK = (31, 34, 40)
ROW_LIGHT = (36, 39, 46)
TEXT = (239, 241, 245)
MUTED_TEXT = (166, 173, 186)

CANVAS_WIDTH = 1400
OUTER_PADDING = 28
COLUMN_GAP = 14
HEADER_HEIGHT = 280
ROW_HEIGHT = 42
FOOTER_HEIGHT = 42
MAX_ROWS_PER_COLUMN = 15
MAX_PAGES = 9
AVATAR_SIZE = 32
MAX_AVATAR_CACHE_ITEMS = 500

_AVATAR_CACHE: Dict[str, bytes] = {}


@dataclass(frozen=True)
class CompactRosterItem:
    label: str
    avatar_url: Optional[str] = None


async def render_compact_roster_pngs(
    *,
    title: str,
    body: str,
    role_name: str,
    items: Iterable[CompactRosterItem],
    updated_at: datetime,
    accent_color: int,
    include_avatars: bool = True,
    banner_bytes: Optional[bytes] = None,
) -> list:
    """Render one or more wide roster PNGs suitable for Discord embeds."""
    items = list(items)
    avatar_bytes = {}
    if include_avatars:
        avatar_bytes = await _fetch_avatar_bytes(items)

    column_count = _column_count_for(len(items))
    rows_per_column = max(
        MAX_ROWS_PER_COLUMN,
        math.ceil(max(len(items), 1) / (column_count * MAX_PAGES)),
    )
    page_size = rows_per_column * column_count
    pages = [
        items[index : index + page_size]
        for index in range(0, len(items), page_size)
    ] or [[]]

    return await asyncio.gather(
        *(
            asyncio.to_thread(
                _render_png,
                title,
                body,
                role_name,
                page_items,
                len(items),
                page_number,
                len(pages),
                column_count,
                updated_at,
                accent_color,
                avatar_bytes,
                banner_bytes,
            )
            for page_number, page_items in enumerate(pages, start=1)
        )
    )


async def _fetch_avatar_bytes(
    items: Iterable[CompactRosterItem],
) -> Dict[str, bytes]:
    urls = {item.avatar_url for item in items if item.avatar_url}
    if not urls:
        return {}

    cached = {url: _AVATAR_CACHE[url] for url in urls if url in _AVATAR_CACHE}
    urls.difference_update(cached)
    if not urls:
        return cached

    timeout = aiohttp.ClientTimeout(total=8)
    connector = aiohttp.TCPConnector(limit=10)
    semaphore = asyncio.Semaphore(10)

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
    ) as session:
        async def fetch(url: str) -> Tuple[str, Optional[bytes]]:
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

        results = await asyncio.gather(*(fetch(url) for url in urls))

    downloaded = {url: data for url, data in results if data}
    for url, data in downloaded.items():
        _AVATAR_CACHE[url] = data
    while len(_AVATAR_CACHE) > MAX_AVATAR_CACHE_ITEMS:
        _AVATAR_CACHE.pop(next(iter(_AVATAR_CACHE)))

    cached.update(downloaded)
    return cached


def _render_png(
    title: str,
    body: str,
    role_name: str,
    items: Iterable[CompactRosterItem],
    total_item_count: int,
    page_number: int,
    page_count: int,
    column_count: int,
    updated_at: datetime,
    accent_color: int,
    avatar_bytes: Dict[str, bytes],
    banner_bytes: Optional[bytes],
) -> bytes:
    items = list(items)
    rows_per_column = max(1, math.ceil(max(len(items), 1) / column_count))
    column_width = (
        CANVAS_WIDTH
        - OUTER_PADDING * 2
        - COLUMN_GAP * (column_count - 1)
    ) // column_count
    width = CANVAS_WIDTH
    height = (
        HEADER_HEIGHT
        + rows_per_column * ROW_HEIGHT
        + FOOTER_HEIGHT
        + OUTER_PADDING
    )

    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    accent = _rgb_from_int(accent_color)

    title_font = ImageFont.truetype(str(FONT_PATH), 38)
    body_font = ImageFont.truetype(str(FONT_PATH), 20)
    meta_font = ImageFont.truetype(str(FONT_PATH), 20)
    name_font = ImageFont.truetype(str(FONT_PATH), 19)
    footer_font = ImageFont.truetype(str(FONT_PATH), 16)

    header_box = (
        OUTER_PADDING,
        OUTER_PADDING,
        width - OUTER_PADDING,
        HEADER_HEIGHT,
    )
    draw.rounded_rectangle(
        header_box,
        radius=12,
        fill=HEADER,
    )
    banner = _prepare_banner(banner_bytes, header_box)
    if banner is not None:
        image.paste(banner, (OUTER_PADDING, OUTER_PADDING))
        overlay = Image.new(
            "RGBA",
            banner.size,
            (15, 17, 21, 172),
        )
        image.paste(overlay, (OUTER_PADDING, OUTER_PADDING), overlay)

    draw.rounded_rectangle(
        (OUTER_PADDING, OUTER_PADDING, OUTER_PADDING + 6, HEADER_HEIGHT),
        radius=3,
        fill=accent,
    )

    title_x = OUTER_PADDING + 22
    title_width = width - title_x - OUTER_PADDING - 18
    safe_title = _fit_text(
        draw,
        _plain_text(title) or f"{role_name} Roster",
        title_font,
        title_width,
    )
    draw.text((title_x, OUTER_PADDING + 18), safe_title, font=title_font, fill=TEXT)

    member_word = "member" if total_item_count == 1 else "members"
    meta = f"{role_name}  •  {total_item_count:,} {member_word}"
    if page_count > 1:
        meta += f"  •  Page {page_number}/{page_count}"
    draw.text(
        (title_x, OUTER_PADDING + 72),
        _fit_text(draw, meta, meta_font, title_width),
        font=meta_font,
        fill=MUTED_TEXT,
    )

    body_lines = _wrapped_body_lines(
        draw,
        _plain_text(body),
        body_font,
        title_width,
        max_lines=5,
    )
    body_y = OUTER_PADDING + 112
    for line in body_lines:
        draw.text((title_x, body_y), line, font=body_font, fill=TEXT)
        body_y += 27

    row_top = HEADER_HEIGHT + 14
    if not items:
        _draw_empty_row(draw, row_top, width, name_font, accent)
    else:
        for index, item in enumerate(items):
            column = index // rows_per_column
            row = index % rows_per_column
            x = OUTER_PADDING + column * (column_width + COLUMN_GAP)
            y = row_top + row * ROW_HEIGHT
            _draw_roster_row(
                image=image,
                draw=draw,
                item=item,
                x=x,
                y=y,
                column_width=column_width,
                row_index=index,
                name_font=name_font,
                accent=accent,
                avatar_data=avatar_bytes.get(item.avatar_url or ""),
            )

    timestamp = updated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    footer = f"Last updated {timestamp}"
    footer_y = height - FOOTER_HEIGHT
    footer_width = draw.textlength(footer, font=footer_font)
    draw.text(
        (width - OUTER_PADDING - footer_width, footer_y),
        footer,
        font=footer_font,
        fill=MUTED_TEXT,
    )

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True, compress_level=9)
    return output.getvalue()


def _draw_roster_row(
    *,
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    item: CompactRosterItem,
    x: int,
    y: int,
    column_width: int,
    row_index: int,
    name_font: ImageFont.FreeTypeFont,
    accent: Tuple[int, int, int],
    avatar_data: Optional[bytes],
) -> None:
    row_bottom = y + ROW_HEIGHT - 3
    draw.rounded_rectangle(
        (x, y, x + column_width, row_bottom),
        radius=7,
        fill=ROW_LIGHT if row_index % 2 else ROW_DARK,
    )
    draw.rounded_rectangle(
        (x, y, x + 3, row_bottom),
        radius=2,
        fill=accent,
    )

    text_x = x + 12
    if avatar_data:
        avatar = _prepare_avatar(avatar_data)
        if avatar is not None:
            avatar_y = y + (ROW_HEIGHT - 3 - AVATAR_SIZE) // 2
            image.paste(avatar, (text_x, avatar_y), avatar)
            text_x += AVATAR_SIZE + 9

    max_name_width = x + column_width - 12 - text_x
    label = _fit_text(draw, item.label, name_font, max_name_width)
    text_box = draw.textbbox((0, 0), label, font=name_font)
    text_height = text_box[3] - text_box[1]
    text_y = y + ((ROW_HEIGHT - 3 - text_height) // 2) - text_box[1]
    draw.text((text_x, text_y), label, font=name_font, fill=TEXT)


def _draw_empty_row(
    draw: ImageDraw.ImageDraw,
    y: int,
    width: int,
    font: ImageFont.FreeTypeFont,
    accent: Tuple[int, int, int],
) -> None:
    draw.rounded_rectangle(
        (OUTER_PADDING, y, width - OUTER_PADDING, y + ROW_HEIGHT - 3),
        radius=7,
        fill=ROW_DARK,
    )
    draw.rounded_rectangle(
        (OUTER_PADDING, y, OUTER_PADDING + 3, y + ROW_HEIGHT - 3),
        radius=2,
        fill=accent,
    )
    draw.text(
        (OUTER_PADDING + 12, y + 5),
        "No members currently have this role.",
        font=font,
        fill=MUTED_TEXT,
    )


def _prepare_avatar(data: bytes) -> Optional[Image.Image]:
    try:
        with Image.open(io.BytesIO(data)) as source:
            avatar = ImageOps.fit(
                source.convert("RGBA"),
                (AVATAR_SIZE, AVATAR_SIZE),
                method=Image.Resampling.LANCZOS,
            )
        mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
        ImageDraw.Draw(mask).ellipse(
            (0, 0, AVATAR_SIZE - 1, AVATAR_SIZE - 1),
            fill=255,
        )
        avatar.putalpha(mask)
        return avatar
    except (OSError, ValueError):
        return None


def _prepare_banner(
    data: Optional[bytes],
    header_box: Tuple[int, int, int, int],
) -> Optional[Image.Image]:
    if not data:
        return None
    try:
        width = header_box[2] - header_box[0]
        height = header_box[3] - header_box[1]
        with Image.open(io.BytesIO(data)) as source:
            banner = ImageOps.fit(
                source.convert("RGB"),
                (width, height),
                method=Image.Resampling.LANCZOS,
            )
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, width, height),
            radius=12,
            fill=255,
        )
        banner.putalpha(mask)
        return banner
    except (OSError, ValueError):
        return None


def _plain_text(value: str) -> str:
    value = re.sub(r"\*\*(.*?)\*\*", r"\1", value, flags=re.DOTALL)
    value = re.sub(r"__(.*?)__", r"\1", value, flags=re.DOTALL)
    value = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?!\*)", r"\1", value, flags=re.DOTALL)
    value = re.sub(r"(?<!_)_(?!_)(.*?)_(?!_)", r"\1", value, flags=re.DOTALL)
    value = re.sub(r"`{1,3}(.*?)`{1,3}", r"\1", value, flags=re.DOTALL)
    return value.strip()


def _wrapped_body_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    max_lines: int,
) -> list:
    if not text:
        return []

    lines = []
    for paragraph in text.splitlines() or [""]:
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

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit_text(
            draw,
            lines[-1] + "…",
            font,
            max_width,
        )
    return lines


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text

    ellipsis = "…"
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle].rstrip() + ellipsis
        if draw.textlength(candidate, font=font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + ellipsis


def _rgb_from_int(color: int) -> Tuple[int, int, int]:
    return (
        (color >> 16) & 255,
        (color >> 8) & 255,
        color & 255,
    )


def _column_count_for(item_count: int) -> int:
    if item_count <= 15:
        return 1
    if item_count <= 30:
        return 2
    return 3
