from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple


Color = Tuple[int, int, int]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIMARY_FONT = PROJECT_ROOT / "assets" / "OpenSansEmoji.ttf"
FALLBACK_FONTS = (
    PROJECT_ROOT / "assets" / "calibri-regular.ttf",
    PROJECT_ROOT / "assets" / "calibri.ttf",
)


@dataclass(frozen=True)
class LayoutProfile:
    name: str
    width: int
    height: int
    minimum_width: int
    minimum_height: int
    max_primary_metrics: int
    max_major_charts: int
    max_rows: int


WIDE_OVERVIEW = LayoutProfile("wide_overview", 1600, 900, 1280, 720, 6, 2, 0)
PORTRAIT_LEADERBOARD = LayoutProfile(
    "portrait_leaderboard", 1200, 1500, 960, 1200, 0, 0, 10
)
SQUARE_SUMMARY = LayoutProfile("square_summary", 1200, 1200, 960, 960, 4, 1, 0)

PROFILES = {
    profile.name: profile
    for profile in (WIDE_OVERVIEW, PORTRAIT_LEADERBOARD, SQUARE_SUMMARY)
}


class Colors:
    # Mirrors the dashboard theme in dashboard/static/styles.css.
    canvas: Color = (12, 13, 18)
    card: Color = (23, 24, 32)
    surface: Color = (32, 33, 43)
    surface_alt: Color = (16, 17, 23)
    border: Color = (48, 49, 61)
    divider: Color = (58, 59, 72)
    text: Color = (244, 244, 247)
    secondary_text: Color = (217, 218, 226)
    muted_text: Color = (167, 168, 179)
    accent: Color = (240, 49, 155)
    accent_soft: Color = (74, 24, 53)
    positive: Color = (94, 211, 154)
    negative: Color = (255, 124, 135)
    neutral: Color = (167, 168, 179)
    warning: Color = (242, 191, 90)
    gold: Color = (246, 197, 84)
    silver: Color = (201, 207, 220)
    bronze: Color = (210, 143, 88)
    chart: Tuple[Color, ...] = (
        (240, 49, 155),
        (94, 211, 154),
        (88, 101, 242),
        (242, 191, 90),
        (173, 112, 255),
        (69, 190, 210),
    )


COLORS = Colors()


class Spacing:
    unit = 8
    outer = 48
    card_padding = 28
    gap = 20
    header_gap = 24
    chart_padding = 28
    avatar_gap = 14
    footer_gap = 20
    border = 2
    radius = 22
    radius_small = 12
    shadow_blur = 18
    avatar_small = 48
    avatar_medium = 64


SPACING = Spacing()


TYPOGRAPHY: Dict[str, int] = {
    "graphic_title": 44,
    "graphic_subtitle": 22,
    "section_heading": 25,
    "primary_metric": 44,
    "metric_label": 18,
    "ranking_number": 23,
    "username": 23,
    "supporting_stat": 17,
    "chart_label": 17,
    "footer": 15,
    "page_indicator": 16,
    "empty_title": 28,
    "empty_body": 19,
}

MIN_BODY_TEXT_SIZE = 16
LEADERBOARD_ROWS_PER_PAGE = PORTRAIT_LEADERBOARD.max_rows
ROSTER_ROWS_PER_PAGE = 12

