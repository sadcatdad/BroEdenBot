import math
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageOps

from .avatars import fetch_avatars, prepare_avatar
from .components import (
    base_canvas,
    draw_empty_state,
    draw_error_state,
    draw_footer,
    draw_header,
    draw_leaderboard_row,
    draw_metric_card,
    draw_section_heading,
)
from .models import RenderResult, RenderState
from .output import build_render_result
from .text import (
    format_number,
    format_percent,
    format_timestamp,
    pluralize,
    truncate_text,
    wrap_text,
)
from .theme import (
    COLORS,
    LEADERBOARD_ROWS_PER_PAGE,
    PORTRAIT_LEADERBOARD,
    ROSTER_ROWS_PER_PAGE,
    WIDE_OVERVIEW,
)


Color = Tuple[int, int, int]


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


@dataclass(frozen=True)
class CompactRosterItem:
    label: str
    avatar_url: Optional[str] = None


def rgb_from_int(color: int) -> Color:
    return ((color >> 16) & 255, (color >> 8) & 255, color & 255)


def _ranked_pages(
    sections: Sequence[RankedGraphicSection],
) -> List[RankedGraphicSection]:
    pages = []
    for section in sections:
        items = list(section.items)
        if not items:
            pages.append(section)
            continue
        for offset in range(0, len(items), LEADERBOARD_ROWS_PER_PAGE):
            pages.append(
                RankedGraphicSection(
                    title=section.title,
                    items=items[offset : offset + LEADERBOARD_ROWS_PER_PAGE],
                    rank_start=section.rank_start + offset,
                )
            )
    return pages or [RankedGraphicSection("Leaderboard", [])]


async def render_ranked_graphic_result(
    *,
    title: str,
    subtitle: str,
    sections: Iterable[RankedGraphicSection],
    updated_at: datetime,
    accent_color: int,
    total_entries: Optional[int] = None,
    page_number: Optional[int] = None,
    page_count: Optional[int] = None,
    target_bytes: Optional[int] = None,
    banner_bytes: Optional[bytes] = None,
    background_bytes: Optional[bytes] = None,
    footer_text: Optional[str] = None,
) -> RenderResult:
    sections = list(sections)
    display_pages = _ranked_pages(sections)
    all_items = [item for section in sections for item in section.items]
    avatars = await fetch_avatars(item.avatar_url for item in all_items)
    state = RenderState()
    accent = rgb_from_int(accent_color) if accent_color else COLORS.accent
    total = total_entries
    if total is None:
        total = sum(len(section.items) for section in sections)
    factories = []
    internal_count = len(display_pages)

    for internal_number, section in enumerate(display_pages, start=1):
        shown_number = (
            page_number
            if page_number is not None and internal_count == 1
            else internal_number
        )
        shown_count = (
            page_count
            if page_count is not None and internal_count == 1
            else internal_count
        )

        def factory(
            width: int,
            height: int,
            section: RankedGraphicSection = section,
            shown_number: int = shown_number,
            shown_count: int = shown_count,
        ) -> Image.Image:
            canvas = base_canvas(
                width,
                height,
                PORTRAIT_LEADERBOARD,
                state,
                accent,
                background_bytes=background_bytes,
                banner_bytes=banner_bytes,
            )
            draw_header(
                canvas,
                title=title,
                subtitle=subtitle,
                date_range="{:,} ranked {}".format(
                    total, pluralize(total, "entry", "entries")
                ),
                page_number=shown_number,
                page_count=shown_count,
            )
            panel_x, panel_y, panel_width, panel_height = 48, 286, 1104, 1144
            canvas.draw.rounded_rectangle(
                canvas.box(
                    (
                        panel_x,
                        panel_y,
                        panel_x + panel_width,
                        panel_y + panel_height,
                    )
                ),
                radius=canvas.s(22),
                fill=COLORS.surface_alt,
                outline=COLORS.border,
                width=max(1, canvas.s(2)),
            )
            draw_section_heading(
                canvas, section.title, panel_x + 28, panel_y + 24, panel_width - 56
            )
            if not section.items:
                draw_empty_state(
                    canvas,
                    title="Nothing to rank yet",
                    message="No matching activity is available for this report and date range.",
                    x=panel_x + 22,
                    y=panel_y + 84,
                    width=panel_width - 44,
                    height=260,
                )
            else:
                maximum = max((max(0.0, item.score) for item in section.items), default=0) or 1
                row_height = 96
                row_gap = 10
                row_y = panel_y + 78
                for offset, item in enumerate(section.items):
                    draw_leaderboard_row(
                        canvas,
                        rank=section.rank_start + offset,
                        label=item.label,
                        value=item.value,
                        subtitle=item.subtitle,
                        avatar_data=avatars.data.get(item.avatar_url or ""),
                        avatar_expected=bool(item.avatar_url),
                        x=panel_x + 18,
                        y=row_y + offset * (row_height + row_gap),
                        width=panel_width - 36,
                        height=row_height,
                        progress=max(0.0, item.score) / maximum,
                    )
            draw_footer(
                canvas,
                left_text=footer_text or "BRO EDEN • COMMUNITY STATS",
                right_text="Updated {}".format(format_timestamp(updated_at)),
            )
            return canvas.image

        factories.append(factory)

    return build_render_result(
        graphic_type="ranked_graphic",
        profile=PORTRAIT_LEADERBOARD,
        factories=factories,
        state=state,
        target_bytes=target_bytes,
    )


def _metric_layout(count: int) -> Tuple[int, int, int]:
    gap = 18
    width = (1504 - gap * (count - 1)) // max(1, count)
    return 48, width, gap


def render_rolecompare_result(
    *,
    title: str,
    body: str,
    role_1_name: str,
    role_2_name: str,
    counts: Dict[str, int],
    updated_at: datetime,
    accent_color: int,
    target_bytes: Optional[int] = None,
) -> RenderResult:
    state = RenderState()
    accent = rgb_from_int(accent_color) if accent_color else COLORS.accent

    def factory(width: int, height: int) -> Image.Image:
        canvas = base_canvas(width, height, WIDE_OVERVIEW, state, accent)
        draw_header(
            canvas,
            title=title,
            subtitle=body or "Role membership comparison",
            date_range="Current server membership",
        )
        cards = [
            (role_1_name, counts["role_1_total"], accent),
            (role_2_name, counts["role_2_total"], COLORS.chart[2]),
            ("In both", counts["both"], COLORS.positive),
            ("Only {}".format(role_1_name), counts["role_1_only"], accent),
            ("Only {}".format(role_2_name), counts["role_2_only"], COLORS.chart[2]),
        ]
        start_x, card_width, gap = _metric_layout(len(cards))
        for index, (label, value, color) in enumerate(cards):
            draw_metric_card(
                canvas,
                x=start_x + index * (card_width + gap),
                y=294,
                width=card_width,
                height=204,
                label=label,
                value=format_number(value),
                accent=color,
                supporting="{} {}".format(value, pluralize(value, "member")),
            )
        venn_x, venn_y, radius = 800, 650, 132
        overlay = Image.new("RGBA", canvas.image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        left = canvas.s(venn_x - 92)
        right = canvas.s(venn_x + 92)
        center_y = canvas.s(venn_y)
        radius_px = canvas.s(radius)
        overlay_draw.ellipse(
            (left - radius_px, center_y - radius_px, left + radius_px, center_y + radius_px),
            fill=(*accent, 105),
            outline=(*accent, 230),
            width=max(2, canvas.s(4)),
        )
        overlay_draw.ellipse(
            (right - radius_px, center_y - radius_px, right + radius_px, center_y + radius_px),
            fill=(*COLORS.chart[2], 105),
            outline=(*COLORS.chart[2], 230),
            width=max(2, canvas.s(4)),
        )
        canvas.image.paste(overlay, (0, 0), overlay)
        canvas.draw = ImageDraw.Draw(canvas.image)
        labels = (
            (venn_x - 142, counts["role_1_only"], "only"),
            (venn_x, counts["both"], "both"),
            (venn_x + 142, counts["role_2_only"], "only"),
        )
        for center_x, value, label in labels:
            value_text = format_number(value)
            value_width = canvas.draw.textlength(value_text, font=canvas.fonts["primary_metric"])
            canvas.draw.text(
                (canvas.s(center_x) - value_width / 2, canvas.s(venn_y - 34)),
                value_text,
                font=canvas.fonts["primary_metric"],
                fill=COLORS.text,
            )
            label_width = canvas.draw.textlength(label, font=canvas.fonts["supporting_stat"])
            canvas.draw.text(
                (canvas.s(center_x) - label_width / 2, canvas.s(venn_y + 30)),
                label,
                font=canvas.fonts["supporting_stat"],
                fill=COLORS.secondary_text,
            )
        draw_footer(
            canvas,
            left_text="BRO EDEN • ROLE STATS",
            right_text="Updated {}".format(format_timestamp(updated_at)),
        )
        return canvas.image

    return build_render_result(
        graphic_type="role_comparison",
        profile=WIDE_OVERVIEW,
        factories=[factory],
        state=state,
        target_bytes=target_bytes,
    )


def render_missingrole_result(
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
    target_bytes: Optional[int] = None,
) -> RenderResult:
    state = RenderState()
    accent = rgb_from_int(accent_color) if accent_color else COLORS.accent

    def factory(width: int, height: int) -> Image.Image:
        canvas = base_canvas(width, height, WIDE_OVERVIEW, state, accent)
        draw_header(
            canvas,
            title=title,
            subtitle=body or "Required-role coverage",
            date_range="Current server membership",
        )
        cards = [
            ("With {}".format(has_role_name), has_role_total, accent),
            ("With {}".format(missing_role_name), missing_role_total, COLORS.positive),
            ("Missing required role", missing_count, COLORS.negative),
        ]
        start_x, card_width, gap = _metric_layout(len(cards))
        for index, (label, value, color) in enumerate(cards):
            draw_metric_card(
                canvas,
                x=start_x + index * (card_width + gap),
                y=306,
                width=card_width,
                height=224,
                label=label,
                value=format_number(value),
                accent=color,
            )
        panel = canvas.box((48, 574, 1552, 788))
        canvas.draw.rounded_rectangle(
            panel,
            radius=canvas.s(22),
            fill=COLORS.card,
            outline=COLORS.border,
            width=max(1, canvas.s(2)),
        )
        label = "{} missing {}".format(
            format_percent(missing_percent), missing_role_name
        )
        canvas.draw.text(
            (canvas.s(78), canvas.s(608)),
            truncate_text(
                canvas.draw,
                label,
                canvas.fonts["section_heading"],
                canvas.s(1444),
                state,
            ),
            font=canvas.fonts["section_heading"],
            fill=COLORS.text,
        )
        bar_x, bar_y, bar_width = 78, 690, 1444
        canvas.draw.rounded_rectangle(
            canvas.box((bar_x, bar_y, bar_x + bar_width, bar_y + 26)),
            radius=canvas.s(13),
            fill=COLORS.surface,
        )
        filled = bar_width * min(max(missing_percent, 0), 100) / 100
        if filled:
            canvas.draw.rounded_rectangle(
                canvas.box((bar_x, bar_y, bar_x + filled, bar_y + 26)),
                radius=canvas.s(13),
                fill=COLORS.negative,
            )
        draw_footer(
            canvas,
            left_text="BRO EDEN • ROLE STATS",
            right_text="Updated {}".format(format_timestamp(updated_at)),
        )
        return canvas.image

    return build_render_result(
        graphic_type="missing_role",
        profile=WIDE_OVERVIEW,
        factories=[factory],
        state=state,
        target_bytes=target_bytes,
    )


def render_error_result(
    *,
    title: str,
    message: str,
    updated_at: datetime,
    accent_color: int,
    target_bytes: Optional[int] = None,
) -> RenderResult:
    state = RenderState()
    accent = rgb_from_int(accent_color) if accent_color else COLORS.accent

    def factory(width: int, height: int) -> Image.Image:
        canvas = base_canvas(width, height, WIDE_OVERVIEW, state, accent)
        draw_header(
            canvas,
            title=title,
            subtitle="The report could not be rendered from its current configuration.",
        )
        draw_error_state(
            canvas,
            message=message,
            x=48,
            y=318,
            width=1504,
            height=350,
        )
        draw_footer(
            canvas,
            left_text="BRO EDEN • STATS",
            right_text="Updated {}".format(format_timestamp(updated_at)),
        )
        return canvas.image

    return build_render_result(
        graphic_type="stats_error",
        profile=WIDE_OVERVIEW,
        factories=[factory],
        state=state,
        target_bytes=target_bytes,
    )


def _prepare_banner(data: Optional[bytes], width: int, height: int) -> Optional[Image.Image]:
    if not data:
        return None
    try:
        import io

        with Image.open(io.BytesIO(data)) as source:
            source.seek(0)
            return ImageOps.fit(
                source.convert("RGB"),
                (width, height),
                method=Image.Resampling.LANCZOS,
            )
    except (EOFError, OSError, ValueError):
        return None


async def render_compact_roster_result(
    *,
    title: str,
    body: str,
    role_name: str,
    items: Iterable[CompactRosterItem],
    updated_at: datetime,
    accent_color: int,
    include_avatars: bool = True,
    banner_bytes: Optional[bytes] = None,
    target_bytes: Optional[int] = None,
) -> RenderResult:
    items = list(items)
    pages = [
        items[index : index + ROSTER_ROWS_PER_PAGE]
        for index in range(0, len(items), ROSTER_ROWS_PER_PAGE)
    ] or [[]]
    avatars = await fetch_avatars(
        item.avatar_url for item in items if include_avatars
    )
    state = RenderState()
    accent = rgb_from_int(accent_color) if accent_color else COLORS.accent
    factories = []
    page_count = len(pages)

    for page_number, page_items in enumerate(pages, start=1):
        def factory(
            width: int,
            height: int,
            page_items: Sequence[CompactRosterItem] = page_items,
            page_number: int = page_number,
        ) -> Image.Image:
            canvas = base_canvas(
                width, height, PORTRAIT_LEADERBOARD, state, accent
            )
            banner = _prepare_banner(
                banner_bytes, canvas.s(1104), canvas.s(208)
            )
            if banner is not None:
                canvas.image.paste(banner, (canvas.s(48), canvas.s(48)))
                overlay = Image.new("RGBA", banner.size, (12, 13, 18, 184))
                canvas.image.paste(
                    overlay, (canvas.s(48), canvas.s(48)), overlay
                )
                canvas.draw = ImageDraw.Draw(canvas.image)
            draw_header(
                canvas,
                title=title or "{} Members".format(role_name),
                subtitle=body or "Current role roster",
                date_range="{:,} {} • {}".format(
                    len(items), pluralize(len(items), "member"), role_name
                ),
                page_number=page_number,
                page_count=page_count,
            )
            panel_x, panel_y, panel_width = 48, 286, 1104
            panel_height = 1144
            canvas.draw.rounded_rectangle(
                canvas.box(
                    (panel_x, panel_y, panel_x + panel_width, panel_y + panel_height)
                ),
                radius=canvas.s(22),
                fill=COLORS.surface_alt,
                outline=COLORS.border,
                width=max(1, canvas.s(2)),
            )
            draw_section_heading(
                canvas, "Member roster", panel_x + 28, panel_y + 24, panel_width - 56
            )
            if not page_items:
                draw_empty_state(
                    canvas,
                    title="No members yet",
                    message="No members currently have this role.",
                    x=panel_x + 22,
                    y=panel_y + 84,
                    width=panel_width - 44,
                    height=260,
                )
            else:
                row_height, row_gap = 78, 9
                for index, item in enumerate(page_items):
                    x, y = panel_x + 18, panel_y + 76 + index * (row_height + row_gap)
                    canvas.draw.rounded_rectangle(
                        canvas.box((x, y, x + panel_width - 36, y + row_height)),
                        radius=canvas.s(14),
                        fill=COLORS.surface if index % 2 else COLORS.card,
                        outline=COLORS.border,
                        width=max(1, canvas.s(1)),
                    )
                    avatar_size = 48
                    avatar = prepare_avatar(
                        avatars.data.get(item.avatar_url or ""), canvas.s(avatar_size)
                    )
                    text_x = x + 22
                    if include_avatars:
                        if avatar is not None:
                            canvas.image.paste(
                                avatar,
                                (canvas.s(text_x), canvas.s(y + 15)),
                                avatar,
                            )
                        else:
                            if item.avatar_url:
                                state.avatar_fallback_count += 1
                            canvas.draw.ellipse(
                                canvas.box((text_x, y + 15, text_x + avatar_size, y + 63)),
                                fill=COLORS.accent_soft,
                                outline=accent,
                                width=max(1, canvas.s(2)),
                            )
                            initial = (item.label[:1] or "?").upper()
                            font = canvas.fonts["ranking_number"]
                            bbox = canvas.draw.textbbox((0, 0), initial, font=font)
                            canvas.draw.text(
                                (
                                    canvas.s(text_x + avatar_size / 2) - (bbox[2] - bbox[0]) / 2,
                                    canvas.s(y + 39) - (bbox[3] - bbox[1]) / 2 - bbox[1],
                                ),
                                initial,
                                font=font,
                                fill=COLORS.text,
                            )
                        text_x += avatar_size + 16
                    safe = truncate_text(
                        canvas.draw,
                        item.label,
                        canvas.fonts["username"],
                        canvas.s(panel_width - (text_x - panel_x) - 58),
                        state,
                    )
                    bbox = canvas.draw.textbbox((0, 0), safe, font=canvas.fonts["username"])
                    canvas.draw.text(
                        (
                            canvas.s(text_x),
                            canvas.s(y + row_height / 2) - (bbox[3] - bbox[1]) / 2 - bbox[1],
                        ),
                        safe,
                        font=canvas.fonts["username"],
                        fill=COLORS.text,
                    )
            draw_footer(
                canvas,
                left_text="BRO EDEN • ROLE ROSTER",
                right_text="Updated {}".format(format_timestamp(updated_at)),
            )
            return canvas.image

        factories.append(factory)

    return build_render_result(
        graphic_type="role_roster",
        profile=PORTRAIT_LEADERBOARD,
        factories=factories,
        state=state,
        target_bytes=target_bytes,
    )
