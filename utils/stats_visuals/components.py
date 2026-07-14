import io
from typing import Iterable, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageOps

from .avatars import prepare_avatar
from .models import RenderState
from .text import load_font, truncate_text, wrap_text
from .theme import COLORS, SPACING, LayoutProfile


Color = Tuple[int, int, int]


class VisualCanvas:
    def __init__(
        self,
        width: int,
        height: int,
        profile: LayoutProfile,
        state: RenderState,
        accent: Color,
        *,
        background_bytes: Optional[bytes] = None,
        banner_bytes: Optional[bytes] = None,
    ) -> None:
        self.width = width
        self.height = height
        self.profile = profile
        self.state = state
        self.scale = min(width / profile.width, height / profile.height)
        self.image = Image.new("RGB", (width, height), COLORS.canvas)
        background = _fitted_image(background_bytes, (width, height))
        if background is not None:
            shade = Image.new("RGB", background.size, COLORS.canvas)
            self.image.paste(Image.blend(background, shade, 0.56))
        self.draw = ImageDraw.Draw(self.image)
        self.accent = accent
        self.banner_bytes = banner_bytes
        self.fonts = {
            role: load_font(role, self.scale)
            for role in (
                "graphic_title",
                "graphic_subtitle",
                "section_heading",
                "primary_metric",
                "metric_label",
                "ranking_number",
                "username",
                "supporting_stat",
                "chart_label",
                "footer",
                "page_indicator",
                "empty_title",
                "empty_body",
            )
        }

    def s(self, value: float) -> int:
        return int(round(value * self.scale))

    def box(self, values: Sequence[float]) -> Tuple[int, int, int, int]:
        return tuple(self.s(value) for value in values)  # type: ignore[return-value]


def base_canvas(
    width: int,
    height: int,
    profile: LayoutProfile,
    state: RenderState,
    accent: Color,
    *,
    background_bytes: Optional[bytes] = None,
    banner_bytes: Optional[bytes] = None,
) -> VisualCanvas:
    return VisualCanvas(
        width,
        height,
        profile,
        state,
        accent,
        background_bytes=background_bytes,
        banner_bytes=banner_bytes,
    )


def _fitted_image(data: Optional[bytes], size: Tuple[int, int]) -> Optional[Image.Image]:
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as source:
            return ImageOps.fit(
                source.convert("RGB"),
                size,
                method=Image.Resampling.LANCZOS,
            )
    except (OSError, ValueError):
        return None


def draw_brand_mark(canvas: VisualCanvas, x: float, y: float) -> None:
    draw = canvas.draw
    x, y = canvas.s(x), canvas.s(y)
    size = canvas.s(26)
    stripe = max(2, canvas.s(4))
    colors = (canvas.accent, COLORS.positive, COLORS.chart[2], COLORS.warning)
    for index, color in enumerate(colors):
        offset = index * stripe
        draw.rounded_rectangle(
            (x + offset, y, x + offset + stripe + 1, y + size),
            radius=max(1, stripe // 2),
            fill=color,
        )


def draw_page_indicator(
    canvas: VisualCanvas,
    page_number: int,
    page_count: int,
    right: float,
    top: float,
) -> None:
    label = "Page {} of {}".format(page_number, page_count)
    font = canvas.fonts["page_indicator"]
    width = canvas.draw.textlength(label, font=font) + canvas.s(28)
    height = canvas.s(38)
    right_px, top_px = canvas.s(right), canvas.s(top)
    canvas.draw.rounded_rectangle(
        (right_px - width, top_px, right_px, top_px + height),
        radius=height // 2,
        fill=COLORS.surface,
        outline=COLORS.border,
        width=max(1, canvas.s(SPACING.border)),
    )
    canvas.draw.text(
        (right_px - width + canvas.s(14), top_px + canvas.s(8)),
        label,
        font=font,
        fill=COLORS.secondary_text,
    )


def draw_date_range_label(canvas: VisualCanvas, label: str, x: float, y: float) -> None:
    font = canvas.fonts["supporting_stat"]
    x_px, y_px = canvas.s(x), canvas.s(y)
    safe = truncate_text(
        canvas.draw,
        label,
        font,
        canvas.s(620),
        state=canvas.state,
    )
    canvas.draw.text((x_px, y_px), safe, font=font, fill=COLORS.muted_text)


def draw_header(
    canvas: VisualCanvas,
    *,
    title: str,
    subtitle: str,
    date_range: str = "",
    page_number: int = 1,
    page_count: int = 1,
    compact: bool = False,
) -> int:
    margin = 48
    height = 180 if compact else 208
    left, top, right, bottom = canvas.box(
        (margin, margin, canvas.profile.width - margin, margin + height)
    )
    banner = _fitted_image(canvas.banner_bytes, (right - left, bottom - top))
    if banner is None:
        canvas.draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=canvas.s(SPACING.radius),
            fill=COLORS.card,
        )
    else:
        shade = Image.new("RGB", banner.size, COLORS.card)
        banner = Image.blend(banner, shade, 0.42)
        mask = Image.new("L", banner.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, banner.width - 1, banner.height - 1),
            radius=canvas.s(SPACING.radius),
            fill=255,
        )
        canvas.image.paste(banner, (left, top), mask)
    canvas.draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=canvas.s(SPACING.radius),
        outline=COLORS.border,
        width=max(1, canvas.s(SPACING.border)),
    )
    canvas.draw.rounded_rectangle(
        (left, top, left + canvas.s(8), bottom),
        radius=canvas.s(4),
        fill=canvas.accent,
    )
    draw_brand_mark(canvas, margin + 28, margin + 30)
    text_x = margin + 72
    show_page = page_count > 1 or canvas.profile.name == "portrait_leaderboard"
    title_width = canvas.profile.width - text_x - margin - (190 if show_page else 20)
    safe_title = truncate_text(
        canvas.draw,
        title,
        canvas.fonts["graphic_title"],
        canvas.s(title_width),
        state=canvas.state,
    )
    canvas.draw.text(
        (canvas.s(text_x), canvas.s(margin + 24)),
        safe_title,
        font=canvas.fonts["graphic_title"],
        fill=COLORS.text,
    )
    safe_subtitle = truncate_text(
        canvas.draw,
        subtitle,
        canvas.fonts["graphic_subtitle"],
        canvas.s(canvas.profile.width - text_x - margin - 20),
        state=canvas.state,
    )
    canvas.draw.text(
        (canvas.s(text_x), canvas.s(margin + 88)),
        safe_subtitle,
        font=canvas.fonts["graphic_subtitle"],
        fill=COLORS.secondary_text,
    )
    if date_range:
        draw_date_range_label(canvas, date_range, text_x, margin + 132)
    if show_page:
        draw_page_indicator(
            canvas,
            page_number,
            page_count,
            canvas.profile.width - margin - 22,
            margin + 24,
        )
    return bottom


def draw_section_heading(
    canvas: VisualCanvas,
    title: str,
    x: float,
    y: float,
    width: float,
) -> None:
    safe = truncate_text(
        canvas.draw,
        title,
        canvas.fonts["section_heading"],
        canvas.s(width),
        state=canvas.state,
    )
    canvas.draw.text(
        (canvas.s(x), canvas.s(y)),
        safe,
        font=canvas.fonts["section_heading"],
        fill=COLORS.text,
    )


def draw_metric_card(
    canvas: VisualCanvas,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    label: str,
    value: str,
    accent: Optional[Color] = None,
    supporting: str = "",
) -> None:
    accent = accent or canvas.accent
    box = canvas.box((x, y, x + width, y + height))
    canvas.draw.rounded_rectangle(
        box,
        radius=canvas.s(SPACING.radius),
        fill=COLORS.card,
        outline=COLORS.border,
        width=max(1, canvas.s(SPACING.border)),
    )
    canvas.draw.rounded_rectangle(
        canvas.box((x + 22, y + 22, x + 70, y + 28)),
        radius=canvas.s(3),
        fill=accent,
    )
    safe_label = truncate_text(
        canvas.draw,
        label,
        canvas.fonts["metric_label"],
        canvas.s(width - 44),
        state=canvas.state,
    )
    safe_value = truncate_text(
        canvas.draw,
        value,
        canvas.fonts["primary_metric"],
        canvas.s(width - 44),
        state=canvas.state,
    )
    canvas.draw.text(
        (canvas.s(x + 22), canvas.s(y + 42)),
        safe_label,
        font=canvas.fonts["metric_label"],
        fill=COLORS.muted_text,
    )
    canvas.draw.text(
        (canvas.s(x + 22), canvas.s(y + 78)),
        safe_value,
        font=canvas.fonts["primary_metric"],
        fill=COLORS.text,
    )
    if supporting:
        safe_supporting = truncate_text(
            canvas.draw,
            supporting,
            canvas.fonts["supporting_stat"],
            canvas.s(width - 44),
            state=canvas.state,
        )
        canvas.draw.text(
            (canvas.s(x + 22), canvas.s(y + height - 42)),
            safe_supporting,
            font=canvas.fonts["supporting_stat"],
            fill=COLORS.secondary_text,
        )


def draw_trend_indicator(
    canvas: VisualCanvas,
    value: float,
    x: float,
    y: float,
    label: str = "",
) -> None:
    if value > 0:
        color, arrow = COLORS.positive, "↑"
    elif value < 0:
        color, arrow = COLORS.negative, "↓"
    else:
        color, arrow = COLORS.neutral, "→"
    text = "{} {:+.1f}%{}".format(arrow, value, " " + label if label else "")
    canvas.draw.text(
        (canvas.s(x), canvas.s(y)),
        text,
        font=canvas.fonts["supporting_stat"],
        fill=color,
    )


def draw_chart_container(
    canvas: VisualCanvas,
    x: float,
    y: float,
    width: float,
    height: float,
    title: str,
) -> Tuple[int, int, int, int]:
    canvas.draw.rounded_rectangle(
        canvas.box((x, y, x + width, y + height)),
        radius=canvas.s(SPACING.radius),
        fill=COLORS.card,
        outline=COLORS.border,
        width=max(1, canvas.s(SPACING.border)),
    )
    draw_section_heading(canvas, title, x + 26, y + 22, width - 52)
    return canvas.box((x + 26, y + 68, x + width - 26, y + height - 26))


def draw_legend(
    canvas: VisualCanvas,
    entries: Iterable[Tuple[str, Color]],
    x: float,
    y: float,
    max_width: float,
) -> None:
    cursor = canvas.s(x)
    top = canvas.s(y)
    right = cursor + canvas.s(max_width)
    font = canvas.fonts["chart_label"]
    for label, color in entries:
        safe = truncate_text(canvas.draw, label, font, canvas.s(180), canvas.state)
        label_width = canvas.draw.textlength(safe, font=font)
        needed = canvas.s(18) + label_width + canvas.s(22)
        if cursor + needed > right:
            canvas.state.overflow_warnings.append("Legend entries exceeded one row")
            break
        canvas.draw.ellipse(
            (cursor, top + canvas.s(4), cursor + canvas.s(12), top + canvas.s(16)),
            fill=color,
        )
        canvas.draw.text(
            (cursor + canvas.s(18), top),
            safe,
            font=font,
            fill=COLORS.secondary_text,
        )
        cursor += int(needed)


def draw_rank_badge(
    canvas: VisualCanvas,
    rank: int,
    x: float,
    y: float,
    size: float = 48,
) -> None:
    color = {1: COLORS.gold, 2: COLORS.silver, 3: COLORS.bronze}.get(
        rank, COLORS.muted_text
    )
    box = canvas.box((x, y, x + size, y + size))
    canvas.draw.rounded_rectangle(
        box,
        radius=canvas.s(12),
        fill=COLORS.surface if rank > 3 else tuple(max(0, c - 135) for c in color),
        outline=color,
        width=max(1, canvas.s(2)),
    )
    text = "#{}".format(rank)
    font = canvas.fonts["ranking_number"]
    bbox = canvas.draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    canvas.draw.text(
        (
            canvas.s(x + size / 2) - text_width / 2,
            canvas.s(y + size / 2) - text_height / 2 - bbox[1],
        ),
        text,
        font=font,
        fill=color,
    )


def draw_avatar_container(
    canvas: VisualCanvas,
    *,
    avatar_data: Optional[bytes],
    fallback_label: str,
    x: float,
    y: float,
    size: float,
    count_fallback: bool = True,
) -> None:
    size_px = canvas.s(size)
    avatar = prepare_avatar(avatar_data, size_px)
    if avatar is not None:
        canvas.image.paste(avatar, (canvas.s(x), canvas.s(y)), avatar)
        canvas.draw.ellipse(
            canvas.box((x, y, x + size, y + size)),
            outline=COLORS.border,
            width=max(1, canvas.s(2)),
        )
        return
    if count_fallback:
        canvas.state.avatar_fallback_count += 1
    canvas.draw.ellipse(
        canvas.box((x, y, x + size, y + size)),
        fill=COLORS.accent_soft,
        outline=canvas.accent,
        width=max(1, canvas.s(2)),
    )
    initial = (fallback_label[:1] or "?").upper()
    font = canvas.fonts["ranking_number"]
    bbox = canvas.draw.textbbox((0, 0), initial, font=font)
    canvas.draw.text(
        (
            canvas.s(x + size / 2) - (bbox[2] - bbox[0]) / 2,
            canvas.s(y + size / 2) - (bbox[3] - bbox[1]) / 2 - bbox[1],
        ),
        initial,
        font=font,
        fill=COLORS.text,
    )


def draw_leaderboard_row(
    canvas: VisualCanvas,
    *,
    rank: int,
    label: str,
    value: str,
    subtitle: str,
    avatar_data: Optional[bytes],
    avatar_expected: bool,
    x: float,
    y: float,
    width: float,
    height: float,
    progress: float,
) -> None:
    fill = COLORS.surface if rank <= 3 else COLORS.card
    canvas.draw.rounded_rectangle(
        canvas.box((x, y, x + width, y + height)),
        radius=canvas.s(16),
        fill=fill,
        outline=COLORS.border,
        width=max(1, canvas.s(SPACING.border)),
    )
    if progress > 0:
        rail_width = max(0, min(width - 8, (width - 8) * progress))
        canvas.draw.rounded_rectangle(
            canvas.box((x + 4, y + height - 7, x + 4 + rail_width, y + height - 4)),
            radius=canvas.s(2),
            fill=canvas.accent,
        )
    draw_rank_badge(canvas, rank, x + 18, y + (height - 48) / 2)
    draw_avatar_container(
        canvas,
        avatar_data=avatar_data,
        fallback_label=label,
        x=x + 82,
        y=y + (height - 56) / 2,
        size=56,
        count_fallback=avatar_expected,
    )
    value_font = canvas.fonts["username"]
    value_safe = truncate_text(
        canvas.draw, value, value_font, canvas.s(245), canvas.state
    )
    value_width = canvas.draw.textlength(value_safe, font=value_font)
    pill_width = min(canvas.s(265), max(canvas.s(126), value_width + canvas.s(34)))
    pill_right = canvas.s(x + width - 18)
    pill_left = pill_right - pill_width
    canvas.draw.rounded_rectangle(
        (pill_left, canvas.s(y + 22), pill_right, canvas.s(y + height - 22)),
        radius=canvas.s(22),
        fill=COLORS.surface_alt,
        outline=COLORS.border,
        width=max(1, canvas.s(1)),
    )
    canvas.draw.text(
        (pill_left + (pill_width - value_width) / 2, canvas.s(y + 31)),
        value_safe,
        font=value_font,
        fill=COLORS.text,
    )
    text_x = x + 154
    max_text = (pill_left - canvas.s(18)) - canvas.s(text_x)
    safe_label = truncate_text(
        canvas.draw, label, canvas.fonts["username"], max_text, canvas.state
    )
    canvas.draw.text(
        (canvas.s(text_x), canvas.s(y + 22)),
        safe_label,
        font=canvas.fonts["username"],
        fill=COLORS.text,
    )
    if subtitle:
        safe_subtitle = truncate_text(
            canvas.draw,
            subtitle,
            canvas.fonts["supporting_stat"],
            max_text,
            canvas.state,
        )
        canvas.draw.text(
            (canvas.s(text_x), canvas.s(y + 55)),
            safe_subtitle,
            font=canvas.fonts["supporting_stat"],
            fill=COLORS.muted_text,
        )


def draw_empty_state(
    canvas: VisualCanvas,
    *,
    title: str,
    message: str,
    x: float,
    y: float,
    width: float,
    height: float,
) -> None:
    canvas.draw.rounded_rectangle(
        canvas.box((x, y, x + width, y + height)),
        radius=canvas.s(SPACING.radius),
        fill=COLORS.card,
        outline=COLORS.border,
        width=max(1, canvas.s(SPACING.border)),
    )
    canvas.draw.text(
        (canvas.s(x + 32), canvas.s(y + 34)),
        truncate_text(
            canvas.draw,
            title,
            canvas.fonts["empty_title"],
            canvas.s(width - 64),
            canvas.state,
        ),
        font=canvas.fonts["empty_title"],
        fill=COLORS.text,
    )
    lines = wrap_text(
        canvas.draw,
        message,
        canvas.fonts["empty_body"],
        canvas.s(width - 64),
        4,
        canvas.state,
    )
    for index, line in enumerate(lines):
        canvas.draw.text(
            (canvas.s(x + 32), canvas.s(y + 92 + index * 30)),
            line,
            font=canvas.fonts["empty_body"],
            fill=COLORS.muted_text,
        )


def draw_error_state(
    canvas: VisualCanvas,
    *,
    message: str,
    x: float,
    y: float,
    width: float,
    height: float,
) -> None:
    draw_empty_state(
        canvas,
        title="Report unavailable",
        message=message,
        x=x,
        y=y,
        width=width,
        height=height,
    )
    canvas.draw.rounded_rectangle(
        canvas.box((x, y, x + 8, y + height)),
        radius=canvas.s(4),
        fill=COLORS.negative,
    )


def draw_footer(
    canvas: VisualCanvas,
    *,
    left_text: str,
    right_text: str,
) -> None:
    y = canvas.profile.height - 42
    font = canvas.fonts["footer"]
    left = truncate_text(canvas.draw, left_text, font, canvas.s(500), canvas.state)
    right = truncate_text(canvas.draw, right_text, font, canvas.s(520), canvas.state)
    canvas.draw.text(
        (canvas.s(48), canvas.s(y)), left, font=font, fill=COLORS.muted_text
    )
    right_width = canvas.draw.textlength(right, font=font)
    canvas.draw.text(
        (canvas.s(canvas.profile.width - 48) - right_width, canvas.s(y)),
        right,
        font=font,
        fill=COLORS.muted_text,
    )
